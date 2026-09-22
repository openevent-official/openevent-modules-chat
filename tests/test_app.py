"""Application contract tests with an in-memory public OpenEvent boundary."""

from concurrent.futures import ThreadPoolExecutor
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import DEFAULT, patch

import grpc
from openevent.sdk import OpenEventClient
from openevent.sdk.proto import openevent_pb2 as pb
from openevent.chat_sdk import ObjectKey, TextPart
from openevent.chat_sdk.codec import encode_payload, parse_message
from openevent.chat_sdk.errors import ChatProtocolError, FetchPageError, PublishFailedError, make_failure
from openevent.chat_app.config import ConfigStore, ConfigurationError, ServerConfig, new_ulid, strict_json
from openevent.chat_app.http import ChatHTTPServer
from openevent.chat_app.service import AppError, ChatService


class Unavailable(grpc.RpcError):
    def code(self):
        return grpc.StatusCode.UNAVAILABLE


class Events:
    timeout_ms = 500.0

    def __init__(self):
        self.channels = {}
        self.messages = []
        self.objects = {}
        self.calls = []
        self.lock = threading.Lock()
        self.publish_entered = threading.Event()
        self.publish_release = threading.Event()
        self.block_publish = False
        self.fail_publish = False
        self.fail_fetch = False
        self.fail_create = False
        self.bad_data = False
        self.closed = False

    def get_status(self, **kwargs):
        self.calls.append("GetStatus")
        return NS(max_seq=len(self.messages))

    def get_channel(self, channel_id, **kwargs):
        self.calls.append("GetChannel")
        return NS(channel=self.channels[channel_id])

    def create_channel(self, principal, name, members, visibility, protocol, description, **kwargs):
        self.calls.append("CreateChannel")
        if self.fail_create:
            raise Unavailable()
        cid = len(self.channels) + 1
        channel = NS(channel_id=cid, name=name, members=[principal, *members], creator=principal,
                     visibility=visibility, protocol=protocol, description=description)
        self.channels[cid] = channel
        return NS(channel=channel)

    def write_object(self, name, type, data, description, **kwargs):
        self.calls.append("WriteObject")
        oid = len(self.objects) + 1
        self.objects[oid] = (NS(name=name, type=type, nbytes=len(data), description=description), data)
        return NS(object_id=oid, object_token="object-secret")

    def get_object_metadata(self, object_id, **kwargs):
        self.calls.append("GetObjectMetadata")
        return self.objects[object_id][0]

    def read_object(self, object_id, **kwargs):
        self.calls.append("ReadObject")
        return NS(data=b"" if self.bad_data else self.objects[object_id][1])

    def close(self):
        self.closed = True
        self.publish_release.set()


class Chat:
    def __init__(self, events, principal, token, channel_id, max_retries, channel_validator=None):
        self.events = events
        self.principal = principal
        self.channel_id = channel_id
        channel = events.get_channel(channel_id=channel_id).channel
        if channel_validator is not None:
            channel_validator(channel)

    def publish(self, payload, keys=()):
        events = self.events
        events.calls.append(payload["kind"])
        if payload["kind"] == "turn.single" and events.block_publish:
            events.publish_entered.set()
            if not events.publish_release.wait(3):
                raise RuntimeError("test publish timeout")
        if events.fail_publish:
            raise PublishFailedError(make_failure("PublishAutoSeq", Unavailable()), 123)
        with events.lock:
            seq = len(events.messages) + 1
            events.messages.append(NS(seq=seq, ts_ms=123, uuid=seq, channel_id=self.channel_id,
                                      principal=self.principal, recipients=(), object_keys=tuple(keys), payload=payload))
        return seq

    def reserve_submissions(self, reserved_through):
        return self.publish({"kind": "submission.reserve", "reserved_through": reserved_through})

    def single_turn(self, turn_id, content, reply_to_seqs, object_keys):
        return self.publish({"kind": "turn.single", "turn_id": turn_id,
                             "content": [{"type": "text", "text": part.text} for part in content],
                             "reply_to_seqs": list(reply_to_seqs)}, object_keys)

    def cancel_turn(self, target_turn):
        return self.publish({"kind": "turn.cancel", "target_turn": {"principal": target_turn.principal, "turn_id": target_turn.turn_id}})

    def fetch_page(self, from_seq, limit):
        events = self.events
        events.calls.append("Fetch")
        if events.fail_fetch:
            raise FetchPageError(make_failure("Fetch", Unavailable()))
        messages = [item for item in events.messages if item.channel_id == self.channel_id and item.seq >= from_seq][:limit]
        maximum = len(events.messages)
        next_seq = messages[-1].seq + 1 if len(messages) == limit else maximum + 1
        return NS(messages=tuple(messages), next_seq=next_seq, last_seq=maximum)

    def close(self):
        pass


