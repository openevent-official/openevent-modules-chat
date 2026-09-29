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
    if (call.url.endsWith('/submissions')) return ok({start: '1', end: '100'}, 201);
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
      if (call.url.endsWith('/submissions')) return ok({start: '1', end: '100'}, 201);
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
    if (call.url.endsWith('/submissions')) return ok({start: '1', end: '100'}, 201);
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
    if (call.url.endsWith('/turns')) return new Promise(resolve => { finishAllocation = resolve; });
    if (call.url.endsWith('/submissions')) return ok({start: '101', end: '200'}, 201);
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
  otherReply.click(); await flush(); assertPreserved(); assert.equal(ui.get('reply-selection').children.length, 1);
  findElement(otherFile, element => element.textContent === '移除').click(); await flush();
  assertPreserved(); assert.equal(ui.get('draft-files').children.length, 1);
  typeAndSend(ui, '暂存草稿'); await until(() => !!finishAllocation); await flush();
  assert.equal(reply.disabled, true); assert.equal(remove.disabled, true); assertPreserved();
  for (const id of ['message-input', 'choose-files', 'file-input', 'send']) assert.equal(ui.get(id).disabled, true);
  assert.equal(retry.disabled, true);
  finishAllocation(err(400, 'invalid_request'));
  await until(() => !ui.get('send').disabled);
  assert.equal(ui.get('message-input').value, '暂存草稿');
  assert.equal(reply.disabled, false); assert.equal(remove.disabled, false);
  assertPreserved(); const previous = finishAllocation;
  ui.get('composer').dispatch('submit');
  await until(() => finishAllocation !== previous);
  const bodies = ui.calls.filter(call => call.url.endsWith('/turns')).map(call => JSON.parse(call.body));
  assert.equal(bodies[1].text, '暂存草稿'); assert.notEqual(bodies[0].submission_id, bodies[1].submission_id);
  finishAllocation(ok({status: 'committed', submission_id: bodies[1].submission_id,
    turn_ref: {role: 'user', turn_id: `user:${bodies[1].submission_id}`}}, 201));
  await until(() => !ui.get('send').disabled);
  assert.equal(ui.get('message-input').value, '');
  assert.equal(ui.get('reply-selection').children.length, 0); assert.equal(ui.get('draft-files').children.length, 0);
  ui.get('logout').click();
});

