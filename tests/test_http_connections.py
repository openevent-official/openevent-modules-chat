"""HTTP connection reuse preserves framing, errors and server shutdown."""

import http.client
import json
import socket
import unittest
from unittest.mock import patch

from openevent.chat_app.config import new_ulid
from openevent.chat_app.http import ChatRequestHandler

import test_app as support


class HTTPConnectionTests(unittest.TestCase):
    setUp = support.HTTPTests.setUp
    tearDown = support.HTTPTests.tearDown

    def connection(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        self.addCleanup(connection.close)
        connection.connect()
        return connection

    def exchange(self, connection, method, path, data=None, headers=None):
        values = {"Cookie": "web_token=browser-secret", "Origin": self.origin, **(headers or {})}
        if data is not None:
            data = json.dumps(data)
            values["Content-Type"] = "application/json"
        connection.request(method, path, body=data, headers=values)
        response = connection.getresponse()
        body = response.read()
        if method == "HEAD":
            self.assertEqual(body, b"")
        else:
            self.assertEqual(int(response.getheader("Content-Length")), len(body))
        return response, body

    def test_one_connection_serves_head_static_post_send_and_history(self):
        connection = self.connection()
        original_socket = connection.sock

        def exchange(method, path, data=None):
            response, body = self.exchange(connection, method, path, data)
            self.assertIn(response.status, (200, 201))
            self.assertFalse(response.will_close)
            self.assertNotEqual(response.getheader("Connection"), "close")
            self.assertIs(connection.sock, original_socket)
            return body

        exchange("HEAD", "/static/app.js")
        self.assertIn(b"historyLoop", exchange("GET", "/static/app.js"))
        session = json.loads(exchange("POST", "/api/chat/sessions", {"create_request_id": new_ulid()}))
        base = "/api/chat/sessions/" + session["session_id"]
        allocation = json.loads(exchange("POST", base + "/submissions", {"count": "1"}))
        sent = json.loads(exchange("POST", base + "/turns", {
            "submission_id": allocation["start"], "text": "same connection"}))
        self.assertEqual(sent["status"], "committed")
        page = json.loads(exchange("GET", base + "/history?fetch_seq=" + session["scan_start_seq"]))
        messages = [event for event in page["events"] if event["payload"]["kind"] == "turn.single"]
        self.assertEqual(messages[0]["payload"]["content"], [{"type": "text", "text": "same connection"}])
        tail = json.loads(exchange("GET", base + "/history?fetch_seq=" + page["next_seq"]))
        self.assertEqual(tail["events"], [])
        connection.close()

    def test_client_close_and_http_10_are_respected(self):
        for version, headers in ((11, {"Connection": "close"}),
                                 (11, {"Connection": "keep-alive, close"}), (10, {})):
            with self.subTest(version=version, headers=headers):
                connection = self.connection()
                connection._http_vsn = version
                connection._http_vsn_str = "HTTP/1.1" if version == 11 else "HTTP/1.0"
                response, _ = self.exchange(connection, "GET", "/api/chat/sessions", headers=headers)
                self.assertEqual(response.status, 200)
                self.assertTrue(response.will_close)
                self.assertEqual(response.getheader("Connection"), "close")
                self.assertIsNone(connection.sock)

    def test_unread_bodies_and_errors_cannot_become_another_request(self):
        next_request = b"GET /api/chat/sessions HTTP/1.1\r\nHost: localhost\r\nCookie: web_token=browser-secret\r\n\r\n"
        cases = (
            ("GET", "Content-Length: 4\r\n", b"body", 200),
            ("HEAD", "Content-Length: 4\r\n", b"body", 200),
            ("GET", "Transfer-Encoding: chunked\r\n", b"4\r\nbody\r\n0\r\n\r\n", 200),
            ("GET", "Content-Length: 0\r\nContent-Length: 4\r\n", b"body", 200),
            ("POST", "Content-Length: 4\r\n", b"body", 415),
            ("POST", "Content-Type: application/json\r\nContent-Length: 4\r\n", b"nope", 400),
            ("PUT", "Content-Length: 4\r\n", b"body", 501),
        )
        for method, framing, body, status in cases:
            with self.subTest(method=method, framing=framing):
                path = "/api/chat/" if method == "HEAD" else "/api/chat/sessions"
                request = (f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{self.server.server_port}\r\n"
                           f"Origin: {self.origin}\r\nCookie: web_token=browser-secret\r\n{framing}\r\n").encode()
                with socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2) as client:
                    client.sendall(request + body + next_request)
                    chunks = []
                    while chunk := client.recv(65536):
                        chunks.append(chunk)
                response = b"".join(chunks)
                self.assertTrue(response.startswith(f"HTTP/1.1 {status} ".encode()), response[:100])
                self.assertIn(b"Connection: close\r\n", response)
                self.assertEqual(response.count(b"HTTP/1.1 "), 1)
        self.assertFalse(self.service.fatal.is_set())

    def test_authentication_failure_closes_a_reused_connection(self):
        connection = self.connection()
        self.exchange(connection, "GET", "/api/chat/sessions")
        response, _ = self.exchange(connection, "POST", "/api/chat/sessions", {"create_request_id": new_ulid()},
                                    {"Cookie": "web_token=wrong"})
        self.assertEqual(response.status, 401)
        self.assertTrue(response.will_close)
        self.assertIsNone(connection.sock)
        self.assertFalse(self.service.fatal.is_set())

    def test_broken_transport_closes_without_stopping_the_service(self):
        connection = self.connection()
        self.exchange(connection, "GET", "/api/chat/sessions")
        with patch.object(ChatRequestHandler, "_dispatch", side_effect=ConnectionResetError):
            connection.request("GET", "/api/chat/sessions")
            with self.assertRaises(http.client.RemoteDisconnected):
                connection.getresponse()
        self.assertFalse(self.service.fatal.is_set())

    def test_unexpected_failure_closes_the_connection_and_stops_service(self):
        connection = self.connection()
        with patch.object(self.service, "list_sessions", side_effect=RuntimeError("failure")), \
                self.assertLogs("openevent.chat_app.service", level="ERROR"):
            response, _ = self.exchange(connection, "GET", "/api/chat/sessions")
        self.assertEqual(response.status, 503)
        self.assertTrue(response.will_close)
        self.assertIsNone(connection.sock)
        self.assertTrue(self.service.fatal.is_set())

    def test_shutdown_finishes_current_response_without_keepalive(self):
        connection = self.connection()
        self.exchange(connection, "GET", "/api/chat/sessions")
        original = self.service.list_sessions

        def stop_during_request():
            self.server.request_stop()
            return original()

        with patch.object(self.service, "list_sessions", side_effect=stop_during_request):
            response, _ = self.exchange(connection, "GET", "/api/chat/sessions")
        self.assertEqual(response.status, 200)
        self.assertTrue(response.will_close)
        self.assertIsNone(connection.sock)
        self.thread.join(1)
        self.assertFalse(self.thread.is_alive())
        self.assertFalse(self.service.fatal.is_set())


if __name__ == "__main__":
    unittest.main()
