import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from '../application/frontend/node_modules/typescript/lib/typescript.js';
import * as noticeModel from '../application/frontend/src/runtimeNotices.ts';
import {
  apiReadAllowed, dismissRuntimeNotice, DISMISSED_RUNTIME_NOTICES_KEY,
  parseRuntimeState, readDismissedNotices, runtimeNotices,
} from '../application/frontend/src/runtimeNotices.ts';

const ready = {status: 'ready', api_read: {allowed: true, reason: 'ready'}, collection: {allowed: true}, user_work: {allowed: true}, time: {state: 'trusted'}};
const notices = value => runtimeNotices(parseRuntimeState({...ready, ...value}));
assert.deepEqual(notices({}), []);
for (const status of ['disabled', 'absent', 'removed']) {
  assert.deepEqual(notices({update_copy: {status, expires_at: null}}), []);
}
assert.match(notices({update_copy: {status: 'current', expires_at: 1791550000}})[0].text, /temporary database copy/);
assert.match(notices({update_copy: {status: 'error', expires_at: null}})[0].text, /copy needs attention/);
for (const invalid of [null, {status: 'untrusted', expires_at: null}, {status: 'current', expires_at: 'never'},
  {status: 'current', expires_at: Number.MAX_SAFE_INTEGER + 1}, {status: 'current', expires_at: -1}]) {
  assert.deepEqual(notices({update_copy: invalid}), []);
}
assert.equal(apiReadAllowed(parseRuntimeState({...ready, update_copy: {status: 'error', expires_at: null}})), true,
  'Copy cleanup warnings do not change database admission.');
assert.equal(apiReadAllowed(parseRuntimeState({...ready, status: 'blocked'})), true, 'The API switch, not the legacy label, controls display.');
assert.equal(apiReadAllowed(parseRuntimeState({...ready, api_read: {allowed: false}})), false);

const cleaner = notices({maintenance_failed: true});
assert.equal(cleaner.length, 1);
assert.match(cleaner[0].text, /retry automatically; expired data stays hidden/);
assert.doesNotMatch(cleaner[0].text, /stopped|paused|cannot|must recover/);
assert.equal(cleaner[0].dismissible, undefined, 'A continuing failure cannot be dismissed as resolved.');
assert.match(notices({time: {state: 'unverified'}})[0].text, /still working/);
const notChecked = notices({time: {state: 'unverified', reason: 'not_checked_yet'}})[0];
assert.equal(notChecked.tone, 'info');
assert.match(notChecked.text, /has not been checked yet/);
assert.doesNotMatch(notChecked.text, /could not|retry/);
for (const reason of ['check_failed', 'check_expired', 'clock_jumped', '<script>private</script>']) {
  const notice = notices({time: {state: 'unverified', reason}})[0];
  assert.equal(notice.tone, 'warning');
  assert.match(notice.text, /could not be verified/);
  assert.doesNotMatch(notice.text, /private|script/);
}
assert.equal(apiReadAllowed(parseRuntimeState({...ready, time: {state: 'unverified', reason: 'not_checked_yet'}})), true);
assert.deepEqual(notices({time: {status: 'mismatch'}}), [], 'Only time.state is the status contract.');
const blocked = {api_read: {allowed: false, reason: 'startup_cleanup'}};
assert.match(notices(blocked)[0].text, /Removing expired YouTube data/);
assert.match(notices(blocked)[0].text, /collections, backups and settings are still available/);
assert.doesNotMatch(notices({...blocked, user_work: {allowed: false}})[0].text, /still available/);
for (const [reason, expected] of [
  ['time_mismatch', /server clock/], ['clock_changed', /server clock/],
  ['schema_invalid', /database needs attention/], ['database_unavailable', /Reconnecting/],
  ['<script>server payload</script>', /temporarily unavailable/],
]) {
  const result = notices({api_read: {allowed: false, reason}})[0];
  assert.match(result.text, expected);
  assert.doesNotMatch(result.text, /script|server payload/);
}
assert.match(notices({collection: {allowed: false, reason: 'collection_priority'}})[0].text, /resume automatically/);

