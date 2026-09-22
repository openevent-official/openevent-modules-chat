import test from 'node:test';
import assert from 'node:assert/strict';

// A small DOM boundary lets the real entry module exercise its HTTP and lifecycle
// logic without a browser dependency. Projection and timer races have separate tests.
class Element {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase(); this.children = []; this.listeners = new Map(); this.dataset = {};
    this.value = ''; this.textContent = ''; this.disabled = false; this.hidden = false;
    this.scrollTop = 0; this.scrollHeight = 100; this.clientHeight = 100; this.files = [];
  }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  set textContent(value) {
    this._text = value;
    for (const child of this.children) child.parentNode = null;
    this.children = [];
  }
  append(...children) {
    for (const child of children) {
      for (const element of child?.tagName === 'FRAGMENT' ? [...child.children] : [child]) this.insertBefore(element, null);
    }
  }
  insertBefore(child, reference) {
    child.remove?.(); child.parentNode = this;
    this.children.splice(reference ? this.children.indexOf(reference) : this.children.length, 0, child);
  }
  replaceChildren(...children) { this.textContent = ''; this.append(...children); }
  addEventListener(name, callback) { if (!this.listeners.has(name)) this.listeners.set(name, []); this.listeners.get(name).push(callback); }
  setAttribute(name, value) { this[name] = value; }
  focus() {} scrollIntoView() {}
  remove() {
    if (this.parentNode) this.parentNode.children.splice(this.parentNode.children.indexOf(this), 1);
    this.parentNode = null;
  }
  click() { return this.dispatch('click'); }
  dispatch(name, overrides = {}) {
    const event = {target: this, preventDefault() {}, ...overrides};
    for (const callback of this.listeners.get(name) || []) callback(event);
  }
}

function environment(handler) {
  const elements = new Map();
  const document = {
    body: new Element('body'), cookie: 'web_token=test-token',
    getElementById: id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); },
    createElement: name => new Element(name), createDocumentFragment: () => new Element('fragment'),
    createTextNode: data => ({data, get textContent() { return this.data; }, appendData(text) { this.data += text; }}),
  };
  const timers = new Map(); let timerId = 0;
  const storage = new Map(); const calls = [];
  Object.assign(globalThis, {
    document, window: new Element('window'), location: {protocol: 'https:'},
    sessionStorage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key)},
    setTimeout: (callback, delay) => { timers.set(++timerId, {callback, delay}); return timerId; },
    clearTimeout: id => timers.delete(id),
    fetch: async (url, options) => { const call = {url, ...options}; calls.push(call); return handler(call); },
  });
  return {elements, document, timers, calls, storage, get: document.getElementById,
    runTimer(delay) {
      const match = [...timers].find(([, timer]) => timer.delay === delay);
      assert.ok(match, `expected a ${delay} ms timer`);
      timers.delete(match[0]); match[1].callback();
    }};
}

const SID = '01ARZ3NDEKTSV4RRFFQ69G5FAV';
const description = {session_id: SID, scan_start_seq: '1'};
const ok = (body, status = 200) => new Response(JSON.stringify(body), {status, headers: {'Content-Type': 'application/json'}});
const err = (status, code) => ok({error: {code, message: code}}, status);
const empty = (next = '1', last = '0') => ({events: [], next_seq: next, last_seq: last});
const flush = () => new Promise(resolve => setImmediate(resolve));
async function until(predicate) {
  for (let attempt = 0; attempt < 50; attempt++) { if (predicate()) return; await flush(); }
  assert.ok(predicate(), 'expected asynchronous operation to finish');
}
async function load(id) { const module = await import(`../../src/openevent/chat_app/static/app.js?test=${id}`); await flush(); return module; }
function typeAndSend(ui, text) {
  ui.get('message-input').value = text; ui.get('message-input').dispatch('input'); ui.get('composer').dispatch('submit');
}
function historyEvent(seq, payload, role = 'agent', attachments = []) {
  return {event_seq: String(seq), ts_ms: '0', publisher_role: role, recipients: [], attachments, payload};
}
function historyPage(events, next) { return {events, next_seq: String(next), last_seq: String(BigInt(next) - 1n)}; }
function findElement(element, predicate) {
  if (predicate(element)) return element;
  for (const child of element.children || []) { const found = findElement(child, predicate); if (found) return found; }
}

