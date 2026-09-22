"""Optional real-browser checks against the live E2E fixture."""
import json
import os
from pathlib import Path
import tempfile


def run_browser(origin, web_token, events, agent_token, channels):
    try:
        from playwright.sync_api import sync_playwright, expect
    except ImportError:
        raise RuntimeError('Browser E2E requires an installed playwright package and Chromium') from None
    from openevent.chat_sdk import TextPart, create_client

    with sync_playwright() as runtime:
        launch = {'headless': True}
        if os.environ.get('CHAT_CHROMIUM_BIN'):
            launch['executable_path'] = os.environ['CHAT_CHROMIUM_BIN']
        browser = runtime.chromium.launch(**launch)
        context = browser.new_context(viewport={'width': 1280, 'height': 860}, accept_downloads=True)
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(origin + '/api/chat/')
        page.locator('#token').fill(web_token)
        page.locator('#login-form button[type=submit]').click()
        expect(page.locator('#workspace')).to_be_visible()
        with page.expect_response(lambda response: response.request.method == 'POST' and response.url.endswith('/sessions')) as created:
            page.locator('#new-session').click()
        session = created.value.json()
        sid = session['session_id']
        expect(page.locator('#message-input')).to_be_enabled()
        page.locator('#message-input').fill('浏览器发出的消息 <b>按纯文本显示</b>')
        page.locator('#file-input').set_input_files({'name': 'browser.txt', 'mimeType': 'text/plain', 'buffer': b'browser attachment'})
        expect(page.locator('#draft-files')).to_contain_text('已上传')
        metadata_requests = []
        page.on('request', lambda req: metadata_requests.append(req.url) if '/metadata?' in req.url else None)
        with page.expect_response(lambda response: response.request.method == 'POST' and response.url.endswith('/turns')) as sent:
            page.locator('#send').click()
        assert sent.value.status == 201
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
        writer = agent.start_turn(turn_id='browser-stream', content=(TextPart('正在生成'),))
        expect(page.locator('.turn.agent')).to_contain_text('正在生成')
        writer.append(content=(TextPart('，内容已追加'),))
        expect(page.locator('.turn.agent')).to_contain_text('内容已追加')
        page.get_by_role('button', name='停止输出').click()
        expect(page.locator('.turn.agent')).to_contain_text('已取消')
        del writer  # External cancellation was observed; releasing performs no RPC.
        agent.close()

        # Lose the POST response after the server committed. Browser must query, never resubmit.
        posts = []
        def lose_send_response(route):
            posts.append(route.request.post_data_json)
            route.fetch()
            route.abort('failed')
        page.route('**/turns', lose_send_response)
        page.locator('#message-input').fill('响应丢失仍只提交一次')
        page.locator('#send').click()
        expect(page.locator('.turn.user')).to_have_count(3)
        expect(page.locator('#message-input')).to_have_value('')
        assert len(posts) == 1
        page.unroute('**/turns', lose_send_response)

        second = context.new_page()
        second.goto(origin + '/api/chat/')
        expect(second.locator('#workspace')).to_be_visible()
        expect(second.locator('.turn.user')).to_have_count(3)
        expect(second.locator('.attachment')).to_contain_text('browser.txt')
        expect(second.locator('.turn.agent')).to_contain_text('已取消')
        page.set_viewport_size({'width': 390, 'height': 844})
        expect(page.locator('#send')).to_be_visible()
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
    print('PASS: Chromium: login, sessions, files, replies, stream/cancel, uncertain send, two tabs, mobile')