const id = 'a'.repeat(32);
const expired = {id, records: 1250, complete: true};
assert.deepEqual(notices({startup_notice: {...expired, complete: false}}), []);
assert.equal(notices({startup_notice: expired})[0].dismissible, true);
assert.match(notices({startup_notice: expired})[0].text, /1,250 stored records expired/);
assert.doesNotMatch(notices({startup_notice: expired})[0].text, /videos/);
assert.match(notices({startup_notice: {...expired, records: 1}})[0].text, /1 stored record expired.*was removed/);
for (const invalid of [
  {...expired, id: 'title'}, {...expired, id: '<'.repeat(32)}, {...expired, records: -1},
  {...expired, records: 1.5}, {...expired, records: Number.MAX_SAFE_INTEGER + 1}, {...expired, complete: 'true'},
]) assert.deepEqual(notices({startup_notice: invalid}), []);

const values = new Map();
const storage = {getItem: key => values.get(key) || null, setItem: (key, value) => values.set(key, value)};
const dismissed = dismissRuntimeNotice(id, [], storage);
assert.deepEqual(readDismissedNotices(storage), [id]);
assert.deepEqual(runtimeNotices(parseRuntimeState({...ready, startup_notice: expired}), dismissed), []);
assert.deepEqual([...values.keys()], [DISMISSED_RUNTIME_NOTICES_KEY]);
assert.equal(values.get(DISMISSED_RUNTIME_NOTICES_KEY), JSON.stringify([id]), 'Only a local notice ID is persisted.');
const next = 'b'.repeat(32);
assert.equal(runtimeNotices(parseRuntimeState({...ready, startup_notice: {...expired, id: next}}), dismissed).length, 1);
assert.deepEqual(dismissRuntimeNotice('bad', dismissed, storage), dismissed);
assert.deepEqual(readDismissedNotices({getItem: () => '{broken'}), []);
assert.deepEqual(readDismissedNotices({getItem() {throw new Error('disabled');}}), []);
assert.deepEqual(dismissRuntimeNotice(next, dismissed, {setItem() {throw new Error('full');}}), [id, next]);
storage.setItem(DISMISSED_RUNTIME_NOTICES_KEY, JSON.stringify([id, id, 'video title', 2, next]));
assert.deepEqual(readDismissedNotices(storage), [id, next]);
let many = [];
for (let i = 0; i < 30; i++) many = dismissRuntimeNotice(i.toString(16).padStart(32, '0'), many, storage);
assert.equal(many.length, 20);
assert.equal(readDismissedNotices(storage).length, 20);
for (const invalid of [null, [], '', {status: 'running'}]) assert.throws(() => parseRuntimeState(invalid));

// Local sidebar fallback uses its own route and exposes only local identity/organization fields.
{
  const compile = path => ts.transpileModule(readFileSync(new URL(path, import.meta.url), 'utf8'), {
    compilerOptions: {target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS},
  }).outputText;
  const contracts = {};
  new Function('require', 'exports', compile('../application/frontend/src/explorer/contracts.ts'))(
    name => {throw new Error(`Unexpected contract dependency ${name}`);}, contracts);
  const imported = {
    './contracts': contracts,
    '../sourceLifetime.ts': {sourceLifetimeParser: parser => parser, captureSourceLifetime: payload => payload},
    '../serverText.ts': {serverErrorMessage: () => 'Unavailable'},
  };
  const result = {}, calls = [];
  const channelId = 'UC' + 'a'.repeat(22);
  const local = {channels: [{channel_id: channelId, folder_id: 'folder1', label_keys: ['mine'], added_at: '2026-10-15T12:00:00Z'}],
    folders: [{id: 'folder1', name: 'My folder'}], labels: [{label_key: 'mine', name: 'My label', label_type: 'custom'}]};
  // Additional fields from a future endpoint change cannot accidentally turn this fallback into an API cache.
  const supplied = structuredClone(local); supplied.channels[0].title = 'API title'; supplied.channels[0].video_count = 700;
  new Function('require', 'exports', 'window', 'fetch', compile('../application/frontend/src/explorer/api.ts'))(
    name => imported[name] ?? {}, result, {setTimeout: () => 1, clearTimeout() {}},
    async (path, options) => {calls.push({path, options}); return {ok: true, headers: new Headers(), json: async () => supplied};});
  assert.deepEqual(await result.loadExplorerUserSidebar(), local);
  assert.deepEqual(calls.map(call => call.path), ['/sidebar-user-data']);
  assert.equal(calls[0].options.cache, 'no-store');
}