test('a definitively rejected send unlocks its original draft for editing and a new-id send', async () => {
  let rejectFirst; let sends = 0;
  const ui = environment(call => {
    if (call.url === '/api/chat/sessions') return ok({sessions: [description]});
    if (call.url.includes('/history?')) return ok(empty());
    if (call.url.endsWith('/submissions')) return ok({start: '1', end: '100'}, 201);
    if (call.url.endsWith('/turns')) {
      if (++sends === 1) return new Promise(resolve => { rejectFirst = resolve; });
      const id = JSON.parse(call.body).submission_id;
      return ok({submission_id: id, status: 'committed', turn_ref: {role: 'user', turn_id: `user:${id}`}}, 201);
    }
    throw new Error(`unexpected request ${call.url}`);
  });
  await load('edit-rejected'); typeAndSend(ui, '被拒绝的原文'); await until(() => !!rejectFirst);
  assert.equal(ui.get('message-input').disabled, true);
  rejectFirst(err(413, 'request_too_large'));
  await until(() => !ui.get('send').disabled);
  assert.equal(ui.get('message-input').value, '被拒绝的原文');
  assert.equal(ui.get('message-input').disabled, false);
  assert.equal(ui.get('file-input').disabled, false);
  assert.equal(sends, 1);
  typeAndSend(ui, '已修改的新草稿'); await until(() => ui.get('submission-status').textContent === '消息已发送');
  const bodies = ui.calls.filter(call => call.url.endsWith('/turns')).map(call => JSON.parse(call.body));
  assert.deepEqual(bodies.map(body => [body.submission_id, body.text]), [['1', '被拒绝的原文'], ['2', '已修改的新草稿']]);
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
    if (call.url.endsWith('/submissions')) return ok({start: '1', end: '100'}, 201);
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
      if (call.url.endsWith('/submissions')) return ok({start: '1', end: '100'}, 201);
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
    if (call.url.endsWith('/submissions')) return ok({start: '1', end: '100'}, 201);
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

test('uncertain sends retry the same POST, then require a fresh history boundary before a new id', async () => {
  let sends = 0; let histories = 0; let allocations = 0;
  const ui = environment(call => {
    if (call.url === '/api/chat/sessions') return ok({sessions: [description]});
    if (call.url.includes('/history?')) { histories++; return ok(empty(histories === 1 ? '1' : '2', histories === 1 ? '0' : '1')); }
    if (call.url.endsWith('/submissions')) {
      const start = 9007199254740993n + BigInt(allocations++) * 10000n;
      return ok({start: String(start), end: String(start + 99n)}, 201);
    }
    if (call.url.endsWith('/turns')) {
      sends++;
      if (sends === 1) throw new TypeError('connection interrupted');
      if (sends === 2) return ok({submission_id: '9007199254740993', status: 'processing'}, 202);
      if (sends === 3) return err(409, 'submission_out_of_range');
      const id = JSON.parse(call.body).submission_id;
      return ok({submission_id: id, status: 'committed', turn_ref: {role: 'user', turn_id: `user:${id}`}}, 201);
    }
    throw new Error(`unexpected request ${call.url}`);
  });
  await load('unknown');
  typeAndSend(ui, '保留原文\n<script>这不是 HTML</script>');
  await until(() => ui.get('submission-status').textContent.includes('发送结果未确认'));
  assert.equal(ui.get('send').disabled, true);
  assert.equal(ui.get('message-input').disabled, true);
  for (let step = 0; sends < 3 && step < 8; step++) { ui.runTimer(1000); await flush(); }
  await until(() => ui.get('submission-status').textContent.includes('没有发送成功'));
  const bodies = ui.calls.filter(call => call.url.endsWith('/turns')).map(call => JSON.parse(call.body));
  assert.equal(bodies.length, 3); assert.deepEqual(bodies[0], bodies[1]); assert.deepEqual(bodies[1], bodies[2]);
  assert.equal(bodies[0].submission_id, '9007199254740993');
  assert.equal(bodies[0].text, '保留原文\n<script>这不是 HTML</script>');
  assert.equal(ui.get('message-input').value, bodies[0].text);
  assert.equal(ui.get('message-input').disabled, false);
  assert.equal(ui.calls.some(call => call.url.includes('/submissions/')), false);
  assert.ok(histories >= 2); assert.equal(allocations, 2);
  assert.ok(ui.calls.every(call => call.cache === 'no-store' && call.credentials === 'same-origin'));
  for (let index = 0; index < 3; index++) { ui.runTimer(1000); await flush(); }
  assert.equal(sends, 3);
  ui.get('composer').dispatch('submit');
  await until(() => ui.get('submission-status').textContent === '消息已发送');
  const resent = JSON.parse(ui.calls.filter(call => call.url.endsWith('/turns')).at(-1).body);
  assert.equal(resent.submission_id, '9007199254750993'); assert.equal(resent.text, bodies[0].text);
  assert.equal(ui.get('message-input').value, '');
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
      return sends === 1 ? err(409, 'submission_out_of_range') : ok({status: 'committed', submission_id: JSON.parse(call.body).submission_id, turn_ref: {role: 'user', turn_id: `user:${JSON.parse(call.body).submission_id}`}}, 201);
    }
    if (call.url.endsWith('/submissions')) return ok({start: '1', end: '100'}, 201);
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

test('attachment-unavailable unlocks the retained draft for manual reupload and a new-id send', async () => {
  let uploads = 0; let sends = 0;
  const ui = environment(call => {
    if (call.url === '/api/chat/sessions') return ok({sessions: [description]});
    if (call.url.includes('/history?')) return ok(empty());
    if (call.url.endsWith('/attachments')) {
      uploads++; assert.deepEqual([...call.body.keys()], ['file']);
      assert.equal(call.body.get('file').name, '数据.txt');
      return ok({object_id: String(41 + uploads), name: '数据.txt', type: 'text/plain', description: '', nbytes: 4}, 201);
    }
    if (call.url.endsWith('/submissions')) return ok({start: '1', end: '100'}, 201);
    if (call.url.endsWith('/turns')) {
      if (++sends === 1) return err(409, 'attachment_unavailable');
      const id = JSON.parse(call.body).submission_id;
      return ok({submission_id: id, status: 'committed', turn_ref: {role: 'user', turn_id: `user:${id}`}}, 201);
    }
    throw new Error(`unexpected request ${call.url}`);
  });
  await load('upload');
  ui.get('file-input').files = [new File(['data'], '数据.txt', {type: 'text/plain'})];
  ui.get('file-input').dispatch('change'); await until(() => uploads === 1); await flush();
  assert.equal(ui.get('send').disabled, false);
  typeAndSend(ui, '附件说明');
  await until(() => ui.get('submission-status').textContent.includes('重新上传'));
  assert.equal(ui.get('message-input').value, '附件说明'); assert.equal(uploads, 1); assert.equal(ui.get('send').disabled, true);
  assert.equal(ui.get('message-input').disabled, false);
  ui.get('message-input').value = '附件说明（重新上传）'; ui.get('message-input').dispatch('input');
  const row = ui.get('draft-files').children[0];
  const retry = row.children.find(child => child.textContent === '重新上传'); assert.ok(retry); retry.click();
  await until(() => uploads === 2); await flush();
  assert.equal(ui.get('send').disabled, false); ui.get('composer').dispatch('submit');
  await until(() => ui.get('submission-status').textContent === '消息已发送');
  const bodies = ui.calls.filter(call => call.url.endsWith('/turns')).map(call => JSON.parse(call.body));
  assert.equal(bodies[0].submission_id, '1'); assert.equal(bodies[1].submission_id, '2');
  assert.deepEqual(bodies[0].attachments, ['42']); assert.deepEqual(bodies[1].attachments, ['43']);
  assert.equal(bodies[1].text, '附件说明（重新上传）'); assert.equal(ui.get('message-input').value, '');
  ui.get('logout').click();
});

test('a lost attachment refusal after restart still unlocks the draft for reupload and a new-id send', async () => {
  let histories = 0; let allocations = 0; let uploads = 0; let sends = 0;
  const ui = environment(call => {
    if (call.url === '/api/chat/sessions') return ok({sessions: [description]});
    if (call.url.includes('/history?')) {
      if (++histories === 2) throw new TypeError('old server disconnected');
      return ok(empty());
    }
    if (call.url.endsWith('/submissions')) {
      return ok(++allocations === 1 ? {start: '1', end: '100'} : {start: '10001', end: '10100'}, 201);
    }
    if (call.url.endsWith('/attachments')) {
      uploads++; assert.equal(call.body.get('file').name, '保留.txt');
      return ok({object_id: String(41 + uploads), name: '保留.txt', type: 'text/plain', description: '', nbytes: 4}, 201);
    }
    if (call.url.endsWith('/turns')) {
      if (++sends === 1) throw new TypeError('attachment refusal response was lost');
      if (sends === 2) return err(409, 'attachment_unavailable');
      const id = JSON.parse(call.body).submission_id;
      return ok({submission_id: id, status: 'committed', turn_ref: {role: 'user', turn_id: `user:${id}`}}, 201);
    }
    throw new Error(`unexpected request ${call.url}`);
  });
  await load('lost-attachment-refusal');
  ui.get('file-input').files = [new File(['data'], '保留.txt', {type: 'text/plain'})];
  ui.get('file-input').dispatch('change'); await until(() => ui.get('draft-files').textContent.includes('已上传'));
  ui.runTimer(1000); await until(() => ui.get('sync-status').textContent === '正在重连');
  ui.runTimer(1000); await until(() => allocations === 2 && !ui.get('send').disabled);
  typeAndSend(ui, '重启后发送已上传附件');
  await until(() => ui.get('submission-status').textContent.includes('发送结果未确认'));
  assert.equal(ui.get('message-input').disabled, true);
  for (let step = 0; sends < 2 && step < 6; step++) { ui.runTimer(1000); await flush(); }
  await until(() => ui.get('submission-status').textContent.includes('重新上传'));
  assert.equal(ui.get('message-input').disabled, false);
  assert.equal(ui.get('message-input').value, '重启后发送已上传附件');
  assert.equal(ui.get('send').disabled, true); assert.equal(ui.get('draft-files').children.length, 1);
  const row = ui.get('draft-files').children[0];
  const retry = findElement(row, element => element.textContent === '重新上传');
  assert.equal(retry.hidden, false); assert.equal(retry.disabled, false);
  assert.equal(findElement(row, element => element.textContent === '移除').disabled, false);
  for (let step = 0; step < 2; step++) { ui.runTimer(1000); await flush(); }
  assert.equal(sends, 2); assert.equal(uploads, 1);
  retry.click(); await until(() => uploads === 2 && !ui.get('send').disabled);
  ui.get('composer').dispatch('submit'); await until(() => ui.get('submission-status').textContent === '消息已发送');
  const bodies = ui.calls.filter(call => call.url.endsWith('/turns')).map(call => JSON.parse(call.body));
  assert.deepEqual(bodies[0], bodies[1]);
  assert.deepEqual(bodies.map(body => body.submission_id), ['10001', '10001', '10002']);
  assert.deepEqual(bodies.map(body => body.attachments), [['42'], ['42'], ['43']]);
  assert.equal(bodies[2].text, bodies[0].text);
  assert.equal(ui.get('message-input').value, ''); assert.equal(ui.get('draft-files').children.length, 0);
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
    if (call.url.endsWith('/submissions')) return ok({start: '1', end: '100'}, 201);
    throw new Error(`unexpected request ${call.url}`);
  });
  await load('creation'); ui.get('new-session').click();
  await until(() => ui.get('new-session').textContent.includes('重试创建'));
  const requestId = JSON.parse(ui.calls.find(call => call.method === 'POST').body).create_request_id;
  assert.match(requestId, /^[0-7][0-9A-HJKMNP-TV-Z]{25}$/);
  assert.equal(ui.storage.get('openevent.pending-create'), requestId);
  ui.get('new-session').click(); await until(() => !!lateHistory);
  assert.deepEqual(ui.calls.filter(call => call.method === 'POST' && call.url === '/api/chat/sessions').map(call => JSON.parse(call.body).create_request_id), [requestId, requestId]);
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
    if (call.url.endsWith('/submissions')) return ok({start: '1', end: '100'}, 201);
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
          failure: {category: failure, detail: 'Fetch is temporarily unavailable'}}}, 503);
      }
      if (call.url.endsWith('/submissions')) return ok({start: '1', end: '100'}, 201);
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
          failure: {category, detail}}}, 503);
      }
      if (call.url.endsWith('/submissions')) return ok({start: '1', end: '100'}, 201);
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

