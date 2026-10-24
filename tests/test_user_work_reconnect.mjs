import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import ts from '../application/frontend/node_modules/typescript/lib/typescript.js';

function compiled(path, imports = {}) {
  const {outputText} = ts.transpileModule(readFileSync(new URL(path, import.meta.url), 'utf8'), {
    compilerOptions: {target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX},
  });
  const exports = {};
  new Function('require', 'exports', outputText)(name => {assert.ok(name in imports, name); return imports[name];}, exports);
  return exports;
}
const saved = compiled('../application/frontend/src/explorer/savedWork.ts');
const work = compiled('../application/frontend/src/explorer/userWorkBackup.ts', {
  './savedWork.ts': saved, './catalogueQuery.ts': {},
  './cataloguePreferences.ts': {catalogueDensityPreference: {isValid: () => true}},
  '../state/browserPreferences.ts': {}, './bookmarks.ts': {}, './folderCollapse.ts': {},
});
const api = compiled('../application/frontend/src/explorer/userWorkBackupApi.ts', {
  './userWorkBackup.ts': work, './savedWork.ts': saved, '../serverText.ts': {plainServerText: () => undefined},
});
const id = '11111111-1111-4111-8111-111111111111';
const snapshot = 'reconnect-v1:' + 'a'.repeat(64);
const item = changes => ({id, revision: 1, user_input: '@synthetic', parent_ref: null, reason: 'ready',
  is_hidden: false, reconnect_state: 'idle', reconnect_not_before: null, reconnect_error_code: null, ...changes});
const status = changes => ({available: true, can_restore: false, pending_more: false, pending: [item()],
  channel_mappings: [], user_channel_refs: [id], folder_ids: [],
  reconnect: {snapshot, counts: {idle: 1, queued: 0, running: 0, waiting: 0, blocked: 0}, excluded_hidden: 0}, ...changes});
const receipt = {status: 'queued', snapshot, queued_count: 1, blocked_count: 0, already_requested_count: 0, excluded_hidden: 0};
const response = (value, code = 200) => new Response(JSON.stringify(value), {status: code, headers: {'Content-Type': 'application/json'}});
const flush = async () => {for (let i = 0; i < 6; i++) await new Promise(resolve => setImmediate(resolve));};

test('status and receipt are closed, bounded contracts; hidden and waiting states are explicit', () => {
  assert.equal(work.parseUserWorkStatus(status()).reconnect.snapshot, snapshot);
  for (const state of ['idle', 'queued', 'running', 'waiting', 'blocked']) {
    const row = item({reconnect_state: state, reconnect_not_before: state === 'waiting' ? '2026-10-10T10:00:00Z' : null});
    assert.equal(work.parseUserWorkStatus(status({pending: [row]})).pending[0].reconnect_state, state);
  }
  assert.equal(work.parseUserWorkStatus(status({pending: [item({is_hidden: true})]})).pending[0].is_hidden, true);
  for (const row of [item({reconnect_state: 'waiting'}), item({reconnect_state: 'running', is_hidden: true}),
    item({reconnect_error_code: '<provider payload>'}), item({reconnect_state: 'waiting', reconnect_not_before: 'not-a-time'})]) {
    assert.throws(() => work.parseUserWorkStatus(status({pending: [row]})));
  }
  const bad = structuredClone(status()); delete bad.reconnect;
  assert.throws(() => work.parseUserWorkStatus(bad));
  assert.throws(() => work.parseUserWorkStatus(status({reconnect: {...bad, snapshot}})));
  assert.deepEqual(work.parseReconnectReceipt(receipt), receipt);
  for (const invalid of [{status: 'resolved', id, channel_id: 'UC'+'a'.repeat(22)}, {...receipt, completed: 1},
    {...receipt, queued_count: 10001}, {...receipt, queued_count: 9999, excluded_hidden: 2}, {...receipt, snapshot: 'capability'}]) {
    assert.throws(() => work.parseReconnectReceipt(invalid));
  }
});

test('bulk and individual send one explicit POST, exact snapshot/revision, and require 202', async () => {
  const old = globalThis.fetch, calls = [];
  try {
    globalThis.fetch = async (path, options) => {calls.push([path, options]); return response(receipt, 202);};
    await api.reconnectAllUserChannels(snapshot);
    await api.resolvePendingUserChannel(item({revision: 9}));
    assert.deepEqual(calls.map(([path, options]) => [path, options.method, JSON.parse(options.body)]), [
      ['/user-work/pending/reconnect', 'POST', {expected_snapshot: snapshot}],
      [`/user-work/pending/${id}/resolve`, 'POST', {expected_revision: 9}],
    ]);
    assert.ok(calls.every(([, options]) => options.credentials === 'same-origin' && options.headers['X-Saved-Work'] === '1'));
    globalThis.fetch = async () => response(receipt, 200);
    await assert.rejects(api.reconnectAllUserChannels(snapshot), /not confirmed/);
    const before = calls.length;
    await assert.rejects(api.reconnectAllUserChannels('invalid'));
    assert.equal(calls.length, before);
  } finally {globalThis.fetch = old;}
});