class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "channels"
        self.config = ServerConfig("unused", self.path, "browser-secret", 10, "user-secret", 20, 500.0, 0)
        self.events = Events()
        self.service = ChatService(self.config, self.events, chat_factory=Chat)

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def session(self, request_id=None):
        return self.service.create_session({"create_request_id": request_id or new_ulid()})[1]["session_id"]

    def send(self, sid, number="1", **kwargs):
        return self.service.send(sid, dict(submission_id=number, text="hello", **kwargs))

    def assert_error(self, code, operation):
        with self.assertRaises(AppError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def assert_fatal_log(self, operation, **location):
        with self.assertLogs("openevent.chat_app.service", level="ERROR") as captured:
            error = self.assert_error("server_unavailable", operation)
        self.assertTrue(self.service.fatal.is_set())
        self.assertEqual(len(captured.records), 1)
        message = captured.records[0].getMessage()
        record = json.loads(message.removeprefix("chat server fatal: "))
        self.assertEqual({key: value for key, value in record.items() if key != "failure"}, location)
        self.assertEqual(record["failure"]["stage"], error.failure.stage)
        for secret in ("browser-secret", "user-secret", "object-secret", "body-secret", "file-secret",
                       "object_token", "ObjectKey", "payload"):
            self.assertNotIn(secret, message)
        return record

    def test_create_is_idempotent_and_startup_never_fetches(self):
        request = {"create_request_id": new_ulid()}
        status, first = self.service.create_session(request)
        self.assertEqual(status, 201)
        self.assertEqual(first["scan_start_seq"], "1")
        self.assertEqual(self.service.create_session(request), (200, first))
        self.assertEqual(self.events.calls.count("CreateChannel"), 1)
        self.service.close()
        self.events.calls.clear()
        self.service = ChatService(self.config, self.events, chat_factory=Chat)
        self.assertNotIn("Fetch", self.events.calls)
        self.assertNotIn("GetStatus", self.events.calls)
        self.assertEqual(self.events.calls.count("GetChannel"), 1)
        self.assertEqual(self.service.create_session(request), (200, first))

    def test_sdk_construction_checks_session_channel_before_startup_succeeds(self):
        sid = self.session()
        self.service.close()
        channel = self.events.channels[1]
        # Startup must use only GetChannel, despite the SDK requiring these methods.
        with patch.multiple(self.events, create=True, get_uuid=DEFAULT,
                            get_seq_by_uuid=DEFAULT, publish_auto_seq=DEFAULT, fetch=DEFAULT) as unused:
            self.events.calls.clear()
            self.service = ChatService(self.config, self.events)
            self.assertEqual(self.events.calls, ["GetChannel"])
            self.assertEqual(self.service.list_sessions()["sessions"][0]["session_id"], sid)
            self.service.close()
            for name, value in (("name", "different"), ("members", [10]),
                                ("visibility", 1), ("creator", 20), ("description", "changed")):
                with self.subTest(field=name), patch.object(channel, name, value):
                    self.events.calls.clear()
                    with self.assertRaises(AppError) as caught:
                        ChatService(self.config, self.events)
                    self.assertEqual(caught.exception.failure.category, "contract")
                    self.assertEqual(self.events.calls, ["GetChannel"])
            for method in unused.values():
                method.assert_not_called()

    def test_simultaneous_create_shares_transaction(self):
        request = {"create_request_id": new_ulid()}
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: self.service.create_session(request), range(2)))
        self.assertEqual(sorted(item[0] for item in results), [200, 201])
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(self.events.calls.count("CreateChannel"), 1)

    def test_processing_query_does_not_wait_for_publish_and_rotation_does(self):
        sid = self.session()
        self.assertEqual(self.service.allocate(sid, {"count": "1"}), {"start": "1", "end": "1"})
        self.events.block_publish = True
        with ThreadPoolExecutor(3) as pool:
            sending = pool.submit(self.send, sid)
            self.assertTrue(self.events.publish_entered.wait(1))
            status, result = self.service.submission(sid, "1")
            self.assertEqual((status, result["status"]), (202, "processing"))
            rotation = pool.submit(self.service.allocate, sid, {"count": "10000"})
            time.sleep(.03)
            self.assertFalse(rotation.done())
            self.assertEqual(self.events.calls.count("submission.reserve"), 1)
            self.events.publish_release.set()
            sending.result(1)
            self.assertEqual(rotation.result(1), {"start": "10001", "end": "20000"})
        self.assert_error("submission_out_of_range", lambda: self.service.submission(sid, "1"))
        self.assert_error("submission_out_of_range", lambda: self.send(sid))
        self.assertEqual(self.events.calls.count("turn.single"), 1)

    def test_restart_recovers_only_on_allocation_and_issues_fresh_batch(self):
        sid = self.session()
        self.service.allocate(sid, {"count": "100"})
        self.send(sid)
        self.service.close()
        self.events.calls.clear()
        self.service = ChatService(self.config, self.events, chat_factory=Chat)
        self.assert_error("submission_out_of_range", lambda: self.service.submission(sid, "1"))
        self.assertNotIn("Fetch", self.events.calls)
        self.assertEqual(self.service.allocate(sid, {"count": "2"}), {"start": "10001", "end": "10002"})
        self.assertEqual(self.events.calls.count("Fetch"), 1)
        self.events.calls.clear()
        self.service.allocate(sid, {"count": "1"})
        self.send(sid, "10001")
        self.service.submission(sid, "10001")
        self.assertNotIn("Fetch", self.events.calls)
        self.assertNotIn("GetStatus", self.events.calls)

    def test_history_is_one_page_and_redacts_capabilities(self):
        sid = self.session()
        self.service.allocate(sid, {"count": "5"})
        obj = self.service.upload(sid, name="报告.pdf", content_type="application/pdf", data=b"data")
        self.send(sid, attachments=[obj["object_id"], obj["object_id"]], reply_to_seqs=[])
        self.events.calls.clear()
        page = self.service.history(sid, "1", 1)
        self.assertEqual(page["events"][0]["payload"]["reserved_through"], "10000")
        self.assertEqual(page["next_seq"], "2")
        self.assertEqual(self.events.calls, ["Fetch"])
        page = self.service.history(sid, "2")
        event = page["events"][0]
        self.assertEqual(event["attachments"], [{"object_id": "1"}, {"object_id": "1"}])
        encoded = json.dumps(page)
        for secret in ("object-secret", "user-secret", "principal", "uuid", "channel_id"):
            self.assertNotIn(secret, encoded)

    def test_metadata_does_not_read_data_and_download_checks_complete_data(self):
        sid = self.session()
        self.service.allocate(sid, {"count": "1"})
        self.service.upload(sid, name="file.txt", content_type="text/plain", data=b"data")
        self.send(sid, attachments=["1"])
        self.events.calls.clear()
        self.assertEqual(self.service.metadata(sid, "1", "2")["nbytes"], 4)
        self.assertEqual(self.events.calls, ["Fetch", "GetObjectMetadata"])
        self.assert_error("attachment_reference_not_found", lambda: self.service.metadata(sid, "1", "1"))
        self.events.bad_data = True
        self.assert_error("server_unavailable", lambda: self.service.download(sid, "1", "2"))
        self.assertTrue(self.service.fatal.is_set())

    def test_attachment_failures_log_location_without_capabilities_or_contents(self):
        sid = self.session()
        self.service.allocate(sid, {"count": "1"})
        self.service.upload(sid, name="file-secret.txt", content_type="text/plain", data=b"file-secret")
        self.service.send(sid, {"submission_id": "1", "text": "body-secret", "attachments": ["1"]})
        faults = (
            ("GetObjectMetadata", "get_object_metadata", "metadata", {"side_effect": RuntimeError("object-secret file-secret")}),
            ("GetObjectMetadata", "get_object_metadata", "metadata", {
                "return_value": NS(name="file-secret", type="text/plain", description="body-secret", nbytes=0)}),
            ("ReadObject", "read_object", "download", {"side_effect": RuntimeError("user-secret object-secret")}),
            ("ReadObject", "read_object", "download", {"return_value": NS(data=b"")}),
        )
        for stage, method, operation, result in faults:
            with self.subTest(stage=stage, fault=result):
                self.service.close()
                self.service = ChatService(self.config, self.events, chat_factory=Chat)
                with patch.object(self.events, method, **result):
                    record = self.assert_fatal_log(lambda: getattr(self.service, operation)(sid, "1", "2"),
                                                   session_id=sid, channel_id="1", event_seq="2", object_id="1")
                self.assertEqual(record["failure"]["stage"], stage)

    def test_history_failures_log_page_or_event_location_without_payload(self):
        sid = self.session()
        self.service.allocate(sid, {"count": "1"})
        self.service.send(sid, {"submission_id": "1", "text": "body-secret"})
        failure = FetchPageError(make_failure("Fetch", ChatProtocolError("payload body-secret")))
        with patch.object(self.service.sessions[sid].chat, "fetch_page", side_effect=failure):
            self.assert_fatal_log(lambda: self.service.history(sid, "2"),
                                  session_id=sid, channel_id="1", from_seq="2")
        self.service.close()
        self.service = ChatService(self.config, self.events, chat_factory=Chat)
        self.events.messages[-1].principal = 999
        self.assert_fatal_log(lambda: self.service.history(sid, "2"),
                              session_id=sid, channel_id="1", event_seq="2")

    def test_upload_failure_logs_session_without_rpc_arguments(self):
        sid = self.session()
        with patch.object(self.events, "write_object", side_effect=RuntimeError("user-secret file-secret")):
            self.assert_fatal_log(lambda: self.service.upload(
                sid, name="file-secret.txt", content_type="text/plain", data=b"file-secret"),
                session_id=sid, channel_id="1")

    def test_upload_record_missing_rejects_before_sdk(self):
        sid = self.session()
        self.service.allocate(sid, {"count": "1"})
        self.assert_error("attachment_unavailable", lambda: self.send(sid, attachments=["1"]))
        self.assertNotIn("turn.single", self.events.calls)
        self.assertFalse(self.service.fatal.is_set())
        self.assert_error("submission_not_observed", lambda: self.service.submission(sid, "1"))

    def test_read_transient_is_request_local_and_write_failure_is_fatal(self):
        sid = self.session()
        self.service.allocate(sid, {"count": "1"})
        self.events.fail_fetch = True
        error = self.assert_error("server_unavailable", lambda: self.service.history(sid, "1"))
        self.assertEqual(error.failure.category, "external_unavailable")
        self.assertFalse(self.service.fatal.is_set())
        self.events.fail_publish = True
        self.assert_error("server_unavailable", lambda: self.send(sid))
        self.assertTrue(self.service.fatal.is_set())

    def test_fatal_closes_shared_transport_before_another_session_retries(self):
        attempts = []
        entered = threading.Event()

        class RetryingChat(Chat):
            def single_turn(inner, **kwargs):
                attempts.append("first publish reached transport")
                entered.set()
                if not inner.events.publish_release.wait(2):
                    raise RuntimeError("test synchronization timed out")
                if inner.events.closed:
                    raise PublishFailedError(make_failure("PublishAutoSeq", Unavailable()), 321)
                attempts.append("retry reached transport")
                return super().single_turn(**kwargs)

        self.service.close()
        self.service = ChatService(self.config, self.events, chat_factory=RetryingChat, owns_events=True)
        first, second = self.session(), self.session()
        self.service.allocate(first, {"count": "1"})
        self.service.allocate(second, {"count": "1"})
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(self.send, first)
            self.assertTrue(entered.wait(1))
            self.events.fail_publish = True
            self.assert_error("server_unavailable", lambda: self.service.cancel(second, {"target_turn_id": "agent:1"}))
            self.assert_error("server_unavailable", lambda: pending.result(1))
        self.assertTrue(self.events.closed)
        self.assertEqual(attempts, ["first publish reached transport"])

    def test_fatal_cancels_blocked_sdk_uuid_allocation(self):
        sid = self.session()
        self.service.close()
        entered, cancelled, release = (threading.Event() for _ in range(3))

        def allocate(request, context):
            def cancel():
                cancelled.set()
                release.set()
            context.add_callback(cancel)
            entered.set()
            if not release.wait(10):
                context.abort(grpc.StatusCode.DEADLINE_EXCEEDED, "test allocation was not released")
            return pb.AllocateUuidsResponse(uuids=range(1, request.count + 1))

        with ThreadPoolExecutor(max_workers=1) as rpc_pool:
            server = grpc.server(rpc_pool)
            server.add_generic_rpc_handlers((grpc.method_handlers_generic_handler("openevent.EventService", {
                "AllocateUuids": grpc.unary_unary_rpc_method_handler(
                    allocate, request_deserializer=pb.AllocateUuidsRequest.FromString,
                    response_serializer=pb.AllocateUuidsResponse.SerializeToString),
            }),))
            port = server.add_insecure_port("127.0.0.1:0")
            server.start()
            events = OpenEventClient(f"127.0.0.1:{port}", timeout_ms=10000)
            try:
                with patch.object(events, "get_channel", side_effect=self.events.get_channel):
                    self.service = ChatService(self.config, events, owns_events=True)
                chat = self.service.sessions[sid].chat
                with ThreadPoolExecutor(max_workers=2) as calls:
                    pending = calls.submit(
                        self.service._call, "single_turn",
                        lambda: chat.single_turn(turn_id="blocked", content=(TextPart("waiting"),)),
                        fatal=True, session_id=sid)
                    try:
                        self.assertTrue(entered.wait(2))
                        failure = make_failure("Fetch", category="contract", detail="test failure")
                        stopped = calls.submit(self.service._fail, failure, session_id=sid)
                        self.assertTrue(cancelled.wait(2), "shutdown waited for the UUID RPC timeout")
                        stopped.result(timeout=2)
                        with self.assertRaises(AppError) as caught:
                            pending.result(timeout=2)
                        self.assertEqual(caught.exception.failure.grpc_code, grpc.StatusCode.CANCELLED)
                        self.assertTrue(self.service.fatal.is_set())
                        self.assertIs(self.service.failure, failure)
                    finally:
                        release.set()
                        server.stop(0).wait(2)
            finally:
                release.set()
                server.stop(0).wait(2)
                events.close()

    def test_close_does_not_close_borrowed_transport(self):
        self.session()
        self.service.close()
        self.assertFalse(self.events.closed)

    def test_uncertain_create_keeps_pending_and_blocks_restart(self):
        self.events.fail_create = True
        sid = new_ulid()
        with patch("openevent.chat_app.service.new_ulid", return_value=sid):
            self.assert_fatal_log(lambda: self.session(), session_id=sid, filename=sid + ".json")
        self.assertEqual(len(list((self.path / ".pending").glob("*.json"))), 1)
        self.service.close()
        with self.assertRaises(ConfigurationError):
            ChatService(self.config, self.events, chat_factory=Chat)

    def test_configuration_commit_failure_logs_created_channel_and_filename(self):
        sid = new_ulid()
        with patch("openevent.chat_app.service.new_ulid", return_value=sid), \
                patch.object(self.service.store, "commit", side_effect=OSError("file-secret")):
            self.assert_fatal_log(lambda: self.session(), session_id=sid, channel_id="1", filename=sid + ".json")

    def test_restart_cleans_only_pending_matching_committed_configuration(self):
        sid = self.session()
        formal = self.path / (sid + ".json")
        pending = self.path / ".pending" / (sid + ".json")
        pending.write_bytes(formal.read_bytes())
        self.service.close()
        self.service = ChatService(self.config, self.events, chat_factory=Chat)
        self.assertFalse(pending.exists())
        self.assertEqual(self.service.list_sessions()["sessions"][0]["session_id"], sid)
        self.assertTrue(formal.exists())

    def test_channel_scopes_and_cancel_never_scan(self):
        first, second = self.session(), self.session()
        self.assertEqual(self.service.allocate(first, {"count": "1"})["start"], "1")
        self.assertEqual(self.service.allocate(second, {"count": "1"})["start"], "1")
        self.events.calls.clear()
        self.service.cancel(first, {"target_turn_id": "agent:x"})
        self.assertEqual(self.events.calls, ["turn.cancel"])

    def test_directory_has_single_owner_and_strict_json(self):
        with self.assertRaises(ConfigurationError):
            ConfigStore(self.path)
        for data in ('{"x":1,"x":2}', '{"x":NaN}', '[]'):
            with self.assertRaises(ValueError):
                strict_json(data)