test('explicit send failures stop automatic same-id requests and preserve the uncertain draft', async () => {
  for (const category of ['protocol', 'contract', 'authentication', 'permission', 'not_found', 'lifecycle']) {
    let sends = 0;
    const detail = `submission ${category}`;
    const ui = environment(call => {
      if (call.url === '/api/chat/sessions') return ok({sessions: [description]});
      if (call.url.includes('/history?')) return ok(empty());
      if (call.url.endsWith('/submissions')) return ok({start: '1', end: '100'}, 201);
      if (call.url.endsWith('/turns')) {
        if (++sends === 1) throw new TypeError('lost send response');
        return ok({error: {code: 'server_unavailable', message: 'chat server is stopping',
          failure: {category, detail}}}, 503);
      }
      throw new Error(`unexpected request ${call.url}`);
    });
    await load(`send-fatal-${category}`); typeAndSend(ui, '结果未确认的原始草稿');
    await until(() => ui.get('submission-status').textContent.includes('发送结果未确认'));
    for (let step = 0; sends < 2 && step < 4; step++) { ui.runTimer(1000); await flush(); }
    assert.ok(ui.get('submission-status').textContent.startsWith(`chat server is stopping：${detail}`));
    for (let step = 0; step < 3; step++) { ui.runTimer(1000); await flush(); }
    assert.equal(sends, 2); assert.equal(ui.get('message-input').value, '结果未确认的原始草稿');
    assert.equal(ui.get('send').disabled, true);
    ui.get('logout').click();
  }
});