test('preview prefixes preserve trimming and UTF-16 slicing without reading unnecessary parts', async () => {
  const ui = environment(() => ok({sessions: []}));
  const {textPrefix} = await load('prefix');
  const cases = [
    [], [''], [' \t\n', '\u00a0\ufeff', '中文', '  '],
    ['x'.repeat(34), ' ', '\t \u00a0', 'tail'],
    ['x'.repeat(34), ' ', '\t \u00a0'],
    ['x'.repeat(34), ' \t \u00a0 tail'],
    ['😀'.repeat(17), '😀tail'], ['\ud83d', '\ude00', 'a'.repeat(100)],
    ['', ' \t', 'αβ'.repeat(50), '\n'], [' \t', '\n', '\ufeff'],
  ];
  for (const texts of cases) {
    const parts = texts.map(text => ({text}));
    assert.equal(textPrefix(parts, 35, true), texts.join('').trim().slice(0, 35));
    assert.equal(textPrefix(parts, 80), texts.join('').slice(0, 80));
  }
  for (const [limit, trim] of [[35, true], [80, false]]) {
    let reads = 0;
    const parts = Array.from({length: 100000}, () => ({get text() { reads++; return 'x'; }}));
    assert.equal(textPrefix(parts, limit, trim), 'x'.repeat(limit));
    assert.equal(reads, limit);
    assert.equal(textPrefix([
      {text: 'x'.repeat(limit)}, {get text() { throw new Error('unused text was read'); }},
    ], limit, trim), 'x'.repeat(limit));
  }
  ui.get('logout').click();
});

test('session titles and quoted replies retain their existing whitespace and length', async () => {
  const texts = [' \t', 'x'.repeat(34), ' ', '\u00a0\n', 'y'.repeat(100)];
  const fullText = texts.join('');
  const ui = environment(call => {
    if (call.url === '/api/chat/sessions') return ok({sessions: [description]});
    if (call.url.includes('/history?')) return ok(historyPage([
      historyEvent(1, {kind: 'turn.single', turn_id: 'user:1', content: texts.map(text => ({type: 'text', text})), reply_to_seqs: []}, 'user'),
      historyEvent(2, {kind: 'turn.single', turn_id: 'answer', content: [], reply_to_seqs: ['1']}),
    ], 3));
    throw new Error(`unexpected request ${call.url}`);
  });
  await load('preview-display');
  const expectedTitle = fullText.trim().slice(0, 35);
  assert.equal(ui.get('sessions').children[0].textContent, expectedTitle);
  assert.equal(ui.get('sessions').children[0].title, expectedTitle);
  assert.equal(ui.get('session-title').textContent, expectedTitle);
  const quote = findElement(ui.get('messages'), element => element.className === 'quoted-reply');
  assert.equal(quote.textContent, `你：${fullText.slice(0, 80)}`);
  ui.get('logout').click();
});

test('session buttons survive polling, title updates, insertions and switching; logout clears their cache', async () => {
  const newer = {session_id: '01ARZ3NDEKTSV4RRFFQ69G5FAW', scan_start_seq: '1'};
  let histories = 0; let created = false;
  const ui = environment(call => {
    if (call.url === '/api/chat/sessions') {
      if (call.method === 'POST') { created = true; return ok(newer); }
      return ok({sessions: created ? [description, newer] : [description]});
    }
    if (call.url.includes(`/sessions/${SID}/history?`)) {
      if (++histories === 1) return ok(empty());
      if (histories === 2) return ok(historyPage([historyEvent(1, {
        kind: 'turn.single', turn_id: 'user:1', content: [{type: 'text', text: '更新后的标题'}], reply_to_seqs: [],
      }, 'user')], 2));
      return ok(empty('2', '1'));
    }
    if (call.url.includes('/history?')) return ok(empty());
    throw new Error(`unexpected request ${call.url}`);
  });
  await load('session-controls');
  const original = ui.get('sessions').children[0];
  ui.runTimer(1000); await until(() => original.textContent === '更新后的标题');
  assert.equal(ui.get('sessions').children[0], original); assert.equal(original.title, original.textContent);
  ui.runTimer(1000); await flush(); assert.equal(ui.get('sessions').children[0], original);
  ui.get('new-session').click(); await until(() => ui.get('sessions').children.length === 2); await flush();
  const latest = ui.get('sessions').children[0];
  assert.equal(ui.get('sessions').children[1], original);
  assert.equal(latest['aria-current'], 'page'); assert.equal(original['aria-current'], 'false');
  original.click(); await flush();
  assert.equal(ui.get('sessions').children[0], latest); assert.equal(ui.get('sessions').children[1], original);
  assert.equal(original['aria-current'], 'page'); assert.equal(ui.get('session-title').textContent, '更新后的标题');
  ui.get('refresh-sessions').click(); await flush();
  assert.deepEqual(ui.get('sessions').children, [latest, original]);
  ui.get('logout').click(); assert.equal(ui.get('sessions').children.length, 0);
  ui.get('token').value = 'new-token'; ui.get('login-form').dispatch('submit'); await flush();
  assert.equal(ui.get('sessions').children.length, 2);
  assert.notEqual(ui.get('sessions').children[0], latest); assert.notEqual(ui.get('sessions').children[1], original);
  ui.get('logout').click();
});

