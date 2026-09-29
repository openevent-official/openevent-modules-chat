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
    from openevent.chat_sdk import ObjectKey, TextPart, create_client

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
        page.locator('#token').fill(web_token)
        page.locator('#login-form button[type=submit]').click()
        expect(page.locator('#workspace')).to_be_visible()
        expect(page.locator('#message-input')).to_be_enabled()
        allocations = []
        def hold_allocation(route):
            allocations.append(route)
        page.route('**/submissions', hold_allocation)
        with page.expect_request(lambda request: request.url.endswith('/submissions')):
            with page.expect_response(lambda response: response.request.method == 'POST' and response.url.endswith('/sessions')) as created:
                page.locator('#new-session').click()
        session = created.value.json()
        sid = session['session_id']
        expect(page.locator('#message-input')).to_be_disabled()
        expect(page.locator('#file-input')).to_be_disabled()
        expect(page.locator('#send')).to_be_disabled()
        assert len(allocations) == 1
        allocations[0].continue_()
        page.unroute('**/submissions', hold_allocation)
        expect(page.locator('#message-input')).to_be_enabled()
        page.locator('#message-input').fill('浏览器发出的消息 <b>按纯文本显示</b>')
        page.locator('#file-input').set_input_files({'name': 'browser.txt', 'mimeType': 'text/plain', 'buffer': b'browser attachment'})
        expect(page.locator('#draft-files')).to_contain_text('已上传')
        metadata_requests = []
        page.on('request', lambda req: metadata_requests.append(req.url) if '/metadata?' in req.url else None)
        # Hold the first send so the real DOM must stay locked until its outcome is known.
        sends = []
        def hold_send(route):
            sends.append(route)
        page.route('**/turns', hold_send)
        with page.expect_request(lambda request: request.url.endswith('/turns')):
            page.locator('#send').click()
        expect(page.locator('#message-input')).to_be_disabled()
        expect(page.locator('#choose-files')).to_be_disabled()
        expect(page.locator('#send')).to_be_disabled()
        assert len(sends) == 1
        sends[0].fulfill(status=400, content_type='application/json', body=json.dumps({
            'error': {'code': 'invalid_request', 'message': 'test request rejection'}}))
        expect(page.locator('#message-input')).to_be_enabled()
        expect(page.locator('#message-input')).to_have_value('浏览器发出的消息 <b>按纯文本显示</b>')
        expect(page.locator('#draft-files')).to_contain_text('已上传')
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
            page.locator('.attachment button').filter(has_text='下载').click()
        with tempfile.TemporaryDirectory() as target:
            saved = Path(target) / 'download'
            downloading.value.save_as(saved)
            assert saved.read_bytes() == b'browser attachment'
        page.locator('.turn .reply-action').first.click()
        page.locator('#message-input').fill('带引用的第二条消息')
        page.locator('#send').click()
        expect(page.locator('.turn.user')).to_have_count(2)
        expect(page.locator('.turn-replies')).to_have_count(1)
        config = json.loads((channels / f'{sid}.json').read_text())
        agent = create_client(events, principal=9002, token=agent_token, channel_id=int(config['channel_id']))
        writer = agent.start_turn(turn_id='browser-stream')
        agent_turn = page.locator('.turn.agent')
        expect(agent_turn).to_have_count(1)
        expect(agent_turn.locator('.turn-content')).to_have_text('')
        expect(agent_turn.get_by_role('button', name='停止输出')).to_be_enabled()
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
        expect(agent_turn.locator('.attachment')).to_have_count(0)
        expect(agent_turn).to_have_attribute('data-e2e-original', 'true')
        writer.reset()
        expect(agent_turn.locator('.turn-content')).to_have_text('')
        expect(agent_turn.get_by_role('button', name='停止输出')).to_be_enabled()
        writer.append(content=(TextPart('最终输出'),))
        expect(agent_turn.locator('.turn-content')).to_have_text('最终输出')
        page.get_by_role('button', name='停止输出').click()
        expect(page.locator('.turn.agent')).to_contain_text('已取消')
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
        second.goto(origin + '/api/chat/')
        expect(second.locator('#workspace')).to_be_visible()
        expect(second.locator('.turn.user')).to_have_count(4)
        expect(second.locator('.attachment')).to_contain_text('browser.txt')
        expect(second.locator('.turn.agent')).to_contain_text('已取消')
        page.set_viewport_size({'width': 390, 'height': 844})
        expect(page.locator('#send')).to_be_visible()
        # Mobile keeps the same list retry control; refreshing never reloads the draft.
        refresh = page.get_by_role('button', name='刷新会话列表')
        expect(refresh).to_be_visible()
        page.locator('#message-input').fill('刷新列表后保留的草稿')
        list_attempts = []
        def fail_first_list(route):
            list_attempts.append(route.request.method)
            if len(list_attempts) == 1:
                route.fulfill(status=503, content_type='application/json', body=json.dumps({
                    'error': {'code': 'request_failed', 'message': '列表暂时不可用'}}))
            else:
                route.continue_()
        page.route('**/api/chat/sessions', fail_first_list)
        refresh.click()
        expect(page.locator('#directory-error')).to_have_text('列表暂时不可用')
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
        page.set_viewport_size({'width': 1280, 'height': 860})
        screenshot = Path('build/browser-chat.png')
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(screenshot), full_page=True)
        page.locator('#logout').click()
        expect(page.locator('#login')).to_be_visible()
        assert page.locator('.turn').count() == 0
        assert 'web_token=' not in page.evaluate('document.cookie')
        assert not errors, errors
        context.close()
        browser.close()
    print('PASS: Chromium: initialization, files, replies, empty start/reset/cancel, same-ID retry, two tabs, mobile')