test('a first fatal or malformed send response remains uncertain until fresh old-batch history confirms it', async () => {
  for (const failure of ['contract', 'invalid-json', 'null-json']) {
    let sends = 0; let histories = 0; let allocations = 0; let finishHistory;
    const ui = environment(call => {
      if (call.url === '/api/chat/sessions') return ok({sessions: [description]});
      if (call.url.includes('/history?')) {
        if (++histories === 1) return ok(empty());
        return new Promise(resolve => { finishHistory = resolve; });
      }
      if (call.url.endsWith('/submissions')) {
        return ok(++allocations === 1 ? {start: '1', end: '100'} : {start: '10001', end: '10100'}, 201);
      }
      if (call.url.endsWith('/turns')) {
        if (++sends === 1) {
          if (failure === 'invalid-json') return new Response('<html>proxy failure</html>', {status: 200});
          if (failure === 'null-json') return ok(null);
          return ok({error: {code: 'server_unavailable', message: 'publication outcome unknown', failure: {category: 'contract'}}}, 503);
        }
        return err(409, 'submission_out_of_range');
      }
      throw new Error(`unexpected request ${call.url}`);
    });
    await load(`first-send-${failure}`); typeAndSend(ui, '原消息');
    await until(() => !!findElement(ui.get('submission-status'), element => element.textContent === '重试确认'));
    assert.equal(ui.get('message-input').disabled, true); assert.equal(sends, 1);
    findElement(ui.get('submission-status'), element => element.textContent === '重试确认').click();
    await until(() => !!finishHistory && allocations === 2); await flush();
    assert.equal(sends, 2); assert.equal(ui.get('message-input').disabled, true);
    assert.equal(ui.get('message-input').value, '原消息');
    finishHistory(ok(historyPage([historyEvent(1, {
      kind: 'turn.single', turn_id: 'user:1', content: [{type: 'text', text: '原消息'}], reply_to_seqs: [],
    }, 'user')], 2)));
    await until(() => ui.get('submission-status').textContent === '消息已发送');
    assert.equal(ui.get('message-input').value, ''); assert.equal(ui.get('message-input').disabled, false);
    assert.deepEqual(ui.calls.filter(call => call.url.endsWith('/turns')).map(call => JSON.parse(call.body).submission_id), ['1', '1']);
    ui.get('logout').click();
  }
});

test('hiding a page pauses the same-id POST until a back-forward-cache restore', async () => {
  let rejectSend; let sends = 0;
  const ui = environment(call => {
    if (call.url === '/api/chat/sessions') return ok({sessions: [description]});
    if (call.url.includes('/history?')) return ok(empty());
    if (call.url.endsWith('/submissions')) return ok({start: '1', end: '100'}, 201);
    if (call.url.endsWith('/turns')) {
      if (++sends === 1) return new Promise((resolve, reject) => { rejectSend = reject; });
      return ok({submission_id: '1', status: 'processing'}, 202);
    }
    throw new Error(`unexpected request ${call.url}`);
  });
  await load('pagehide'); typeAndSend(ui, '离开页面前的消息'); await until(() => !!rejectSend);
  window.dispatch('pagehide'); rejectSend(new DOMException('Aborted', 'AbortError')); await flush();
  assert.equal(ui.timers.size, 0); assert.equal(sends, 1);
  window.dispatch('pageshow', {persisted: true}); await until(() => sends === 2);
  const bodies = ui.calls.filter(call => call.url.endsWith('/turns')).map(call => JSON.parse(call.body));
  assert.deepEqual(bodies[0], bodies[1]);
  ui.get('logout').click();
});

test('initialization waits 120 seconds, ignores a late allocation and only enables the current retry', async () => {
  const allocations = [];
  const ui = environment(call => {
    if (call.url === '/api/chat/sessions') return ok({sessions: [description]});
    if (call.url.includes('/history?')) return ok(empty());
    if (call.url.endsWith('/submissions')) return new Promise(resolve => allocations.push(resolve));
    if (call.url.endsWith('/turns')) {
      const body = JSON.parse(call.body);
      return ok({submission_id: body.submission_id, status: 'committed', turn_ref: {role: 'user', turn_id: `user:${body.submission_id}`}}, 201);
    }
    throw new Error(`unexpected request ${call.url}`);
  });
  await load('initialization-timeout');
  assert.equal(ui.get('message-input').disabled, true); assert.equal(ui.get('choose-files').disabled, true);
  assert.equal(ui.get('send').disabled, true); assert.equal(allocations.length, 1);
  ui.runTimer(120000); await flush();
  assert.match(ui.get('submission-status').textContent, /正在重试/);
  for (let index = 0; allocations.length < 2 && index < 4; index++) { ui.runTimer(1000); await flush(); }
  allocations[0](ok({start: '1', end: '100'}, 201)); await flush();
  assert.equal(ui.get('message-input').disabled, true);
  allocations[1](ok({start: '101', end: '200'}, 201)); await until(() => !ui.get('send').disabled);
  typeAndSend(ui, '初始化后的消息'); await until(() => ui.get('submission-status').textContent === '消息已发送');
  assert.equal(JSON.parse(ui.calls.find(call => call.url.endsWith('/turns')).body).submission_id, '101');
  assert.ok(ui.calls.filter(call => call.url.endsWith('/submissions')).every(call => JSON.parse(call.body).count === '100'));
  ui.get('logout').click(); assert.equal(ui.timers.size, 0);
});

test('recovery discards an earlier allocation and only the new preparation response enables operations', async () => {
  let histories = 0; const allocations = [];
  const ui = environment(call => {
    if (call.url === '/api/chat/sessions') return ok({sessions: [description]});
    if (call.url.includes('/history?')) {
      if (++histories === 1) throw new TypeError('service disconnected');
      return ok(empty());
    }
    if (call.url.endsWith('/submissions')) return new Promise(resolve => allocations.push({resolve, signal: call.signal}));
    throw new Error(`unexpected request ${call.url}`);
  });
  await load('reprepare-stale-allocation'); await until(() => ui.get('sync-status').textContent === '正在重连');
  assert.equal(allocations.length, 1);
  ui.runTimer(1000); await until(() => allocations.length === 2);
  assert.equal(allocations[0].signal.aborted, true);
  allocations[0].resolve(ok({start: '1', end: '100'}, 201)); await flush();
  for (const id of ['message-input', 'choose-files', 'file-input', 'send']) assert.equal(ui.get(id).disabled, true);
  allocations[1].resolve(ok({start: '10001', end: '10100'}, 201)); await until(() => !ui.get('send').disabled);
  assert.equal(ui.get('message-input').disabled, false);
  ui.get('logout').click();
});