test('draft controls survive polling and update in place through streaming, upload retry and send locking', async () => {
  let histories = 0; let finishAllocation; const uploads = [];
  const ui = environment(call => {
    if (call.url === '/api/chat/sessions') return ok({sessions: [description]});
    if (call.url.includes('/history?')) {
      if (++histories === 1) return ok(historyPage([
        historyEvent(1, {kind: 'turn.start', turn_id: 'answer', content: [{type: 'text', text: '开头'}], reply_to_seqs: []}),
        historyEvent(2, {kind: 'turn.single', turn_id: 'user:1', content: [{type: 'text', text: '问题'}], reply_to_seqs: []}, 'user'),
      ], 3));
      if (histories === 2) return ok(historyPage([historyEvent(3, {
        kind: 'turn.append', turn_id: 'answer', pre_seq: '1', content: [{type: 'text', text: '追加'}],
      })], 4));
      return ok(empty('4', '3'));
    }
    if (call.url.endsWith('/attachments')) return new Promise(resolve => uploads.push(resolve));
    if (call.url.endsWith('/submissions')) return new Promise(resolve => { finishAllocation = resolve; });
    throw new Error(`unexpected request ${call.url}`);
  });
  await load('draft-controls');
  for (const article of ui.get('messages').children) findElement(article, element => element.textContent === '回复').click();
  await flush();
  const [reply, otherReply] = ui.get('reply-selection').children;
  ui.get('file-input').files = [new File(['one'], '一.txt'), new File(['two'], '二.txt')];
  ui.get('file-input').dispatch('change'); await until(() => uploads.length === 2); await flush();
  const [file, otherFile] = ui.get('draft-files').children;
  const remove = findElement(file, element => element.textContent === '移除');
  const retry = findElement(file, element => element.textContent === '重新上传');
  const assertPreserved = () => {
    assert.equal(ui.get('reply-selection').children[0], reply);
    assert.equal(ui.get('draft-files').children[0], file);
    assert.equal(findElement(file, element => element.textContent === '移除'), remove);
  };
  ui.runTimer(1000); await until(() => reply.textContent.includes('开头追加')); assertPreserved();
  ui.runTimer(1000); await flush(); assertPreserved();
  uploads[0](err(503, 'upload_failed')); uploads[1](ok({object_id: '42', name: '服务端文件名.txt', type: 'text/plain', description: '', nbytes: 3}, 201));
  await until(() => !retry.hidden && otherFile.textContent.includes('服务端文件名.txt')); assertPreserved();
  retry.click(); await until(() => uploads.length === 3); await flush();
  assert.equal(retry.hidden, true); assertPreserved();
  uploads[2](ok({object_id: '43', name: '一.txt', type: 'text/plain', description: '', nbytes: 3}, 201));
  await until(() => !ui.get('send').disabled); assertPreserved();
  typeAndSend(ui, '暂存草稿'); await until(() => !!finishAllocation); await flush();
  assert.equal(reply.disabled, true); assert.equal(remove.disabled, true); assertPreserved();
  finishAllocation(err(503, 'allocation_failed')); await until(() => !reply.disabled);
  assert.equal(remove.disabled, false); assertPreserved();
  otherReply.click(); await flush(); assertPreserved(); assert.equal(ui.get('reply-selection').children.length, 1);
  findElement(otherFile, element => element.textContent === '移除').click(); await flush();
  assertPreserved(); assert.equal(ui.get('draft-files').children.length, 1);
  reply.click(); remove.click(); await flush();
  assert.equal(ui.get('reply-selection').children.length, 0); assert.equal(ui.get('draft-files').children.length, 0);
  ui.get('logout').click();
});

