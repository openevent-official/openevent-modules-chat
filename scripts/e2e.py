"""Exercise a freshly installed Chat wheel against an actual OpenEvent server.

Only the SDK already installed in this interpreter is used. Set
OPENEVENT_SERVER_BIN to a successfully built server executable.
"""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote

from check_sdk import check


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


@contextmanager
def fixture_directory():
    parent = Path('build/e2e')
    parent.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix='run-', dir=parent)).resolve()
    try:
        yield root
    except BaseException:
        print(f'E2E failure artifacts: {root}', flush=True)
        raise
    else:
        shutil.rmtree(root)


@contextmanager
def process(args, log):
    with log.open('wb') as output:
        child = subprocess.Popen(args, stdout=output, stderr=subprocess.STDOUT)
        try:
            yield child
        finally:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)


def wait_http(child, port):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise AssertionError('Chat server exited during startup')
        try:
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=1)
            conn.request('GET', '/api/chat/')
            response = conn.getresponse()
            response.read()
            conn.close()
            if response.status == 200:
                return
        except OSError:
            pass
        time.sleep(.05)
    raise AssertionError('Chat HTTP server did not become ready')


def run():
    check()
    binary = os.environ.get('OPENEVENT_SERVER_BIN')
    if not binary or not os.path.isfile(binary) or not os.access(binary, os.X_OK):
        sys.exit('OPENEVENT_SERVER_BIN must point to an executable OpenEvent server')
    binary = str(Path(binary).resolve())
    import grpc
    from openevent.sdk import AdminClient, OpenEventClient
    from openevent.chat_sdk import ObjectKey, TextPart, create_client
    from openevent.chat_app.config import new_ulid

    with fixture_directory() as root:
        event_port, admin_port, http_port = free_port(), free_port(), free_port()
        event_target = f'127.0.0.1:{event_port}'
        server_config = root / 'openevent.yaml'
        server_config.write_text(
            f'grpc:\n  listen_addr: "{event_target}"\n'
            f'admin:\n  listen_addr: "127.0.0.1:{admin_port}"\n'
            f'storage:\n  path: "{root / "data"}"\n', encoding='utf-8')
        with process([binary, str(server_config)], root / 'openevent.log') as event_process:
            admin_channel = grpc.insecure_channel(f'127.0.0.1:{admin_port}')
            try:
                try:
                    grpc.channel_ready_future(admin_channel).result(timeout=15)
                except grpc.FutureTimeoutError:
                    raise AssertionError(f'OpenEvent did not become ready (exit={event_process.poll()}); '
                                         f'see {root / "openevent.log"}') from None
            finally:
                admin_channel.close()
            with AdminClient(f'127.0.0.1:{admin_port}') as admin:
                user_token = admin.add_token(9001).token
                agent_token = admin.add_token(9002).token
            web_token = secrets.token_urlsafe(24)
            app_config = root / 'chat.json'
            channels = root / 'channels'
            app_config.write_text(json.dumps({
                'openevent_target': event_target, 'channels_dir': str(channels),
                'web_token': web_token, 'user_principal': '9001',
                'user_openevent_token': user_token, 'agent_principal': '9002',
                'rpc_timeout_ms': 2000, 'max_retries': 1}), encoding='utf-8')
            origin = f'http://127.0.0.1:{http_port}'
            command = [sys.executable, '-B', '-m', 'openevent.chat_app', '--config', str(app_config),
                       '--host', '127.0.0.1', '--port', str(http_port)]

            def request(method, path, body=None, *, auth=True, raw=None, content_type=None, request_origin=origin):
                headers = {}
                if auth:
                    headers['Cookie'] = 'web_token=' + quote(web_token, safe='')
                if method == 'POST':
                    headers['Origin'] = request_origin
                if body is not None:
                    raw = json.dumps(body).encode()
                    content_type = 'application/json'
                if content_type:
                    headers['Content-Type'] = content_type
                conn = http.client.HTTPConnection('127.0.0.1', http_port, timeout=10)
                try:
                    conn.request(method, path, body=raw, headers=headers)
                    response = conn.getresponse()
                    data = response.read()
                    typ = response.getheader('Content-Type', '')
                    value = json.loads(data) if typ.startswith('application/json') else data
                    return response.status, value, dict(response.getheaders())
                finally:
                    conn.close()

            def api(method, path, body=None, expected=200, **kwargs):
                status, data, _ = request(method, path, body, **kwargs)
                matched = status == expected if isinstance(expected, int) else status in expected
                assert matched, (method, path, status, data)
                return data

            def history(base, start, limit=2):
                cursor = start
                result = []
                while True:
                    page = api('GET', base + f'/history?fetch_seq={cursor}&limit={limit}')
                    result.extend(page['events'])
                    cursor = page['next_seq']
                    if int(cursor) > int(page['last_seq']):
                        return result

            def upload(base, data, *, expected=201):
                boundary = 'chat-e2e-' + secrets.token_hex(8)
                multipart = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="report.txt"\r\n'
                             'Content-Type: text/plain\r\n\r\n').encode() + data + f'\r\n--{boundary}--\r\n'.encode()
                return api('POST', base + '/attachments', expected=expected, raw=multipart,
                           content_type=f'multipart/form-data; boundary={boundary}')

            with OpenEventClient(event_target, timeout_ms=2000) as events:
                with process(command, root / 'chat.log') as child:
                    wait_http(child, http_port)
                    api('GET', '/api/chat/sessions', expected=401, auth=False)
                    assert api('GET', '/api/chat/sessions') == {'sessions': []}
                    request_id = new_ulid()
                    api('POST', '/api/chat/sessions', {'create_request_id': request_id},
                        expected=403, request_origin='http://other.invalid')
                    session = api('POST', '/api/chat/sessions', {'create_request_id': request_id}, expected=201)
                    assert api('POST', '/api/chat/sessions', {'create_request_id': request_id}) == session
                    sid = session['session_id']
                    base = f'/api/chat/sessions/{sid}'
                    config = json.loads((channels / f'{sid}.json').read_text())
                    channel_id = int(config['channel_id'])
                    agent = create_client(events, principal=9002, token=agent_token, channel_id=channel_id)
                    assert upload(base, b'x', expected=409)['error']['code'] == 'session_not_initialized'
                    assert api('POST', base + '/cancellations', {'target_turn_id': 'not-started'},
                               expected=409)['error']['code'] == 'session_not_initialized'
                    assert api('POST', base + '/turns', {'submission_id': '1', 'text': 'not ready'},
                               expected=409)['error']['code'] == 'submission_out_of_range'
                    allocation = api('POST', base + '/submissions', {'count': '3'}, expected=201)
                    assert allocation == {'start': '1', 'end': '3'}
                    # Outcome checks use the same POST; the old GET endpoint is absent.
                    api('GET', base + '/submissions/1', expected=404)
                    upload(base, b'', expected=range(400, 500))
                    upload(base, b'x' * (4 * 1024 * 1024 + 1), expected=range(400, 500))
                    assert session in api('GET', '/api/chat/sessions')['sessions']
                    file_data = b'\x00'
                    uploaded = upload(base, file_data)
                    oid = uploaded['object_id']
                    assert uploaded['nbytes'] == len(file_data)
                    send_body = {'submission_id': '1', 'text': '你好，OpenEvent',
                                 'attachments': [oid, oid], 'reply_to_seqs': []}
                    with ThreadPoolExecutor(max_workers=4) as pool:
                        replies = list(pool.map(lambda _: request('POST', base + '/turns', send_body), range(4)))
                    assert sum(status == 201 for status, _, _ in replies) == 1, replies
                    assert all(status in (200, 201, 202) for status, _, _ in replies), replies
                    sent = api('POST', base + '/turns', send_body)
                    assert sent == {'status': 'committed', 'submission_id': '1',
                                    'turn_ref': {'role': 'user', 'turn_id': 'user:1'}}
                    user_events = [event for event in history(base, session['scan_start_seq'])
                                   if event['payload'].get('turn_id') == 'user:1']
                    assert len(user_events) == 1, user_events
                    creation = int(user_events[0]['event_seq'])
                    api('GET', base + f'/attachments/{oid}/metadata?event_seq={creation + 1}', expected=404)
                    metadata = api('GET', base + f'/attachments/{oid}/metadata?event_seq={creation}')
                    assert metadata['name'] == 'report.txt' and metadata['nbytes'] == len(file_data)
                    status, download, headers = request('GET', base + f'/attachments/{oid}?event_seq={creation}')
                    assert status == 200 and download == file_data
                    assert headers['Content-Type'] == 'application/octet-stream'
                    assert headers['X-Content-Type-Options'] == 'nosniff'
                    assert 'attachment' in headers['Content-Disposition']
                    assert 'no-store' in headers['Cache-Control']
                    original = events.fetch(9001, user_token, from_seq=creation, limit=1,
                                            channels=[channel_id]).messages[0]
                    key = ObjectKey(original.object_keys[0].object_id, original.object_keys[0].object_token)
                    writer = agent.start_turn(turn_id='stream', reply_to_seqs=(creation,))
                    writer.append(object_keys=(key,))
                    writer.append(content=(TextPart('旧输出'),))
                    writer.reset(content=(TextPart('替换输出'),), object_keys=(key,))
                    writer.reset()
                    writer.append(content=(TextPart('最终输出'),))
                    writer.complete()
                    resumable = agent.start_turn(turn_id='resume', content=(TextPart('一'),))
                    resume_seq = resumable.creation_seq
                    resumable.reset(content=(TextPart('重置后'),))
                    del resumable
                    resumed = agent.resume_turn('resume', state_start_seq=int(session['scan_start_seq']))
                    assert resumed.creation_seq == resume_seq
                    resumed.append(content=(TextPart('二'),))
                    api('POST', base + '/cancellations', {'target_turn_id': 'resume'}, expected=201)
                    large_data = b'\x00\xff' * (2 * 1024 * 1024)
                    large_upload = upload(base, large_data)
                    large_body = {'submission_id': '3', 'text': '', 'attachments': [large_upload['object_id']]}
                    api('POST', base + '/turns', large_body, expected=201)
                    records = history(base, session['scan_start_seq'])
                    assert {'turn.single', 'turn.start', 'turn.append', 'turn.reset', 'turn.end', 'turn.cancel',
                            'submission.reserve'} <= {e['payload']['kind'] for e in records}
                    stream_records = [e for e in records if e['payload'].get('turn_id') == 'stream']
                    assert stream_records[0]['payload']['content'] == []
                    assert stream_records[1]['payload']['content'] == [] and stream_records[1]['attachments']
                    assert stream_records[4]['payload']['content'] == [] and not stream_records[4]['attachments']
                    user_event = next(e for e in records if e['event_seq'] == str(creation))
                    assert len(user_event['attachments']) == 2
                    large_creation = next(e['event_seq'] for e in records if e['payload'].get('turn_id') == 'user:3')
                    _, large_download, _ = request('GET', base + f'/attachments/{large_upload["object_id"]}?event_seq={large_creation}')
                    assert large_download == large_data
                    serialized = json.dumps(records)
                    assert user_token not in serialized and agent_token not in serialized
                    assert 'object_token' not in serialized and 'principal' not in serialized and 'uuid' not in serialized
                    # Rotate while retaining an unused issued ID. Both new and duplicate old-ID POSTs are rejected.
                    rotated = api('POST', base + '/submissions', {'count': '10000'}, expected=201)
                    assert rotated == {'start': '10001', 'end': '20000'}
                    api('POST', base + '/turns', {'submission_id': '2', 'text': 'expired'}, expected=409)
                    api('POST', base + '/turns', send_body, expected=409)
                    second = api('POST', '/api/chat/sessions', {'create_request_id': new_ulid()}, expected=201)
                    second_base = f'/api/chat/sessions/{second["session_id"]}'
                    assert api('POST', second_base + '/submissions', {'count': '1'}, expected=201)['start'] == '1'
                    api('POST', second_base + '/turns', {'submission_id': '1', 'text': '', 'attachments': [oid]}, expected=409)
                    pending_request = new_ulid()
                    with ThreadPoolExecutor(max_workers=3) as pool:
                        creations = list(pool.map(lambda _: request('POST', '/api/chat/sessions',
                                                   {'create_request_id': pending_request}), range(3)))
                    assert sorted(status for status, _, _ in creations) == [200, 200, 201], creations
                    pending_session = creations[0][1]
                    assert all(value == pending_session for _, value, _ in creations)
                    if os.environ.get('CHAT_BROWSER_E2E') == '1':
                        from browser_e2e import run_browser
                        run_browser(origin, web_token, events, agent_token, channels)
                    expected_next = str(1 + max(int(event['payload']['reserved_through'])
                        for event in history(base, session['scan_start_seq'])
                        if event['payload']['kind'] == 'submission.reserve'))
                    agent.close()
                # Every issued request has returned before this restart, as required by deployment.
                # Represent a crash after recording the Channel ID but before the atomic config move.
                pending_name = pending_session['session_id'] + '.json'
                (channels / pending_name).replace(channels / '.pending' / pending_name)
                with process(command, root / 'chat-restart.log') as child:
                    wait_http(child, http_port)
                    assert session in api('GET', '/api/chat/sessions')['sessions']
                    assert pending_session in api('GET', '/api/chat/sessions')['sessions']
                    assert api('POST', '/api/chat/sessions', {'create_request_id': pending_request}) == pending_session
                    api('POST', base + '/turns', send_body, expected=409)
                    fresh = api('POST', base + '/submissions', {'count': '1'}, expected=201)
                    assert fresh == {'start': expected_next, 'end': expected_next}
                    api('POST', base + '/turns', {'submission_id': fresh['start'], 'text': '', 'attachments': [oid]}, expected=409)
                    assert api('GET', base + f'/attachments/{oid}/metadata?event_seq={creation}')['nbytes'] == len(file_data)
                    assert not list((channels / '.pending').iterdir())
                    # Exhaust an actual RPC retry budget and require the whole Chat process to exit.
                    event_process.terminate()
                    event_process.wait(timeout=10)
                    try:
                        status, _, _ = request('GET', base + '/history?fetch_seq=1&limit=1')
                        assert status == 503, status
                    except (OSError, http.client.HTTPException):
                        pass  # Shutdown may close the HTTP connection before an error is returned.
                    assert child.wait(timeout=15) != 0, 'RPC exhaustion must exit the Chat process unsuccessfully'
    print('PASS: real OpenEvent + built Chat wheel: initialization, duplicate sends, reset, files, allocation, pending recovery, fatal RPC exhaustion')


if __name__ == '__main__':
    run()