test('HTTP deadlines cover incomplete bodies and finish even when abort is ignored', async () => {
  let finishBody;
  const ui = environment(call => {
    if (call.url === '/api/chat/sessions') return ok({sessions: []});
    if (call.url === '/test-body') return {status: 200, ok: true, text: () => new Promise(resolve => { finishBody = resolve; })};
    if (call.url.endsWith('/submissions')) return new Promise(() => {});
    throw new Error(`unexpected request ${call.url}`);
  });
  const {request} = await load('body-deadline');
  const body = request('/test-body'); const bodyRejected = assert.rejects(body, error => error.code === 'timeout');
  await until(() => !!finishBody); ui.runTimer(30000); await bodyRejected;
  finishBody('{"late":true}'); await flush(); assert.equal(ui.timers.size, 0);
  const allocation = request('/test/submissions', {method: 'POST', body: {count: '100'}});
  const allocationRejected = assert.rejects(allocation, error => error.code === 'timeout');
  assert.ok([...ui.timers.values()].some(timer => timer.delay === 120000));
  ui.runTimer(120000); await allocationRejected;
  ui.get('logout').click();
});

test('send body timeout retries the frozen POST and a late success cannot clear the next draft', async () => {
  let sends = 0; let finishBody;
  const ui = environment(call => {
    if (call.url === '/api/chat/sessions') return ok({sessions: [description]});
    if (call.url.includes('/history?')) return ok(empty());
    if (call.url.endsWith('/submissions')) return ok({start: '1', end: '100'}, 201);
    if (call.url.endsWith('/turns')) {
      if (++sends === 1) return {status: 201, ok: true, text: () => new Promise(resolve => { finishBody = resolve; })};
      return ok({submission_id: '1', status: 'committed', turn_ref: {role: 'user', turn_id: 'user:1'}}, 200);
    }
    throw new Error(`unexpected request ${call.url}`);
  });
  await load('send-body-timeout'); typeAndSend(ui, '原始请求'); await until(() => !!finishBody);
  assert.equal(ui.get('message-input').disabled, true);
  ui.runTimer(30000); await flush();
  assert.equal(ui.get('message-input').disabled, true);
  for (let index = 0; sends < 2 && index < 4; index++) { ui.runTimer(1000); await flush(); }
  await until(() => ui.get('submission-status').textContent === '消息已发送');
  assert.equal(ui.get('message-input').value, ''); assert.equal(ui.get('message-input').disabled, false);
  ui.get('message-input').value = '编辑下一条'; ui.get('message-input').dispatch('input');
  finishBody(JSON.stringify({submission_id: '1', status: 'committed', turn_ref: {role: 'user', turn_id: 'user:1'}})); await flush();
  const bodies = ui.calls.filter(call => call.url.endsWith('/turns')).map(call => call.body);
  assert.equal(bodies.length, 2); assert.equal(bodies[0], bodies[1]);
  assert.equal(ui.get('message-input').value, '编辑下一条'); assert.equal(ui.get('send').disabled, false);
  for (let index = 0; index < 3; index++) { ui.runTimer(1000); await flush(); }
  assert.equal(sends, 2); assert.equal(ui.get('messages').children.some(child => child.tagName === 'ARTICLE'), false);
  ui.get('logout').click();
});

test('A → B → A initialization discards earlier responses for the same session', async () => {
  const other = {...description, session_id: '01ARZ3NDEKTSV4RRFFQ69G5FAW'};
  const allocations = [];
  const ui = environment(call => {
    if (call.url === '/api/chat/sessions') return ok({sessions: [description, other]});
    if (call.url.includes('/history?')) return ok(empty());
    if (call.url.endsWith('/submissions')) return new Promise(resolve => allocations.push({url: call.url, resolve}));
    throw new Error(`unexpected request ${call.url}`);
  });
  await load('initialization-switch');
  const [b, a] = ui.get('sessions').children;
  a.click(); b.click(); await until(() => allocations.length === 3);
  assert.equal(allocations[0].url, allocations[2].url);
  allocations[0].resolve(ok({start: '1', end: '100'}, 201));
  allocations[1].resolve(ok({start: '1', end: '100'}, 201)); await flush();
  assert.equal(ui.get('send').disabled, true);
  allocations[2].resolve(ok({start: '101', end: '200'}, 201)); await until(() => !ui.get('send').disabled);
  a.click(); await until(() => allocations.length === 4); assert.equal(ui.get('send').disabled, true);
  allocations[3].resolve(ok({start: '101', end: '200'}, 201)); await until(() => !ui.get('send').disabled);
  ui.get('logout').click();
});

