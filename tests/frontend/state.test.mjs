import test from 'node:test';
import assert from 'node:assert/strict';
import {
  ActiveLoop, FileInfoStore, MAX_UINT64, applyPage, beginOldBatchCheck, createSession,
  decimal, inspectSubmission, setInventory, takeSubmission, turnKey,
} from '../../src/openevent/chat_app/static/state.mjs';

const SESSION = '01ARZ3NDEKTSV4RRFFQ69G5FAV';
function session(start = '1') { return createSession({session_id: SESSION, scan_start_seq: start}); }
function event(seq, kind, fields = {}, role = 'agent', attachments = []) {
  return {event_seq: String(seq), ts_ms: '1788336000000', publisher_role: role, recipients: [], attachments,
    payload: {kind, ...fields}};
}
function create(seq, id = 'a', kind = 'turn.start', role = 'agent', text = '开头') {
  return event(seq, kind, {turn_id: id, content: [{type: 'text', text}], reply_to_seqs: []}, role);
}
function page(events, next, last = BigInt(next) - 1n) { return {events, next_seq: String(next), last_seq: String(last)}; }
function apply(state, response) { applyPage(state, response); return state; }
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}
function flush() { return new Promise(resolve => setImmediate(resolve)); }
function scheduler() {
  let id = 0;
  const jobs = new Map();
  return {jobs, timer: (fn, delay) => { jobs.set(++id, {fn, delay}); return id; }, clearTimer: key => jobs.delete(key),
    run: () => { const [key, job] = jobs.entries().next().value; jobs.delete(key); job.fn(); }};
}
const metadata = id => ({object_id: id, name: '报告.txt', type: 'text/plain', description: '', nbytes: 15});

test('canonical decimal identifiers retain all uint64 bits', () => {
  assert.equal(decimal('9007199254740993'), '9007199254740993');
  assert.equal(decimal(MAX_UINT64.toString()), MAX_UINT64.toString());
  for (const bad of [1, '', '01', '-1', '0', '1.0', (MAX_UINT64 + 1n).toString()]) assert.throws(() => decimal(bad));
  assert.equal(decimal('0', {zero: true}), '0');
});

test('one page commits records and cursor atomically, keeping previous objects untouched', () => {
  const state = apply(session(), page([create(1)], 2));
  const original = state.turns.get(turnKey('agent', 'a'));
  const oldMap = state.turns;
  const response = page([
    event(3, 'turn.append', {turn_id: 'a', pre_seq: '1', content: [{type: 'text', text: '第二段'}]}),
    event(4, 'turn.append', {turn_id: 'missing', pre_seq: '2', content: [{type: 'text', text: '坏数据'}]}),
  ], 5);
  assert.throws(() => apply(state, response));
  assert.equal(state.fetchSeq, '2'); assert.equal(state.turns, oldMap);
  assert.equal(original.parts.map(part => part.text).join(''), '开头');
  apply(state, page(response.events.slice(0, 1), 4));
  assert.equal(state.fetchSeq, '4'); assert.notEqual(state.turns.get(original.key), original);
  assert.equal(original.parts.length, 1);
});

test('global sequence gaps, control-only pages, and unchanged empty tail are legal', () => {
  const state = session('9007199254740993');
  apply(state, page([event('9007199254740998', 'submission.reserve', {reserved_through: '10000'}, 'user')], '9007199254741001', '9007199254741100'));
  assert.equal(state.turns.size, 0); assert.equal(state.creationIndex.size, 0);
  assert.equal(state.fetchSeq, '9007199254741001');
  apply(state, page([], '9007199254741101', '9007199254741100'));
  apply(state, page([], '9007199254741101', '9007199254741100'));
  assert.equal(state.fetchSeq, '9007199254741101');
});

