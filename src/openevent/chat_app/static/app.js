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
let preparationController = null;
let preparationTimer = null;
let preparationGeneration = 0;
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
  return error instanceof TypeError || error?.status === 0 || [502, 504].includes(error?.status) ||
    (error?.status === 503 && (!error.failure?.category || error.failure.category === 'external_unavailable'));
}

export async function request(url, {method = 'GET', body, signal, binary = false} = {}) {
  const generation = epoch;
  const controller = new AbortController();
  const cancel = () => controller.abort();
  signal?.addEventListener('abort', cancel, {once: true});
  if (signal?.aborted) controller.abort();
  requests.add(controller);
  let timedOut = false;
  const timeout = binary || body instanceof FormData || url.endsWith('/submissions') ? 120000 : 30000;
  const timer = setTimeout(() => { timedOut = true; controller.abort(); }, timeout);
  const headers = {};
  if (body !== undefined && !(body instanceof FormData)) {
    headers['Content-Type'] = 'application/json'; body = JSON.stringify(body);
  }
  try {
    const cancelled = new Promise((_, reject) => {
      const rejectAbort = () => reject(new DOMException('Aborted', 'AbortError'));
      if (controller.signal.aborted) rejectAbort();
      else controller.signal.addEventListener('abort', rejectAbort, {once: true});
    });
    const operation = async () => {
      const response = await fetch(url, {method, body, headers, signal: controller.signal, credentials: 'same-origin', cache: 'no-store'});
      if (controller.signal.aborted) throw new DOMException('Aborted', 'AbortError');
      if (binary && response.ok) {
        const disposition = response.headers.get('Content-Disposition') || '';
        const cache = response.headers.get('Cache-Control') || '';
        if (response.headers.get('Content-Type')?.toLowerCase() !== 'application/octet-stream' ||
            !/^attachment(?:;|$)/i.test(disposition) || response.headers.get('X-Content-Type-Options')?.toLowerCase() !== 'nosniff' ||
            !/(?:^|,)\s*private\s*(?:,|$)/i.test(cache) || !/(?:^|,)\s*no-store\s*(?:,|$)/i.test(cache)) {
          throw new Error('下载响应格式不正确');
        }
        return {data: await response.blob(), response};
      }
      return {text: await response.text(), response};
    };
    const result = await Promise.race([operation(), cancelled]);
    const {response} = result;
    if (response.status === 401) {
      if (epoch === generation && loggedIn) logout('访问口令无效，请重新输入。');
      throw new ApiError(401, 'unauthenticated', '访问口令无效，请重新输入。');
    }
    if (!response.ok) {
      let data;
      try { data = JSON.parse(result.text); } catch { /* A proxy may return a non-JSON failure. */ }
      throw new ApiError(response.status, data?.error?.code || 'request_failed', data?.error?.message || `请求未完成（${response.status}）`, data?.error?.failure);
    }
    if (binary) return result;
    const data = JSON.parse(result.text);
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
  historyLoop.stop(); stopQuery(); stopPreparation();
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
    session = candidate; session.canceling = new Map(); session.downloads = new Set();
    session.fileInfo.onChange = objectId => {
      if (renderedMessages?.session === session) {
        const keys = objectId === undefined ? renderedMessages.views.keys() : renderedMessages.byObject.get(objectId) || [];
        for (const key of keys) renderedMessages.dirty.add(key);
      }
      scheduleRender();
    };
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
  stopQuery(); stopPreparation(); activeId = id;
  const session = current(); session.sync = 'loading'; session.error = '';
  render(); historyLoop.activate(session); prepareSession(); startQuery();
}

const historyLoop = new ActiveLoop({
  read: async (session, signal) => {
    const {data} = await request(endpoint(session, `/history?fetch_seq=${session.fetchSeq}&limit=100`), {signal});
    return data;
  },
  apply: (session, page, serial) => {
    const changes = applyPage(session, page);
    for (const [key, turn] of changes) {
      if (turn.role === 'user') session.title = null;
      if (renderedMessages?.session === session) {
        renderedMessages.dirty.add(key);
        for (const dependent of renderedMessages.byReply.get(turn.creationSeq) || []) renderedMessages.dirty.add(dependent);
      }
      if (turn.state !== 'open' && session.canceling.has(key)) finishCancellation(session, turn);
    }
    const recovered = session.sync === 'retrying';
    session.sync = 'ready'; session.error = '';
    if (recovered) requirePreparation(session);
    const result = inspectSubmission(session, session.pending, page, serial);
    if (result === 'committed') confirmSend(session, session.pending);
    else if (result === 'absent') rejectSend(session, session.pending, '已确认这条消息没有发送成功。');
    scheduleRender();
  },
  error: (session, error) => {
    session.error = message(error); session.sync = temporary(error) ? 'retrying' : 'stopped'; scheduleRender();
    return temporary(error);
  },
});

function stopQuery() {
  if (queryController && ['sending', 'unknown'].includes(current()?.pending?.phase)) {
    current().pending.uncertain = true;
    if (current().pending.phase === 'sending') current().pending.phase = 'unknown';
  }
  queryGeneration++; clearTimeout(queryTimer); queryTimer = null;
  queryController?.abort(); queryController = null;
}

function startQuery(delay = 0) {
  const session = current();
  if (pageSuspended || !session?.pending || !['unknown', 'sending'].includes(session.pending.phase) ||
      session.pending.queryStopped || session.preparation !== 'ready' || queryController || queryTimer) return;
  const generation = queryGeneration;
  if (delay) queryTimer = setTimeout(() => { queryTimer = null; querySubmission(session, session.pending, generation); }, delay);
  else querySubmission(session, session.pending, generation);
}

async function querySubmission(session, pending, generation) {
  if (!pending || current() !== session || generation !== queryGeneration || session.pending !== pending) return;
  const controller = new AbortController(); queryController = controller;
  const previouslyUncertain = !!pending.uncertain;
  let again = true;
  try {
    const {data, status} = await request(endpoint(session, '/turns'), {method: 'POST', signal: controller.signal, body: {
      submission_id: pending.submissionId, text: pending.text, reply_to_seqs: pending.replies, attachments: pending.attachments,
    }});
    if (current() !== session || generation !== queryGeneration || session.pending !== pending) return;
    pending.uncertain = true;
    if (data?.submission_id !== pending.submissionId) throw new Error('发送结果的编号不匹配');
    if (status === 202 && data.status === 'processing') {
      pending.phase = 'unknown'; pending.detail = '后端正在处理，正在等待结果…';
    } else if ([200, 201].includes(status) && data.status === 'committed' &&
        data.turn_ref?.role === 'user' && data.turn_ref.turn_id === `user:${pending.submissionId}`) {
      confirmSend(session, pending); again = false;
    } else throw new Error('发送响应格式不正确');
  } catch (error) {
    if (current() !== session || generation !== queryGeneration || session.pending !== pending) return;
    if (error.status === 409 && error.code === 'submission_out_of_range') {
      if (previouslyUncertain) {
        beginOldBatchCheck(session, pending); pending.detail = '正在读取消息记录，确认这次发送结果…';
        if (session.sync !== 'stopped') historyLoop.kick();
      } else { pending.submissionId = null; pending.phase = 'allocating'; }
      requirePreparation(session);
      again = false;
    } else if (error.status === 409 && error.code === 'attachment_unavailable') {
      // The backend checks the current batch's submission state before looking up attachments.
      for (const file of session.files) { file.status = 'error'; file.error = '文件需要重新上传'; }
      rejectSend(session, pending, '文件上传记录已失效，请重新上传后发送。'); again = false;
    } else if (temporary(error)) {
      pending.uncertain = true; pending.phase = 'unknown'; pending.detail = '发送结果未确认，正在重试确认…';
    } else if (error.status >= 400 && error.status < 500 && !previouslyUncertain) {
      rejectSend(session, pending, message(error)); again = false;
    } else {
      pending.uncertain = true; pending.phase = 'unknown'; pending.detail = message(error); pending.queryStopped = true; again = false;
    }
  } finally {
    if (generation === queryGeneration) {
      queryController = null; scheduleRender();
      if (again && session.pending === pending && current() === session) startQuery(1000);
    }
  }
}

function rejectSend(session, pending, reason) {
  if (!pending || session.pending !== pending) return;
  session.pending = null; session.notice = `${reason} 草稿已保留，可以修改后重新发送。`;
  if (current() === session) stopQuery();
  scheduleRender();
}

function confirmSend(session, pending) {
  if (!pending || session.pending !== pending) return;
  session.pending = null; session.text = ''; session.replies = []; session.files = [];
  session.notice = '消息已发送';
  if (current() === session) stopQuery();
  scheduleRender();
}

function stopPreparation() {
  preparationGeneration++; clearTimeout(preparationTimer); preparationTimer = null;
  preparationController?.abort(); preparationController = null;
}

function requirePreparation(session) {
  session.inventory = null; session.preparation = 'waiting'; session.preparationError = '';
  if (current() === session) { stopQuery(); stopPreparation(); prepareSession(); }
  scheduleRender();
}

async function prepareSession() {
  const session = current();
  if (!session || pageSuspended || !loggedIn || preparationController || preparationTimer ||
      (session.preparation === 'ready' && session.inventory && session.inventory.next <= session.inventory.end)) return;
  const generation = preparationGeneration;
  const controller = new AbortController(); preparationController = controller;
  if (session.preparation !== 'ready') session.preparation = 'loading';
  session.preparationError = ''; scheduleRender();
  let retry = false;
  try {
    const {data} = await request(endpoint(session, '/submissions'), {method: 'POST', body: {count: '100'}, signal: controller.signal});
    if (current() !== session || generation !== preparationGeneration) return;
    setInventory(session, data); session.preparation = 'ready';
    if (session.pending?.phase === 'allocating') allocatePending(session);
    else startQuery();
  } catch (error) {
    if (current() !== session || generation !== preparationGeneration) return;
    retry = temporary(error) && error.code !== 'submission_id_exhausted';
    if (session.preparation !== 'ready') session.preparation = retry ? 'loading' : 'failed';
    session.preparationError = retry ? '等待初始化，正在重试…' : message(error);
    if (!retry && session.pending?.phase === 'allocating' && !session.pending.uncertain) {
      rejectSend(session, session.pending, message(error));
    }
  } finally {
    if (generation === preparationGeneration) {
      preparationController = null; scheduleRender();
      if (retry && current() === session && !pageSuspended) preparationTimer = setTimeout(() => {
        preparationTimer = null; prepareSession();
      }, 1000);
    }
  }
}

function allocatePending(session) {
  const id = takeSubmission(session);
  if (id === null) { prepareSession(); return; }
  session.pending.submissionId = id; session.pending.phase = 'sending'; startQuery();
}

function sendMessage() {
  const session = current();
  if (!session || session.preparation !== 'ready' || session.pending || session.files.some(file => file.status !== 'ready')) return;
  session.pending = {phase: 'allocating', submissionId: null, text: session.text, replies: session.replies.slice(),
    attachments: session.files.map(file => file.objectId), uncertain: false};
  session.notice = ''; allocatePending(session); scheduleRender();
}

async function upload(session, item) {
  if (!valid(session) || session.preparation !== 'ready' || session.pending || !session.files.includes(item) || item.status === 'uploading') return;
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
    item.status = 'error'; item.error = error.code === 'timeout' ? '上传结果未确认，可以重新上传' : message(error);
    if (error.code === 'session_not_initialized') requirePreparation(session);
  } finally {
    if (valid(session, generation) && session.files.includes(item) && item.controller === controller) { item.controller = null; scheduleRender(); }
  }
}

