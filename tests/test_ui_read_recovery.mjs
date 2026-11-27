import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from '../application/frontend/node_modules/typescript/lib/typescript.js';
import * as lifetime from '../application/frontend/src/sourceLifetime.ts';
import { homeSummaryIncomplete, systemSummaryIncomplete } from '../application/frontend/src/explorer/summaryRecovery.ts';

const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return {promise, resolve, reject}; };
assert.equal(homeSummaryIncomplete({saved_videos: null}), true);
assert.equal(homeSummaryIncomplete({saved_videos: {total: 0}}), false);
for (const field of [{recent_runs_read_status: 'unavailable'}, {queue_health: null},
  {video_type_reviews: {available: false}}, {quota: {read_status: 'unavailable'}}]) {
  assert.equal(systemSummaryIncomplete(field), true);
}
for (const value of [{}, {latest_run: null, recent_runs: [], recent_runs_read_status: 'ok'},
  {status: 'warning', warnings: [{code: 'recorded_failure'}]}, {status: 'paused'}, {quota: {read_status: 'partial'}},
  {queue_health: {}, video_type_reviews: {available: true}, quota: {read_status: 'ok', used: 0}}]) {
  assert.equal(systemSummaryIncomplete(value), false);
}
function compile(relative, imports = {}, globals = {}) {
  const source = readFileSync(new URL(relative, import.meta.url), 'utf8');
  const {outputText} = ts.transpileModule(source, {compilerOptions: {
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
  }});
  const exports = {};
  new Function('require', 'exports', ...Object.keys(globals), outputText)(
    name => { assert.ok(name in imports, name); return imports[name]; }, exports, ...Object.values(globals));
  return exports;
}
function clock() {
  let id = 0, now = 0;
  const jobs = new Map();
  return {
    setTimeout(fn, delay) { const key = ++id; jobs.set(key, {fn, at: now + delay}); return key; },
    clearTimeout(key) { jobs.delete(key); },
    async advance(ms) {
      const end = now + ms;
      while (true) {
        const next = [...jobs].sort((a, b) => a[1].at - b[1].at)[0];
        if (!next || next[1].at > end) break;
        now = next[1].at; jobs.delete(next[0]); next[1].fn(); await flush();
      }
      now = end; await flush();
    },
    get size() { return jobs.size; },
  };
}
function readerFixture() {
  const time = clock(), calls = [], received = [], failed = [];
  let visible = true;
  const module = compile('../application/frontend/src/explorer/visibleRead.ts', {}, time);
  const reader = module.createVisibleRead({
    visible: () => visible, started() {},
    load: signal => { const next = deferred(); calls.push({...next, signal}); return next.promise; },
    received: value => received.push(value), failed: error => failed.push(error.message),
    incomplete: value => value.saved_videos === null,
  });
  return {time, calls, received, failed, reader, visible(value) { visible = value; reader.visibilityChanged(); }};
}

// Null inventory is a successful partial response, not a terminal loading state.
{
  const f = readerFixture(); f.reader.refresh(); await flush();
  f.calls[0].resolve({saved_videos: null}); await flush();
  assert.equal(f.received.length, 1);
  await f.time.advance(4999); assert.equal(f.calls.length, 1);
  await f.time.advance(1); assert.equal(f.calls.length, 2);
  f.calls[1].resolve({saved_videos: {total: 0}}); await flush();
  await f.time.advance(120_000);
  assert.equal(f.calls.length, 2, 'a confirmed zero completes recovery');
  assert.equal(f.time.size, 0);
  f.reader.stop();
}
// One initial attempt plus three retries, then only a new user/visibility event.
{
  const f = readerFixture(); f.reader.refresh(); await flush();
  for (const [index, delay] of [5000, 15000, 30000, 120000].entries()) {
    f.calls[index].reject(new Error('HTTP 500')); await flush(); await f.time.advance(delay);
  }
  assert.equal(f.calls.length, 4);
  assert.equal(f.failed.length, 4);
  f.reader.refresh(); await flush(); assert.equal(f.calls.length, 5);
  f.reader.stop(); await flush(); assert.equal(f.time.size, 0);
}
// Hiding releases ownership immediately; an ignored abort cannot block or replace return.
{
  const f = readerFixture(); f.reader.refresh(); await flush();
  const old = f.calls[0];
  f.visible(false); assert.equal(old.signal.aborted, true);
  await f.time.advance(90_000); assert.equal(f.calls.length, 1);
  f.visible(true); await flush(); assert.equal(f.calls.length, 2);
  old.resolve({saved_videos: {total: 999}}); await flush();
  assert.deepEqual(f.received, []);
  f.calls[1].resolve({saved_videos: {total: 12}}); await flush();
  assert.deepEqual(f.received, [{saved_videos: {total: 12}}]);
  f.reader.stop();
}
// A loader that ignores abort still has a deadline and cannot publish afterward.
{
  const f = readerFixture(); f.reader.refresh(); await flush();
  await f.time.advance(30_000);
  assert.equal(f.calls[0].signal.aborted, true);
  assert.match(f.failed[0], /timed out/);
  await f.time.advance(5000); assert.equal(f.calls.length, 2);
  f.calls[0].resolve({saved_videos: {total: 999}}); await flush();
  assert.deepEqual(f.received, []);
  f.reader.stop(); f.calls[1].reject(new Error('late error')); await flush();
  assert.equal(f.failed.length, 1);
  assert.equal(f.time.size, 0);
}
// A hidden initial mount starts no requests; stopping clears delayed recovery.
{
  const f = readerFixture(); f.visible(false); f.reader.refresh(); await flush();
  assert.equal(f.calls.length, 0);
  f.visible(true); await flush();
  f.calls[0].resolve({saved_videos: null}); await flush();
  f.reader.stop(); await f.time.advance(120_000);
  assert.equal(f.calls.length, 1);
}