test('reset replaces text and attachments in the original bubble; old metadata never restores removed files', async () => {
  let histories = 0; let oldMetadata;
  const pages = [
    historyPage([historyEvent(1, {kind: 'turn.start', turn_id: 'a', content: [], reply_to_seqs: []}, 'agent', [{object_id: '42'}])], 2),
    historyPage([historyEvent(2, {kind: 'turn.append', turn_id: 'a', pre_seq: '1', content: []}, 'agent', [{object_id: '43'}])], 3),
    historyPage([historyEvent(3, {kind: 'turn.reset', turn_id: 'a', pre_seq: '2', content: [{type: 'text', text: '新回答'}]}, 'agent', [{object_id: '43'}])], 4),
    historyPage([historyEvent(4, {kind: 'turn.reset', turn_id: 'a', pre_seq: '3', content: []})], 5),
  ];
  const metadata = id => ({object_id: id, name: `${id}.txt`, type: 'text/plain', description: '', nbytes: 3});
  const ui = environment(call => {
    if (call.url === '/api/chat/sessions') return ok({sessions: [description]});
    if (call.url.endsWith('/submissions')) return ok({start: '1', end: '100'}, 201);
    if (call.url.includes('/history?')) return ok(pages[histories++] || empty('5', '4'));
    if (call.url.includes('/attachments/42/metadata')) return new Promise(resolve => { oldMetadata = resolve; });
    if (call.url.includes('/attachments/43/metadata')) return ok(metadata('43'));
    throw new Error(`unexpected request ${call.url}`);
  });
  await load('reset-dom'); await until(() => !!oldMetadata);
  const article = ui.get('messages').children[0]; const content = findElement(article, element => element.className === 'turn-content');
  const textNode = content.children[0]; const cancel = findElement(article, element => element.textContent === '停止输出');
  assert.equal(content.textContent, ''); assert.equal(cancel.disabled, false);
  ui.runTimer(1000); await until(() => article.textContent.includes('43.txt'));
  ui.runTimer(1000); await until(() => content.textContent === '新回答');
  assert.equal(ui.get('messages').children[0], article); assert.equal(content.children[0], textNode);
  assert.equal(article.children.filter(child => child.className === 'attachment').length, 1);
  assert.equal(ui.calls.filter(call => call.url.includes('/attachments/43/metadata')).length, 1);
  oldMetadata(ok(metadata('42'))); await flush(); assert.equal(article.textContent.includes('42.txt'), false);
  ui.runTimer(1000); await until(() => content.textContent === '');
  assert.equal(article.children.filter(child => child.className === 'attachment').length, 0);
  assert.equal(cancel.hidden, false); assert.equal(cancel.disabled, false);
  ui.get('logout').click();
});