test('reply indexes use creation seq; append attachments keep their carrying event seq and duplicates', () => {
  const state = session('9007199254740993');
  const first = create('9007199254740993', 'user:1', 'turn.single', 'user');
  const reply = create('9007199254740995'); reply.payload.reply_to_seqs = [first.event_seq];
  apply(state, page([first, reply], '9007199254740996'));
  apply(state, page([event('9007199254740998', 'turn.append', {
    turn_id: 'a', pre_seq: '9007199254740995', content: [{type: 'text', text: '文件'}],
  }, 'agent', [{object_id: '9007199254741000'}, {object_id: '9007199254741000'}])], '9007199254740999'));
  const turn = state.turns.get(turnKey('agent', 'a'));
  assert.equal(turn.creationSeq, '9007199254740995'); assert.deepEqual(turn.replies, ['9007199254740993']);
  assert.deepEqual(turn.attachments, [
    {object_id: '9007199254741000', event_seq: '9007199254740998'},
    {object_id: '9007199254741000', event_seq: '9007199254740998'},
  ]);
  assert.equal(state.creationIndex.get('9007199254740995'), turn.key);
  assert.deepEqual(state.order, [turnKey('user', 'user:1'), turnKey('agent', 'a')]);
});

test('the first terminal event wins and later events do not change content or files', () => {
  const state = apply(session(), page([create(1)], 2));
  apply(state, page([
    event(3, 'turn.cancel', {target_turn: {role: 'agent', turn_id: 'a'}}, 'user'),
    event(4, 'turn.append', {turn_id: 'a', pre_seq: '1', content: [{type: 'text', text: '迟到'}]}, 'agent', [{object_id: '9'}]),
    event(5, 'turn.end', {turn_id: 'a', pre_seq: '4'}),
  ], 6));
  const turn = state.turns.get(turnKey('agent', 'a'));
  assert.equal(turn.state, 'cancelled'); assert.equal(turn.parts.length, 1);
  assert.deepEqual(turn.attachments, []);
});

test('roles have independent turn ids and completed single turns stay completed', () => {
  const state = apply(session(), page([
    create(1, 'same', 'turn.single', 'user'), create(2, 'same'),
    event(3, 'turn.cancel', {target_turn: {role: 'user', turn_id: 'same'}}, 'agent'),
    event(4, 'turn.end', {turn_id: 'same', pre_seq: '2'}),
  ], 5));
  assert.equal(state.turns.size, 2);
  assert.equal(state.turns.get(turnKey('user', 'same')).state, 'completed');
  assert.equal(state.turns.get(turnKey('agent', 'same')).state, 'completed');
});

test('large legal content arrays append without a function argument limit', () => {
  const state = apply(session(), page([create(1)], 2));
  const content = Array.from({length: 150000}, () => ({type: 'text', text: 'a'}));
  apply(state, page([event(2, 'turn.append', {turn_id: 'a', pre_seq: '1', content})], 3));
  const turn = state.turns.get(turnKey('agent', 'a'));
  assert.equal(turn.parts.length, 150001);
  assert.equal(turn.parts.map(part => part.text).join(''), `开头${'a'.repeat(150000)}`);
  assert.equal(state.fetchSeq, '3');
});

test('page commits reuse session indexes and unchanged turns without leaking malformed creations', () => {
  const state = apply(session(), page([create(1), create(2, 'b')], 3));
  const {turns, order, creationIndex} = state;
  const untouched = turns.get(turnKey('agent', 'b'));
  const append = event(3, 'turn.append', {turn_id: 'a', pre_seq: '1', content: [{type: 'text', text: '追加'}]});
  apply(state, page([append], 4));
  assert.equal(state.turns, turns); assert.equal(state.order, order); assert.equal(state.creationIndex, creationIndex);
  assert.equal(state.turns.get(untouched.key), untouched);
  const revision = state.revision;
  assert.throws(() => apply(state, page([create(4, 'c'), create(5, 'c')], 6)));
  assert.equal(state.fetchSeq, '4'); assert.equal(state.revision, revision);
  assert.deepEqual(state.order, [turnKey('agent', 'a'), turnKey('agent', 'b')]);
  assert.equal(state.creationIndex.has('4'), false); assert.equal(state.turns.has(turnKey('agent', 'c')), false);
});