// Minimal deterministic React lifecycle: actual component effects, state and callbacks.
function hooks() {
  const slots = [], pending = [];
  let cursor = 0, tree, component;
  const changed = (a, b) => !a || !b || a.length !== b.length || a.some((v, i) => v !== b[i]);
  const api = {
    useState(initial) {
      const i = cursor++;
      if (!(i in slots)) slots[i] = typeof initial === 'function' ? initial() : initial;
      return [slots[i], next => { slots[i] = typeof next === 'function' ? next(slots[i]) : next; }];
    },
    useRef(initial) { const i = cursor++; return slots[i] ??= {current: initial}; },
    useMemo(fn, deps) {
      const i = cursor++, previous = slots[i];
      if (!previous || changed(previous.deps, deps)) slots[i] = {deps, value: fn()};
      return slots[i].value;
    },
    useCallback(fn, deps) { return api.useMemo(() => fn, deps); },
    useEffect(fn, deps) {
      const i = cursor++, previous = slots[i];
      if (!previous || changed(previous.deps, deps)) pending.push(() => {
        previous?.cleanup?.(); slots[i] = {deps, cleanup: fn()};
      });
    },
  };
  return {
    api,
    mount(fn) { component = fn; return this.render(); },
    render() {
      cursor = 0; tree = component();
      for (const node of nodes(tree)) if (node.props?.ref && !node.props.ref.current) {
        node.props.ref.current = node.type === 'dialog'
          ? {open: false, showModal() { this.open = true; }, close() { this.open = false; }}
          : {getBoundingClientRect: () => ({top: 0}), scrollIntoView() {}};
      }
      pending.splice(0).forEach(fn => fn()); return tree;
    },
    stop() { slots.forEach(slot => slot?.cleanup?.()); },
  };
}
function nodes(tree) {
  if (tree == null || typeof tree !== 'object') return [];
  if (Array.isArray(tree)) return tree.flatMap(nodes);
  return [tree, ...nodes(tree.props?.children)];
}
const jsx = (type, props) => ({type, props});
{
  const host = hooks(), time = clock(), calls = [], listeners = new Map();
  let enabled = true;
  const document = {visibilityState: 'visible', addEventListener: (name, fn) => listeners.set(name, fn),
    removeEventListener: name => listeners.delete(name)};
  const visible = compile('../application/frontend/src/explorer/visibleRead.ts', {}, time);
  const summary = compile('../application/frontend/src/explorer/useVisibleSummary.ts', {
    react: host.api, '../sourceLifetime': lifetime, '../sourceLifetimeReact': {useSourceExpiry() {}}, './visibleRead': visible,
  }, {document});
  const load = signal => { const next = deferred(); calls.push({...next, signal}); return next.promise; };
  host.mount(() => summary.useVisibleSummary(load, enabled)); await flush();
  enabled = false; host.render();
  enabled = true; host.render(); await flush();
  assert.equal(calls.length, 2, 'returning to Home starts a new owner immediately');
  calls[0].resolve({old: true}); await flush();
  assert.equal(host.render().state.kind, 'loading');
  const expiredAt = Date.now() - 1000;
  calls[1].resolve(lifetime.captureSourceLifetime({
    source_read_at: new Date(expiredAt - 1000).toISOString(), source_expires_at: new Date(expiredAt).toISOString(),
  }, undefined, performance.now())); await flush();
  const result = host.render();
  assert.equal(result.state.kind, 'error');
  assert.equal(result.state.message, lifetime.SOURCE_EXPIRED_MESSAGE);
  host.stop(); await flush();
  assert.equal(time.size, 0);
  assert.equal(listeners.size, 0);
}
const unusedRequest = () => { throw new Error('The adapter stub owns every Home request.'); };
const homeStub = loaders => ({HOME_PACE_HELP: 'pace help', ...loaders,
  paceRatio: (pace, median) => pace == null || median == null || !(Number(median) > 0) ? undefined : Number(pace) / Number(median)});
{
  const host = hooks(), calls = [], paceCalls = [], intervals = new Set(), listeners = new Map(), observers = [];
  let expiryCallbacks;
  const document = {visibilityState: 'visible', addEventListener: (name, fn) => listeners.set(name, fn),
    removeEventListener: name => listeners.delete(name)};
  const window = {setInterval: fn => { intervals.add(fn); return fn; }, clearInterval: fn => intervals.delete(fn)};
  class IntersectionObserver {
    constructor(callback) { observers.push(callback); }
    observe() {}
    disconnect() {}
  }
  const component = compile('../application/frontend/src/explorer/ExplorerRecentVideos.tsx', {
    react: host.api, 'react/jsx-runtime': {jsx, jsxs: jsx}, 'lucide-react': {},
    '../sourceLifetimeReact': {useSourceExpiry(value, expired, refresh) {
      if (Array.isArray(value)) expiryCallbacks = {expired, refresh};
    }}, '../sourceLifetime': lifetime, '../sourceLifetime.ts': lifetime,
    './api': {isAbortError: error => error.name === 'AbortError', isContractError: () => false, requestJson: unusedRequest},
    './libraryData/home': homeStub({
      loadHomeFeedPage: (request, windowKey, cursor, signal) => {
        assert.equal(request, unusedRequest);
        const next = deferred(); calls.push({...next, windowKey, cursor, signal}); return next.promise;
      },
      loadHomePace: (request, windowKey, signal) => { const next = deferred(); paceCalls.push({...next, windowKey, signal}); return next.promise; },
    }),
    './libraryData/errors': {libraryErrorState: () => ({action: 'error', code: 'unknown'})},
    './format': {formatCount: String, formatTimestamp: String}, './videoPresentation': {safeVideoThumbnailUrl: () => undefined},
  }, {document, window, IntersectionObserver});
  let tree = host.mount(() => component.ExplorerRecentVideos({channels: [], channelPath: id => id}));
  intervals.forEach(fn => fn()); await flush();
  assert.equal(calls.length, 1, 'poll cannot duplicate the initial first page');
  const button = nodes(tree).find(node => node.type === 'button' && node.props.children === '24 hours');
  assert.ok(button); button.props.onClick(); host.render();
  assert.equal(calls.length, 2);
  assert.equal(calls[0].signal.aborted, true);
  const page = title => ({items: [{video_id: title, title, published_at: '2026-10-20T12:00:00Z',
    views: 1, like_count: 0, comment_count: 0}], generated_at: '2026-10-20T12:01:00Z',
    published_from: '2026-10-19T12:00:00Z', next_cursor: 'next-page', has_more: true});
  calls[1].resolve(page('new-window-video')); await flush();
  calls[0].resolve(page('obsolete-window-video')); await flush();
  tree = host.render();
  assert.ok(JSON.stringify(tree).includes('new-window-video'));
  assert.ok(!JSON.stringify(tree).includes('obsolete-window-video'));
  intervals.forEach(fn => fn()); await flush();
  calls[2].reject(new Error('The server could not complete the request (HTTP 500).')); await flush();
  tree = host.render();
  assert.ok(JSON.stringify(tree).includes('HTTP 500'));
  assert.ok(!JSON.stringify(tree).includes(lifetime.SOURCE_EXPIRED_MESSAGE), 'an HTTP failure is not a claimed expiry');
  intervals.forEach(fn => fn()); await flush();
  const hiddenRead = calls[3];
  document.visibilityState = 'hidden'; listeners.get('visibilitychange')(); host.render();
  assert.equal(hiddenRead.signal.aborted, true);
  assert.ok(observers.length > 0);
  observers.forEach(callback => callback([{isIntersecting: true}]));
  intervals.forEach(fn => fn()); await flush();
  assert.equal(calls.length, 4, 'queued pagination observers and polling cannot restart hidden reads');
  document.visibilityState = 'visible'; listeners.get('visibilitychange')(); await flush();
  assert.equal(calls.length, 5, 'returning immediately owns a replacement request');
  calls[4].resolve(page('visible-again-video')); await flush();
  hiddenRead.resolve(page('obsolete-hidden-video')); await flush();
  tree = host.render();
  assert.ok(JSON.stringify(tree).includes('visible-again-video'));
  assert.ok(!JSON.stringify(tree).includes('obsolete-hidden-video'));
  intervals.forEach(fn => fn()); await flush();
  const savedDateNow = Date.now;
  const readAt = savedDateNow(), expiry = readAt + 1000;
  let wallTime = readAt;
  Date.now = () => wallTime;
  try {
    calls[5].resolve(lifetime.captureSourceLifetime({
      ...page('will-expire'), source_read_at: new Date(readAt).toISOString(),
      source_expires_at: new Date(expiry).toISOString(),
    }, undefined, performance.now()));
    await flush(); host.render();
    wallTime = expiry;
    tree = host.render();
    assert.ok(JSON.stringify(tree).includes('Latest videos unavailable'), 'the render guard masks expired rows before effects');
    assert.ok(!JSON.stringify(tree).includes('No stored long-form videos'));
    expiryCallbacks.expired(); expiryCallbacks.refresh(); await flush();
    calls[6].reject(new Error('The server could not complete the request (HTTP 500).')); await flush();
    tree = host.render();
    assert.ok(JSON.stringify(tree).includes('HTTP 500'));
    assert.ok(!JSON.stringify(tree).includes('No stored long-form videos'), 'failed recovery does not turn expiry into a false empty catalogue');
    intervals.forEach(fn => fn()); await flush();
    calls[7].resolve({...page('unused'), items: [], next_cursor: null, has_more: false}); await flush();
    tree = host.render();
    assert.ok(JSON.stringify(tree).includes('No stored long-form videos'), 'a fresh successful empty page remains a legitimate empty result');
    assert.ok(!JSON.stringify(tree).includes('Latest videos unavailable'));
  } finally { Date.now = savedDateNow; }
  intervals.forEach(fn => fn()); await flush();
  const late = calls[8]; host.stop();
  late.reject(new Error('obsolete failure')); await flush();
  assert.equal(intervals.size, 0);
  assert.equal(listeners.size, 0);
}
// Home pace: the ratio is the server's row pace over the server median of the
// whole period; "Load 20 more" does not move it, a structure change asks again.
{
  const host = hooks(), calls = [], paceCalls = [], intervals = new Set(), observers = [];
  const document = {visibilityState: 'visible', addEventListener() {}, removeEventListener() {}};
  const window = {setInterval: fn => { intervals.add(fn); return fn; }, clearInterval: fn => intervals.delete(fn)};
  class IntersectionObserver { constructor(callback) { observers.push(callback); } observe() {} disconnect() {} }
  const component = compile('../application/frontend/src/explorer/ExplorerRecentVideos.tsx', {
    react: host.api, 'react/jsx-runtime': {jsx, jsxs: jsx}, 'lucide-react': {},
    '../sourceLifetimeReact': {useSourceExpiry() {}}, '../sourceLifetime': lifetime, '../sourceLifetime.ts': lifetime,
    './api': {isAbortError: error => error.name === 'AbortError', isContractError: () => false, requestJson: unusedRequest},
    './libraryData/home': homeStub({
      loadHomeFeedPage: (request, windowKey, cursor, signal) => { const next = deferred(); calls.push({...next, windowKey, cursor, signal}); return next.promise; },
      loadHomePace: (request, windowKey, signal) => { const next = deferred(); paceCalls.push({...next, windowKey, signal}); return next.promise; },
    }),
    './libraryData/errors': {libraryErrorState: () => ({action: 'error', code: 'unknown'})},
    './format': {formatCount: String, formatTimestamp: String}, './videoPresentation': {safeVideoThumbnailUrl: () => undefined},
  }, {document, window, IntersectionObserver});
  const later = new Date(Date.now() + 3_600_000).toISOString();
  const video = (id, pace) => ({video_id: id, channel_id: 'UC' + 'A'.repeat(22), title: id, thumbnail_url: null,
    published_at: '2026-10-20T12:00:00Z', checked_at: null, views: '1', like_count: '0', comment_count: '0', views_per_day: pace});
  const feed = (items, more) => ({items, generated_at: '2026-10-20T12:01:00Z', published_from: '2026-10-13T12:01:00Z',
    next_cursor: more ? 'home-key' : null, has_more: more});
  // Function components (the rows) render with their props, like React would.
  const expand = node => node == null || typeof node !== 'object' ? node : Array.isArray(node) ? node.map(expand)
    : typeof node.type === 'function' ? expand(node.type(node.props))
    : {...node, props: node.props && {...node.props, children: expand(node.props.children)}};
  const text = () => JSON.stringify(expand(host.render()));
  host.mount(() => component.ExplorerRecentVideos({channels: [], channelPath: id => id})); await flush();
  assert.equal(calls.length, 1); assert.equal(paceCalls.length, 1, 'the period median is read with the first page');
  assert.equal(paceCalls[0].windowKey, '7d');
  calls[0].resolve(feed([video('first-video', '24000'), video('second-video', null)], true)); await flush();
  assert.ok(text().includes('Not enough data'), 'no ratio before the median');
  paceCalls[0].resolve({median: '12000', valid_before: later}); await flush();
  let tree = text();
  assert.ok(tree.includes('2.0× median'), 'row pace over the period median');
  assert.ok(tree.includes('pace help'));
  observers.forEach(callback => callback([{isIntersecting: true}])); await flush();
  assert.equal(calls[1].cursor, 'home-key', 'Load 20 more continues by the adapter key');
  calls[1].resolve(feed([video('third-video', '6000')], false)); await flush();
  tree = text();
  assert.ok(tree.includes('0.5× median') && tree.includes('2.0× median'), 'loading more does not move the median');
  assert.equal(paceCalls.length, 1);
  assert.ok(tree.includes('End of the selected period'));
  // Same first page on the poll: no new median while it is current.
  intervals.forEach(fn => fn()); await flush();
  calls[2].resolve(feed([video('first-video', '24000'), video('second-video', null)], true)); await flush();
  assert.equal(paceCalls.length, 1);
  // New videos change the first page: the period median is read again.
  intervals.forEach(fn => fn()); await flush();
  calls[3].resolve(feed([video('new-video', '48000'), video('first-video', '24000')], true)); await flush();
  assert.equal(paceCalls.length, 2);
  paceCalls[1].reject(new Error('policy_denied')); await flush();
  assert.ok(text().includes('2.0× median'), 'a failed refresh keeps the current median');
  host.stop();
}
// Without a connection the reason is a plain sentence, not the browser's "Failed to fetch".
// It stays an uncertain failure for Saved writes, an ordinary error with Retry for Library, and a cancel stays a cancel.
{
  const serverText = await import('../application/frontend/src/serverText.ts');
  const contracts = await import('../application/frontend/src/explorer/contracts.ts');
  const errors = await import('../application/frontend/src/explorer/libraryData/errors.ts');
  const { savedFailure } = await import('../application/frontend/src/explorer/savedWork.ts');
  let failure;
  const api = compile('../application/frontend/src/explorer/api.ts',
    {'../sourceLifetime.ts': lifetime, '../serverText.ts': serverText, './contracts': contracts,
      '../discoveryJournal': {observeDiscoveryResponse() {}}},
    {window: {setTimeout: () => 1, clearTimeout() {}}, performance: {now: () => 0},
      fetch: async () => { if (failure) throw failure; return {ok: false, status: 500, json: async () => ({}), headers: {get: () => null}}; }});
  for (const browserText of ['Failed to fetch', 'NetworkError when attempting to fetch resource.', 'Load failed']) {
    failure = new TypeError(browserText);
    const error = await api.requestJson('/research/queries/execute').then(() => null, value => value);
    assert.equal(error.message, 'No connection to the Leti Cook server.', browserText);
    assert.ok(error instanceof api.ExplorerNetworkError, browserText);
    assert.equal(error.cause, failure, 'the browser error stays attached for debugging');
    assert.equal(savedFailure(error).uncertain, true, 'a write may have reached the server');
    assert.deepEqual(errors.libraryErrorState(error, 'videos_more'), {action: 'error', code: 'unknown', message: 'No connection to the Leti Cook server.'});
  }
  failure = Object.assign(new Error('Cancelled'), {name: 'AbortError'});
  assert.equal(await api.requestJson('/x').then(() => null, value => value), failure, 'a cancel is not a lost connection');
  failure = undefined;
  const server = await api.requestJson('/x').then(() => null, value => value);
  assert.ok(server instanceof api.ExplorerApiError && server.message === 'The server could not complete the request (HTTP 500).', 'a server answer keeps its own text');
  // The refusal's `limit` reaches the Library text; an unknown value is dropped.
  for (const [limit, expected] of [['memory', 'memory'], ['time', 'time'], ['rows', 'rows'], ['request', 'request'], ['response', 'response'],
    ['bytes', undefined], [undefined, undefined]]) {
    const refused = compile('../application/frontend/src/explorer/api.ts',
      {'../sourceLifetime.ts': lifetime, '../serverText.ts': serverText, './contracts': contracts,
        '../discoveryJournal': {observeDiscoveryResponse() {}}},
      {window: {setTimeout: () => 1, clearTimeout() {}}, performance: {now: () => 0}, fetch: async () => ({ok: false, status: 422,
        json: async () => ({code: 'query_budget_exceeded', path: 'consistency', ...(limit === undefined ? {} : {limit})}), headers: {get: () => null}})});
    const error = await refused.requestJson('/research/queries/execute').then(() => null, value => value);
    assert.deepEqual([error.code, error.limit], ['query_budget_exceeded', expected], String(limit));
  }
}

