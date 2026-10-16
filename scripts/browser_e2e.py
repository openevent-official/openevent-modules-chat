"""Optional real-browser checks against the live E2E fixture."""
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import parse_qs, urlsplit


def run_browser(origin, web_token, events, agent_token, channels):
    try:
        from playwright.sync_api import sync_playwright, expect
    except ImportError:
        raise RuntimeError('Browser E2E requires an installed playwright package and Chromium') from None
    from openevent.chat_sdk import ObjectKey, RawExtensions, TextPart, create_client

    with sync_playwright() as runtime:
        launch = {'headless': True}
        if os.environ.get('CHAT_CHROMIUM_BIN'):
            launch['executable_path'] = os.environ['CHAT_CHROMIUM_BIN']
        browser = runtime.chromium.launch(**launch)
        context = browser.new_context(viewport={'width': 1280, 'height': 860}, accept_downloads=True)
        context.set_default_timeout(10000)
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(origin + '/api/chat/')
        expect(page.locator('html')).to_have_attribute('lang', 'en')
        expect(page.get_by_role('heading', name='Continue your conversation')).to_be_visible()
        page.get_by_label('Access token', exact=True).fill(web_token)
        page.get_by_role('button', name='Open chat', exact=True).click()
        expect(page.locator('#workspace')).to_be_visible()
        expect(page.locator('#message-input')).to_be_enabled()
        allocations = []
        def hold_allocation(route):
            allocations.append(route)
        page.route('**/submissions', hold_allocation)
        with page.expect_response(lambda response: response.request.method == 'POST' and response.url.endswith('/sessions')) as created:
            page.locator('#new-session').click()
        session = created.value.json()
        sid = session['session_id']
        config_path = channels / f'{sid}.json'
        config = json.loads(config_path.read_text())
        assert session['channel_id'] == config['channel_id']
        assert session['name'] == config['name'] == f'Session {sid[-6:]}'
        channel_label = f"Channel ID: {config['channel_id']}"
        expect(page.locator('#session-title')).to_have_text(config['name'])
        expect(page.locator('#session-channel')).to_have_text(channel_label)
        expect(page.locator('.empty-state h2')).to_have_text('Start with a message')
        expect(page.locator('.empty-state p')).to_have_text('Ask a question or add a file.')
        expect(page.get_by_placeholder('Write a message…')).to_be_visible()
        rename = page.get_by_role('button', name='Rename session', exact=True)
        expect(rename).to_be_visible()
        expect(rename).to_have_attribute('title', 'Rename session')
        expect(rename.locator('svg')).to_have_attribute('aria-hidden', 'true')
        assert rename.inner_text().strip() == '', 'rename should display only the pencil icon'
        rename_box = rename.bounding_box()
        assert rename_box['width'] >= 44 and rename_box['height'] >= 44
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        expect(page.locator('#message-input')).to_be_enabled()
        expect(page.locator('#file-input')).to_be_enabled()
        expect(page.locator('#send')).to_be_enabled()
        page.locator('#message-input').fill('浏览器发出的消息 <b>按纯文本显示</b>')
        assert allocations == [], 'opening a session must not allocate submission IDs'
        page.locator('#file-input').set_input_files({'name': 'browser.txt', 'mimeType': 'text/plain', 'buffer': b'browser attachment'})
        expect(page.locator('#draft-files')).to_contain_text('Uploaded')
        assert allocations == [], 'uploading must not allocate submission IDs'
        metadata_requests = []
        page.on('request', lambda req: metadata_requests.append(req.url) if '/metadata?' in req.url else None)
        # Hold the first send so the real DOM must stay locked until its outcome is known.
        sends = []
        def hold_send(route):
            sends.append(route)
        page.route('**/turns', hold_send)
        with page.expect_request(lambda request: request.url.endswith('/submissions')):
            page.locator('#send').click()
        expect(page.locator('#message-input')).to_be_disabled()
        expect(page.locator('#choose-files')).to_be_disabled()
        expect(page.locator('#send')).to_be_disabled()
        assert len(allocations) == 1 and sends == []
        with page.expect_request(lambda request: request.url.endswith('/turns')):
            allocations[0].continue_()
        page.unroute('**/submissions', hold_allocation)
        assert len(sends) == 1
        sends[0].fulfill(status=400, content_type='application/json', body=json.dumps({
            'error': {'code': 'invalid_request', 'message': 'test request rejection'}}))
        expect(page.locator('#message-input')).to_be_enabled()
        expect(page.locator('#message-input')).to_have_value('浏览器发出的消息 <b>按纯文本显示</b>')
        expect(page.locator('#draft-files')).to_contain_text('Uploaded')
        page.unroute('**/turns', hold_send)
        with page.expect_response(lambda response: response.request.method == 'POST' and response.url.endswith('/turns')) as sent:
            page.locator('#send').click()
        assert sent.value.status == 201
        expect(page.locator('#message-input')).to_be_enabled()
        expect(page.locator('#message-input')).to_have_value('')
        expect(page.locator('#draft-files')).to_be_empty()
        try:
            expect(page.locator('.turn.user')).to_have_count(1)
        except AssertionError:
            print('Browser diagnostics:', errors, page.locator('#workspace').inner_text())
            raise
        expect(page.locator('.turn-content')).to_contain_text('<b>按纯文本显示</b>')
        assert page.locator('.turn-content b').count() == 0
        expect(page.locator('.attachment')).to_contain_text('browser.txt')
        assert metadata_requests == [], 'upload metadata must be reused after publication'
        with page.expect_download() as downloading:
            page.locator('.attachment').get_by_role('button', name='Download', exact=True).click()
        with tempfile.TemporaryDirectory() as target:
            saved = Path(target) / 'download'
            downloading.value.save_as(saved)
            assert saved.read_bytes() == b'browser attachment'
        page.locator('.turn .reply-action').first.click()
        page.locator('#message-input').fill('带引用的第二条消息')
        page.locator('#send').click()
        expect(page.locator('.turn.user')).to_have_count(2)
        expect(page.locator('.turn-replies')).to_have_count(1)
        # The configured name remains independent of the first message. Rename
        # updates both views and the durable config without changing the Channel.
        expect(page.locator('#session-title')).to_have_text(config['name'])
        rename.focus()
        expect(rename).to_be_focused()
        rename.press('Enter')
        expect(page.locator('#session-name')).to_have_value(config['name'])
        expect(page.get_by_role('button', name='Save', exact=True)).to_be_visible()
        expect(page.get_by_role('button', name='Cancel', exact=True)).to_be_visible()
        renamed_name = '浏览器会话 <重命名> 😀'
        page.locator('#session-name').fill(renamed_name)
        with page.expect_response(lambda response: response.request.method == 'POST' and
                                  response.url.endswith(f'/sessions/{sid}/rename')) as renamed:
            page.locator('#save-session-name').click()
        assert renamed.value.status == 200
        renamed_session = renamed.value.json()
        assert renamed_session == {**session, 'name': renamed_name}
        expect(page.locator('#session-title')).to_have_text(renamed_name)
        expect(page.locator('.session-item.active')).to_have_text(renamed_name)
        expect(page.locator('#session-channel')).to_have_text(channel_label)
        assert json.loads(config_path.read_text()) == {**config, 'name': renamed_name}
        page.reload()
        expect(page.locator('#workspace')).to_be_visible()
        expect(page.locator('#session-title')).to_have_text(renamed_name)
        expect(page.locator('.session-item.active')).to_have_text(renamed_name)
        expect(page.locator('#session-channel')).to_have_text(channel_label)
        expect(page.locator('.turn.user')).to_have_count(2)
        expect(page.locator('#file-input')).to_be_enabled()
        agent = create_client(events, principal=9002, token=agent_token, channel_id=int(config['channel_id']))
        # Extension values never participate in browser projection or rendering.
        opaque = RawExtensions('{"large":1e400,"precise":0.12345678901234567890123456789,"deep":'
                               + '[' * 2000 + '0' + ']' * 2000 + '}')
        writer = agent.start_turn(turn_id='browser-stream', extensions=opaque)
        agent_turn = page.locator('.turn.agent')
        expect(agent_turn).to_have_count(1)
        expect(agent_turn.locator('.turn-content')).to_have_text('')
        expect(agent_turn.get_by_role('button', name='Stop generating')).to_be_enabled()
        expect(agent_turn.locator('.reply-action')).to_be_disabled()
        agent_turn.evaluate("element => element.setAttribute('data-e2e-original', 'true')")
        agent_file = events.write_object(principal=9002, token=agent_token, name='agent.txt',
                                        type='text/plain', description='', data=b'agent attachment')
        writer.append(object_keys=(ObjectKey(agent_file.object_id, agent_file.object_token),))
        expect(agent_turn.locator('.attachment')).to_contain_text('agent.txt')
        expect(agent_turn.locator('.turn-content')).to_have_text('')
        writer.append(content=(TextPart('原来的内容'),))
        expect(agent_turn.locator('.turn-content')).to_have_text('原来的内容')
        writer.reset(content=(TextPart('重置后的内容'),))
        expect(agent_turn.locator('.turn-content')).to_have_text('重置后的内容')
        expect(agent_turn.locator('.reply-action')).to_be_disabled()
        expect(agent_turn.locator('.attachment')).to_have_count(0)
        expect(agent_turn).to_have_attribute('data-e2e-original', 'true')
        writer.reset()
        expect(agent_turn.locator('.turn-content')).to_have_text('')
        expect(agent_turn.get_by_role('button', name='Stop generating')).to_be_enabled()
        writer.append(content=(TextPart('最终输出'),))
        expect(agent_turn.locator('.turn-content')).to_have_text('最终输出')
        page.get_by_role('button', name='Stop generating').click()
        expect(page.locator('.turn.agent')).to_contain_text('Canceled')
        expect(agent_turn.locator('.reply-action')).to_be_enabled()
        agent_turn.locator('.reply-action').click()
        expect(page.locator('#reply-selection .reply-chip')).to_contain_text('最终输出')
        page.locator('#reply-selection .reply-chip').click()
        expect(page.locator('#reply-selection')).to_be_empty()
        del writer  # External cancellation was observed; releasing performs no RPC.
        agent.close()

        # A request that never reaches the backend must be resent with the frozen original body.
        posts = []
        def lose_first_request(route):
            posts.append(route.request.post_data_json)
            if len(posts) == 1:
                route.abort('failed')
            else:
                route.continue_()
        page.route('**/turns', lose_first_request)
        page.locator('#message-input').fill('请求未送达后自动重试')
        page.locator('#send').click()
        expect(page.locator('.turn.user')).to_have_count(3)
        expect(page.locator('#message-input')).to_have_value('')
        assert len(posts) >= 2 and all(body == posts[0] for body in posts)
        page.unroute('**/turns', lose_first_request)

        # Freeze history so only a duplicate POST can confirm the lost success response.
        def hold_history(route):
            cursor = parse_qs(urlsplit(route.request.url).query)['fetch_seq'][0]
            route.fulfill(status=200, content_type='application/json', body=json.dumps({
                'events': [], 'next_seq': cursor, 'last_seq': str(int(cursor) - 1)}))
        with page.expect_response(lambda response: '/history?' in response.url):
            page.route('**/history?*', hold_history)
        posts = []
        statuses = []
        def lose_send_response(route):
            posts.append(route.request.post_data_json)
            response = route.fetch()
            statuses.append(response.status)
            if len(posts) == 1:
                route.abort('failed')
            else:
                route.fulfill(response=response)
        page.route('**/turns', lose_send_response)
        page.locator('#message-input').fill('响应丢失仍只提交一次')
        page.locator('#send').click()
        expect(page.locator('#message-input')).to_have_value('')
        assert statuses[0] == 201 and 200 in statuses[1:], statuses
        assert len(posts) >= 2 and all(body == posts[0] for body in posts)
        expect(page.locator('.turn.user')).to_have_count(3)
        page.unroute('**/turns', lose_send_response)
        page.unroute('**/history?*', hold_history)
        expect(page.locator('.turn.user')).to_have_count(4)

        second = context.new_page()
        second_allocations = []
        second.route('**/submissions', lambda route: second_allocations.append(route))
        metadata_attempts = []
        def fail_first_metadata(route):
            metadata_attempts.append(route.request.url)
            if len(metadata_attempts) == 1:
                route.fulfill(status=503, content_type='application/json', body=json.dumps({
                    'error': {'code': 'request_failed', 'message': 'File information temporarily unavailable'}}))
            else:
                route.continue_()
        second.route('**/metadata?*', fail_first_metadata)
        second.goto(origin + '/api/chat/')
        expect(second.locator('#workspace')).to_be_visible()
        expect(second.locator('.turn.user')).to_have_count(4)
        expect(second.locator('#message-input')).to_be_enabled()
        expect(second.locator('#file-input')).to_be_enabled()
        expect(second.locator('#send')).to_be_enabled()
        second.locator('#message-input').fill('尚未发送的草稿')
        second.locator('.turn .reply-action').first.click()
        expect(second.locator('#reply-selection .reply-chip')).to_have_count(1)
        second.locator('#reply-selection .reply-chip').click()
        expect(second.locator('#reply-selection')).to_be_empty()
        second.get_by_role('button', name='Retry file info').click()
        expect(second.locator('.attachment')).to_contain_text('browser.txt')
        assert len(metadata_attempts) == 2, metadata_attempts
        with second.expect_download() as downloading:
            second.locator('.attachment').get_by_role('button', name='Download', exact=True).click()
        with tempfile.TemporaryDirectory() as target:
            saved = Path(target) / 'download'
            downloading.value.save_as(saved)
            assert saved.read_bytes() == b'browser attachment'
        expect(second.locator('#message-input')).to_have_value('尚未发送的草稿')
        assert second_allocations == [], 'reading, replying and downloading must not allocate submission IDs'
        second.unroute('**/submissions')
        second.unroute('**/metadata?*', fail_first_metadata)
        expect(second.locator('#send')).to_be_enabled()
        expect(second.locator('.turn.agent')).to_contain_text('Canceled')
        page.set_viewport_size({'width': 390, 'height': 844})
        expect(page.locator('#send')).to_be_visible()
        expect(page.get_by_role('button', name='Rename session', exact=True)).to_be_visible()
        expect(page.locator('#session-title')).to_have_text(renamed_name)
        expect(page.locator('#session-channel')).to_have_text(channel_label)
        expect(page.get_by_role('button', name='Sign out', exact=True)).to_be_visible()
        # Mobile keeps the same list retry control; refreshing never reloads the draft.
        refresh = page.get_by_role('button', name='Refresh sessions')
        expect(refresh).to_be_visible()
        page.locator('#message-input').fill('刷新列表后保留的草稿')
        list_attempts = []
        def fail_first_list(route):
            list_attempts.append(route.request.method)
            if len(list_attempts) == 1:
                route.fulfill(status=503, content_type='application/json', body=json.dumps({
                    'error': {'code': 'request_failed', 'message': 'Session list temporarily unavailable'}}))
            else:
                route.continue_()
        page.route('**/api/chat/sessions', fail_first_list)
        refresh.click()
        expect(page.locator('#directory-error')).to_have_text('Session list temporarily unavailable')
        expect(refresh).to_be_enabled()
        expect(page.locator('#message-input')).to_have_value('刷新列表后保留的草稿')
        refresh.click()
        expect(page.locator('#directory-error')).to_be_empty()
        expect(refresh).to_be_enabled()
        assert list_attempts == ['GET', 'GET'], list_attempts
        expect(page.locator('#message-input')).to_have_value('刷新列表后保留的草稿')
        page.unroute('**/api/chat/sessions', fail_first_list)
        page.locator('#message-input').fill('')
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        page.locator('#composer').screenshot(path='build/browser-composer-mobile.png', animations='disabled')
        page.locator('.turn.agent').scroll_into_view_if_needed()
        page.locator('#messages').screenshot(path='build/browser-history-mobile.png', animations='disabled')
        page.set_viewport_size({'width': 1280, 'height': 860})
        screenshot = Path('build/browser-chat.png')
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(screenshot), full_page=True)
        page.locator('#message-input').fill('Ready to send.')
        page.locator('#composer').screenshot(path='build/browser-composer-ready.png', animations='disabled')
        page.locator('#logout').click()
        expect(page.locator('#login')).to_be_visible()
        assert page.locator('.turn').count() == 0
        assert 'web_token=' not in page.evaluate('document.cookie')
        assert not errors, errors
        context.close()
        browser.close()
    print('PASS: Chromium: English UI, accessible rename icon, names, Channel IDs, reload, files, replies, empty start/reset/cancel, same-ID retry, two tabs, mobile')