test('uncertain cancellation allows a manual retry without an automatic POST, then clears on terminal history', async () => {
  let histories = 0; let cancellations = 0; let committed = false;
  const ui = environment(call => {
    if (call.url === '/api/chat/sessions') return ok({sessions: [description]});
    if (call.url.includes('/history?')) {
      histories++;
      if (histories === 1) return ok(historyPage([historyEvent(1, {
        kind: 'turn.start', turn_id: 'answer', content: [{type: 'text', text: '正在回答'}], reply_to_seqs: [],
      })], 2));
      if (committed) return ok(historyPage([historyEvent(2, {kind: 'turn.cancel', target_turn: {role: 'agent', turn_id: 'answer'}}, 'user')], 3));
      return ok(empty('2', '1'));
    }
    if (call.url.endsWith('/cancellations')) {
      cancellations++;
      if (cancellations === 1) throw new TypeError('offline');
      committed = true; return ok({status: 'committed'}, 201);
    }
    throw new Error(`unexpected request ${call.url}`);
  });
  await load('cancel-retry');
  const article = ui.get('messages').children[0];
  const cancel = findElement(article, element => element.tagName === 'BUTTON' && element.textContent === '停止输出');
  assert.ok(cancel); cancel.click();
  await until(() => cancel.textContent === '重试取消');
  assert.equal(cancel.disabled, false);
  assert.match(ui.get('submission-status').textContent, /未确认.*手动重试/);
  for (let step = 0; step < 3; step++) { ui.runTimer(1000); await flush(); }
  assert.equal(cancellations, 1);
  cancel.click(); cancel.click();
  await until(() => cancel.textContent === '等待取消结果');
  assert.equal(cancellations, 2); assert.equal(cancel.disabled, true);
  ui.runTimer(1000); await until(() => article.textContent.includes('已取消'));
  assert.equal(ui.get('messages').children[0], article);
  assert.equal(cancel.hidden, true); assert.equal(cancel.disabled, false); assert.equal(cancel.textContent, '停止输出');
  assert.equal(ui.get('submission-status').textContent, '输出已取消。');
  assert.deepEqual(ui.calls.filter(call => call.url.endsWith('/cancellations')).map(call => JSON.parse(call.body)), [
    {target_turn_id: 'answer'}, {target_turn_id: 'answer'},
  ]);
  ui.get('logout').click();
});

test('late cancellation responses keep the terminal message and cleared button state', async () => {
  for (const fails of [false, true]) {
    let histories = 0; let completeCancellation;
    const ui = environment(call => {
      if (call.url === '/api/chat/sessions') return ok({sessions: [description]});
      if (call.url.includes('/history?')) {
        if (++histories === 1) return ok(historyPage([historyEvent(1, {
          kind: 'turn.start', turn_id: 'answer', content: [{type: 'text', text: '回答'}], reply_to_seqs: [],
        })], 2));
        return ok(historyPage([historyEvent(2, {kind: 'turn.end', turn_id: 'answer', pre_seq: '1'})], 3));
      }
      if (call.url.endsWith('/cancellations')) return new Promise((resolve, reject) => {
        completeCancellation = () => fails ? reject(new TypeError('offline')) : resolve(ok({status: 'committed'}, 201));
      });
      throw new Error(`unexpected request ${call.url}`);
    });
    await load(`late-cancel-${fails}`);
    const cancel = findElement(ui.get('messages'), element => element.tagName === 'BUTTON' && element.textContent === '停止输出');
    cancel.click(); await until(() => !!completeCancellation);
    ui.runTimer(1000); await until(() => cancel.hidden);
    assert.equal(ui.get('submission-status').textContent, '输出已结束。');
    completeCancellation(); await flush();
    assert.equal(cancel.hidden, true); assert.equal(cancel.disabled, false);
    assert.equal(ui.get('submission-status').textContent, '输出已结束。');
    ui.get('logout').click();
  }
});