// A 200 status alone does not acknowledge an edit. Exercise the manager and its actual API and sidebar reader.
{
  const contracts = await import('../application/frontend/src/explorer/contracts.ts');
  const serverText = await import('../application/frontend/src/serverText.ts');
  const sidebar = await import('../application/frontend/src/explorer/sidebarReads.ts');
  const calls = [], time = clock(), headers = new Headers();
  let mode = 'broken', folders = [{id: 'sample', name: 'Before'}], shown = structuredClone(folders), listState = 'ready', failReads = false;
  const route = async (path, init) => {
    calls.push([init.method ?? 'GET', path]);
    if (['/sidebar-data', '/sidebar-user-data'].includes(path)) {
      if (failReads) throw new TypeError('offline');
      return Response.json({folders, labels: [], channels: []});
    }
    if (mode === 'refused') return new Response('bad gateway body', {status: 409});
    if (mode === 'empty') return new Response(null, {status: 204});
    if (mode === 'wrong') return Response.json({id: 'another-id', name: 'After'});
    if (mode === 'stale') return Response.json(path.startsWith('/folders') ? {id: 'sample', name: 'Before'}
      : {label_key: 'sample', name: 'Before', label_type: 'custom'});
    if (mode === 'normalized') return Response.json(path.startsWith('/folders') ? {id: 'sample', name: 'A  B'}
      : {label_key: 'sample', name: 'A B', label_type: 'custom'});
    if (mode === 'during-body') return {ok: true, status: 200, headers, json: () =>
      new Promise((_, reject) => init.signal.addEventListener('abort', () => reject(new TypeError('terminated')), {once:true}))};
    if (mode === 'lost') return {ok: true, status: 200, headers, json: async () => { throw new TypeError('terminated'); }};
    const name = JSON.parse(init.body).name;
    folders = [{id: 'sample', name}];
    return mode === 'valid' ? Response.json(folders[0]) : new Response('{"id":', {status: 200});
  };
  const api = compile('../application/frontend/src/explorer/api.ts',
    {'../sourceLifetime.ts': lifetime, '../serverText.ts': serverText, './contracts': contracts,
      '../discoveryJournal': {observeDiscoveryResponse() {}}}, {window: time, fetch: route});
  const reader = sidebar.createSidebarReader({read: () => api.loadExplorerSidebar(), fallback: () => api.loadExplorerUserSidebar(),
    show: payload => { shown = payload.folders; listState = 'ready'; }, fail: () => { shown = []; listState = 'error'; }});
  const host = hooks();
  const manager = compile('../application/frontend/src/explorer/FolderLabelManager.tsx',
    {react: host.api, 'react/jsx-runtime': {jsx, jsxs: jsx}, './api': api, './sidebarReads': sidebar});
  const current = () => nodes(host.render());
  const button = (label) => current().find(n => n.type === 'button' &&
    (n.props['aria-label'] === label || n.props.children === label));
  host.mount(() => manager.FolderLabelManager({folders: shown, labels: [], listState, onChanged() { reader.changedLocally(); return reader.load(); }}));
  button('Folders and labels').props.onClick();
  button('Rename Before').props.onClick();
  current().find(n => n.type === 'input').props.onChange({target: {value: 'After'}});
  current().find(n => n.type === 'form').props.onSubmit({preventDefault() {}});
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(calls, [['PATCH', '/folders/sample'], ['GET', '/sidebar-data']]);
  assert.equal(shown[0].name, 'After', 'the committed change comes back from the server');
  assert.match(current().find(n => n.props?.role === 'status').props.children, /could not be confirmed/);
  assert.ok(button('Rename After'), 'the draft closes and the dialog shows the actual saved name');
  failReads = true; calls.length = 0;
  button('Rename After').props.onClick();
  current().find(n => n.type === 'input').props.onChange({target: {value: 'Third'}});
  current().find(n => n.type === 'form').props.onSubmit({preventDefault() {}});
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(calls, [['PATCH', '/folders/sample'], ['GET','/sidebar-data'], ['GET','/sidebar-user-data']]);
  assert.ok(current().some(n => n.props?.children === 'Folders and labels are unavailable.'));
  assert.ok(!current().some(n => ['No folders yet.', 'No labels yet.'].includes(n.props?.children)));
  assert.match(current().find(n => n.props?.role === 'status').props.children, /could not be reloaded/);
  failReads = false;
  button('Reload folders and labels').props.onClick();
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(calls.slice(3), [['GET', '/sidebar-data']], 'manual recovery never submits the edit again');
  assert.ok(button('Rename Third'));
  host.stop();

  mode = 'lost'; await assert.rejects(api.requestJson('/x'), api.ExplorerNetworkError);
  mode = 'empty'; await assert.rejects(api.deleteExplorerLabel('sample'), {name: 'UnreadableAnswer'});
  mode = 'wrong'; await assert.rejects(api.renameExplorerFolder('sample', 'After'), contracts.ExplorerContractError);
  mode = 'stale';
  await assert.rejects(api.renameExplorerFolder('sample', 'After'), contracts.ExplorerContractError);
  await assert.rejects(api.renameExplorerLabel('sample', 'After'), contracts.ExplorerContractError);
  mode = 'normalized';
  await api.renameExplorerFolder('sample', ' A  B ');
  await api.renameExplorerLabel('sample', ' A\u0085\u001e B ');
  mode = 'lost';
  await assert.rejects(sidebar.organizationWrite(() => api.deleteExplorerLabel('sample'), async () => false),
    /could not be reloaded.*Reload Library/);
  mode = 'refused'; let refreshed = 0;
  await assert.rejects(sidebar.organizationWrite(() => api.deleteExplorerFolder('sample'), async () => { refreshed++; }), {status: 409});
  assert.equal(refreshed, 0, 'a known refusal keeps its HTTP status even with an unreadable error body');
  mode = 'during-body';
  const controller = new AbortController();
  const cancelled = sidebar.organizationWrite(() => api.renameExplorerLabel('sample', 'Name', controller.signal), async () => { refreshed++; });
  await flush(); controller.abort(); await assert.rejects(cancelled, {name: 'AbortError'});
  assert.equal(refreshed, 0, 'caller cancellation stays cancellation');
  const timeout = api.renameExplorerLabel('sample', 'Name');
  await flush(); await time.advance(120_000); await assert.rejects(timeout, {code: 'client_timeout'});
  assert.equal(time.size, 0);
  mode = 'valid'; await api.renameExplorerFolder('sample', 'Valid');
}

