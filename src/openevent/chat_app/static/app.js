import {
  ActiveLoop, MAX_FILE_BYTES, applyPage, beginOldBatchCheck, createSession, decimal, fileInfo,
  inspectSubmission, setInventory, takeSubmission,
} from './state.mjs';

const $ = id => document.getElementById(id);
const sessions = new Map();
const requests = new Set();
let activeId = null;
let epoch = 0;
let loggedIn = false;
let pageSuspended = false;
let queryController = null;
let queryTimer = null;
let queryGeneration = 0;
let renderQueued = false;
let listing = false;
let creating = false;
let pendingCreate = readCreateId();
let renderedMessages = null;
let renderedComposer = null;
const sessionEntries = new Map();
const API = '/api/chat';

class ApiError extends Error {
  constructor(status, code, message, failure) {
    super(message); Object.assign(this, {status, code, failure});
  }
}

function node(tag, className = '', text = '') {
  const element = document.createElement(tag);
  element.className = className;
  element.textContent = text;
  return element;
}

function button(text, action, className = 'quiet') {
  const element = node('button', className, text);
  element.type = 'button'; element.addEventListener('click', action);
  return element;
}

function endpoint(session, suffix) { return `${API}/sessions/${encodeURIComponent(session.id)}${suffix}`; }
function valid(session, generation = epoch) { return loggedIn && epoch === generation && sessions.get(session.id) === session; }
function current() { return sessions.get(activeId); }
function message(error) {
  const summary = error?.message || '请求未完成，请稍后再试。';
  const detail = error?.failure?.detail;
  return typeof detail === 'string' && detail && detail !== summary ? `${summary}：${detail}` : summary;
}
function temporary(error) {
  return error instanceof TypeError || error?.status === 0 ||
    (error?.status === 503 && (!error.failure?.category || error.failure.category === 'external_unavailable'));
}

async function request(url, {method = 'GET', body, signal, binary = false} = {}) {
  const generation = epoch;
  const controller = new AbortController();
  const cancel = () => controller.abort();
  signal?.addEventListener('abort', cancel, {once: true});
  if (signal?.aborted) controller.abort();
  requests.add(controller);
  let timedOut = false;
  const timer = setTimeout(() => { timedOut = true; controller.abort(); }, 30000);
  const headers = {};
  if (body !== undefined && !(body instanceof FormData)) {
    headers['Content-Type'] = 'application/json'; body = JSON.stringify(body);
  }
  try {
    const response = await fetch(url, {method, body, headers, signal: controller.signal, credentials: 'same-origin', cache: 'no-store'});
    if (response.status === 401) {
      if (epoch === generation && loggedIn) logout('访问口令无效，请重新输入。');
      throw new ApiError(401, 'unauthenticated', '访问口令无效，请重新输入。');
    }
    if (!response.ok) {
      let data;
      try { data = await response.json(); } catch { /* A proxy may return a non-JSON failure. */ }
      throw new ApiError(response.status, data?.error?.code || 'request_failed', data?.error?.message || `请求未完成（${response.status}）`, data?.error?.failure);
    }
    if (binary) return {data: await response.blob(), response};
    const data = await response.json();
    return {data, status: response.status};
  } catch (error) {
    if (timedOut) throw new ApiError(0, 'timeout', '请求超时，请稍后再试。');
    throw error;
  } finally {
    clearTimeout(timer); requests.delete(controller); signal?.removeEventListener('abort', cancel);
  }
}

function cookieToken() {
  const value = document.cookie.split(';').map(part => part.trim()).find(part => part.startsWith('web_token='));
  try { return value ? decodeURIComponent(value.slice('web_token='.length)) : ''; } catch { return ''; }
}

function writeToken(value) {
  const secure = location.protocol === 'https:' ? '; Secure' : '';
  document.cookie = `web_token=${encodeURIComponent(value)}; Path=/api/chat; SameSite=Strict; Expires=${value ? 'Fri, 31 Dec 9999 23:59:59 GMT' : 'Thu, 01 Jan 1970 00:00:00 GMT'}${secure}`;
}

