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
        node.props.ref.current = {getBoundingClientRect: () => ({top: 0}), scrollIntoView() {}};
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
{
  const host = hooks(), calls = [], intervals = new Set(), listeners = new Map(), observers = [];
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
    './api': {isAbortError: error => error.name === 'AbortError', isContractError: () => false,
      loadExplorerRecentVideos: (windowKey, cursor, signal) => {
        const next = deferred(); calls.push({...next, windowKey, cursor, signal}); return next.promise;
      }},
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
console.log('UI read recovery: bounded visible retries, cancellation ownership, deadlines and recent-feed races passed');