// An empty search page can carry a paid continuation; a later empty terminal page confirms no results.
{
  const host = hooks(), paid = [], cost = {search_queries: 1, general: 1, video_batch_get_stats: 1};
  let result;
  const finder = compile('../application/frontend/src/explorer/ChannelSearchPage.tsx', {
    react: host.api, 'react/jsx-runtime': {jsx, jsxs: jsx}, './channelSearch.css': {}, '../components/YouTubeAttribution': {},
    './api': {requestJson: async (path) => {
      if (path.endsWith('/status')) return {};
      paid.push(path.split('/').at(-1));
      return path.endsWith('/preview') ? {action_id: String(paid.length), max_cost: cost} : result;
    }},
  }, {window: {setTimeout() {}, clearTimeout() {}, addEventListener() {}, removeEventListener() {}}});
  host.mount(() => finder.default({}));
  const elements = () => nodes(host.render());
  const text = () => elements().map(n => Array.isArray(n.props?.children)
    ? n.props.children.filter(v => typeof v === 'string').join('') : n.props?.children).filter(v => typeof v === 'string').join(' ');
  const page = (stop, more) => ({valid_for_ms: 10000, hits: [], profiles: {}, library: {}, views: {}, steps: {},
    search_step: {cost: {}, rejected: 0, stop}, more, retry: false});
  const first = () => elements().find(n => n.type === 'form').props.onSubmit({preventDefault() {}});
  result = page('budget_requests', true); first(); await flush();
  assert.match(text(), /This page has no results. Another page is available/);
  assert.doesNotMatch(text(), /did not return a usable page/);
  const next = elements().find(n => n.type === 'button' && n.props.children[0] === 'Load next page');
  assert.match(next.props.children.join(''), /up to 1 Search/);
  assert.deepEqual(paid, ['preview', 'execute'], 'no automatic request for the next page');
  result = page(null, false); next.props.onClick(); await flush();
  assert.match(text(), /YouTube returned no results/);
  assert.doesNotMatch(text(), /Another page is available|did not return a usable page/);
  result = page('quota_exhausted', false); first(); await flush();
  assert.match(text(), /quota exhausted/); assert.match(text(), /did not return a usable page/);
  host.stop();
}