test('streaming, reply selection and metadata updates preserve message and text nodes', async () => {
  let histories = 0; let finishMetadata;
  const ui = environment(call => {
    if (call.url === '/api/chat/sessions') return ok({sessions: [description]});
    if (call.url.includes('/history?')) {
      histories++;
      if (histories === 1) return ok(historyPage([
        historyEvent(1, {kind: 'turn.single', turn_id: 'user:1', content: [{type: 'text', text: '问题'}], reply_to_seqs: []}, 'user'),
        historyEvent(2, {kind: 'turn.start', turn_id: 'answer', content: [{type: 'text', text: '开头'}], reply_to_seqs: ['1']}, 'agent', [{object_id: '42'}]),
      ], 3));
      if (histories === 2) return ok(historyPage([
        historyEvent(3, {kind: 'turn.append', turn_id: 'answer', pre_seq: '2', content: [{type: 'text', text: '追加'}]}),
      ], 4));
      return ok(empty('4', '3'));
    }
    if (call.url.includes('/metadata?')) return new Promise(resolve => { finishMetadata = resolve; });
    throw new Error(`unexpected request ${call.url}`);
  });
  await load('incremental'); await until(() => !!finishMetadata);
  const messages = ui.get('messages');
  const [question, answer] = messages.children;
  const questionContent = findElement(question, element => element.className === 'turn-content');
  const answerContent = findElement(answer, element => element.className === 'turn-content');
  const questionText = questionContent.children[0]; const answerText = answerContent.children[0];
  const attachment = findElement(answer, element => element.className === 'attachment');
  const reply = findElement(question, element => element.tagName === 'BUTTON' && element.textContent === '回复');
  const assertPreserved = () => {
    assert.equal(messages.children[0], question); assert.equal(messages.children[1], answer);
    assert.equal(findElement(question, element => element.className === 'turn-content'), questionContent);
    assert.equal(findElement(answer, element => element.className === 'turn-content'), answerContent);
    assert.equal(questionContent.children[0], questionText); assert.equal(answerContent.children[0], answerText);
    assert.equal(findElement(answer, element => element.className === 'attachment'), attachment);
  };
  reply.click(); await flush(); assert.equal(reply.textContent, '已选回复'); assertPreserved();
  reply.click(); await flush(); assert.equal(reply.textContent, '回复'); assertPreserved();
  finishMetadata(ok({object_id: '42', name: '报告.txt', type: 'text/plain', description: '', nbytes: 15}));
  await until(() => attachment.textContent.includes('报告.txt')); assertPreserved();
  messages.scrollTop = 17; messages.scrollHeight = 1000;
  ui.runTimer(1000); await until(() => answerText.textContent === '开头追加'); assertPreserved();
  assert.equal(questionText.textContent, '问题'); assert.equal(messages.scrollTop, 17);
  assert.equal(ui.calls.filter(call => call.url.includes('/metadata?')).length, 1);
  ui.get('logout').click();
});

test('entry module queries uncertain sends, never repeats a POST, then checks a fresh fixed history boundary', async () => {
  let queries = 0; let histories = 0;
  const ui = environment(call => {
    if (call.url === '/api/chat/sessions') return ok({sessions: [description]});
    if (call.url.includes('/history?')) { histories++; return ok(empty(histories === 1 ? '1' : '2', histories === 1 ? '0' : '1')); }
    if (call.url.endsWith('/submissions')) return ok({start: '9007199254740993', end: '9007199254741092'}, 201);
    if (call.url.endsWith('/turns')) throw new TypeError('connection interrupted');
    if (call.url.includes('/submissions/')) {
      queries++;
      if (queries === 1) return ok({submission_id: '9007199254740993', status: 'processing'}, 202);
      if (queries === 2) return err(404, 'submission_not_observed');
      return err(409, 'submission_out_of_range');
    }
    throw new Error(`unexpected request ${call.url}`);
  });
  await load('unknown');
  assert.equal(ui.get('workspace').hidden, false); assert.match(ui.document.cookie, /Path=\/api\/chat/); assert.match(ui.document.cookie, /Secure/);
  typeAndSend(ui, '保留原文\n<script>这不是 HTML</script>');
  await until(() => ui.get('submission-status').textContent.includes('发送结果未确认'));
  assert.equal(ui.get('message-input').disabled, true);
  ui.runTimer(0); await until(() => queries === 1); await flush();
  // Both polling loops wait one second. Run each until the query reaches 404.
  for (let step = 0; queries < 2 && step < 4; step++) { ui.runTimer(1000); await flush(); }
  assert.equal(queries, 2); assert.equal(ui.get('message-input').disabled, true);
  for (let step = 0; queries < 3 && step < 4; step++) { ui.runTimer(1000); await flush(); }
  await until(() => ui.get('submission-status').textContent.includes('没有发送成功'));
  assert.equal(ui.calls.filter(call => call.url.endsWith('/turns')).length, 1);
  const body = JSON.parse(ui.calls.find(call => call.url.endsWith('/turns')).body);
  assert.equal(body.submission_id, '9007199254740993'); assert.equal(body.text, '保留原文\n<script>这不是 HTML</script>');
  assert.equal(ui.get('message-input').value, body.text); assert.equal(ui.get('message-input').disabled, false);
  assert.ok(histories >= 2);
  assert.ok(ui.calls.every(call => call.cache === 'no-store' && call.credentials === 'same-origin'));
  ui.get('logout').click(); assert.equal(ui.timers.size, 0);
});