class HTTPTests(unittest.TestCase):
    session = AppTests.session
    send = AppTests.send

    def setUp(self):
        AppTests.setUp(self)
        self.server = ChatHTTPServer(("127.0.0.1", 0), self.service)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.origin = "http://127.0.0.1:" + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(2)
        self.server.server_close()
        AppTests.tearDown(self)

    def request(self, method, path, body=None, *, token=True, origin=True, headers=None):
        values = dict(headers or {})
        if token:
            values["Cookie"] = "web_token=browser-secret"
        if origin:
            values["Origin"] = self.origin
        if isinstance(body, dict):
            body = json.dumps(body)
            values["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        connection.request(method, path, body=body, headers=values)
        response = connection.getresponse()
        result = response.read()
        status, response_headers = response.status, dict(response.getheaders())
        connection.close()
        return status, response_headers, result

    def test_http_auth_origin_json_and_history_headers(self):
        status, _, _ = self.request("GET", "/api/chat/sessions", token=False)
        self.assertEqual(status, 401)
        status, _, _ = self.request("POST", "/api/chat/sessions", {"create_request_id": new_ulid()}, origin=False)
        self.assertEqual(status, 403)
        status, _, data = self.request("POST", "/api/chat/sessions", {"create_request_id": new_ulid()})
        self.assertEqual(status, 201)
        sid = json.loads(data)["session_id"]
        status, headers, data = self.request("GET", f"/api/chat/sessions/{sid}/history?fetch_seq=1&limit=100")
        self.assertEqual(status, 200)
        self.assertIn("no-store", headers["Cache-Control"])
        self.assertEqual(json.loads(data), {"events": [], "next_seq": "1", "last_seq": "0"})
        status, _, data = self.request("POST", f"/api/chat/sessions/{sid}/submissions", '{"count":"1","count":"2"}', headers={"Content-Type": "application/json"})
        self.assertEqual(status, 400)
        self.assertFalse(self.service.fatal.is_set())

    def test_http_multipart_and_attachment_download(self):
        sid = self.session()
        boundary = "testboundary"
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="报告.txt"\r\nContent-Type: text/plain\r\n\r\nhello\r\n--{boundary}--\r\n').encode()
        status, _, data = self.request("POST", f"/api/chat/sessions/{sid}/attachments", body,
                                        headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(data)["nbytes"], 5)
        self.assertEqual(json.loads(data)["name"], "报告.txt")
        self.service.allocate(sid, {"count": "1"})
        self.send(sid, attachments=["1"])
        status, headers, data = self.request("GET", f"/api/chat/sessions/{sid}/attachments/1?event_seq=2")
        self.assertEqual(status, 200)
        self.assertEqual(data, b"hello")
        self.assertTrue(headers["Content-Disposition"].startswith("attachment;"))
        self.assertEqual(headers["Content-Type"], "application/octet-stream")

    def test_http_multipart_uploads_email_as_an_ordinary_file(self):
        sid = self.session()
        boundary = "testboundary"
        content = (b"From: sender@example.com\r\nTo: recipient@example.com\r\n"
                   b"Subject: Attached email\r\nContent-Type: text/plain\r\n\r\n"
                   b"Keep this entire email unchanged.\r\n")
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="message.eml"\r\n'
                'Content-Type: message/rfc822\r\n\r\n').encode() + content + f"\r\n--{boundary}--\r\n".encode()
        status, _, data = self.request("POST", f"/api/chat/sessions/{sid}/attachments", body,
                                       headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
        self.assertEqual(status, 201)
        uploaded = json.loads(data)
        self.assertEqual(uploaded["name"], "message.eml")
        self.assertEqual(uploaded["nbytes"], len(content))
        self.assertEqual(self.events.objects[int(uploaded["object_id"])][0].type, "message/rfc822")
        self.service.allocate(sid, {"count": "1"})
        self.send(sid, attachments=[uploaded["object_id"]])
        status, _, downloaded = self.request(
            "GET", f'/api/chat/sessions/{sid}/attachments/{uploaded["object_id"]}?event_seq=2')
        self.assertEqual(status, 200)
        self.assertEqual(downloaded, content)
        self.assertFalse(self.service.fatal.is_set())

    def test_http_multipart_preserves_binary_bytes_and_type_fallback(self):
        sid = self.session()
        boundary = "testboundary"
        binary = b"\x00\xff\r\n--testboundarY\r\ntestboundary\x80\r\n\r\n"
        cases = [
            ("binary", "Content-Type: application/octet-stream\r\n", binary),
            ("missing type", "", binary),
            ("empty type", "Content-Type:\r\n", binary),
            ("transfer encoding", "Content-Type: application/octet-stream\r\nContent-Transfer-Encoding: base64\r\n",
             b"SGVsbG8=\r\n"),
        ]
        self.service.allocate(sid, {"count": str(len(cases))})
        for index, (label, part_headers, content) in enumerate(cases, 1):
            with self.subTest(case=label):
                body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="data.bin"\r\n'
                        + part_headers + "\r\n").encode() + content + f"\r\n--{boundary}--\r\n".encode()
                status, _, data = self.request("POST", f"/api/chat/sessions/{sid}/attachments", body,
                                               headers={"Content-Type": 'multipart/form-data; boundary="' + boundary + '"'})
                self.assertEqual(status, 201)
                uploaded = json.loads(data)
                self.assertEqual(uploaded["nbytes"], len(content))
                self.assertEqual(self.events.objects[int(uploaded["object_id"])][0].type, "application/octet-stream")
                self.send(sid, str(index), attachments=[uploaded["object_id"]])
                status, _, downloaded = self.request(
                    "GET", f'/api/chat/sessions/{sid}/attachments/{uploaded["object_id"]}?event_seq={index + 1}')
                self.assertEqual(status, 200)
                self.assertEqual(downloaded, content)
        self.assertFalse(self.service.fatal.is_set())

    def test_http_multipart_rejects_invalid_file_forms_before_storage(self):
        sid = self.session()
        boundary = "testboundary"
        part = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="file.txt"\r\n'
                'Content-Type: text/plain\r\n\r\nhello\r\n').encode()
        end = f"--{boundary}--\r\n".encode()
        cases = {
            "multiple files": part + part + end,
            "missing filename": part.replace(b'; filename="file.txt"', b"") + end,
            "missing closing boundary": part,
        }
        for label, body in cases.items():
            with self.subTest(case=label):
                status, _, _ = self.request("POST", f"/api/chat/sessions/{sid}/attachments", body,
                                            headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
                self.assertEqual(status, 400)
                self.assertFalse(self.service.fatal.is_set())
                self.assertEqual(self.events.objects, {})
                self.assertNotIn("WriteObject", self.events.calls)

    def test_history_preserves_deep_extensions_and_original_sdk_payloads(self):
        sid = self.session()
        extension = {"leaf": "完整保留"}
        for _ in range(600):
            extension = {"nested": extension}
        content = [{"type": "text", "text": "正文"}]
        records = [
            (20, {"kind": "turn.single", "turn_id": "first", "content": content, "reply_to_seqs": []}),
            (20, {"kind": "turn.start", "turn_id": "answer", "content": content, "reply_to_seqs": [1]}),
            (20, {"kind": "turn.append", "turn_id": "answer", "pre_seq": 2, "content": content,
                  "extensions": extension}),
            (20, {"kind": "turn.cancel", "target_turn": {"principal": 20, "turn_id": "answer"}}),
            (10, {"kind": "submission.reserve", "reserved_through": 10000}),
        ]
        snapshots = []
        for seq, (principal, payload) in enumerate(records, 1):
            wire = encode_payload(payload)
            snapshots.append(wire)
            self.events.messages.append(parse_message(NS(
                seq=seq, channel_id=1, principal=principal, ts_ms=1, uuid=seq,
                recipients=[], object_keys=[], payload=wire)))

        status, _, data = self.request("GET", f"/api/chat/sessions/{sid}/history?fetch_seq=1")
        self.assertEqual(status, 200)
        self.assertFalse(self.service.fatal.is_set())
        page = json.loads(data)
        self.assertEqual(page["next_seq"], "6")
        events = page["events"]
        self.assertEqual(events[1]["payload"]["reply_to_seqs"], ["1"])
        self.assertEqual(events[2]["payload"]["pre_seq"], "2")
        self.assertEqual(events[3]["payload"]["target_turn"], {"role": "agent", "turn_id": "answer"})
        self.assertEqual(events[4]["payload"]["reserved_through"], "10000")
        restored = events[2]["payload"]["extensions"]
        for _ in range(600):
            restored = restored["nested"]
        self.assertEqual(restored, {"leaf": "完整保留"})
        for original, wire in zip(self.events.messages, snapshots):
            self.assertEqual(encode_payload(original.payload), wire)