// Losing the receipt after a paid first search must keep the original action across a reload.
{
  const storage = new Map(), sent = [], quotes = [], cost = {search_queries: 1, general: 1, video_batch_get_stats: 1};
  const browser = {setTimeout() {}, clearTimeout() {}, addEventListener() {}, removeEventListener() {},
    sessionStorage: {getItem: k => storage.get(k) ?? null, setItem: (k, v) => storage.set(k,v), removeItem: k => storage.delete(k)}};
  let failure = true;
  function loadFinder() {
    const host = hooks();
    const finder = compile('../application/frontend/src/explorer/ChannelSearchPage.tsx', {
      react: host.api, 'react/jsx-runtime': {jsx, jsxs: jsx}, './channelSearch.css': {}, '../components/YouTubeAttribution': {},
      './api': {requestJson: async (path, signal, options) => {
        if (path.endsWith('/status')) return {};
        const body = JSON.parse(options.body);
        if (path.endsWith('/preview')) {quotes.push(body); return {action_id: 'ticket' + quotes.length, max_cost: cost};}
        sent.push(body);
        if (failure) throw new Error('The response was interrupted.');
        return {valid_for_ms: 9000, hits: [], profiles: {}, library: {}, views: {}, steps: {},
          search_step: {cost: {}, rejected: 0, stop: null}, more: false, retry: false};
      }},
    }, {window: browser});
    host.mount(() => finder.default({}));
    const tree = () => nodes(host.render());
    return {stop: () => host.stop(), first: () => tree().find(n => n.type === 'form').props.onSubmit({preventDefault() {}}),
      button: label => tree().find(n => n.type === 'button' && n.props.children === label)};
  }
  const before = loadFinder(); before.first(); await flush();
  assert.equal(storage.size, 1); assert.equal(before.button('Search YouTube').props.disabled, true);
  before.stop();
  const after = loadFinder();
  after.first(); await flush(); assert.equal(quotes.length, 1); assert.equal(sent.length, 1);
  after.button('Resume original search').props.onClick(); await flush();
  assert.deepEqual(sent[1], sent[0]); assert.equal(storage.size, 1);
  failure = false;
  after.button('Resume original search').props.onClick(); await flush();
  assert.deepEqual(sent[2], sent[0]); assert.equal(storage.size, 0); assert.equal(quotes.length, 1);
  failure = true; after.first(); await flush();
  after.button('Discard request').props.onClick(); await flush();
  assert.equal(storage.size, 0); assert.equal(quotes.length, 2); assert.equal(sent.length, 4);
  after.stop();
}