function logout(reason = '') {
  epoch++; loggedIn = false; activeId = null; listing = false; creating = false;
  historyLoop.stop(); stopQuery();
  for (const controller of requests) controller.abort();
  requests.clear();
  for (const session of sessions.values()) session.fileInfo.clear();
  sessions.clear(); setCreateId(null); writeToken('');
  $('token').value = ''; $('login-error').textContent = reason;
  $('directory-error').textContent = '';
  render(); $('token').focus();
}

function readCreateId() {
  try {
    const value = sessionStorage.getItem('openevent.pending-create');
    return value && /^[0-7][0-9A-HJKMNP-TV-Z]{25}$/.test(value) ? value : null;
  } catch { return null; }
}

function setCreateId(value) {
  pendingCreate = value;
  try {
    if (value) sessionStorage.setItem('openevent.pending-create', value);
    else sessionStorage.removeItem('openevent.pending-create');
  } catch { /* The in-memory request id still survives a retry on this page. */ }
}

function ulid() {
  const alphabet = '0123456789ABCDEFGHJKMNPQRSTVWXYZ';
  let value = BigInt(Date.now());
  for (const byte of crypto.getRandomValues(new Uint8Array(10))) value = (value << 8n) | BigInt(byte);
  let result = '';
  for (let index = 0; index < 26; index++) { result = alphabet[Number(value & 31n)] + result; value >>= 5n; }
  return result;
}

function register(description) {
  const candidate = createSession(description);
  let session = sessions.get(candidate.id);
  if (session) {
    if (session.start !== candidate.start) throw new Error('会话读取起点发生变化');
  } else {
    session = candidate; session.fileInfo.onChange = scheduleRender; session.canceling = new Map();
    sessions.set(session.id, session);
  }
  return session;
}

async function loadSessions() {
  if (listing || !loggedIn) return;
  listing = true; const generation = epoch; $('directory-error').textContent = ''; scheduleRender();
  try {
    const {data} = await request(`${API}/sessions`);
    if (!loggedIn || epoch !== generation) return;
    if (!Array.isArray(data.sessions)) throw new Error('会话列表格式不正确');
    // Validate the full list before registering any of it.
    data.sessions.forEach(createSession);
    for (const description of data.sessions) register(description);
    if (!activeId && sessions.size) activate([...sessions.keys()].sort().at(-1));
  } catch (error) {
    if (loggedIn && epoch === generation) $('directory-error').textContent = message(error);
  } finally {
    if (epoch === generation) { listing = false; scheduleRender(); }
  }
}

async function createConversation() {
  if (creating || !loggedIn) return;
  creating = true; const generation = epoch; $('directory-error').textContent = '';
  if (!pendingCreate) setCreateId(ulid());
  const requestId = pendingCreate; scheduleRender();
  try {
    const {data} = await request(`${API}/sessions`, {method: 'POST', body: {create_request_id: requestId}});
    if (!loggedIn || epoch !== generation) return;
    const session = register(data); setCreateId(null); activate(session.id);
  } catch (error) {
    if (loggedIn && epoch === generation) $('directory-error').textContent = `${message(error)} 可以使用“重试创建”继续确认同一次创建。`;
  } finally {
    if (epoch === generation) { creating = false; scheduleRender(); }
  }
}

function activate(id) {
  if (!loggedIn || !sessions.has(id)) return;
  activeId = id; stopQuery();
  const session = current(); session.sync = 'loading'; session.error = '';
  render(); historyLoop.activate(session); startQuery();
}

