export const MAX_UINT64 = (1n << 64n) - 1n;
export const MAX_FILE_BYTES = 4 * 1024 * 1024;

export function decimal(value, {zero = false} = {}) {
  if (typeof value !== 'string' || !/^(0|[1-9][0-9]*)$/.test(value)) throw new Error('Invalid ID format');
  const number = BigInt(value);
  if ((!zero && number === 0n) || number > MAX_UINT64) throw new Error('ID is out of range');
  return value;
}

export function turnKey(role, turnId) {
  if (!['user', 'agent'].includes(role) || typeof turnId !== 'string' || !turnId) throw new Error('Invalid message identity');
  return JSON.stringify([role, turnId]);
}

function list(value, name) {
  if (!Array.isArray(value)) throw new Error(`Invalid ${name} format`);
  return value;
}

function parts(value) {
  return list(value, 'message content').map(part => {
    if (!part || part.type !== 'text' || typeof part.text !== 'string' || !part.text) throw new Error('Invalid message content format');
    return {type: 'text', text: part.text};
  });
}

export function fileInfo(value, expectedId = value?.object_id) {
  decimal(value?.object_id);
  if (value.object_id !== expectedId || typeof value.name !== 'string' || typeof value.type !== 'string' ||
      typeof value.description !== 'string' || !Number.isInteger(value.nbytes) || value.nbytes < 1 || value.nbytes > MAX_FILE_BYTES) {
    throw new Error('Invalid file info');
  }
  return {name: value.name, type: value.type, description: value.description, nbytes: value.nbytes, state: 'ready'};
}

export function sessionName(value) {
  // Match the server's Unicode whitespace rules, including the four separators.
  if (typeof value !== 'string' || /^[\p{White_Space}\u001c-\u001f]*$/u.test(value)) throw new Error('Session name cannot be blank');
  if (/[\uD800-\uDFFF]/u.test(value)) throw new Error('Session name contains invalid characters');
  return value;
}

export function createSession(description) {
  if (!description || typeof description.session_id !== 'string' || !/^[0-7][0-9A-HJKMNP-TV-Z]{25}$/.test(description.session_id)) {
    throw new Error('Invalid session ID');
  }
  return {
    id: description.session_id, name: sessionName(description.name), channelId: decimal(description.channel_id),
    start: decimal(description.scan_start_seq), fetchSeq: description.scan_start_seq,
    turns: new Map(), order: [], creationIndex: new Map(), fileInfo: new FileInfoStore(), historySerial: 0,
    inventory: null, pending: null, preparation: 'waiting', preparationError: '', text: '', replies: [], files: [], error: '', notice: '', sync: 'waiting',
  };
}