test('only an explicit send range refusal allocates a new id and republishes; success inserts no optimistic turn', async () => {
  let allocations = 0; let sends = 0;
  const ui = environment(call => {
    if (call.url === '/api/chat/sessions') return ok({sessions: [description]});
    if (call.url.includes('/history?')) return ok(empty());
    if (call.url.endsWith('/submissions')) {
      allocations++; return ok(allocations === 1 ? {start: '1', end: '100'} : {start: '10001', end: '10100'}, 201);
    }
    if (call.url.endsWith('/turns')) {
      sends++;
      return sends === 1 ? err(409, 'submission_out_of_range') : ok({status: 'committed', submission_id: JSON.parse(call.body).submission_id}, 201);
    }
    throw new Error(`unexpected request ${call.url}`);
  });
  await load('range'); typeAndSend(ui, '第一条');
  await until(() => ui.get('submission-status').textContent === '消息已发送');
  assert.equal(allocations, 2); assert.equal(sends, 2);
  const requests = ui.calls.filter(call => call.url.endsWith('/turns')).map(call => JSON.parse(call.body));
  assert.deepEqual(requests.map(body => body.submission_id), ['1', '10001']);
  assert.equal(ui.get('messages').children.some(child => child.tagName === 'ARTICLE'), false);
  typeAndSend(ui, '第二条'); await until(() => sends === 3); await flush();
  assert.equal(allocations, 2); assert.equal(JSON.parse(ui.calls.filter(call => call.url.endsWith('/turns')).at(-1).body).submission_id, '10002');
  ui.get('logout').click();
});

test('upload uses one file field and attachment-unavailable preserves the local file for manual reupload', async () => {
  let uploads = 0;
  const ui = environment(call => {
    if (call.url === '/api/chat/sessions') return ok({sessions: [description]});
    if (call.url.includes('/history?')) return ok(empty());
    if (call.url.endsWith('/attachments')) {
      uploads++; assert.deepEqual([...call.body.keys()], ['file']);
      assert.equal(call.body.get('file').name, '数据.txt');
      return ok({object_id: String(41 + uploads), name: '数据.txt', type: 'text/plain', description: '', nbytes: 4}, 201);
    }
    if (call.url.endsWith('/submissions')) return ok({start: '1', end: '100'}, 201);
    if (call.url.endsWith('/turns')) return err(409, 'attachment_unavailable');
    throw new Error(`unexpected request ${call.url}`);
  });
  await load('upload');
  ui.get('file-input').files = [new File(['data'], '数据.txt', {type: 'text/plain'})];
  ui.get('file-input').dispatch('change'); await until(() => uploads === 1); await flush();
  assert.equal(ui.get('send').disabled, false);
  typeAndSend(ui, '附件说明');
  await until(() => ui.get('submission-status').textContent.includes('重新上传'));
  assert.equal(ui.get('message-input').value, '附件说明'); assert.equal(uploads, 1); assert.equal(ui.get('send').disabled, true);
  const row = ui.get('draft-files').children[0];
  const retry = row.children.find(child => child.textContent === '重新上传'); assert.ok(retry); retry.click();
  await until(() => uploads === 2); await flush();
  assert.equal(ui.get('send').disabled, false);
  ui.get('logout').click();
});