// Exercise the mounted notice component's real polling and buttons without a browser or network.
{
  const compile = (path, imports) => {
    const {outputText} = ts.transpileModule(readFileSync(new URL(path, import.meta.url), 'utf8'), {
      compilerOptions: {target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS},
    });
    const result = {};
    new Function('require', 'exports', outputText)(name => {assert.ok(name in imports, name); return imports[name];}, result);
    return result;
  };
  const work = compile('../application/frontend/src/explorer/savedWork.ts', {});
  const collectionId = '44444444-4444-4444-8444-444444444444', at = '2026-10-09T10:00:00Z';
  const data = {contract_version: 1, collection: {id: collectionId, kind: 'collection', name: 'Mine', revision: 2,
    member_count: 2, created_at: at, updated_at: at}, members: [{video_id: 'a'.repeat(11), added_at: at}], next_cursor: 'next'};
  let failure = Object.assign(new Error('blocked'), {status: 503, code: 'hot_runtime_not_ready'});
  const calls = [];
  const collections = compile('../application/frontend/src/explorer/savedCollections.ts', {
    './api': {requestJson: async path => {calls.push(path); if (path.includes('/members?')) throw failure; return data;}},
    '../explorerRoutes': {EXPLORER_V3_ROOT: '/app-explorer-v3'}, './savedWork': work, './contracts': {},
  });
  const first = await collections.loadCollectionMembers(collectionId);
  assert.equal(first.source_read_at, null);
  assert.equal(first.members[0].reason, 'api_paused');
  assert.equal(first.members[0].video, null);
  assert.equal(first.members[0].video_id, 'a'.repeat(11));
  assert.deepEqual(calls, [`/saved/collections/${collectionId}/members?limit=50`, `/saved/collections/${collectionId}/member-identities?limit=50`]);
  const second = collections.parseMemberIdentitiesPage({...data, members: [{video_id: 'b'.repeat(11), added_at: at}], next_cursor: null}, collectionId, 'next');
  assert.equal(collections.mergeCollectionMembers(first, second).members.length, 2);
  const available = {...first, members: [{...first.members[0], status: 'available', channel_id: 'UC' + 'c'.repeat(22), video: {title: 'Source'}, reason: null}]};
  const stripped = collections.withoutCollectionSources(available);
  assert.equal(stripped.members[0].video, null);
  assert.equal(stripped.members[0].video_id, 'a'.repeat(11));
  assert.equal(collections.collectionSourceDelay(stripped), null);
  for (const members of [[{video_id: 'bad', added_at: at}], [{video_id: 'a'.repeat(11), added_at: at, title: 'API'}], Array(51).fill(data.members[0])]) {
    assert.throws(() => collections.parseMemberIdentitiesPage({...data, members}, collectionId));
  }
  assert.throws(() => collections.parseMemberIdentitiesPage({...data, next_cursor: null}, collectionId), /incomplete/);
  assert.throws(() => collections.parseMemberIdentitiesPage({...data, members: [data.members[0], data.members[0]]}, collectionId), /invalid/);
  const command = {request_id: '55555555-5555-4555-8555-555555555555', expected_revision: 2, video_ids: ['a'.repeat(11)]};
  const receipt = collections.parseCollectionMutation({contract_version: 1, collection: {...data.collection, revision: 3, member_count: 1},
    mutation: {request_id: command.request_id, replayed: false, requested_count: 1, changed_count: 1, applied_revision: 3}}, collectionId, command);
  assert.equal(receipt.collection.member_count, 1, 'The same local revision permits removal while enrichment is blocked.');
  for (const error of [new Error('network'), Object.assign(new Error('auth'), {status: 401}), Object.assign(new Error('other'), {status: 503, code: 'different'})]) {
    failure = error; const before = calls.length;
    await assert.rejects(() => collections.loadCollectionMembers(collectionId), error);
    assert.equal(calls.length, before + 1, 'Unrelated failures are not silently converted into identity mode.');
  }
}