const historyLoop = new ActiveLoop({
  read: async (session, signal) => {
    const {data} = await request(endpoint(session, `/history?fetch_seq=${session.fetchSeq}&limit=100`), {signal});
    return data;
  },
  apply: (session, page, serial) => {
    const changes = applyPage(session, page);
    for (const [key, turn] of changes) {
      if (turn.state !== 'open' && session.canceling.has(key)) finishCancellation(session, turn);
    }
    session.sync = 'ready'; session.error = '';
    const result = inspectSubmission(session, session.pending, page, serial);
    if (result === 'committed') confirmSend(session, session.pending);
    else if (result === 'absent') {
      session.pending = null; session.notice = '已确认这条消息没有发送成功。草稿已保留，可以重新发送。';
      stopQuery();
    }
    scheduleRender();
  },
  error: (session, error) => {
    session.error = message(error); session.sync = temporary(error) ? 'retrying' : 'stopped'; scheduleRender();
    return temporary(error);
  },
});

function stopQuery() {
  queryGeneration++; clearTimeout(queryTimer); queryTimer = null;
  queryController?.abort(); queryController = null;
}

function startQuery(delay = 0) {
  const session = current();
  if (pageSuspended || !session?.pending || session.pending.phase !== 'unknown' || queryController || queryTimer) return;
  const generation = queryGeneration;
  queryTimer = setTimeout(() => { queryTimer = null; querySubmission(session, session.pending, generation); }, delay);
}

async function querySubmission(session, pending, generation) {
  if (!pending || current() !== session || generation !== queryGeneration || session.pending !== pending) return;
  const controller = new AbortController(); queryController = controller;
  let again = true;
  try {
    const {data, status} = await request(endpoint(session, `/submissions/${pending.submissionId}`), {signal: controller.signal});
    if (current() !== session || generation !== queryGeneration || session.pending !== pending) return;
    if (data.submission_id !== pending.submissionId) throw new Error('发送结果的编号不匹配');
    if (status === 202 && data.status === 'processing') pending.detail = '后端正在处理，正在等待结果…';
    else if (status === 200) { decimal(data.seq); confirmSend(session, pending); again = false; }
    else throw new Error('发送查询返回格式不正确');
  } catch (error) {
    if (current() !== session || generation !== queryGeneration || session.pending !== pending) return;
    if (error.status === 409 && error.code === 'submission_out_of_range') {
      beginOldBatchCheck(session, pending); pending.detail = '正在读取消息记录，确认这次发送结果…';
      if (session.sync !== 'stopped') historyLoop.kick();
      again = false;
    } else if (error.status === 404 && error.code === 'submission_not_observed') {
      pending.detail = '结果尚未确认，正在等待后端处理…';
    } else if (temporary(error)) pending.detail = '暂时无法查询发送结果，稍后继续确认…';
    else { pending.detail = message(error); pending.queryStopped = true; again = false; }
  } finally {
    if (generation === queryGeneration) {
      queryController = null; scheduleRender();
      if (again && session.pending === pending && current() === session) startQuery(1000);
    }
  }
}

function confirmSend(session, pending) {
  if (!pending || session.pending !== pending) return;
  session.pending = null; session.text = ''; session.replies = []; session.files = [];
  session.notice = '消息已发送';
  if (current() === session) stopQuery();
  scheduleRender();
}