test('creating again after a lost response reuses its stored ULID and logout discards late history', async () => {
  let creations = 0; let lateHistory;
  const ui = environment(call => {
    if (call.url === '/api/chat/sessions' && call.method === 'GET') return ok({sessions: []});
    if (call.url === '/api/chat/sessions' && call.method === 'POST') {
      creations++;
      if (creations === 1) throw new TypeError('lost create response');
      return ok(description);
    }
    if (call.url.includes('/history?')) return new Promise(resolve => { lateHistory = resolve; });
    throw new Error(`unexpected request ${call.url}`);
  });
  await load('creation'); ui.get('new-session').click();
  await until(() => ui.get('new-session').textContent.includes('重试创建'));
  const requestId = JSON.parse(ui.calls.find(call => call.method === 'POST').body).create_request_id;
  assert.match(requestId, /^[0-7][0-9A-HJKMNP-TV-Z]{25}$/);
  assert.equal(ui.storage.get('openevent.pending-create'), requestId);
  ui.get('new-session').click(); await until(() => !!lateHistory);
  assert.deepEqual(ui.calls.filter(call => call.method === 'POST').map(call => JSON.parse(call.body).create_request_id), [requestId, requestId]);
  ui.get('logout').click(); lateHistory(ok(empty('100', '99'))); await flush();
  assert.equal(ui.get('workspace').hidden, true); assert.equal(ui.get('messages').children.length, 0);
  assert.equal(ui.timers.size, 0); assert.equal(ui.storage.size, 0);
});

test('an unauthenticated API response returns to the token page and sends no further requests', async () => {
  const ui = environment(() => err(401, 'unauthenticated'));
  await load('auth'); await flush();
  assert.equal(ui.get('workspace').hidden, true); assert.equal(ui.get('login').hidden, false);
  assert.equal(ui.calls.length, 1); assert.equal(ui.timers.size, 0); assert.match(ui.get('login-error').textContent, /访问口令/);
});

test('a proxy 503 without failure metadata retries history from the unchanged cursor', async () => {
  let histories = 0;
  const ui = environment(call => {
    if (call.url === '/api/chat/sessions') return ok({sessions: [description]});
    if (call.url.includes('/history?')) {
      histories++;
      return histories === 1 ? new Response('temporarily unavailable', {status: 503}) : ok(empty());
    }
    throw new Error(`unexpected request ${call.url}`);
  });
  await load('proxy'); await until(() => ui.get('sync-status').textContent === '正在重连');
  ui.runTimer(1000); await until(() => histories === 2); await flush();
  const historyRequests = ui.calls.filter(call => call.url.includes('/history?'));
  assert.equal(historyRequests[0].url, historyRequests[1].url);
  assert.equal(ui.get('sync-status').textContent, '已连接');
  ui.get('logout').click();
});

test('temporary service, network and timeout failures retry history without changing its cursor', async () => {
  for (const failure of ['external_unavailable', 'network', 'timeout']) {
    let histories = 0;
    const ui = environment(call => {
      if (call.url === '/api/chat/sessions') return ok({sessions: [description]});
      if (call.url.includes('/history?')) {
        if (++histories > 1) return ok(empty());
        if (failure === 'network') throw new TypeError('network unavailable');
        if (failure === 'timeout') return new Promise((resolve, reject) => {
          call.signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), {once: true});
        });
        return ok({error: {code: 'server_unavailable', message: 'external service temporarily unavailable',
          failure: {category: failure, retryable: true, detail: 'Fetch is temporarily unavailable'}}}, 503);
      }
      throw new Error(`unexpected request ${call.url}`);
    });
    await load(`transient-${failure}`);
    if (failure === 'timeout') ui.runTimer(30000);
    await until(() => ui.get('sync-status').textContent === '正在重连');
    if (failure === 'external_unavailable') assert.match(ui.get('message-error').textContent, /Fetch is temporarily unavailable/);
    ui.runTimer(1000); await until(() => histories === 2); await flush();
    const historyRequests = ui.calls.filter(call => call.url.includes('/history?'));
    assert.equal(historyRequests[0].url, historyRequests[1].url);
    assert.equal(ui.get('sync-status').textContent, '已连接');
    ui.get('logout').click();
  }
});