test('network error and unknown commit never cause an API-level POST retry', async () => {
  const old = globalThis.fetch;
  try {
    for (const mode of ['network', 'unknown']) {
      let calls = 0;
      globalThis.fetch = async () => {calls++; if (mode === 'network') throw new TypeError('lost acknowledgement');
        return response({code: 'database_write_outcome_unknown', outcome_unknown: true}, 503);};
      await assert.rejects(api.reconnectAllUserChannels(snapshot));
      assert.equal(calls, 1);
    }
  } finally {globalThis.fetch = old;}
});

async function withTimers(action) {
  const originals = {setTimeout, clearTimeout, fetch};
  const tasks = new Map(); let next = 1;
  globalThis.setTimeout = (fn, delay) => {const id = next++; tasks.set(id, {fn, delay}); return id;};
  globalThis.clearTimeout = id => tasks.delete(id);
  const fire = async () => {
    const task = [...tasks].find(([, value]) => value.delay === 5000); assert.ok(task, 'one scheduled poll');
    tasks.delete(task[0]); task[1].fn(); await flush();
  };
  try {await action({tasks, fire});} finally {Object.assign(globalThis, originals);}
}

test('polling is GET-only, has one in-flight read and cancels on dialog cleanup', async () => {
  await withTimers(async ({tasks, fire}) => {
    const calls = []; let complete;
    globalThis.fetch = (path, options) => {calls.push([path, options]); return new Promise(resolve => {complete = resolve;});};
    let updates = 0;
    const stop = api.pollUserWorkStatus({pendingOffset: 100, onStatus: () => updates++, onStopped: assert.fail, isBusy: () => false});
    await fire();
    assert.equal(calls.length, 1); assert.equal(calls[0][1].method, 'GET'); assert.match(calls[0][0], /pending_offset=100/);
    assert.equal([...tasks.values()].filter(task => task.delay === 5000).length, 0);
    stop(); assert.equal(calls[0][1].signal.aborted, true);
    complete(response(status())); await flush();
    assert.equal(updates, 0); assert.equal(tasks.size, 0);
  });
});

test('poll stops after three failures or no active work; busy work does not add another read', async () => {
  await withTimers(async ({tasks, fire}) => {
    let reads = 0, stopped = 0, busy = true;
    globalThis.fetch = async () => {reads++; throw new TypeError('offline');};
    const stop = api.pollUserWorkStatus({pendingOffset: 0, onStatus: assert.fail, onStopped: () => stopped++, isBusy: () => busy});
    await fire(); assert.equal(reads, 0); busy = false;
    await fire(); await fire(); await fire();
    assert.equal(reads, 3); assert.equal(stopped, 1); assert.equal(tasks.size, 0); stop();
    globalThis.fetch = async () => response(status());
    let updates = 0;
    api.pollUserWorkStatus({pendingOffset: 0, onStatus: () => updates++, onStopped: assert.fail, isBusy: () => false});
    await fire(); assert.equal(updates, 1); assert.equal(tasks.size, 0);
  });
});

function hooks() {
  const values = [], effects = []; let index = 0, pending = [];
  return {values,
    api: {useState(initial) {const i = index++; if (!(i in values)) values[i] = typeof initial === 'function' ? initial() : initial;
      return [values[i], value => {values[i] = typeof value === 'function' ? value(values[i]) : value;}];},
      useRef(initial) {const i = index++; return values[i] ??= {current: initial};},
      useCallback(fn) {index++; return fn;},
      useEffect(fn, deps) {const i = index++; const before = effects[i];
        if (!before || deps.some((value, n) => value !== before.deps[n])) pending.push(() => {before?.cleanup?.(); effects[i] = {deps, cleanup: fn()};});},
    },
    render(fn) {index = 0; const result = fn(); for (const effect of pending) effect(); pending = []; return result;},
    cleanup() {for (const effect of effects) effect?.cleanup?.();},
  };
}
const nodes = value => Array.isArray(value) ? value.flatMap(nodes) : value && typeof value === 'object' ? [value, ...nodes(value.children)] : [];
const text = value => typeof value === 'string' || typeof value === 'number' ? String(value) : Array.isArray(value) ? value.map(text).join(' ')
  : value && typeof value === 'object' ? text(value.children) : '';