// Exercise the mounted notice component's real polling and buttons without a browser or network.
{
  const source = readFileSync(new URL('../application/frontend/src/components/RuntimeNotices.tsx', import.meta.url), 'utf8');
  const {outputText} = ts.transpileModule(source, {compilerOptions: {
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
  }});
  const cells = [], effects = [], timers = new Map(), events = new Map(), observed = [];
  let cursor = 0, timerId = 0, payload = ready, failure = false, calls = 0, reloads = 0;
  const hooks = {
    useState(initial) {const at = cursor++; if (!(at in cells)) cells[at] = typeof initial === 'function' ? initial() : initial;
      return [cells[at], value => {cells[at] = typeof value === 'function' ? value(cells[at]) : value;}];},
    useRef(initial) {const at = cursor++; if (!(at in cells)) cells[at] = {current: initial}; return cells[at];},
    useEffect(effect) {const at = cursor++; if (!(at in cells)) {cells[at] = true; effects.push(effect);}},
  };
  const document = {hidden: false, addEventListener: (name, fn) => events.set(name, fn), removeEventListener: name => events.delete(name)};
  const window = {localStorage: storage, location: {reload: () => reloads++}, addEventListener: document.addEventListener, removeEventListener: document.removeEventListener};
  const imports = {react: hooks, '../runtimeNotices': noticeModel, './runtimeNotices.css': {},
    'react/jsx-runtime': {jsx: (type, props) => ({type, props}), jsxs: (type, props) => ({type, props})}};
  const exports = {};
  const requests = [];
  const fetch = async (path, options) => {
    calls++; requests.push({path, options});
    if (failure) throw new Error('connection lost');
    return {ok: true, json: async () => payload};
  };
  new Function('require', 'exports', 'document', 'window', 'fetch', 'setTimeout', 'clearTimeout', outputText)(
    name => {assert.ok(name in imports, name); return imports[name];}, exports, document, window, fetch,
    (fn, ms) => {const id = ++timerId; timers.set(id, {fn, ms}); return id;}, id => timers.delete(id));
  const render = () => {cursor = 0; return exports.RuntimeNotices({onChange: state => observed.push(state)});};
  const flatten = node => !node ? [] : Array.isArray(node) ? node.flatMap(flatten)
    : typeof node !== 'object' ? [node] : [node, ...flatten(node.props?.children)];
  const texts = tree => flatten(tree).filter(node => typeof node === 'string').join(' ');
  const flush = () => new Promise(resolve => setImmediate(resolve));
  const poll = async () => {const entry = [...timers].find(([, timer]) => timer.ms === 5000); assert.ok(entry, 'Next bounded poll exists.'); timers.delete(entry[0]); entry[1].fn(); await flush();};
  assert.equal(render(), null);
  const stop = effects[0](); await flush();
  assert.equal(render(), null);
  assert.equal(calls, 1);
  assert.equal(requests[0].path, '/system/hot-runtime');
  assert.equal(requests[0].options.cache, 'no-store');
  assert.equal(requests[0].options.credentials, 'same-origin');
  payload = {...ready, ...blocked}; await poll();
  assert.match(texts(render()), /Removing expired YouTube data/);
  payload = {...ready, startup_notice: expired}; await poll();
  let tree = render();
  assert.match(texts(tree), /available again/);
  assert.match(texts(tree), /1,250 stored records/);
  assert.equal(reloads, 0, 'Recovery must not destroy an unsaved form through automatic navigation.');
  failure = true; await poll();
  tree = render();
  assert.match(texts(tree), /status is unavailable/);
  assert.match(texts(tree), /1,250 stored records/, 'Completed notice remains until dismissed even during an outage.');
  assert.doesNotMatch(texts(tree), /available again/, 'Do not claim current readiness from stale state.');
  const dismiss = flatten(tree).find(node => node.type === 'button' && node.props.children === 'Dismiss');
  dismiss.props.onClick();
  assert.doesNotMatch(texts(render()), /1,250 stored records/);
  failure = false; await poll(); tree = render();
  flatten(tree).find(node => node.type === 'button' && node.props.children === 'Reload data').props.onClick();
  assert.equal(reloads, 1);
  document.hidden = true; const before = calls; await poll(); assert.equal(calls, before);
  document.hidden = false; events.get('visibilitychange')(); await flush(); assert.equal(calls, before + 1);
  stop(); assert.equal(timers.size, 0); assert.equal(events.size, 0);
  assert.ok(observed.some(state => state.api_read.allowed === false));
}
console.log('Runtime notices: state, completion, bounded persistence, mounted polling, recovery and dismissal passed.');