async function sendMessage() {
  const session = current();
  if (!session || session.pending || session.files.some(file => file.status !== 'ready')) return;
  const generation = epoch;
  const pending = {phase: 'allocating', submissionId: null, text: session.text, replies: session.replies.slice(), attachments: session.files.map(file => file.objectId)};
  session.pending = pending; session.notice = ''; scheduleRender();
  try {
    while (valid(session, generation) && session.pending === pending) {
      pending.phase = 'allocating';
      let id = takeSubmission(session);
      if (id === null) {
        const {data} = await request(endpoint(session, '/submissions'), {method: 'POST', body: {count: '100'}});
        if (!valid(session, generation) || session.pending !== pending) return;
        setInventory(session, data); id = takeSubmission(session);
      }
      pending.submissionId = id; pending.phase = 'sending'; scheduleRender();
      try {
        const {data} = await request(endpoint(session, '/turns'), {method: 'POST', body: {
          submission_id: id, text: pending.text, reply_to_seqs: pending.replies, attachments: pending.attachments,
        }});
        if (!valid(session, generation) || session.pending !== pending) return;
        if (data.status !== 'committed' || data.submission_id !== id) throw new Error('发送响应无法确认');
        confirmSend(session, pending); return;
      } catch (error) {
        if (!valid(session, generation) || session.pending !== pending) return;
        if (error.status === 409 && error.code === 'submission_out_of_range') {
          session.inventory = null; pending.submissionId = null; continue;
        }
        if (error.status === 409 && error.code === 'attachment_unavailable') {
          for (const file of session.files) { file.status = 'error'; file.error = '文件需要重新上传'; }
          session.pending = null; session.notice = '文件上传记录已失效，请重新上传后发送。'; return;
        }
        if (error.status >= 400 && error.status < 500) {
          session.pending = null; session.notice = message(error); return;
        }
        pending.phase = 'unknown'; pending.detail = '发送结果未确认，正在查询…';
        if (inspectSubmission(session, pending) === 'committed') confirmSend(session, pending);
        else if (current() === session) startQuery();
        return;
      }
    }
  } catch (error) {
    if (valid(session, generation) && session.pending === pending) {
      session.pending = null; session.notice = `未取得发送编号，消息还没有发送。${message(error)}`;
    }
  } finally { if (valid(session, generation)) scheduleRender(); }
}

async function upload(session, item) {
  if (!valid(session) || !session.files.includes(item) || item.status === 'uploading') return;
  if (!item.file) { item.status = 'error'; item.error = '请重新选择文件'; scheduleRender(); return; }
  if (item.file.size === 0 || item.file.size > MAX_FILE_BYTES) {
    item.status = 'error'; item.error = '请选择非空且不超过 4 MiB 的文件'; scheduleRender(); return;
  }
  const generation = epoch; const controller = new AbortController(); item.controller = controller;
  item.status = 'uploading'; item.error = ''; scheduleRender();
  const body = new FormData(); body.append('file', item.file, item.file.name);
  try {
    const {data} = await request(endpoint(session, '/attachments'), {method: 'POST', body, signal: controller.signal});
    if (!valid(session, generation) || !session.files.includes(item) || item.controller !== controller) return;
    fileInfo(data); item.objectId = data.object_id; item.status = 'ready'; session.fileInfo.uploaded(data);
  } catch (error) {
    if (!valid(session, generation) || !session.files.includes(item) || item.controller !== controller) return;
    item.status = 'error'; item.error = message(error);
  } finally {
    if (valid(session, generation) && session.files.includes(item) && item.controller === controller) { item.controller = null; scheduleRender(); }
  }
}

function metadata(session, reference, retry = false) {
  return session.fileInfo.ensure(reference, async ref => {
    const {data} = await request(endpoint(session, `/attachments/${ref.object_id}/metadata?event_seq=${ref.event_seq}`));
    return data;
  }, retry);
}