// The channel list's reads. A read asked for during another is not dropped (exactly one more follows), and a
// local change (a channel added, a folder or label made or assigned) keeps an older, slower answer off the page.
{
  const { createSidebarReader } = await import('../application/frontend/src/explorer/sidebarReads.ts');
  const harness = () => {
    const reads = [], fallbacks = [], shown = [], failed = [];
    const reader = createSidebarReader({
      read: () => { const step = deferred(); reads.push(step); return step.promise; },
      fallback: () => { const step = deferred(); fallbacks.push(step); return step.promise; },
      show: (payload) => shown.push(payload), fail: (message) => failed.push(message),
    });
    return { reader, reads, fallbacks, shown, failed };
  };
  const list = (...ids) => ({ channels: ids.map((channel_id) => ({ channel_id })) });
  // A slow read started before a channel was added answers without it: that answer is not shown, the next read is.
  {
    const h = harness();
    void h.reader.load();
    h.reader.changedLocally();            // the page merged the new channel into what it shows
    h.reads[0].resolve(list('UCold')); await flush();
    assert.deepEqual(h.shown, [], 'the older answer never replaces the newer list');
    assert.equal(h.reads.length, 2, 'one read follows, after the change was saved');
    h.reads[1].resolve(list('UCold', 'UCnew')); await flush();
    assert.deepEqual(h.shown, [list('UCold', 'UCnew')]);
    assert.equal(h.reads.length, 2);
  }
  // Calls during a read (Library opened, window focused, a folder renamed) are not dropped: exactly one read follows.
  {
    const h = harness();
    let refreshed = false;
    void h.reader.load(); void h.reader.load();
    const followup = h.reader.load().then(() => { refreshed = true; });
    assert.equal(h.reads.length, 1, 'one read at a time');
    h.reads[0].resolve(list('UCa')); await flush();
    assert.deepEqual(h.shown, [list('UCa')], 'nothing changed locally: the running answer is shown');
    assert.equal(h.reads.length, 2, 'and one more read, not two');
    assert.equal(refreshed, false, 'a recovery queued behind an older read still awaits the new result');
    h.reads[1].resolve(list('UCa', 'UCb')); await followup;
    assert.equal(refreshed, true);
    assert.deepEqual(h.shown.at(-1), list('UCa', 'UCb'));
    assert.equal(h.reads.length, 2);
  }
  // A local change with no read running asks for no read: the merge is what the page shows.
  {
    const h = harness();
    h.reader.changedLocally();
    await flush();
    assert.equal(h.reads.length, 0);
  }
  // The fallback to local work and the error follow the same rule.
  {
    const h = harness();
    void h.reader.load();
    h.reads[0].reject(new Error('offline')); await flush();
    h.reader.changedLocally();
    h.fallbacks[0].resolve(list('UCstale')); await flush();
    assert.deepEqual(h.shown, [], 'a stale fallback is not shown either');
    h.reads[1].reject(new Error('still offline')); await flush();
    h.fallbacks[1].reject(new Error('local work unavailable')); await flush();
    assert.deepEqual(h.failed, ['still offline'], 'the read error is kept when local work is unavailable too');
    assert.equal(h.reads.length, 2);
  }
  // The page reads only through the reader, and every local merge marks a running read stale.
  const app = readFileSync(new URL('../application/frontend/src/explorer/ExplorerApp.tsx', import.meta.url), 'utf8');
  assert.match(app, /const loadSidebar = sidebarReader\.load;/);
  assert.doesNotMatch(app, /sidebarReading|sidebarRequestRef/);
  for (const write of ['assignExplorerChannelFolder(channelId, folderId)', 'createExplorerFolder(name)', 'createExplorerLabel(name)',
    'assignExplorerChannelLabel(channelId, labelKey, enabled)']) {
    const at = app.indexOf(`await organizationWrite(() => ${write}, refreshOrganization)`);
    assert.ok(at > 0, write);
    assert.match(app.slice(at, app.indexOf('setSidebarState', at)), /sidebarReader\.changedLocally\(\);/, write);
  }
  assert.match(app, /\} else \{\n\s+sidebarReader\.changedLocally\(\);\n\s+setSidebarState\(\(current\) => \{\n\s+if \(current\.kind !== 'ready'\) return current;\n\s+return \{\n\s+kind: 'ready',\n\s+payload: \{\n\s+\.\.\.current\.payload,\n\s+channels: mergeExplorerSidebarChannel/);
  console.log('an older sidebar answer never replaces a local change; a read asked for during another runs once after it.');
}
console.log('UI read recovery: bounded visible retries, cancellation ownership, deadlines and recent-feed races passed');