test('malformed page ordering and numeric fields cannot advance a cursor', () => {
  const state = session();
  for (const response of [page([create(3), create(2, 'b')], 4), page([create(3)], 3), page([], 5, 3), {events: [], next_seq: 1, last_seq: '0'}]) {
    assert.throws(() => apply(state, response)); assert.equal(state.fetchSeq, '1'); assert.equal(state.turns.size, 0);
  }
});

test('inventory consumes exact integers once and reaches uint64 exhaustion without wraparound', () => {
  const state = session();
  setInventory(state, {start: (MAX_UINT64 - 2n).toString(), end: MAX_UINT64.toString()}, '3');
  assert.equal(takeSubmission(state), (MAX_UINT64 - 2n).toString());
  assert.equal(takeSubmission(state), (MAX_UINT64 - 1n).toString());
  assert.equal(takeSubmission(state), MAX_UINT64.toString()); assert.equal(takeSubmission(state), null);
  assert.throws(() => setInventory(state, {start: '1', end: '10'}, '100'));
});

test('old-batch confirmation ignores requests started before rejection and fixes its first fresh watermark', () => {
  const state = session(); state.historySerial = 5;
  const pending = {submissionId: '7', phase: 'unknown'};
  beginOldBatchCheck(state, pending);
  apply(state, page([], 20, 19));
  assert.equal(inspectSubmission(state, pending, page([], 20, 19), 5), 'unknown');
  assert.equal(pending.barrier, null);
  apply(state, page([], 25, 100));
  assert.equal(inspectSubmission(state, pending, page([], 25, 100), 6), 'unknown');
  assert.equal(pending.barrier, '100');
  apply(state, page([], 101, 500));
  assert.equal(inspectSubmission(state, pending, page([], 101, 500), 7), 'absent');
  assert.equal(pending.barrier, '100');
});

test('a current batch absence stays unknown, and a loaded user turn confirms without reaching the tail', () => {
  const state = session(); const pending = {submissionId: '9007199254740993', phase: 'unknown'};
  apply(state, page([], 10, 9));
  assert.equal(inspectSubmission(state, pending, page([], 10, 9), 1), 'unknown');
  apply(state, page([create(10, 'user:9007199254740993', 'turn.single', 'user')], 11, 100));
  assert.equal(inspectSubmission(state, pending), 'committed');
});

test('file information requests are shared, failures require a manual retry, and uploaded info avoids a request', async () => {
  const store = new FileInfoStore(); const ref = {object_id: '42', event_seq: '7'};
  let calls = 0; const first = deferred();
  const loader = () => { calls++; return first.promise; };
  const one = store.ensure(ref, loader); const two = store.ensure({...ref, event_seq: '8'}, loader);
  assert.equal(one, two); await flush(); assert.equal(calls, 1);
  first.reject(new Error('暂时失败')); await one;
  assert.equal(store.get('42').state, 'unavailable');
  await store.ensure(ref, loader); assert.equal(calls, 1);
  await store.ensure(ref, () => { calls++; return metadata('42'); }, true);
  assert.equal(calls, 2); assert.equal(store.get('42').state, 'ready');
  store.uploaded(metadata('43'));
  await store.ensure({object_id: '43', event_seq: '9'}, () => { throw new Error('must not read'); });
  assert.equal(store.get('43').state, 'ready');
});