async function download(session, reference) {
  const generation = epoch;
  try {
    const {data, response} = await request(endpoint(session, `/attachments/${reference.object_id}?event_seq=${reference.event_seq}`), {binary: true});
    if (!valid(session, generation)) return;
    const disposition = response.headers.get('Content-Disposition') || '';
    let name = session.fileInfo.get(reference.object_id)?.name || '附件';
    const encodedName = /filename\*=UTF-8''([^;]+)/i.exec(disposition);
    if (encodedName) { try { name = decodeURIComponent(encodedName[1]); } catch { /* Fall back to metadata. */ } }
    const url = URL.createObjectURL(data); const link = node('a');
    link.href = url; link.download = name; document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (error) {
    if (valid(session, generation)) { session.notice = `下载未完成：${message(error)}`; scheduleRender(); }
  }
}

function finishCancellation(session, turn) {
  session.canceling.delete(turn.key);
  session.notice = turn.state === 'cancelled' ? '输出已取消。' : '输出已结束。';
}

async function cancelTurn(session, turn) {
  const status = session.canceling.get(turn.key);
  if (session.turns.get(turn.key)?.state !== 'open' || (status && status !== 'unknown')) return;
  const generation = epoch; session.canceling.set(turn.key, 'sending'); session.notice = ''; scheduleRender();
  try {
    const {data} = await request(endpoint(session, '/cancellations'), {method: 'POST', body: {target_turn_id: turn.id}});
    if (!valid(session, generation)) return;
    if (data.status !== 'committed') throw new Error('取消结果无法确认');
    const latest = session.turns.get(turn.key);
    if (latest.state === 'open') {
      session.canceling.set(turn.key, 'committed');
      session.notice = '取消请求已发送，正在等待消息更新。';
    } else finishCancellation(session, latest);
  } catch (error) {
    if (valid(session, generation)) {
      const latest = session.turns.get(turn.key);
      if (latest.state === 'open') {
        session.canceling.set(turn.key, 'unknown'); session.notice = `取消结果未确认，可以手动重试取消。${message(error)}`;
      } else finishCancellation(session, latest);
    }
  } finally { if (valid(session, generation)) scheduleRender(); }
}

export function textPrefix(parts, limit, trim = false) {
  let prefix = '';
  for (const part of parts) {
    const text = trim && !prefix ? part.text.trimStart() : part.text;
    const remaining = limit - prefix.length;
    prefix += text.slice(0, remaining);
    // Trailing whitespace belongs in the prefix only if more non-whitespace follows.
    if (prefix.length === limit && (!trim || /\S/.test(prefix.at(-1)) || /\S/.test(text.slice(remaining)))) return prefix;
  }
  return trim ? prefix.trimEnd() : prefix;
}

function title(session) {
  for (const key of session.order) {
    const turn = session.turns.get(key);
    if (turn.role === 'user') {
      const text = textPrefix(turn.parts, 35, true);
      if (text) return text;
    }
  }
  return `会话 ${session.id.slice(-6)}`;
}

function snippet(turn) {
  if (!turn) return '已引用消息';
  return `${turn.role === 'user' ? '你' : 'Agent'}：${textPrefix(turn.parts, 80) || '附件消息'}`;
}

function formatSize(size) { return size >= 1024 * 1024 ? `${(size / 1024 / 1024).toFixed(1)} MiB` : size >= 1024 ? `${Math.ceil(size / 1024)} KiB` : `${size} B`; }

function updateAttachment(session, view) {
  const ref = view.ref;
  let info = session.fileInfo.get(ref.object_id);
  if (!info) { metadata(session, ref); info = session.fileInfo.get(ref.object_id); }
  if (view.info === info) return;
  view.info = info;
  const body = node('div', 'attachment-info');
  body.append(node('strong', '', info?.state === 'ready' ? info.name || '附件' : '附件'));
  body.append(node('span', 'muted', info?.state === 'ready' ? `${formatSize(info.nbytes)} · ${info.type}` : info?.state === 'unavailable' ? '文件信息暂不可用' : '正在读取文件信息…'));
  if (info?.description) body.append(node('span', 'muted', info.description));
  view.element.replaceChildren(node('span', 'file-icon', '↧'), body);
  if (info?.state === 'unavailable') view.element.append(button('重试信息', () => metadata(session, ref, true)));
  view.element.append(button('下载', () => download(session, ref)));
}

function setText(element, text) { if (element.textContent !== text) element.textContent = text; }

function updateChildren(container, children) {
  // Keep unchanged controls attached so polling cannot discard keyboard focus.
  const keep = new Set(children);
  for (const child of [...container.children]) if (!keep.has(child)) child.remove();
  children.forEach((child, index) => {
    if (container.children[index] !== child) container.insertBefore(child, container.children[index] || null);
  });
}

function createTurnView(session, turn) {
  const article = node('article', `turn ${turn.role}`); article.id = `message-${turn.creationSeq}`;
  const header = node('div', 'turn-heading');
  const state = node('span', 'turn-state');
  header.append(node('span', `avatar ${turn.role}`, turn.role === 'user' ? '你' : '↗'), node('strong', '', turn.role === 'user' ? '你' : 'Agent'), state);
  const reply = button('回复', () => {
    if (session.pending) return;
    if (session.replies.includes(turn.creationSeq)) session.replies = session.replies.filter(seq => seq !== turn.creationSeq);
    else session.replies.push(turn.creationSeq);
    scheduleRender(); $('message-input').focus();
  });
  header.append(reply);
  const cancel = turn.role === 'agent' ? button('停止输出', () => cancelTurn(session, turn)) : null;
  if (cancel) header.append(cancel);
  article.append(header);
  const quotes = [];
  if (turn.replies.length) {
    const replies = node('div', 'turn-replies');
    for (const seq of turn.replies) {
      const element = button('', () => document.getElementById(`message-${seq}`)?.scrollIntoView({block: 'center', behavior: 'smooth'}), 'quoted-reply');
      quotes.push({seq, element, target: null}); replies.append(element);
    }
    article.append(replies);
  }
  const content = node('div', 'turn-content'); const text = document.createTextNode('');
  content.append(text); article.append(content);
  return {article, state, reply, cancel, quotes, text, partCount: 0, attachments: [], turn: null};
}

function updateTurnView(session, view, turn, selected) {
  if (view.turn !== turn) {
    // Text parts and attachments only grow. Keep existing nodes while streaming.
    if (view.partCount < turn.parts.length) {
      view.text.appendData(turn.parts.slice(view.partCount).map(part => part.text).join(''));
      view.partCount = turn.parts.length;
    }
    setText(view.state, turn.state === 'open' ? '正在输出' : turn.state === 'cancelled' ? '已取消' : '');
    view.state.hidden = turn.state === 'completed';
    while (view.attachments.length < turn.attachments.length) {
      const attachment = {ref: turn.attachments[view.attachments.length], element: node('div', 'attachment'), info: null};
      view.attachments.push(attachment); view.article.append(attachment.element);
    }
    view.turn = turn;
  }
  const pending = !!session.pending;
  if (view.selected !== selected || view.pending !== pending) {
    setText(view.reply, selected ? '已选回复' : '回复');
    view.reply.className = `quiet reply-action${selected ? ' selected' : ''}`;
    view.reply.disabled = pending; view.reply.setAttribute('aria-pressed', String(selected));
    view.selected = selected; view.pending = pending;
  }
  if (view.cancel) {
    const status = session.canceling.get(turn.key);
    if (view.cancelState !== turn.state || view.cancelStatus !== status) {
      view.cancel.hidden = turn.state !== 'open';
      setText(view.cancel, status === 'sending' ? '正在取消…' : status === 'committed' ? '等待取消结果' : status === 'unknown' ? '重试取消' : '停止输出');
      view.cancel.disabled = status === 'sending' || status === 'committed';
      view.cancelState = turn.state; view.cancelStatus = status;
    }
  }
  for (const quote of view.quotes) {
    const target = session.turns.get(session.creationIndex.get(quote.seq));
    if (quote.target !== target) { quote.target = target; setText(quote.element, snippet(target)); }
  }
  for (const attachment of view.attachments) updateAttachment(session, attachment);
}

function renderTurns(session) {
  const container = $('messages');
  const signature = session ? `${session.revision}|${session.fileInfo.revision}|${session.replies.join(',')}|${JSON.stringify([...session.canceling])}|${!!session.pending}` : '';
  if (renderedMessages && renderedMessages.session === session && renderedMessages.signature === signature) return;
  const switching = renderedMessages?.session !== session;
  if (switching && renderedMessages?.session) renderedMessages.session.scrollTop = container.scrollTop;
  const atBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 100;
  const oldTop = container.scrollTop;
  if (switching || !renderedMessages) {
    container.replaceChildren(); renderedMessages = {session, views: new Map(), empty: false};
  }
  if (!session || !session.order.length) {
    if (!renderedMessages.empty) {
      const empty = node('div', 'empty-state');
      empty.append(node('div', 'empty-symbol', '↗'), node('h2', '', session ? '从一条消息开始' : '留一个地方，展开想法'), node('p', 'muted', session ? '写下问题，或添加一个文件。' : '选择已有会话，或创建一个新的对话。'));
      container.replaceChildren(empty); renderedMessages.empty = true;
    }
  } else {
    if (renderedMessages.empty) { container.replaceChildren(); renderedMessages.empty = false; }
    const selected = new Set(session.replies);
    for (const key of session.order) {
      const turn = session.turns.get(key);
      let view = renderedMessages.views.get(key);
      if (!view) {
        view = createTurnView(session, turn); renderedMessages.views.set(key, view); container.append(view.article);
      }
      updateTurnView(session, view, turn, selected.has(turn.creationSeq));
    }
  }
  renderedMessages.signature = signature;
  if (switching) container.scrollTop = session?.scrollTop ?? container.scrollHeight;
  else if (atBottom) container.scrollTop = container.scrollHeight;
  else container.scrollTop = oldTop;
}

function renderComposer(session) {
  const disabled = !session || !!session.pending;
  $('message-input').disabled = disabled; $('choose-files').disabled = disabled;
  $('send').disabled = disabled || session.files.some(file => file.status !== 'ready');
  const value = session?.text || ''; if ($('message-input').value !== value) $('message-input').value = value;
  const previous = renderedComposer?.session === session ? renderedComposer : null;
  renderedComposer = {session, replies: new Map(), files: new Map()};
  for (const seq of session?.replies || []) {
    const target = session.turns.get(session.creationIndex.get(seq));
    const chip = previous?.replies.get(seq) || button('', () => { session.replies = session.replies.filter(value => value !== seq); scheduleRender(); }, 'reply-chip');
    setText(chip, `${snippet(target)} ×`); chip.disabled = disabled;
    renderedComposer.replies.set(seq, chip);
  }
  updateChildren($('reply-selection'), [...renderedComposer.replies.values()]);
  for (const item of session?.files || []) {
    const info = item.objectId ? session.fileInfo.get(item.objectId) : null;
    let view = previous?.files.get(item);
    if (!view) {
      view = {row: node('div', 'draft-file'), name: node('span', 'draft-file-name'), status: node('span'),
        retry: button('重新上传', () => upload(session, item)),
        remove: button('移除', () => { item.controller?.abort(); session.files = session.files.filter(file => file !== item); scheduleRender(); })};
      view.row.append(node('span', 'file-icon', '↧'), view.name, view.status, view.retry, view.remove);
    }
    setText(view.name, info?.state === 'ready' ? info.name : item.file?.name || '文件');
    setText(view.status, item.status === 'ready' ? '已上传' : item.status === 'uploading' ? '正在上传…' : item.error || '等待上传');
    view.status.className = item.status === 'error' ? 'error' : 'muted';
    view.retry.hidden = item.status !== 'error'; view.retry.disabled = disabled;
    view.remove.disabled = disabled; renderedComposer.files.set(item, view);
  }
  updateChildren($('draft-files'), [...renderedComposer.files.values()].map(view => view.row));
  let status = session?.notice || '';
  if (session?.pending) {
    const pending = session.pending;
    status = pending.phase === 'allocating' ? '正在准备发送…' : pending.phase === 'sending' ? '正在发送…' : pending.detail || '发送结果未确认…';
  }
  $('submission-status').textContent = status; $('submission-status').hidden = !status;
  if (session?.pending?.queryStopped) $('submission-status').append(button('重新查询', () => { session.pending.queryStopped = false; startQuery(); }));
}

function render() {
  $('login').hidden = loggedIn; $('workspace').hidden = !loggedIn;
  if (!loggedIn) {
    renderedMessages = null; renderedComposer = null; sessionEntries.clear();
    for (const id of ['messages', 'sessions', 'reply-selection', 'draft-files', 'submission-status', 'message-error']) $(id).replaceChildren();
    $('message-input').value = ''; $('file-input').value = ''; $('session-title').textContent = '选择一个会话';
    return;
  }
  const session = current(); const list = [];
  let activeTitle = '选择一个会话';
  for (const id of [...sessions.keys()].sort().reverse()) {
    const itemTitle = title(sessions.get(id));
    let entry = sessionEntries.get(id);
    if (!entry) { entry = button('', () => activate(id)); sessionEntries.set(id, entry); }
    setText(entry, itemTitle); entry.className = `session-item${id === activeId ? ' active' : ''}`;
    entry.setAttribute('aria-current', id === activeId ? 'page' : 'false'); entry.title = itemTitle; list.push(entry);
    if (id === activeId) activeTitle = itemTitle;
  }
  if (!sessions.size && !listing) list.push(node('p', 'muted no-sessions', '还没有会话'));
  updateChildren($('sessions'), list);
  $('new-session').disabled = creating; $('new-session').textContent = creating ? '正在创建…' : pendingCreate ? '↻ 重试创建' : '＋ 新建会话';
  $('refresh-sessions').disabled = listing;
  $('session-title').textContent = activeTitle;
  $('sync-status').textContent = session ? ({waiting: '等待连接', loading: '正在读取', ready: '已连接', retrying: '正在重连', stopped: '读取已停止'}[session.sync] || '') : '';
  $('sync-status').dataset.state = session?.sync || '';
  $('message-error').textContent = session?.error || ''; $('message-error').hidden = !session?.error;
  if (session?.sync === 'stopped') $('message-error').append(button('重新读取', () => activate(session.id)));
  renderTurns(session); renderComposer(session);
}

function scheduleRender() {
  if (renderQueued) return;
  renderQueued = true;
  queueMicrotask(() => { renderQueued = false; render(); });
}

$('login-form').addEventListener('submit', event => {
  event.preventDefault(); const token = $('token').value; if (!token) return;
  writeToken(token); $('token').value = ''; $('login-error').textContent = '';
  loggedIn = true; epoch++; render(); loadSessions();
});
$('logout').addEventListener('click', () => logout());
$('new-session').addEventListener('click', createConversation);
$('refresh-sessions').addEventListener('click', loadSessions);
$('composer').addEventListener('submit', event => { event.preventDefault(); sendMessage(); });
$('message-input').addEventListener('input', event => { if (current() && !current().pending) current().text = event.target.value; });
$('message-input').addEventListener('keydown', event => {
  if (event.key === 'Enter' && (event.ctrlKey || event.metaKey) && !event.isComposing) { event.preventDefault(); sendMessage(); }
});
$('choose-files').addEventListener('click', () => $('file-input').click());
$('file-input').addEventListener('change', event => {
  const session = current(); if (!session || session.pending) return;
  for (const file of event.target.files) { const item = {file, objectId: null, status: 'waiting'}; session.files.push(item); upload(session, item); }
  event.target.value = ''; scheduleRender();
});
window.addEventListener('pagehide', () => { pageSuspended = true; historyLoop.stop(); stopQuery(); for (const controller of requests) controller.abort(); });
window.addEventListener('pageshow', event => { pageSuspended = false; if (event.persisted && loggedIn && current()) activate(activeId); });

const token = cookieToken();
if (token) { writeToken(token); loggedIn = true; render(); loadSessions(); }
else render();
