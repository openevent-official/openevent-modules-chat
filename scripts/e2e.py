"""Exercise a freshly installed Chat wheel against an actual OpenEvent server.

Only the SDK already installed in this interpreter is used. Set
OPENEVENT_SERVER_BIN to a successfully built server executable.
"""
from contextlib import contextmanager
import http.client
import json
import os
from pathlib import Path
import secrets
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
    from openevent.chat_sdk import ObjectKey, TextPart, TurnRef, create_client
    from openevent.chat_app.config import new_ulid

    with tempfile.TemporaryDirectory(prefix='openevent-chat-e2e-') as temporary:
        root = Path(temporary)
        event_port, admin_port, http_port = free_port(), free_port(), free_port()
        event_target = f'127.0.0.1:{event_port}'
        server_config = root / 'openevent.yaml'
        server_config.write_text(
            f'grpc:\n  listen_addr: "{event_target}"\n'
            f'admin:\n  listen_addr: "127.0.0.1:{admin_port}"\n'
            f'storage:\n  path: "{root / "data"}"\n', encoding='utf-8')
        with process([binary, str(server_config)], root / 'openevent.log'):
            admin_channel = grpc.insecure_channel(f'127.0.0.1:{admin_port}')
            try:
                grpc.channel_ready_future(admin_channel).result(timeout=15)
            finally:
                admin_channel.close()
            with AdminClient(f'127.0.0.1:{admin_port}') as admin:
                user_token = admin.add_token(9001).binding.token
                agent_token = admin.add_token(9002).binding.token
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
                assert status == expected, (method, path, status, data)
                return data

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
                    allocation = api('POST', base + '/submissions', {'count': '3'}, expected=201)
                    assert allocation == {'start': '1', 'end': '3'}
                    api('GET', base + '/submissions/1', expected=404)
                    file_data = b'attachment\x00bytes\n'
                    boundary = 'chat-e2e-' + secrets.token_hex(8)
                    multipart = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="report.txt"\r\n'
                                 'Content-Type: text/plain\r\n\r\n').encode() + file_data + f'\r\n--{boundary}--\r\n'.encode()
                    uploaded = api('POST', base + '/attachments', expected=201, raw=multipart,
                                   content_type=f'multipart/form-data; boundary={boundary}')
                    oid = uploaded['object_id']
                    assert uploaded['nbytes'] == len(file_data)
                    sent = api('POST', base + '/turns', {'submission_id': '1', 'text': '你好，OpenEvent',
                        'attachments': [oid, oid], 'reply_to_seqs': []}, expected=201)
                    assert sent == {'status': 'committed', 'submission_id': '1',
                                    'turn_ref': {'role': 'user', 'turn_id': 'user:1'}}
                    committed = api('GET', base + '/submissions/1')
                    creation = int(committed['seq'])
                    assert committed['submission_id'] == '1'
                    api('GET', base + f'/attachments/{oid}/metadata?event_seq={creation + 1}', expected=404)
                    metadata = api('GET', base + f'/attachments/{oid}/metadata?event_seq={creation}')
                    assert metadata['name'] == 'report.txt' and metadata['nbytes'] == len(file_data)
                    status, download, headers = request('GET', base + f'/attachments/{oid}?event_seq={creation}')
                    assert status == 200 and download == file_data
                    assert headers['Content-Type'] == 'application/octet-stream'
                    writer = agent.start_turn(turn_id='stream', content=(TextPart('答'),), reply_to_seqs=(creation,))
                    writer.append(content=(TextPart('复'),))
                    writer.complete()
                    resumable = agent.start_turn(turn_id='resume', content=(TextPart('一'),))
                    resume_seq = resumable.creation_seq
                    del resumable
                    resumed = agent.resume_turn('resume', state_start_seq=int(session['scan_start_seq']))
                    assert resumed.creation_seq == resume_seq
                    resumed.append(content=(TextPart('二'),))
                    api('POST', base + '/cancellations', {'target_turn_id': 'resume'}, expected=201)
                    cursor = session['scan_start_seq']
                    history = []
                    while True:
                        page = api('GET', base + f'/history?fetch_seq={cursor}&limit=2')
                        history.extend(page['events'])
                        cursor = page['next_seq']
                        if int(cursor) > int(page['last_seq']):
                            break
                    assert {'turn.single', 'turn.start', 'turn.append', 'turn.end', 'turn.cancel',
                            'submission.reserve'} <= {e['payload']['kind'] for e in history}
                    user_event = next(e for e in history if e['event_seq'] == str(creation))
                    assert len(user_event['attachments']) == 2
                    serialized = json.dumps(history)
                    assert user_token not in serialized and agent_token not in serialized
                    assert 'object_token' not in serialized and 'principal' not in serialized and 'uuid' not in serialized
                    # Rotate while retaining an unused issued ID. Old writes and queries are rejected.
                    rotated = api('POST', base + '/submissions', {'count': '10000'}, expected=201)
                    assert rotated == {'start': '10001', 'end': '20000'}
                    api('POST', base + '/turns', {'submission_id': '2', 'text': 'expired'}, expected=409)
                    api('GET', base + '/submissions/1', expected=409)
                    second = api('POST', '/api/chat/sessions', {'create_request_id': new_ulid()}, expected=201)
                    second_base = f'/api/chat/sessions/{second["session_id"]}'
                    assert api('POST', second_base + '/submissions', {'count': '1'}, expected=201)['start'] == '1'
                    api('POST', second_base + '/turns', {'submission_id': '1', 'text': '', 'attachments': [oid]}, expected=409)
                    if os.environ.get('CHAT_BROWSER_E2E') == '1':
                        from browser_e2e import run_browser
                        run_browser(origin, web_token, events, agent_token, channels)
                    agent.close()
                # Every issued request has returned before this restart, as required by deployment.
                with process(command, root / 'chat-restart.log') as child:
                    wait_http(child, http_port)
                    assert session in api('GET', '/api/chat/sessions')['sessions']
                    api('GET', base + '/submissions/10001', expected=409)
                    fresh = api('POST', base + '/submissions', {'count': '1'}, expected=201)
                    assert fresh == {'start': '20001', 'end': '20001'}
                    api('POST', base + '/turns', {'submission_id': fresh['start'], 'text': '', 'attachments': [oid]}, expected=409)
                    assert api('GET', base + f'/attachments/{oid}/metadata?event_seq={creation}')['nbytes'] == len(file_data)
                    assert not list((channels / '.pending').iterdir())
    print('PASS: real OpenEvent + built Chat wheel: SDK, HTTP, files, allocation, sessions, restart')


if __name__ == '__main__':
    run()