test('explicit failure categories stop history retries, preserve records and display the failure detail', async () => {
  for (const category of ['protocol', 'contract', 'authentication', 'permission', 'not_found', 'lifecycle']) {
    let histories = 0;
    const detail = `Fetch ${category}: diagnostic <not HTML>`;
    const ui = environment(call => {
      if (call.url === '/api/chat/sessions') return ok({sessions: [description]});
      if (call.url.includes('/history?')) {
        if (++histories === 1) return ok(historyPage([historyEvent(1, {
          kind: 'turn.single', turn_id: 'saved', content: [{type: 'text', text: '已经读取的内容'}], reply_to_seqs: [],
        })], 2));
        return ok({error: {code: 'server_unavailable', message: 'chat server is stopping',
          failure: {category, retryable: false, detail}}}, 503);
      }
      throw new Error(`unexpected request ${call.url}`);
    });
    await load(`history-fatal-${category}`);
    const article = ui.get('messages').children[0];
    ui.runTimer(1000); await until(() => ui.get('sync-status').textContent === '读取已停止');
    await flush();
    assert.equal(histories, 2); assert.equal(ui.timers.size, 0);
    assert.equal(ui.get('messages').children[0], article); assert.match(article.textContent, /已经读取的内容/);
    assert.ok(ui.calls.filter(call => call.url.includes('/history?'))[1].url.includes('fetch_seq=2'));
    assert.ok(ui.get('message-error').textContent.startsWith(`chat server is stopping：${detail}`));
    assert.ok(findElement(ui.get('message-error'), element => element.textContent === '重新读取'));
    ui.get('logout').click();
  }
});

test('explicit query failures stop automatic result queries without repeating the send or discarding its draft', async () => {
  for (const category of ['protocol', 'contract', 'authentication', 'permission', 'not_found', 'lifecycle']) {
    let queries = 0;
    const detail = `submission query ${category}`;
    const ui = environment(call => {
      if (call.url === '/api/chat/sessions') return ok({sessions: [description]});
      if (call.url.includes('/history?')) return ok(empty());
      if (call.url.endsWith('/submissions')) return ok({start: '1', end: '100'}, 201);
      if (call.url.endsWith('/turns')) throw new TypeError('lost send response');
      if (call.url.includes('/submissions/')) {
        queries++;
        return ok({error: {code: 'server_unavailable', message: 'chat server is stopping',
          failure: {category, retryable: false, detail}}}, 503);
      }
      throw new Error(`unexpected request ${call.url}`);
    });
    await load(`query-fatal-${category}`); typeAndSend(ui, '结果未确认的原始草稿');
    await until(() => ui.get('submission-status').textContent.includes('发送结果未确认'));
    ui.runTimer(0); await until(() => queries === 1); await flush();
    assert.ok(ui.get('submission-status').textContent.startsWith(`chat server is stopping：${detail}`));
    assert.ok(findElement(ui.get('submission-status'), element => element.textContent === '重新查询'));
    for (let step = 0; step < 3; step++) { ui.runTimer(1000); await flush(); }
    assert.equal(queries, 1);
    assert.equal(ui.calls.filter(call => call.url.endsWith('/turns')).length, 1);
    assert.equal(ui.get('message-input').value, '结果未确认的原始草稿');
    assert.equal(ui.get('message-input').disabled, true);
    ui.get('logout').click();
  }
});

test('hiding a page during a send cannot restart queries until a back-forward-cache restore', async () => {
  let rejectSend;
  const ui = environment(call => {
    if (call.url === '/api/chat/sessions') return ok({sessions: [description]});
    if (call.url.includes('/history?')) return ok(empty());
    if (call.url.endsWith('/submissions')) return ok({start: '1', end: '100'}, 201);
    if (call.url.endsWith('/turns')) return new Promise((resolve, reject) => { rejectSend = reject; });
    if (call.url.includes('/submissions/')) return ok({submission_id: '1', status: 'processing'}, 202);
    throw new Error(`unexpected request ${call.url}`);
  });
  await load('pagehide'); typeAndSend(ui, '离开页面前的消息'); await until(() => !!rejectSend);
  window.dispatch('pagehide'); rejectSend(new DOMException('Aborted', 'AbortError')); await flush();
  assert.equal(ui.timers.size, 0);
  assert.equal(ui.calls.some(call => call.url.includes('/submissions/')), false);
  window.dispatch('pageshow', {persisted: true}); await flush();
  ui.runTimer(0); await until(() => ui.calls.some(call => call.url.includes('/submissions/')));
  assert.equal(ui.calls.filter(call => call.url.endsWith('/turns')).length, 1);
  ui.get('logout').click();
});