test('downloads validate headers, wait for the complete body, and never save a partial file', async () => {
  let finishBody; let saved = 0;
  const headers = {'Content-Type': 'application/octet-stream', 'Content-Disposition': "attachment; filename*=UTF-8''report.txt",
    'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'private, no-store'};
  const ui = environment(call => {
    if (call.url === '/api/chat/sessions') return ok({sessions: []});
    if (call.url === '/bad-download') return new Response('<html>proxy</html>', {headers: {'Content-Type': 'text/html'}});
    if (call.url === '/slow-download') return {ok: true, status: 200, headers: new Headers(headers), blob: () => new Promise(resolve => { finishBody = resolve; })};
    if (call.url === '/good-download') return new Response('file', {headers});
    throw new Error(`unexpected request ${call.url}`);
  });
  const {request} = await load('download-deadline');
  await assert.rejects(request('/bad-download', {binary: true}), /下载响应格式/);
  const slow = request('/slow-download', {binary: true}).then(() => saved++);
  const rejected = assert.rejects(slow, error => error.code === 'timeout');
  await until(() => !!finishBody); ui.runTimer(120000); await rejected;
  finishBody(new Blob(['partial'])); await flush(); assert.equal(saved, 0);
  const result = await request('/good-download', {binary: true}); assert.equal(await result.data.text(), 'file');
  ui.get('logout').click();
});

test('upload timeout retains the local file, never retries automatically and ignores the late object id', async () => {
  const uploads = [];
  const ui = environment(call => {
    if (call.url === '/api/chat/sessions') return ok({sessions: [description]});
    if (call.url.includes('/history?')) return ok(empty());
    if (call.url.endsWith('/submissions')) return ok({start: '1', end: '100'}, 201);
    if (call.url.endsWith('/attachments')) return new Promise(resolve => uploads.push(resolve));
    if (call.url.endsWith('/turns')) return ok({submission_id: '1', status: 'committed', turn_ref: {role: 'user', turn_id: 'user:1'}}, 201);
    throw new Error(`unexpected request ${call.url}`);
  });
  await load('upload-timeout');
  const file = new File(['data'], '原文件.txt', {type: 'text/plain'});
  ui.get('file-input').files = [file]; ui.get('file-input').dispatch('change'); await until(() => uploads.length === 1);
  ui.runTimer(120000); await until(() => ui.get('draft-files').textContent.includes('上传结果未确认'));
  for (let index = 0; index < 3; index++) { ui.runTimer(1000); await flush(); }
  assert.equal(uploads.length, 1); assert.equal(ui.get('send').disabled, true);
  findElement(ui.get('draft-files'), element => element.textContent === '重新上传').click(); await until(() => uploads.length === 2);
  const metadata = id => ({object_id: id, name: file.name, type: file.type, description: '', nbytes: file.size});
  uploads[0](ok(metadata('41'), 201)); await flush(); assert.equal(ui.get('send').disabled, true);
  uploads[1](ok(metadata('42'), 201)); await until(() => !ui.get('send').disabled);
  typeAndSend(ui, '文件'); await until(() => ui.get('submission-status').textContent === '消息已发送');
  const uploadCalls = ui.calls.filter(call => call.url.endsWith('/attachments'));
  assert.equal(uploadCalls[0].body.get('file').name, file.name); assert.equal(uploadCalls[1].body.get('file').name, file.name);
  assert.deepEqual(JSON.parse(ui.calls.find(call => call.url.endsWith('/turns')).body).attachments, ['42']);
  ui.get('logout').click();
});

test('A → B → A send confirmation ignores the earlier response and retains the same frozen id', async () => {
  const other = {...description, session_id: '01ARZ3NDEKTSV4RRFFQ69G5FAT'};
  const sends = [];
  const ui = environment(call => {
    if (call.url === '/api/chat/sessions') return ok({sessions: [description, other]});
    if (call.url.includes('/history?')) return ok(empty());
    if (call.url.endsWith('/submissions')) return ok({start: '1', end: '100'}, 201);
    if (call.url.endsWith('/turns')) return new Promise(resolve => sends.push(resolve));
    throw new Error(`unexpected request ${call.url}`);
  });
  await load('send-switch'); typeAndSend(ui, '切换前原文'); await until(() => sends.length === 1);
  const [a, b] = ui.get('sessions').children;
  b.click(); await flush(); a.click(); await until(() => sends.length === 2);
  const committed = () => ok({submission_id: '1', status: 'committed', turn_ref: {role: 'user', turn_id: 'user:1'}}, 200);
  sends[0](committed()); await flush();
  assert.equal(ui.get('send').disabled, true); assert.equal(ui.get('message-input').value, '切换前原文');
  sends[1](committed()); await until(() => ui.get('submission-status').textContent === '消息已发送');
  const bodies = ui.calls.filter(call => call.url.endsWith('/turns')).map(call => call.body);
  assert.equal(bodies.length, 2); assert.equal(bodies[0], bodies[1]);
  ui.get('logout').click();
});

test('switching during inventory refill keeps the prepared session and discards the earlier issued range', async () => {
  const other = {...description, session_id: '01ARZ3NDEKTSV4RRFFQ69G5FAT'};
  const refills = []; let allocations = 0; let sends = 0;
  const ui = environment(call => {
    if (call.url === '/api/chat/sessions') return ok({sessions: [description, other]});
    if (call.url.includes('/history?')) return ok(empty());
    if (call.url.endsWith('/submissions')) {
      if (!call.url.includes(SID) || ++allocations === 1) return ok({start: '1', end: '100'}, 201);
      return new Promise(resolve => refills.push(resolve));
    }
    if (call.url.endsWith('/turns')) {
      sends++; const id = JSON.parse(call.body).submission_id;
      return ok({submission_id: id, status: 'committed', turn_ref: {role: 'user', turn_id: `user:${id}`}}, 201);
    }
    throw new Error(`unexpected request ${call.url}`);
  });
  await load('refill-switch');
  await until(() => !ui.get('send').disabled);
  for (let index = 1; index <= 100; index++) {
    typeAndSend(ui, `消息${index}`); await until(() => sends === index && ui.get('message-input').value === '');
  }
  typeAndSend(ui, '等待补库的原消息'); await until(() => refills.length === 1);
  const [a, b] = ui.get('sessions').children; b.click(); await flush(); a.click();
  await until(() => refills.length === 2);
  assert.equal(ui.get('message-input').disabled, true); assert.equal(ui.get('send').disabled, true);
  refills[0](ok({start: '101', end: '200'}, 201)); await flush(); assert.equal(sends, 100);
  refills[1](ok({start: '201', end: '300'}, 201)); await until(() => sends === 101 && !ui.get('send').disabled);
  const latest = JSON.parse(ui.calls.filter(call => call.url.endsWith('/turns')).at(-1).body);
  assert.equal(latest.submission_id, '201'); assert.equal(latest.text, '等待补库的原消息');
  ui.get('logout').click();
});

test('a definitive refill failure ends the unsent attempt and preserves its editable draft', async () => {
  let allocations = 0; let sends = 0;
  const ui = environment(call => {
    if (call.url === '/api/chat/sessions') return ok({sessions: [description]});
    if (call.url.includes('/history?')) return ok(empty());
    if (call.url.endsWith('/submissions')) {
      if (++allocations === 2) return ok({error: {code: 'server_unavailable', message: 'reservation failed', failure: {category: 'contract'}}}, 503);
      return ok(allocations === 1 ? {start: '1', end: '100'} : {start: '10001', end: '10100'}, 201);
    }
    if (call.url.endsWith('/turns')) {
      sends++; const id = JSON.parse(call.body).submission_id;
      return ok({submission_id: id, status: 'committed', turn_ref: {role: 'user', turn_id: `user:${id}`}}, 201);
    }
    throw new Error(`unexpected request ${call.url}`);
  });
  await load('refill-rejected');
  for (let index = 1; index <= 100; index++) {
    typeAndSend(ui, `消息${index}`); await until(() => sends === index && ui.get('message-input').value === '');
  }
  typeAndSend(ui, '领号失败前的草稿');
  await until(() => ui.get('submission-status').textContent.includes('reservation failed'));
  assert.equal(ui.get('message-input').disabled, false); assert.equal(ui.get('message-input').value, '领号失败前的草稿');
  assert.equal(sends, 100);
  ui.get('message-input').value = '修改后的草稿'; ui.get('message-input').dispatch('input');
  findElement(ui.get('submission-status'), element => element.textContent === '重试初始化').click();
  await until(() => allocations === 3); await flush();
  assert.equal(sends, 100); assert.equal(ui.get('message-input').value, '修改后的草稿');
  ui.get('composer').dispatch('submit'); await until(() => sends === 101 && ui.get('message-input').value === '');
  const final = JSON.parse(ui.calls.filter(call => call.url.endsWith('/turns')).at(-1).body);
  assert.equal(final.submission_id, '10001'); assert.equal(final.text, '修改后的草稿');
  ui.get('logout').click();
});

test('a long history leaves unrelated attachments untouched during local message and control updates', async () => {
  const count = 1000;
  let histories = 0; let finishMetadata; let finishCancellation;
  const ui = environment(call => {
    if (call.url === '/api/chat/sessions') return ok({sessions: [description]});
    if (call.url.endsWith('/submissions')) return ok({start: '1', end: '100'}, 201);
    if (call.url.includes('/history?')) {
      histories++;
      const from = Number(new URL(call.url, 'https://chat.test').searchParams.get('fetch_seq'));
      if (from <= count) return ok({
        next_seq: String(from + 100), last_seq: String(count),
        events: Array.from({length: 100}, (_, offset) => {
          const seq = from + offset;
          return historyEvent(seq, {
            kind: seq === count ? 'turn.start' : 'turn.single', turn_id: `turn-${seq}`,
            content: [{type: 'text', text: '正文'}], reply_to_seqs: [],
          }, seq === 1 ? 'user' : 'agent', [{object_id: seq === count ? '43' : '42'}]);
        }),
      });
      if (from === count + 1) return ok(historyPage([historyEvent(from, {
        kind: 'turn.append', turn_id: `turn-${count}`, pre_seq: String(count), content: [{type: 'text', text: '追加'}],
      })], count + 2));
      return ok(empty(String(count + 2), String(count + 1)));
    }
    if (call.url.includes('/attachments/42/metadata?')) return ok({object_id: '42', name: '历史.txt', type: 'text/plain', description: '', nbytes: 4});
    if (call.url.includes('/attachments/43/metadata?')) return new Promise(resolve => { finishMetadata = resolve; });
    if (call.url.endsWith('/cancellations')) return new Promise(resolve => { finishCancellation = resolve; });
    throw new Error(`unexpected request ${call.url}`);
  });
  await load('long-history-local-updates');
  while (histories < 10) { ui.runTimer(0); await flush(); }
  await until(() => !!finishMetadata && !ui.get('message-input').disabled); await flush();
  const articles = [...ui.get('messages').children];
  assert.equal(articles.length, count);
  let unrelatedUpdates = 0;
  for (const article of articles.slice(0, -1)) {
    const control = findElement(article, element => element.tagName === 'BUTTON' && element.textContent === '下载');
    let disabled = control.disabled;
    Object.defineProperty(control, 'disabled', {
      get: () => disabled,
      set: value => { disabled = value; unrelatedUpdates++; },
    });
  }
  const active = articles.at(-1);
  const content = findElement(active, element => element.className === 'turn-content');
  ui.runTimer(1000); await until(() => content.textContent === '正文追加');
  assert.equal(unrelatedUpdates, 0, 'one append must not update the other 999 attachments');
  const reply = findElement(active, element => element.tagName === 'BUTTON' && element.textContent === '回复');
  reply.click(); await flush();
  assert.equal(reply.textContent, '已选回复'); assert.equal(unrelatedUpdates, 0);
  finishMetadata(ok({object_id: '43', name: '当前.txt', type: 'text/plain', description: '', nbytes: 4}));
  await until(() => active.textContent.includes('当前.txt'));
  assert.equal(unrelatedUpdates, 0);
  const cancel = findElement(active, element => element.tagName === 'BUTTON' && element.textContent === '停止输出');
  cancel.click(); await until(() => !!finishCancellation); await flush();
  assert.equal(cancel.textContent, '正在取消…'); assert.equal(unrelatedUpdates, 0);
  finishCancellation(ok({status: 'committed'}, 201));
  await until(() => cancel.textContent === '等待取消结果'); assert.equal(unrelatedUpdates, 0);
  assert.equal(ui.calls.filter(call => call.url.includes('/metadata?')).length, 2);
  ui.get('logout').click();
});

test('local append and reset refresh dependent quotes, draft previews and titles without touching unrelated files', async () => {
  let histories = 0;
  const ui = environment(call => {
    if (call.url === '/api/chat/sessions') return ok({sessions: [description]});
    if (call.url.endsWith('/submissions')) return ok({start: '1', end: '100'}, 201);
    if (call.url.includes('/history?')) {
      if (++histories === 1) return ok(historyPage([
        historyEvent(1, {kind: 'turn.start', turn_id: 'source', content: [{type: 'text', text: '原文'}], reply_to_seqs: []}, 'user'),
        historyEvent(2, {kind: 'turn.single', turn_id: 'quote', content: [], reply_to_seqs: ['1']}),
        historyEvent(3, {kind: 'turn.single', turn_id: 'unrelated', content: [], reply_to_seqs: []}, 'agent', [{object_id: '42'}]),
      ], 4));
      if (histories === 2) return ok(historyPage([historyEvent(4, {
        kind: 'turn.append', turn_id: 'source', pre_seq: '1', content: [{type: 'text', text: '追加'}],
      }, 'user')], 5));
      return ok(historyPage([historyEvent(5, {
        kind: 'turn.reset', turn_id: 'source', pre_seq: '4', content: [{type: 'text', text: '重置'}],
      }, 'user')], 6));
    }
    if (call.url.includes('/metadata?')) return ok({object_id: '42', name: '文件.txt', type: 'text/plain', description: '', nbytes: 4});
    throw new Error(`unexpected request ${call.url}`);
  });
  await load('dependent-local-updates');
  await until(() => ui.get('messages').textContent.includes('文件.txt')); await flush();
  const [source, dependent, unrelated] = ui.get('messages').children;
  let unrelatedUpdates = 0;
  const download = findElement(unrelated, element => element.tagName === 'BUTTON' && element.textContent === '下载');
  Object.defineProperty(download, 'disabled', {get: () => false, set: () => { unrelatedUpdates++; }});
  findElement(source, element => element.tagName === 'BUTTON' && element.textContent === '回复').click(); await flush();
  const quote = findElement(dependent, element => element.className === 'quoted-reply');
  const draft = ui.get('reply-selection').children[0];
  for (const text of ['原文追加', '重置']) {
    ui.runTimer(1000); await until(() => quote.textContent === `你：${text}`);
    assert.equal(draft.textContent, `你：${text} ×`);
    assert.equal(ui.get('session-title').textContent, text);
    assert.equal(ui.get('messages').children[1], dependent);
    assert.equal(unrelatedUpdates, 0);
  }
  ui.get('logout').click();
});