// Stage only this page's changes. A malformed page never mutates records or the cursor.
export function applyPage(session, page, requestedSeq = session.fetchSeq) {
  const next = BigInt(decimal(page?.next_seq));
  const last = BigInt(decimal(page?.last_seq, {zero: true}));
  const from = BigInt(decimal(requestedSeq));
  if (next > last + 1n) throw new Error('Invalid message page cursor');
  const events = list(page.events, 'message page');
  const turns = new Map();
  const additions = new Map();
  const order = [];
  const creationIndex = new Map();
  let previous = null;
  for (const event of events) {
    const seq = BigInt(decimal(event?.event_seq));
    decimal(event.ts_ms, {zero: true});
    if (seq < from || seq > last || seq >= next || (previous !== null && seq <= previous)) throw new Error('Invalid message page order or range');
    previous = seq;
    if (!['user', 'agent'].includes(event.publisher_role)) throw new Error('Invalid message identity');
    for (const recipient of list(event.recipients, 'message recipients')) {
      if (!['user', 'agent'].includes(recipient)) throw new Error('Invalid message recipient');
    }
    const attachments = list(event.attachments, 'attachments').map(item => ({object_id: decimal(item?.object_id), event_seq: event.event_seq}));
    const payload = event.payload;
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) throw new Error('Invalid message payload');
    if (payload.kind === 'submission.reserve') {
      decimal(payload.reserved_through);
      if (attachments.length) throw new Error('Submission reservations cannot contain attachments');
      continue;
    }
    if (payload.kind === 'turn.single' || payload.kind === 'turn.start') {
      const key = turnKey(event.publisher_role, payload.turn_id);
      if (turns.has(key) || session.turns.has(key) || creationIndex.has(event.event_seq) || session.creationIndex.has(event.event_seq)) throw new Error('Duplicate message creation event');
      const replyTo = list(payload.reply_to_seqs, 'reply references').map(seq => decimal(seq));
      if (new Set(replyTo).size !== replyTo.length) throw new Error('Duplicate reply reference');
      const content = parts(payload.content);
      turns.set(key, {
        key, role: event.publisher_role, id: payload.turn_id, creationSeq: event.event_seq,
        replies: replyTo, tailSeq: event.event_seq, contentVersion: 0,
        parts: content, attachments, state: payload.kind === 'turn.single' ? 'completed' : 'open',
      });
      order.push(key);
      creationIndex.set(event.event_seq, key);
      continue;
    }
    let key;
    if (payload.kind === 'turn.cancel') key = turnKey(payload.target_turn?.role, payload.target_turn?.turn_id);
    else if (['turn.append', 'turn.reset', 'turn.end'].includes(payload.kind)) {
      key = turnKey(event.publisher_role, payload.turn_id);
      decimal(payload.pre_seq);
    } else throw new Error('Unknown message type');
    const original = turns.get(key) ?? session.turns.get(key);
    if (!original) throw new Error('Missing message creation event');
    const content = ['turn.append', 'turn.reset'].includes(payload.kind) ? parts(payload.content) : [];
    if (payload.kind === 'turn.append' && !content.length && !attachments.length) throw new Error('Message append has no content or attachments');
    if (original.state !== 'open') continue;
    if (payload.kind !== 'turn.cancel' && payload.pre_seq !== original.tailSeq) throw new Error('Invalid previous message reference');
    let turn;
    if (payload.kind === 'turn.reset') {
      turn = {...original, parts: content, attachments, contentVersion: original.contentVersion + 1};
      additions.delete(key);
    } else {
      turn = turns.get(key);
      if (!turn) {
        turn = {...original};
        additions.set(key, {parts: [], attachments: []});
      }
      // Grow shared arrays only after the whole page has passed validation.
      // New and reset turns already own their arrays and can grow while staging.
      const target = additions.get(key) ?? turn;
      if (payload.kind === 'turn.append') {
        for (const part of content) target.parts.push(part);
      } else {
        turn.state = payload.kind === 'turn.cancel' ? 'cancelled' : 'completed';
      }
      for (const attachment of attachments) target.attachments.push(attachment);
    }
    if (payload.kind !== 'turn.cancel') turn.tailSeq = event.event_seq;
    turns.set(key, turn);
  }
  for (const [key, turn] of turns) {
    const added = additions.get(key);
    if (added) {
      for (const part of added.parts) turn.parts.push(part);
      for (const attachment of added.attachments) turn.attachments.push(attachment);
    }
    session.turns.set(key, turn);
  }
  for (const key of order) session.order.push(key);
  for (const [seq, key] of creationIndex) session.creationIndex.set(seq, key);
  session.fetchSeq = page.next_seq;
  return turns;
}

export function setInventory(session, result, count = '100') {
  const start = BigInt(decimal(result?.start));
  const end = BigInt(decimal(result?.end));
  if (end - start + 1n !== BigInt(decimal(count))) throw new Error('Incorrect submission ID allocation size');
  session.inventory = {next: start, end};
}

export function takeSubmission(session) {
  if (!session.inventory || session.inventory.next > session.inventory.end) return null;
  return (session.inventory.next++).toString();
}

export function hasSubmission(session, submissionId) {
  return session.turns.has(turnKey('user', `user:${decimal(submissionId)}`));
}