test('clearing a file map discards late requests, and a later upload wins over older metadata', async () => {
  const store = new FileInfoStore(); const pending = deferred();
  const first = store.ensure({object_id: '42', event_seq: '7'}, () => pending.promise);
  store.clear(); pending.resolve(metadata('42')); await first;
  assert.equal(store.entries.size, 0);
  const older = deferred(); const second = store.ensure({object_id: '42', event_seq: '7'}, () => older.promise);
  store.uploaded({...metadata('42'), name: '上传时的名称'});
  older.resolve(metadata('42')); await second;
  assert.equal(store.get('42').name, '上传时的名称');
});

test('file maps stay independent across sessions and malformed metadata cannot become ready', async () => {
  const one = new FileInfoStore(); const two = new FileInfoStore(); one.uploaded(metadata('42'));
  assert.equal(two.get('42'), undefined);
  await two.ensure({object_id: '42', event_seq: '7'}, () => metadata('43'));
  assert.equal(two.get('42').state, 'unavailable'); assert.equal(one.get('42').state, 'ready');
});

test('A → B → A activations discard both earlier responses, even if abort is ignored', async () => {
  const time = scheduler(); const reads = []; const applied = [];
  const loop = new ActiveLoop({...time, read: state => { const result = deferred(); reads.push({state, ...result}); return result.promise; },
    apply: (state, response) => applied.push([state.id, response.next_seq]), error: () => false});
  const a = session(); const b = session(); b.id = 'another';
  loop.activate(a); loop.activate(b); loop.activate(a);
  reads[0].resolve(page([], 2)); reads[1].resolve(page([], 3)); await flush();
  assert.deepEqual(applied, []); assert.equal(time.jobs.size, 0);
  reads[2].resolve(page([], 4)); await flush();
  assert.deepEqual(applied, [[a.id, '4']]); assert.equal(time.jobs.size, 1);
  loop.stop(); assert.equal(time.jobs.size, 0);
});

test('polling has one in-flight request, follows returned cursors, and delays only at the tail', async () => {
  const time = scheduler(); const reads = [];
  const state = session();
  const loop = new ActiveLoop({...time,
    read: current => { const result = deferred(); reads.push({from: current.fetchSeq, ...result}); return result.promise; },
    apply: (current, response) => apply(current, response), error: () => false});
  loop.activate(state); loop.kick(); loop.kick(); assert.equal(reads.length, 1);
  reads[0].resolve(page([], 20, 100)); await flush();
  assert.equal([...time.jobs.values()][0].delay, 0); time.run(); assert.equal(reads[1].from, '20');
  reads[1].resolve(page([], 101, 100)); await flush();
  assert.equal([...time.jobs.values()][0].delay, 1000); time.run(); assert.equal(reads[2].from, '101');
  loop.stop(); reads[2].resolve(page([], 102, 101)); await flush();
  assert.equal(state.fetchSeq, '101'); assert.equal(time.jobs.size, 0);
});

test('a failed page leaves state untouched and a protocol failure stops polling', async () => {
  const time = scheduler(); const state = session(); let failure;
  const loop = new ActiveLoop({...time, read: async () => page([create(1), create(2, 'a')], 3),
    apply: (current, response) => apply(current, response), error: (_, error) => { failure = error; return false; }});
  loop.activate(state); await flush();
  assert.ok(failure); assert.equal(state.fetchSeq, '1'); assert.equal(state.turns.size, 0); assert.equal(time.jobs.size, 0);
  loop.stop();
});

test('default timers retain the browser global receiver instead of binding to the loop', async () => {
  const originalSet = globalThis.setTimeout; const originalClear = globalThis.clearTimeout;
  let scheduled = 0;
  globalThis.setTimeout = function () { assert.equal(this, globalThis); scheduled++; return 1; };
  globalThis.clearTimeout = function () { assert.equal(this, globalThis); };
  const loop = new ActiveLoop({read: async () => page([], 1, 0), apply: () => {}, error: () => false});
  try {
    loop.activate(session()); await flush(); assert.equal(scheduled, 1); loop.stop();
  } finally { globalThis.setTimeout = originalSet; globalThis.clearTimeout = originalClear; }
});