const button = (tree, label) => nodes(tree).find(node => node.type === 'button' && text(node) === label);
function dialog(initialStatus, overrides = {}) {
  const state = hooks(), calls = [], jsx = (type, props) => ({type, ...props});
  const apiStub = {...api, pollUserWorkStatus: () => {calls.push('poll'); return () => calls.push('stop-poll');},
    reconnectAllUserChannels: async (...args) => {calls.push(['POST bulk', ...args]); return receipt;},
    resolvePendingUserChannel: async (...args) => {calls.push(['POST single', ...args]); return receipt;},
    loadUserWorkStatus: async (...args) => {calls.push(['GET', ...args]); return initialStatus;},
    previewUserWorkRestore: async () => ({can_restore: true, counts: {}, backup: {}}),
    restoreUserWorkBackup: async () => ({status: 'restored', pending_count: 1, browser_preferences: {}}), ...overrides};
  const component = compiled('../application/frontend/src/explorer/UserWorkBackupDialog.tsx', {
    react: state.api, 'react/jsx-runtime': {jsx, jsxs: jsx, Fragment: 'fragment'}, './userWorkBackupApi': apiStub,
    './userWorkBackup': {...work, readUserWorkFile: async () => ({}), installUserWorkPreferences: () => true}, './user-work-backup.css': {},
  });
  const props = {initialStatus, onClose: () => calls.push('close'), onStatus() {}, onPreferencesChanged() {}};
  return {...state, calls, render: () => state.render(() => component.UserWorkBackupDialog(props))};
}

test('dialog bulk uses whole snapshot once on double click; lost ACK reads authoritative status', async () => {
  const oldDocument = globalThis.document; globalThis.document = {activeElement: null};
  try {
    let posted = 0, read = 0;
    const h = dialog(status({pending_more: true}), {
      reconnectAllUserChannels: async token => {assert.equal(token, snapshot); posted++; throw new TypeError('lost acknowledgement');},
      loadUserWorkStatus: async () => {read++; return status({pending: [item({reconnect_state: 'queued'})]});},
    });
    const tree = h.render();
    assert.match(text(tree), /across every page/); assert.match(text(tree), /YouTube API requests and quota/);
    button(tree, 'Reconnect all').onClick(); button(tree, 'Reconnect all').onClick(); await flush();
    assert.equal(posted, 1); assert.equal(read, 1);
    assert.match(text(h.render()), /Status was refreshed/); h.cleanup();
  } finally {globalThis.document = oldDocument;}
});

test('dialog exposes editable restored rows; queue, parent, blocked and hidden controls stay distinct', async () => {
  const oldDocument = globalThis.document; globalThis.document = {activeElement: null};
  try {
    const h = dialog(status()); let tree = h.render();
    const fileInput = nodes(tree).find(node => node.accept === 'application/json,.json');
    fileInput.onChange({target: {files: [{}]}}); await flush();
    button(h.render(), 'Restore user work').onClick(); await flush();
    tree = h.render();
    const rowNode = nodes(tree).find(node => typeof node.type === 'function');
    assert.equal(rowNode.busy, false, 'successful restore leaves row controls enabled');
    assert.match(text(tree), /Choose Reconnect all/);
    const row = value => {
      // Keep parent hooks separate from the subsequent child-row hooks.
      h.render();
      return rowNode.type({...rowNode, item: item(value), onDraftChange() {}});
    };
    assert.match(text(row({reconnect_state: 'queued', reason: 'parent_pending'})), /waiting for its parent/);
    assert.equal(button(row({reconnect_state: 'running'}), 'Reconnect channel').disabled, true);
    assert.equal(button(row({is_hidden: true}), 'Reconnect channel').disabled, true);
    assert.match(text(row({reconnect_state: 'blocked', reconnect_error_code: 'provider_key_rejected'})), /YouTube API key/);
    assert.ok(button(row({reconnect_state: 'blocked'}), 'Retry reconnection'));
    h.cleanup();
  } finally {globalThis.document = oldDocument;}
});

test('closing the dialog stops local polling without another queue mutation', () => {
  const oldDocument = globalThis.document; globalThis.document = {activeElement: null};
  try {
    const initial = status(); initial.reconnect.counts.idle = 0; initial.reconnect.counts.queued = 1;
    const h = dialog(initial), tree = h.render();
    assert.equal(h.calls.filter(value => value === 'poll').length, 1);
    nodes(tree).find(node => node['aria-label'] === 'Close backup dialog').onClick();
    h.cleanup();
    assert.ok(h.calls.includes('close')); assert.ok(h.calls.includes('stop-poll'));
    assert.equal(h.calls.filter(Array.isArray).length, 0);
  } finally {globalThis.document = oldDocument;}
});
console.log('F4 reconnect: strict receipts, explicit mutations, bounded reads and dialog states checked.');