function metadata(session, reference, retry = false) {
  if (retry && session.preparation !== 'ready') return;
  return session.fileInfo.ensure(reference, async ref => {
    const {data} = await request(endpoint(session, `/attachments/${ref.object_id}/metadata?event_seq=${ref.event_seq}`));
    return data;
  }, retry);
}

async function download(session, reference) {
  if (session.preparation !== 'ready') return;
  const key = `${reference.object_id}:${reference.event_seq}`;
  if (session.downloads.has(key)) return;
  session.downloads.add(key); scheduleRender();
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
  } finally { if (valid(session, generation)) { session.downloads.delete(key); scheduleRender(); } }
}

function finishCancellation(session, turn) {
  session.canceling.delete(turn.key);
  session.notice = turn.state === 'cancelled' ? '输出已取消。' : '输出已结束。';
}

async function cancelTurn(session, turn) {
  if (session.preparation !== 'ready') return;
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
      if (error.code === 'session_not_initialized') requirePreparation(session);
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
  if (session.title) return session.title;
  for (const key of session.order) {
    const turn = session.turns.get(key);
    if (turn.role === 'user') {
      const text = textPrefix(turn.parts, 35, true);
      if (text) return session.title = text;
    }
  }
  return session.title = `会话 ${session.id.slice(-6)}`;
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
  const downloading = session.downloads.has(`${ref.object_id}:${ref.event_seq}`);
  if (view.info === info) {
    if (view.retry) view.retry.disabled = session.preparation !== 'ready';
    view.download.disabled = session.preparation !== 'ready' || downloading;
    setText(view.download, downloading ? '正在下载…' : '下载');
    return;
  }
  view.info = info;
  const body = node('div', 'attachment-info');
  body.append(node('strong', '', info?.state === 'ready' ? info.name || '附件' : '附件'));
  body.append(node('span', 'muted', info?.state === 'ready' ? `${formatSize(info.nbytes)} · ${info.type}` : info?.state === 'unavailable' ? '文件信息暂不可用' : '正在读取文件信息…'));
  if (info?.description) body.append(node('span', 'muted', info.description));
  view.element.replaceChildren(node('span', 'file-icon', '↧'), body);
  view.retry = info?.state === 'unavailable' ? button('重试信息', () => metadata(session, ref, true)) : null;
  if (view.retry) { view.retry.disabled = session.preparation !== 'ready'; view.element.append(view.retry); }
  view.download = button(downloading ? '正在下载…' : '下载', () => download(session, ref));
  view.download.disabled = session.preparation !== 'ready' || downloading;
  view.element.append(view.download);
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

function addReference(index, id, key) {
  if (!index.has(id)) index.set(id, new Set());
  index.get(id).add(key);
}

function removeReference(index, id, key) {
  const keys = index.get(id);
  keys?.delete(key);
  if (keys?.size === 0) index.delete(id);
}

function createTurnView(session, turn) {
  const article = node('article', `turn ${turn.role}`); article.id = `message-${turn.creationSeq}`;
  const header = node('div', 'turn-heading');
  const state = node('span', 'turn-state');
  header.append(node('span', `avatar ${turn.role}`, turn.role === 'user' ? '你' : '↗'), node('strong', '', turn.role === 'user' ? '你' : 'Agent'), state);
  const reply = button('回复', () => {
    if (session.preparation !== 'ready' || session.pending) return;
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
    if (view.turn && view.turn.contentVersion !== turn.contentVersion) {
      view.text.data = ''; view.partCount = 0;
      for (const attachment of view.attachments) {
        attachment.element.remove();
        removeReference(renderedMessages.byObject, attachment.ref.object_id, turn.key);
      }
      view.attachments = [];
    }
    // Appends grow existing nodes; reset starts a new content generation in place.
    if (view.partCount < turn.parts.length) {
      view.text.appendData(turn.parts.slice(view.partCount).map(part => part.text).join(''));
      view.partCount = turn.parts.length;
    }
    setText(view.state, turn.state === 'open' ? '正在输出' : turn.state === 'cancelled' ? '已取消' : '');
    view.state.hidden = turn.state === 'completed';
    while (view.attachments.length < turn.attachments.length) {
      const attachment = {ref: turn.attachments[view.attachments.length], element: node('div', 'attachment'), info: null};
      view.attachments.push(attachment); view.article.append(attachment.element);
      addReference(renderedMessages.byObject, attachment.ref.object_id, turn.key);
    }
    view.turn = turn;
  }
  const pending = !!session.pending || session.preparation !== 'ready';
  setText(view.reply, selected ? '已选回复' : '回复');
  view.reply.className = `quiet reply-action${selected ? ' selected' : ''}`;
  view.reply.disabled = pending; view.reply.setAttribute('aria-pressed', String(selected));
  if (view.cancel) {
    const status = session.canceling.get(turn.key);
    view.cancel.hidden = turn.state !== 'open';
    setText(view.cancel, status === 'sending' ? '正在取消…' : status === 'committed' ? '等待取消结果' : status === 'unknown' ? '重试取消' : '停止输出');
    view.cancel.disabled = session.preparation !== 'ready' || status === 'sending' || status === 'committed';
  }
  for (const quote of view.quotes) {
    const target = session.turns.get(session.creationIndex.get(quote.seq));
    if (quote.target !== target) { quote.target = target; setText(quote.element, snippet(target)); }
  }
  for (const attachment of view.attachments) updateAttachment(session, attachment);
}

function renderTurns(session) {
  const container = $('messages');
  const switching = renderedMessages?.session !== session;
  if (switching && renderedMessages?.session) renderedMessages.session.scrollTop = container.scrollTop;
  if (switching || !renderedMessages) {
    container.replaceChildren();
    renderedMessages = {session, views: new Map(), byReply: new Map(), byObject: new Map(), dirty: new Set(session?.order),
      selected: new Set(), canceling: new Map(), downloads: new Set(), empty: false};
  }
  const viewState = renderedMessages;
  const selected = new Set(session?.replies);
  if (session) {
    // Only changes to all controls require visiting all messages.
    if (viewState.pending !== !!session.pending || viewState.preparation !== session.preparation) {
      for (const key of viewState.views.keys()) viewState.dirty.add(key);
    }
    for (const seq of new Set([...viewState.selected, ...selected])) {
      if (viewState.selected.has(seq) !== selected.has(seq)) viewState.dirty.add(session.creationIndex.get(seq));
    }
    for (const key of new Set([...viewState.canceling.keys(), ...session.canceling.keys()])) {
      if (viewState.canceling.get(key) !== session.canceling.get(key)) viewState.dirty.add(key);
    }
    for (const key of new Set([...viewState.downloads, ...session.downloads])) {
      if (viewState.downloads.has(key) !== session.downloads.has(key)) {
        for (const owner of viewState.byObject.get(key.split(':')[0]) || []) viewState.dirty.add(owner);
      }
    }
    viewState.pending = !!session.pending; viewState.preparation = session.preparation;
    viewState.selected = selected; viewState.canceling = new Map(session.canceling); viewState.downloads = new Set(session.downloads);
  }
  if (!switching && !viewState.dirty.size && (viewState.empty || viewState.views.size)) return;
  const atBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 100;
  const oldTop = container.scrollTop;
  if (!session || !session.order.length) {
    if (!renderedMessages.empty) {
      const empty = node('div', 'empty-state');
      empty.append(node('div', 'empty-symbol', '↗'), node('h2', '', session ? '从一条消息开始' : '留一个地方，展开想法'), node('p', 'muted', session ? '写下问题，或添加一个文件。' : '选择已有会话，或创建一个新的对话。'));
      container.replaceChildren(empty); renderedMessages.empty = true;
    }
  } else {
    if (renderedMessages.empty) { container.replaceChildren(); renderedMessages.empty = false; }
    for (const key of viewState.dirty) {
      const turn = session.turns.get(key);
      if (!turn) continue;
      let view = renderedMessages.views.get(key);
      if (!view) {
        view = createTurnView(session, turn); renderedMessages.views.set(key, view); container.append(view.article);
        for (const seq of turn.replies) addReference(viewState.byReply, seq, key);
      }
      updateTurnView(session, view, turn, selected.has(turn.creationSeq));
    }
  }
  viewState.dirty.clear();
  if (switching) container.scrollTop = session?.scrollTop ?? container.scrollHeight;
  else if (atBottom) container.scrollTop = container.scrollHeight;
  else container.scrollTop = oldTop;
}

function renderComposer(session) {
  const disabled = !session || session.preparation !== 'ready' || !!session.pending;
  $('message-input').disabled = disabled; $('choose-files').disabled = disabled;
  $('file-input').disabled = disabled;
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
    view.retry.hidden = item.status !== 'error';
    view.retry.disabled = disabled;
    view.remove.disabled = disabled; renderedComposer.files.set(item, view);
  }
  updateChildren($('draft-files'), [...renderedComposer.files.values()].map(view => view.row));
  let status = session?.notice || '';
  if (session?.pending) {
    const pending = session.pending;
    status = pending.phase === 'allocating' ? '正在准备发送…' : pending.phase === 'sending' ? '正在发送…' : pending.detail || '发送结果未确认…';
  }
  $('submission-status').textContent = status; $('submission-status').hidden = !status;
  if (session && (session.preparation !== 'ready' || session.preparationError)) {
    $('submission-status').hidden = false;
    $('submission-status').append(node('div', '', session.preparationError || '会话初始化中…'));
    if (session.preparation === 'failed' || (session.preparation === 'ready' && session.preparationError)) {
      $('submission-status').append(button('重试初始化', prepareSession));
    }
  }
  if (session?.pending?.queryStopped && session.preparation === 'ready') {
    $('submission-status').append(button('重试确认', () => { session.pending.queryStopped = false; startQuery(); }));
  }
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
$('message-input').addEventListener('input', event => {
  const session = current();
  if (session?.preparation === 'ready' && !session.pending) session.text = event.target.value;
});
$('message-input').addEventListener('keydown', event => {
  if (event.key === 'Enter' && (event.ctrlKey || event.metaKey) && !event.isComposing) { event.preventDefault(); sendMessage(); }
});
$('choose-files').addEventListener('click', () => $('file-input').click());
$('file-input').addEventListener('change', event => {
  const session = current(); if (!session || session.preparation !== 'ready' || session.pending) return;
  for (const file of event.target.files) { const item = {file, objectId: null, status: 'waiting'}; session.files.push(item); upload(session, item); }
  event.target.value = ''; scheduleRender();
});
window.addEventListener('pagehide', () => { pageSuspended = true; historyLoop.stop(); stopQuery(); stopPreparation(); for (const controller of requests) controller.abort(); });
window.addEventListener('pageshow', event => { pageSuspended = false; if (event.persisted && loggedIn && current()) activate(activeId); });

const token = cookieToken();
if (token) { writeToken(token); loggedIn = true; render(); loadSessions(); }
else render();