export function beginOldBatchCheck(session, pending) {
  pending.phase = 'checking';
  pending.minimumHistorySerial = session.historySerial + 1;
  pending.barrier = null;
}

export function inspectSubmission(session, pending, page = null, requestSerial = null) {
  if (!pending?.submissionId) return 'unknown';
  if (hasSubmission(session, pending.submissionId)) return 'committed';
  if (pending.phase !== 'checking') return 'unknown';
  if (pending.barrier === null && page && requestSerial >= pending.minimumHistorySerial) pending.barrier = decimal(page.last_seq, {zero: true});
  if (pending.barrier !== null && BigInt(session.fetchSeq) > BigInt(pending.barrier)) return 'absent';
  return 'unknown';
}

export class FileInfoStore {
  constructor(onChange = () => {}) { this.entries = new Map(); this.generation = 0; this.onChange = onChange; }
  clear() { this.generation++; this.entries.clear(); this.onChange(); }
  uploaded(result) {
    const info = fileInfo(result);
    this.entries.set(result.object_id, info);
    this.onChange(result.object_id);
    return info;
  }
  get(objectId) { return this.entries.get(objectId); }
  ensure(reference, load, retry = false) {
    const id = decimal(reference.object_id);
    const existing = this.entries.get(id);
    if (existing && (existing.state !== 'unavailable' || !retry)) return existing.promise ?? Promise.resolve(existing);
    const entry = {state: 'loading'};
    const generation = this.generation;
    this.entries.set(id, entry);
    entry.promise = Promise.resolve().then(() => load(reference)).then(result => {
      const info = fileInfo(result, id);
      if (generation === this.generation && this.entries.get(id) === entry) {
        this.entries.set(id, info);
        this.onChange(id);
      }
      return info;
    }).catch(error => {
      if (generation === this.generation && this.entries.get(id) === entry) {
        this.entries.set(id, {state: 'unavailable', error: error.message});
        this.onChange(id);
      }
      return null;
    });
    this.onChange(id);
    return entry.promise;
  }
}

// Each activation owns its request, timer, and generation, including A → B → A.
export class ActiveLoop {
  constructor({read, apply, error, delay = 1000,
    timer = (callback, wait) => globalThis.setTimeout(callback, wait),
    clearTimer = id => globalThis.clearTimeout(id)}) {
    Object.assign(this, {read, apply, error, delay, timer, clearTimer});
    this.generation = 0; this.current = null; this.timerId = null; this.controller = null; this.kicked = false;
  }
  stop() {
    this.generation++;
    this.clearTimer(this.timerId); this.timerId = null;
    this.controller?.abort(); this.controller = null; this.current = null; this.kicked = false;
  }
  activate(session) { this.stop(); this.current = session; this.run(session, this.generation); }
  kick() {
    if (!this.current) return;
    if (this.controller) { this.kicked = true; return; }
    this.clearTimer(this.timerId); this.timerId = null;
    this.run(this.current, this.generation);
  }
  async run(session, generation) {
    if (this.current !== session || this.generation !== generation) return;
    const controller = new AbortController(); this.controller = controller;
    const serial = ++session.historySerial;
    let delay = this.delay;
    let proceed = true;
    try {
      const page = await this.read(session, controller.signal, serial);
      if (this.current !== session || this.generation !== generation) return;
      await this.apply(session, page, serial);
      delay = BigInt(page.next_seq) <= BigInt(page.last_seq) ? 0 : this.delay;
    } catch (error) {
      if (this.current !== session || this.generation !== generation) return;
      proceed = this.error(session, error) !== false;
    } finally {
      if (this.current === session && this.generation === generation) {
        this.controller = null;
        if (proceed) {
          if (this.kicked) delay = 0;
          this.kicked = false;
          this.timerId = this.timer(() => { this.timerId = null; this.run(session, generation); }, delay);
        }
      }
    }
  }
}
