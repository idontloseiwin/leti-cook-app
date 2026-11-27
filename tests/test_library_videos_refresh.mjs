// Library Videos: loaded cards stay while the page is open, and their numbers are read again in the
// background. A value past its own lifetime is shown as expired ("—", Needs refresh) until a later read
// brings what the program has; the order of the list changes only when the list itself is read again.
// Offline: real wire rows of the fixture, the real hook on a hook host, and a test clock (wall and
// monotonic time moved by hand, no real timers, no network).
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from '../application/frontend/node_modules/typescript/lib/typescript.js';
import { ExplorerContractError } from '../application/frontend/src/explorer/contracts.ts';
import * as lifetime from '../application/frontend/src/sourceLifetime.ts';
import * as videos from '../application/frontend/src/explorer/libraryData/videos.ts';
import * as wire from '../application/frontend/src/explorer/libraryData/wire.ts';
import * as errors from '../application/frontend/src/explorer/libraryData/errors.ts';
import * as refresh from '../application/frontend/src/explorer/catalogueRefresh.ts';
import { reactHookHost, flush } from './react_hook_host.mjs';

const read = (path) => readFileSync(new URL(path, import.meta.url), 'utf8');
const clone = (value) => structuredClone(value);
const fixture = JSON.parse(read('./fixtures/library_videos_wire.json'));
const example = (name) => clone(fixture[name]);
const contract = (fn, pattern) => assert.throws(fn, (error) => error instanceof ExplorerContractError && pattern.test(error.message), String(pattern));
const at = (text) => Date.parse(text);
const MINUTE = 60_000, HOUR = 60 * MINUTE;

function compiled(file, imports = {}) {
  const { outputText } = ts.transpileModule(read('../application/frontend/src/explorer/' + file),
    { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX } });
  const exports = {};
  new Function('require', 'exports', outputText)((name) => { assert.ok(name in imports, `${file}: ${name}`); return imports[name]; }, exports);
  return exports;
}

/** A clock and timers moved by hand: `advance` moves both clocks (or the wall clock alone: a computer that slept). */
function testEnvironment(start) {
  let wall = start, monotonic = 0, shown = true;
  const watchers = new Set(), timers = new Set();
  return {
    now: () => wall, monotonic: () => monotonic, visible: () => shown,
    watch(check) { watchers.add(check); return () => watchers.delete(check); },
    after(ms, run) { const timer = { at: monotonic + ms, run }; timers.add(timer); return () => timers.delete(timer); },
    advance(ms, { asleep = false } = {}) {
      wall += ms;
      if (!asleep) monotonic += ms;
      for (const timer of [...timers].sort((left, right) => left.at - right.at)) {
        if (timer.at <= monotonic && timers.delete(timer)) timer.run();
      }
    },
    setWall(value) { wall = value; },
    tick() { for (const check of [...watchers]) check(false); },
    show(value) { shown = value; if (value) for (const check of [...watchers]) check(true); },
    get timers() { return timers.size; }, get watchers() { return watchers.size; },
  };
}

// The fixture's frozen pages as current replies of the same rows (the wire differs only in the envelope).
const READ_AT = at(fixture.long_first_anchor.response.source_read_at);
const listIds = example('long_frozen_ids').response.ids;
const currentReply = (name, page, mutate) => {
  const { request, response } = example(name);
  request.consistency = 'current';
  request.page = page;
  response.context.consistency = 'current';
  delete response.context.frozen_ref;
  response.page = { has_more: false, next_cursor: null };
  mutate?.(response, request);
  return { request, response };
};
// As requestJson delivers a reply: the source clock of the HTTP layer, started at `started` on the monotonic clock.
const delivered = (response, started = 0) => lifetime.captureSourceLifetime(response, { get: (name) => (name === 'X-Source-Expires-At'
  ? response.source_expires_at ?? null : name === 'X-Source-Read-At' ? response.source_read_at ?? null : null) }, started);
const parse = ({ request, response }, started = 0) => videos.parseLibraryPage(delivered(response, started), { request, channelId: request.scope.id });
const firstRows = example('long_first_anchor').response.rows.map((row) => row.id);
const firstPage = (mutate) => currentReply('long_first_anchor', { size: 4, growth_lifetime: 'page', order: 'ids', anchor_id: firstRows[0] }, (response, request) => {
  response.page = { has_more: true, next_cursor: 'next-page', order: { total_count: listIds.length, ids: listIds.join(''), limit: 200000 } };
  mutate?.(response, request);
});
const rowsRead = (ids, mutate) => currentReply('long_first_anchor', { size: 200, growth_lifetime: 'page', ids }, (response) => {
  response.rows = response.rows.filter((row) => ids.includes(row.id));
  mutate?.(response);
});
const cell = (response, id, field) => response.rows.find((row) => row.id === id).cells[field];

// 1. Loaded cards stay after their values' lifetimes end. Each value ends by its own clock; the others stay.
{
  const page = parse(firstPage());
  const ids = page.videos.map((video) => video.video_id);
  assert.deepEqual(ids, firstRows);
  const start = READ_AT + 1000;
  assert.equal(videos.shownCatalogueVideos(page, start, 1000), page.videos, 'nothing has ended: the loaded rows themselves');
  // The first row's 28-day growth ends first (09:44 the next day), its metadata before that (03:44), its counters weeks later.
  const metadataEnd = at('2026-10-31T03:44:15.221Z') + 1, counterEnd = at('2026-11-28T09:44:15.221Z') + 1;
  assert.equal(videos.nextCatalogueExpiry(page, start, 1000), at('2026-10-31T03:44:15.221Z') - start, 'the next end of a loaded value');
  const afterMetadata = videos.shownCatalogueVideos(page, metadataEnd, metadataEnd - READ_AT);
  assert.deepEqual(afterMetadata.map((video) => video.video_id), ids, 'every card stays, in its place');
  const [late] = afterMetadata;
  assert.equal(late.views, '6120', 'a value whose own lifetime runs is shown as read');
  assert.equal(late.like_count, '153');
  assert.deepEqual([late.title, late.published_at, late.duration, late.category_id, late.tags], [undefined, undefined, undefined, undefined, undefined],
    'an ended value is never shown as current');
  assert.equal(late.value_states.published_at, 'expired', 'it shows as "—" Needs refresh');
  assert.equal(late.value_states.views, undefined);
  assert.equal(late.stale, true);
  assert.ok(late.view_growth, 'growth of this row ends later');
  assert.equal(afterMetadata[1], page.videos[1], 'rows with nothing ended are the loaded rows themselves');
  assert.equal(videos.shownCatalogueVideos(page, metadataEnd, metadataEnd - READ_AT), afterMetadata, 'the same instant: the same rows (no re-render)');
  assert.equal(videos.shownCatalogueVideos(page, metadataEnd + 5, metadataEnd + 5 - READ_AT)[0], late, 'and the same card object until its next value ends');
  // Growth ended for every row, counters not yet.
  const afterGrowth = videos.shownCatalogueVideos(page, at('2026-11-16T00:00:00Z'), at('2026-11-16T00:00:00Z') - READ_AT);
  assert.deepEqual(afterGrowth.map((video) => [video.video_id, video.view_growth, video.views !== undefined]),
    ids.map((id) => [id, undefined, true]));
  // Everything ended: still every card, each value as expired, no number left.
  const afterAll = videos.shownCatalogueVideos(page, at('2026-12-01T00:00:00Z'), at('2026-12-01T00:00:00Z') - READ_AT);
  assert.deepEqual(afterAll.map((video) => video.video_id), ids);
  for (const video of afterAll) {
    assert.deepEqual([video.views, video.like_count, video.comment_count, video.title, video.thumbnail_url], [undefined, undefined, undefined, undefined, undefined]);
    assert.deepEqual([video.value_states.views, video.value_states.like_count, video.value_states.comment_count], ['expired', 'expired', 'expired']);
  }
  assert.equal(videos.nextCatalogueExpiry(page, at('2026-12-01T00:00:00Z'), at('2026-12-01T00:00:00Z') - READ_AT), Infinity);
  // Sleep and wake, and a changed clock: either clock passing the end ends the value.
  const asleep = videos.shownCatalogueVideos(page, counterEnd, 1000);            // the wall clock moved, the monotonic one stood still
  const setBack = videos.shownCatalogueVideos(page, start, counterEnd - READ_AT); // the wall clock was set back, the monotonic one ran
  for (const rows of [asleep, setBack]) {
    assert.equal(rows.length, 4);
    assert.deepEqual([rows[0].views, rows[0].value_states.views], [undefined, 'expired']);
  }
  // The rows to read again first: a value ended, or ends before the next pass.
  assert.deepEqual([...videos.expiringCatalogueIds(page, 0, start, 1000)], []);
  assert.deepEqual([...videos.expiringCatalogueIds(page, 0, metadataEnd, metadataEnd - READ_AT)], [ids[0]]);
  assert.deepEqual([...videos.expiringCatalogueIds(page, at('2026-10-31T03:44:15.221Z') - start + 1, start, 1000)], [ids[0]], 'ends within the period');
  assert.deepEqual([...videos.expiringCatalogueIds(page, 0, at('2026-12-01T00:00:00Z'), 1000)], ids);
  console.log('Loaded cards stay after their values end; each value by its own clock; sleep and a changed clock.');
}

// 2. A later read of loaded rows replaces their values in place. The order, the window and the list stay.
{
  const page = parse(firstPage());
  const ids = page.videos.map((video) => video.video_id);
  const later = at('2026-10-31T12:00:00Z');
  const before = videos.shownCatalogueVideos(page, later, later - READ_AT);
  assert.equal(before[0].title, undefined);
  assert.equal(before[1].view_growth, undefined);
  // The collector has newer values for row 0 and 1; nothing newer for row 2's counters (the server says expired); row 3 left the results.
  const reread = rowsRead(ids, (response) => {
    const shift = (cellValue, text) => Object.assign(cellValue, { observed_at: '2026-10-31T11:50:00.000000Z', expires_at: text });
    response.context.evaluated_at = '2026-10-31T12:00:00.000000Z';
    response.context.valid_before = '2026-10-31T12:02:00.000000Z';
    response.source_read_at = '2026-10-31T12:00:00.010000Z';
    response.source_expires_at = '2026-10-31T12:02:00.000000Z';
    for (const row of response.rows) {
      for (const [field, value] of Object.entries(row.cells)) {
        if (field.startsWith('observation.') || value.expires_at < '2026-10-31T12:02') shift(value, '2026-11-29T11:50:00.000000Z');
      }
    }
    shift(Object.assign(cell(response, ids[0], 'video.views'), { value: '7000' }), '2026-11-29T11:50:00.000000Z');
    Object.assign(cell(response, ids[0], 'video.like_rate'), { value: '2.185714' });
    for (const field of ['video.views', 'video.likes', 'video.comments', 'video.like_rate', 'video.comment_rate']) {
      Object.assign(cell(response, ids[2], field), { state: 'expired', value: null, observed_at: null, expires_at: null });
    }
    response.rows = response.rows.filter((row) => row.id !== ids[3]);
  });
  const started = later - READ_AT;
  const result = videos.refreshLibraryRows(page, parse(reread, started), ids);
  assert.deepEqual(result.payload.videos.map((video) => video.video_id), ids, 'the same cards in the same order');
  assert.deepEqual(result.absent, [ids[3]], 'a row the read no longer has is named, and its card stays');
  assert.equal(result.payload.videos[3], page.videos[3]);
  assert.equal(result.payload.browse.order, page.browse.order, 'the list is untouched');
  assert.deepEqual([result.payload.browse.start_index, result.payload.browse.end_index, result.payload.browse.session_key, result.payload.browse.left_results],
    [page.browse.start_index, page.browse.end_index, page.browse.session_key, 0]);
  const after = videos.shownCatalogueVideos(result.payload, later + 1000, started + 1000);
  assert.deepEqual([after[0].views, after[0].title, after[0].checked_at], ['7000', 'Growth gLlatemeta0', '2026-10-31T11:50:00.000000Z'], 'newer values in place of the old');
  assert.ok(after[1].view_growth, 'and a value that had ended is current again');
  assert.deepEqual([after[2].views, after[2].value_states.views, after[2].value_states.like_count, after[2].stale], [undefined, 'expired', 'expired', true],
    'nothing newer in the program: Needs refresh, never the old number');
  assert.equal(after[2].title, 'Growth gLbg0400000');
  // The window still continues by slices of the same list.
  const nextIds = listIds.slice(8, 12);
  const slice = parse(currentReply('long_next', { size: 4, growth_lifetime: 'page', ids: nextIds }));
  const more = videos.mergeLibrarySlice(result.payload, slice, 'next', nextIds);
  assert.deepEqual(more.videos.map((video) => video.video_id), [...ids, ...nextIds]);
  // Only a read of the same query's loaded rows is put in place.
  contract(() => videos.refreshLibraryRows(page, { ...parse(reread, started), columns: ['video.id'] }, ids), /another query/);
  contract(() => videos.refreshLibraryRows(page, parse(reread, started), ids.slice(2)), /requested videos/);
  contract(() => videos.parseLibraryPage(delivered(rowsRead(ids).response), { request: { ...rowsRead(ids).request, page: { size: 200, growth_lifetime: 'page', ids: ids.slice(1) } },
    channelId: fixture.long_first_anchor.request.scope.id }), /requested videos/);
  console.log('A later read replaces values in place: same order, window and list; Needs refresh when nothing newer; a departed row stays.');
}

// 3. When a pass runs, what it reads first, and when it gives up.
{
  const origin = { now: 1_000_000, monotonic: 5_000 };
  const later = (minutes) => ({ now: origin.now + minutes * MINUTE, monotonic: origin.monotonic + minutes * MINUTE });
  const fresh = { last: origin, lastFull: origin, started: origin, failures: 0, paused: false };
  const due = (minutes, reason, memory = fresh) => refresh.refreshDue(memory, later(minutes), reason);
  assert.equal(refresh.REFRESH_PERIOD_MS, 5 * MINUTE);
  assert.deepEqual([due(4.9, 'tick'), due(5, 'tick')], [null, 'full'], 'a full pass every period');
  assert.deepEqual([due(0.9, 'visible'), due(1, 'visible'), due(1, 'expiry'), due(5, 'visible')], [null, 'soon', 'soon', 'full'],
    'back on the tab or a value ended: one read soon, not at once; the full pass keeps its period');
  // Single reads do not put off the full pass: it is due a period after the last full one.
  const afterSoon = { ...fresh, last: later(3.5), started: later(3.5) };
  assert.deepEqual([due(4.4, 'expiry', afterSoon), due(5, 'tick', afterSoon)], [null, 'full']);
  // Nothing starts within a minute of the previous start, finished or cancelled: also not an overdue full pass.
  const cancelled = { ...fresh, started: later(9.5) };
  assert.deepEqual([due(10, 'tick', cancelled), due(10.4, 'visible', cancelled), due(10.5, 'tick', cancelled)], [null, null, 'full']);
  // The same after a failure: a retry that a next page cancelled is not started again at the next check, nor on a
  // return to the tab, but a minute after its start (the failure itself was 5 minutes before the retry).
  const failedOnce = refresh.refreshFailed({ ...fresh, started: later(5) }, later(5));
  assert.deepEqual([due(9.9, 'tick', failedOnce), due(10, 'tick', failedOnce)], [null, 'full']);
  const retried = { ...failedOnce, started: later(10) };
  assert.deepEqual([due(10.25, 'tick', retried), due(10.25, 'visible', retried), due(10 + 59 / 60, 'tick', retried), due(11, 'tick', retried), due(11, 'visible', retried)],
    [null, null, null, 'full', 'full']);
  // A computer that slept: the wall clock moved, the monotonic one did not. A clock set back: the other way round.
  assert.equal(refresh.refreshDue(fresh, { now: origin.now + HOUR, monotonic: origin.monotonic }, 'tick'), 'full');
  assert.equal(refresh.refreshDue(fresh, { now: origin.now - HOUR, monotonic: origin.monotonic + 5 * MINUTE }, 'tick'), 'full');
  assert.equal(refresh.refreshDue(fresh, { now: origin.now - HOUR, monotonic: origin.monotonic + MINUTE }, 'tick'), null);
  // Failures: 5, 10, 20, 30 minutes, then no more automatic passes.
  let state = { last: { now: 0, monotonic: 0 }, lastFull: { now: 0, monotonic: 0 }, started: { now: 0, monotonic: 0 }, failures: 0, paused: false };
  const waits = [];
  for (let attempt = 0; attempt < 5; attempt += 1) {
    state = refresh.refreshFailed(state, { now: 0, monotonic: 0 });
    waits.push(state.paused ? 'paused' : refresh.refreshRetryMinutes(state.failures));
    if (!state.paused) {
      const wait = refresh.REFRESH_RETRY_MS[state.failures - 1];
      assert.deepEqual([refresh.refreshDue(state, { now: wait - 1, monotonic: wait - 1 }, 'visible'), refresh.refreshDue(state, { now: wait, monotonic: wait }, 'tick')], [null, 'full']);
    }
  }
  assert.deepEqual(waits, [5, 10, 20, 30, 'paused']);
  assert.equal(refresh.refreshDue(state, { now: 1e12, monotonic: 1e12 }, 'visible'), null, 'no endless retries');
  assert.match(refresh.refreshProblemText(state), /no longer updated in the background.*keep their last values.*Refresh order/);
  assert.match(refresh.refreshProblemText({ ...state, failures: 2, paused: false }), /could not be updated.*keep their last values.*next attempt is in 10 minutes/);
  assert.equal(refresh.refreshFailed(fresh, { now: 1, monotonic: 1 }, true).paused, true, 'an unverifiable reply stops at once');
  // Order: urgent rows, then the cards around the viewed one, then the least recently read.
  const ids = Array.from({ length: 2000 }, (_, index) => 'r' + String(index).padStart(10, '0'));
  const readAt = new Map(ids.map((id, index) => [id, index < 100 ? 10 : 20]));
  const order = refresh.refreshOrder(ids, { near: ids[1500], urgent: new Set([ids[3], ids[1999]]), readAt });
  assert.deepEqual(order.slice(0, 3), [ids[1999], ids[3], ids[1500]], 'urgent rows first (the nearer one before), then the viewed card');
  const near = order.slice(2, refresh.REFRESH_CHUNK);
  assert.deepEqual(near.map((id) => ids.indexOf(id)).sort((a, b) => a - b), Array.from({ length: 198 }, (_, i) => 1401 + i),
    'the first read: the cards around the viewed one');
  const rest = order.slice(refresh.REFRESH_CHUNK);
  assert.deepEqual(rest.slice(0, 99).map((id) => ids.indexOf(id)).sort((a, b) => a - b), Array.from({ length: 100 }, (_, i) => i).filter((i) => i !== 3),
    'then the least recently read');
  // A full pass that is cancelled after its first read, four times, the viewed card not moving: each start reads
  // other rows, and a period later the cards around the viewed one have their turn again.
  {
    const loaded = ids.slice(0, 1000), times = new Map(loaded.map((id) => [id, 0])), firsts = [];
    for (let start = 0; start < 4; start += 1) {
      const now = refresh.REFRESH_PERIOD_MS + start * MINUTE;
      const first = refresh.refreshChunks(refresh.refreshOrder(loaded, { near: loaded[500], readAt: times, now }))[0];
      firsts.push(first);
      for (const id of first) times.set(id, now);
    }
    assert.ok(firsts[0].includes(loaded[500]) && firsts.slice(1).every((first) => !first.includes(loaded[500])), 'the viewed card once, in the first start');
    assert.equal(new Set(firsts.flat()).size, 800, 'four cancelled starts read 800 different rows, not the same 200');
    const again = refresh.refreshOrder(loaded, { near: loaded[500], readAt: times, now: 2 * refresh.REFRESH_PERIOD_MS });
    assert.equal(again[0], loaded[500], 'a period later: the viewed card first again');
    const whole = refresh.refreshOrder(loaded, { near: loaded[500], readAt: times, now: refresh.REFRESH_PERIOD_MS + 4 * MINUTE });
    assert.deepEqual([whole.length, new Set(whole).size], [1000, 1000], 'every loaded row once');
    // A single read without a time override prioritizes the cards around the visible row.
    assert.equal(refresh.refreshOrder(loaded, { near: loaded[500], readAt: times })[0], loaded[500]);
    // An urgent row is read first however recently it was read.
    assert.equal(refresh.refreshOrder(loaded, { near: loaded[500], urgent: new Set([loaded[500]]), readAt: times, now: refresh.REFRESH_PERIOD_MS + MINUTE })[0], loaded[500]);
  }
  assert.equal(new Set(order).size, ids.length, 'every loaded row once');
  assert.deepEqual(refresh.refreshOrder(ids.slice(0, 5)), ids.slice(0, 5), 'without a viewed card: from the top');
  assert.deepEqual(refresh.refreshChunks(ids).map((chunk) => chunk.length), Array(10).fill(200));
  assert.deepEqual(refresh.refreshChunks(ids.slice(0, 450)).map((chunk) => chunk.length), [200, 200, 50]);
  assert.deepEqual([refresh.refreshPause(10), refresh.refreshPause(700), refresh.refreshPause(60_000)], [250, 700, 2000]);
  console.log('Passes: period, soon after a return or an expiry, either clock, bounded retries, urgent and viewed rows first.');
}

// The real hook on the test clock. `parsePage`: identity for synthetic rows, the real parser for wire rows.
function mountBrowse({ parsePage, environment, scope = 'views-desc', channelId = 'UC' + 'a'.repeat(22), plan = { page: { size: 50 }, select: ['video.id'] } }) {
  const host = reactHookHost();
  const calls = [], announcements = [];
  const browse = compiled('useCatalogueBrowse.ts', {
    react: host.React, '../sourceLifetime.ts': lifetime, './catalogueRefresh.ts': refresh,
    './catalogueBrowse': compiled('catalogueBrowse.ts'), './catalogueQuery': { catalogueUsesGrowthFilters: () => false },
    './format': { videoNoun: () => 'videos' }, './libraryData/wire.ts': wire,
    './libraryData/savedLibrary.ts': { planLibraryOpen: async () => ({ status: 'run', translation: { query: {} }, request: plan }) },
    './libraryData/errors.ts': errors,
    './libraryData/videos.ts': { ...videos, libraryFrozenFields: () => [], libraryGrowthFields: () => [], parseLibraryPage: parsePage },
  });
  const inputs = { channelId, videoType: 'long', scopeKey: `${scope}\nreload:0`, listKey: scope, selectionScopeKey: 'channel',
    enabled: true, gates: null, growthAllowed: false, onAnnouncement: (text) => announcements.push(text), onGrowthUnavailable() {},
    refreshEnvironment: environment,
    request: (path, body, signal) => new Promise((resolve, reject) => calls.push({ path, body, signal, resolve, reject })) };
  host.mount(browse.useCatalogueBrowse, inputs);
  return { host, calls, announcements, inputs };
}
const syntheticIds = Array.from({ length: 1001 }, (_, index) => 'v' + String(index).padStart(10, '0'));
// What parseLibraryPage returns for a list page (from `start`) or a read of rows (`list` undefined).
const synthetic = (session, rows, { list, start = 0, more = false, views = '1' } = {}) => ({
  contract_version: 1, video_type: 'long', channel: { channel_id: 'UC' + 'a'.repeat(22) }, growth: false, columns: ['video.id'], time_zone: 'UTC',
  videos: rows.map((id) => ({ video_id: id, video_type: 'long', views })),
  browse: { consistency: 'current', session_key: session, query_fingerprint: 'same-query', created_at: 't', expires_at: '2999-01-01T00:00:00Z',
    start_index: start, end_index: start + rows.length, previous_cursor: null, next_cursor: more ? 'cursor' : null,
    anchor_video_id: null, left_results: 0, ...(list ? { order: { ids: list, total: list.length, limit: 200000 } } : {}) },
  pagination: { matched_count: list?.length, offset: start, returned_count: rows.length, has_more_in_scan: more },
  coverage: { complete: true },
});
const shownIds = (host) => host.current.state.payload.videos.map((video) => video.video_id);

// 4. The background pass: reads the loaded rows by their IDs while the tab is shown, puts newer values in
// place, and never moves a card, the window, the list or the remembered card.
{
  const environment = testEnvironment(at('2026-11-26T10:00:00Z'));
  const { host, calls, announcements, inputs } = mountBrowse({ parsePage: (raw) => raw, environment });
  await flush();
  calls[0].resolve(synthetic('read1', syntheticIds.slice(0, 50), { list: syntheticIds, more: true })); await flush();
  host.current.onLoadMore(); await flush();
  calls[1].resolve(synthetic('rows', syntheticIds.slice(50, 100))); await flush();
  assert.equal(shownIds(host).length, 100);
  const order = host.current.order, announced = announcements.length;
  // Before the period: nothing is read. Time alone never reads the list again.
  environment.advance(4 * MINUTE); environment.tick(); await flush();
  assert.equal(calls.length, 2);
  // Hidden tab: nothing is read, however long.
  environment.show(false);
  environment.advance(20 * MINUTE); environment.tick(); await flush();
  assert.equal(calls.length, 2, 'only while the tab is shown');
  // Shown again: the loaded rows are read by their IDs, in one read (not the list, not a page).
  host.current.onAnchorChange({ videoId: syntheticIds[70], offsetPx: 12 });
  environment.show(true); await flush();
  assert.equal(calls.length, 3, 'right after the return to the tab');
  assert.equal(calls[2].path, wire.ROUTES.execute);
  assert.deepEqual({ ...calls[2].body.page, ids: undefined }, { size: 200, growth_lifetime: 'page', ids: undefined });
  assert.equal(calls[2].body.consistency, 'current');
  assert.equal(calls[2].body.page.ids[0], syntheticIds[70], 'the viewed card first');
  assert.deepEqual([...calls[2].body.page.ids].sort(), syntheticIds.slice(0, 100), 'every loaded row, once');
  assert.equal(host.current.state.kind, 'ready', 'no loading state while it runs');
  assert.equal(host.current.isLoadingMore, false);
  environment.tick(); await flush();
  assert.equal(calls.length, 3, 'one pass at a time');
  // New numbers for most rows; one row left the results.
  const fresh = synthetic('rows', calls[2].body.page.ids.filter((id) => id !== syntheticIds[5]), { views: '2' });
  calls[2].resolve(fresh); await flush();
  assert.deepEqual(shownIds(host), syntheticIds.slice(0, 100), 'the same cards in the same order');
  assert.deepEqual(host.current.state.payload.videos.map((video) => video.views), syntheticIds.slice(0, 100).map((id) => (id === syntheticIds[5] ? '1' : '2')));
  assert.equal(host.current.order, order, 'the list did not change');
  assert.deepEqual([host.current.sessionKey, host.current.hasMore, host.current.leftResults, host.current.restoreAnchor], ['read1', true, 0, undefined]);
  assert.equal(host.current.departedShown, 1, 'a card that no longer matches stays and is counted');
  assert.equal(announcements.length, announced, 'nothing is announced: the numbers just change');
  assert.equal(host.current.refreshProblem, undefined);
  // The next pass a period later; the departed row is read too and can come back.
  environment.advance(4 * MINUTE); environment.tick(); await flush();
  assert.equal(calls.length, 4 - 1, 'not before the period');
  environment.advance(MINUTE); environment.tick(); await flush();
  assert.equal(calls.length, 4);
  calls[3].resolve(synthetic('rows', calls[3].body.page.ids, { views: '3' })); await flush();
  assert.equal(host.current.departedShown, 0);
  assert.ok(host.current.state.payload.videos.every((video) => video.views === '3'));
  // The loaded window still continues by slices of the same list.
  host.current.onLoadMore(); await flush();
  assert.deepEqual(calls[4].body.page, { size: 50, growth_lifetime: 'page', ids: syntheticIds.slice(100, 150) });
  calls[4].resolve(synthetic('rows', syntheticIds.slice(100, 150))); await flush();
  assert.equal(shownIds(host).length, 150);
  // Another tab of the page (the hook is disabled): nothing is read; back on Videos it is read again.
  host.update({ ...inputs, enabled: false }); await flush();
  assert.equal(environment.watchers, 0, 'no checks while another tab of the page is open');
  environment.advance(10 * MINUTE);
  host.update({ ...inputs, enabled: true }); await flush();
  assert.equal(calls.length, 6, 'back on the Videos tab');
  assert.equal(calls[5].body.page.ids.length, 150);
  calls[5].resolve(synthetic('rows', calls[5].body.page.ids, { views: '4' })); await flush();
  assert.deepEqual(shownIds(host), syntheticIds.slice(0, 150));
  host.unmount();
  assert.equal(environment.watchers, 0);
  console.log('Background pass: by IDs, only while shown, values in place, no move of cards, list or remembered card.');
}

// 5. Races: a reply of the background pass never overwrites what the user asked for meanwhile.
{
  const environment = testEnvironment(at('2026-11-26T10:00:00Z'));
  const { host, calls, inputs } = mountBrowse({ parsePage: (raw) => raw, environment });
  await flush();
  calls[0].resolve(synthetic('read1', syntheticIds.slice(0, 50), { list: syntheticIds, more: true })); await flush();
  const pass = async () => { environment.advance(5 * MINUTE); environment.tick(); await flush(); return calls[calls.length - 1]; };
  // Another sort while a pass is in flight: the pass is cancelled, and its late reply is ignored.
  const first = await pass();
  assert.equal(first.body.page.ids.length, 50);
  const reversed = [...syntheticIds].reverse();
  host.update({ ...inputs, scopeKey: 'views-asc\nreload:0', listKey: 'views-asc' }); await flush();
  assert.equal(first.signal.aborted, true, 'the list request comes first');
  const list = calls[calls.length - 1];
  assert.deepEqual(list.body.page, { size: 50, growth_lifetime: 'page', order: 'ids' });
  first.resolve(synthetic('rows', first.body.page.ids, { views: 'stale-pass' })); await flush();
  assert.equal(host.current.state.kind, 'loading');
  list.resolve(synthetic('read2', reversed.slice(0, 50), { list: reversed, more: true, views: 'new-sort' })); await flush();
  assert.deepEqual(shownIds(host), reversed.slice(0, 50));
  assert.ok(host.current.state.payload.videos.every((video) => video.views === 'new-sort'), 'the old reply did not touch the new list');
  // Refresh order while a pass is in flight (the same sort): the new read of the list wins as well.
  const second = await pass();
  host.current.onRefreshOrder(); await flush();
  assert.equal(second.signal.aborted, true);
  const again = calls[calls.length - 1];
  again.resolve(synthetic('read3', reversed.slice(0, 50), { list: reversed, more: true, views: 'refreshed-order' })); await flush();
  second.resolve(synthetic('rows', second.body.page.ids, { views: 'stale-pass' })); await flush();
  assert.ok(host.current.state.payload.videos.every((video) => video.views === 'refreshed-order'));
  assert.equal(host.current.sessionKey, 'read3');
  // A next page while a pass is in flight: the page loads; the pass starts over later.
  const third = await pass();
  host.current.onLoadMore(); await flush();
  assert.equal(third.signal.aborted, true);
  const more = calls[calls.length - 1];
  assert.deepEqual(more.body.page.ids, reversed.slice(50, 100));
  third.resolve(synthetic('rows', third.body.page.ids, { views: 'stale-pass' })); await flush();
  more.resolve(synthetic('rows', reversed.slice(50, 100), { views: 'page-2' })); await flush();
  assert.deepEqual(shownIds(host), reversed.slice(0, 100));
  assert.ok(!host.current.state.payload.videos.some((video) => video.views === 'stale-pass'));
  const before = calls.length;
  environment.tick(); await flush();
  environment.advance(45_000); environment.tick(); await flush();
  assert.equal(calls.length, before, 'a cancelled pass does not start again at once');
  environment.advance(15_000); environment.tick(); await flush();
  assert.equal(calls.length, before + 1, 'it is due again a minute after it started');
  assert.equal(calls[before].body.page.ids.length, 100);
  // No pass starts while the list itself is loading.
  calls[before].resolve(synthetic('rows', calls[before].body.page.ids)); await flush();
  host.current.onLoadMore(); await flush();
  const loading = calls.length;
  environment.advance(10 * MINUTE); environment.tick(); await flush();
  assert.equal(calls.length, loading, 'the pass waits for the page');
  host.unmount();
  console.log('Races: another sort, Refresh order and a next page win over a background reply; a cancelled pass starts over.');
}

// 5b. Scrolling: a next page loads every 10 seconds for 30 minutes and cancels the pass each time. The pass
// starts at most once a minute, and with each start it reads other rows first, so the whole list is covered.
{
  const environment = testEnvironment(at('2026-11-26T10:00:00Z'));
  const { host, calls } = mountBrowse({ parsePage: (raw) => raw, environment });
  await flush();
  const longList = Array.from({ length: 20000 }, (_, index) => 's' + String(index).padStart(10, '0'));
  calls[0].resolve(synthetic('read1', longList.slice(0, 50), { list: longList, more: true })); await flush();
  for (let from = 50; from < 1000; from += 50) {
    host.current.onLoadMore(); await flush();
    calls[calls.length - 1].resolve(synthetic('rows', longList.slice(from, from + 50))); await flush();
  }
  const answered = new Set(calls);
  const background = [], completed = [];
  const answer = async () => {
    for (const call of calls) {
      if (answered.has(call)) continue;
      answered.add(call);
      if (call.body.page.size === 200) { call.at = environment.monotonic(); background.push(call); }
      // A cancelled request ends the way fetch ends it.
      if (call.signal?.aborted) { call.reject(new DOMException('Cancelled', 'AbortError')); await flush(); continue; }
      if (call.body.page.size === 200) completed.push(call);
      call.resolve(synthetic('rows', call.body.page.ids)); await flush();
    }
  };
  let loaded = 1000;
  for (let second = 0; second < 1800; second += 1) {
    environment.advance(1000);
    if (second % 15 === 14) environment.tick();
    if (second % 10 === 4) { host.current.onLoadMore(); loaded += 50; }
    await flush(); await answer();
  }
  assert.equal(shownIds(host).length, loaded);
  const cancelled = background.length - completed.length;
  assert.ok(background.length <= 6 * 31, `at most a start a minute, and few reads before the next page cancels: ${background.length} reads`);
  assert.ok(background.length >= 20, 'and the passes do run');
  const read = new Set(completed.flatMap((call) => call.body.page.ids));
  assert.ok(new Set(background.map((call) => Math.floor(call.at / 60_000))).size <= 31 && read.size >= 5000, `other rows are read each time, not the same 200: ${read.size} rows`);
  host.unmount();
  console.log(`Scrolling for 30 minutes: ${background.length} background reads (${cancelled} cancelled), ${read.size} different rows read.`);
}

// 5c. Reads that take time (1 or 3 seconds), a next page every 10 seconds, the viewed card not moving. A pass
// is cancelled between its reads. After a busy server the retry keeps the same minute between starts, and
// the cancelled passes go through the list instead of reading the same 200 cards. A new read of the list
// starts over: the cards around the viewed one first.
{
  const scroll = async ({ fail, readSeconds }) => {
    const environment = testEnvironment(at('2026-11-26T10:00:00Z'));
    const { host, calls } = mountBrowse({ parsePage: (raw) => raw, environment });
    await flush();
    const longList = Array.from({ length: 20000 }, (_, index) => 't' + String(index).padStart(10, '0'));
    calls[0].resolve(synthetic('read1', longList.slice(0, 50), { list: longList, more: true })); await flush();
    for (let from = 50; from < 1000; from += 50) {
      host.current.onLoadMore(); await flush();
      calls[calls.length - 1].resolve(synthetic('rows', longList.slice(from, from + 50))); await flush();
    }
    host.current.onAnchorChange({ videoId: longList[500], offsetPx: 0 });
    const seen = new Set(calls), pending = [], background = [], completed = [];
    let failed = !fail, loaded = 1000, failedAt = 0;
    const pump = async () => {
      for (const call of calls) {
        if (seen.has(call)) continue;
        seen.add(call); call.at = environment.monotonic(); pending.push(call);
        if (call.body.page.size === 200) background.push(call);
      }
      for (const call of [...pending]) {
        const own = call.body.page.size === 200;
        const done = () => pending.splice(pending.indexOf(call), 1);
        if (call.signal?.aborted) { done(); call.reject(new DOMException('Cancelled', 'AbortError')); await flush(); continue; }
        if (environment.monotonic() - call.at < (own ? readSeconds * 1000 : 1000)) continue;
        done();
        if (own && !failed) {
          failed = true; failedAt = environment.monotonic();
          call.reject(Object.assign(new Error('Busy'), { code: 'query_busy', status: 429 })); await flush(); continue;
        }
        if (own) completed.push(call);
        call.resolve(synthetic('rows', own ? call.body.page.ids : longList.slice(loaded - 50, loaded))); await flush();
      }
    };
    for (let second = 0; second < 1800; second += 1) {
      environment.advance(1000);
      if (second % 15 === 14) environment.tick();
      await flush(); await pump();
      if (second % 10 === 7 && !host.current.state.isLoadingMore) { host.current.onLoadMore(); loaded += 50; }
      await flush(); await pump();
    }
    const later = background.filter((call) => call.at > failedAt + 4 * MINUTE);
    return { host, calls, environment, longList, reads: later.length, minutes: new Set(later.map((call) => Math.floor(call.at / MINUTE))).size,
      rows: new Set(completed.flatMap((call) => call.body.page.ids)).size, viewed: completed.filter((call) => call.body.page.ids.includes(longList[500])).length };
  };
  const busy = await scroll({ fail: true, readSeconds: 1 });
  assert.ok(busy.reads <= 2 * 26 && busy.minutes <= 26, `after a busy server: at most a start a minute, ${busy.reads} reads in ${busy.minutes} minutes`);
  assert.ok(busy.rows >= 3000, `and the retries go through the list: ${busy.rows} rows`);
  assert.equal(busy.host.current.refreshProblem, undefined, 'a later good read clears the notice');
  busy.host.unmount();
  const slow = await scroll({ fail: false, readSeconds: 3 });
  assert.ok(slow.rows >= 3000, `passes cancelled after their first read go through the list: ${slow.rows} rows, not the same 200`);
  assert.ok(slow.viewed >= 4 && slow.viewed <= 7, `the cards around the viewed one about once a period: ${slow.viewed} reads in 30 minutes`);
  // A new read of the list: the progress starts over, the cards around the viewed one first.
  const { host, calls, environment, longList } = slow;
  const before = calls.length;
  host.current.onRefreshOrder(); await flush();
  const reread = calls.slice(before).find((call) => call.body.page.size !== 200);
  reread.resolve(synthetic('read2', longList.slice(480, 530), { list: longList, start: 480, more: true })); await flush();
  host.current.onAnchorChange({ videoId: longList[500], offsetPx: 0 });
  const count = calls.length;
  environment.advance(5 * MINUTE); environment.tick(); await flush();
  const pass = calls.slice(count).filter((call) => call.body.page.size === 200);
  assert.equal(pass.length, 1);
  assert.deepEqual([pass[0].body.page.ids[0], pass[0].body.page.ids.length], [longList[500], 50], 'the new list: its own rows, the viewed card first');
  host.unmount();
  console.log(`Slow reads while scrolling: after a busy server ${busy.reads} reads in ${busy.minutes} minutes, ${busy.rows} rows; passes cancelled after the first read: ${slow.rows} rows, the viewed cards ${slow.viewed} times.`);
}

// 6. A failed pass: every card stays as it was, the failure is said in words, retries are few and far apart.
{
  const environment = testEnvironment(at('2026-11-26T10:00:00Z'));
  const { host, calls } = mountBrowse({ parsePage: (raw) => raw, environment });
  await flush();
  calls[0].resolve(synthetic('read1', syntheticIds.slice(0, 50), { list: syntheticIds, more: true, views: 'kept' })); await flush();
  const offline = () => new Error('No connection to the Leti Cook server.');
  const expectedWaits = [5, 10, 20, 30];
  environment.advance(5 * MINUTE); environment.tick(); await flush();
  for (let failure = 1; failure <= 5; failure += 1) {
    assert.equal(calls.length, 1 + failure, `attempt ${failure}`);
    calls[failure].reject(offline()); await flush();
    assert.deepEqual(shownIds(host), syntheticIds.slice(0, 50), 'the cards stay');
    assert.ok(host.current.state.payload.videos.every((video) => video.views === 'kept'), 'with the values they had');
    assert.equal(host.current.state.kind, 'ready');
    if (failure <= 4) {
      assert.match(host.current.refreshProblem, new RegExp(`could not be updated in the background.*next attempt is in ${expectedWaits[failure - 1]} minutes`));
      environment.advance(expectedWaits[failure - 1] * MINUTE - 30_000); environment.tick(); environment.show(true); await flush();
      assert.equal(calls.length, 1 + failure, 'no attempt before its time, also not on a return to the tab');
      environment.advance(30_000); environment.tick(); await flush();
    }
  }
  assert.match(host.current.refreshProblem, /no longer updated in the background.*Refresh order/);
  for (let hour = 0; hour < 48; hour += 1) { environment.advance(HOUR); environment.tick(); environment.show(true); }
  await flush();
  assert.equal(calls.length, 6, 'no endless retries');
  // Reading the list again starts the updates over.
  host.current.onRefreshOrder(); await flush();
  calls[6].resolve(synthetic('read2', syntheticIds.slice(0, 50), { list: syntheticIds, more: true })); await flush();
  assert.equal(host.current.refreshProblem, undefined);
  environment.advance(5 * MINUTE); environment.tick(); await flush();
  assert.equal(calls.length, 8);
  // A busy server is a failure like any other; a later good pass clears the notice.
  calls[7].reject(Object.assign(new Error('Busy'), { code: 'query_busy', status: 503 })); await flush();
  assert.match(host.current.refreshProblem, /next attempt is in 5 minutes/);
  environment.advance(5 * MINUTE); environment.tick(); await flush();
  calls[8].resolve(synthetic('rows', calls[8].body.page.ids, { views: 'fresh' })); await flush();
  assert.equal(host.current.refreshProblem, undefined);
  assert.ok(host.current.state.payload.videos.every((video) => video.views === 'fresh'));
  // A reply that cannot be verified is not applied and stops the updates at once.
  environment.advance(5 * MINUTE); environment.tick(); await flush();
  calls[9].resolve({ ...synthetic('rows', calls[9].body.page.ids), columns: ['video.other'] }); await flush();
  assert.match(host.current.refreshProblem, /no longer updated in the background/);
  assert.ok(host.current.state.payload.videos.every((video) => video.views === 'fresh'));
  // Withdrawn access removes the loaded data, as for every read of the list.
  host.current.onRefreshOrder(); await flush();
  calls[10].resolve(synthetic('read3', syntheticIds.slice(0, 50), { list: syntheticIds, more: true })); await flush();
  environment.advance(5 * MINUTE); environment.tick(); await flush();
  calls[11].reject(Object.assign(new Error('Permission withdrawn'), { status: 403 })); await flush();
  assert.equal(host.current.state.kind, 'error');
  assert.equal(host.current.state.previous, undefined);
  host.unmount();
  console.log('Failed pass: cards and values stay, said in words, 5-10-20-30 minutes then stop; access withdrawn removes data.');
}

// 7. Sleep and wake, and a long list: the wall clock alone makes a pass due; one pass spends a bounded time.
{
  const environment = testEnvironment(at('2026-11-26T10:00:00Z'));
  const { host, calls } = mountBrowse({ parsePage: (raw) => raw, environment });
  await flush();
  calls[0].resolve(synthetic('read1', syntheticIds.slice(0, 50), { list: syntheticIds, more: true })); await flush();
  for (let from = 50; from < 450; from += 50) {
    host.current.onLoadMore(); await flush();
    calls[calls.length - 1].resolve(synthetic('rows', syntheticIds.slice(from, from + 50))); await flush();
  }
  assert.equal(shownIds(host).length, 450);
  const loaded = calls.length;
  // The computer slept for an hour: timers did not run and the monotonic clock stood still.
  environment.advance(HOUR, { asleep: true });
  assert.equal(calls.length, loaded);
  environment.tick(); await flush();                 // the first check after waking
  assert.equal(calls.length, loaded + 1, 'the pass is due by the wall clock');
  assert.equal(calls[loaded].body.page.ids.length, 200, 'at most 200 videos per read');
  // Reads of one pass follow one another with a pause; none run in parallel.
  environment.advance(300);                          // the read took 300 ms
  calls[loaded].resolve(synthetic('rows', calls[loaded].body.page.ids, { views: '2' })); await flush();
  assert.equal(calls.length, loaded + 1, 'a pause before the next read');
  environment.advance(300); await flush();
  assert.equal(calls.length, loaded + 2);
  assert.equal(calls[loaded + 1].body.page.ids.length, 200);
  assert.deepEqual(calls[loaded].body.page.ids, syntheticIds.slice(0, 200), 'the cards around the viewed one (here: the top) first');
  assert.deepEqual(calls[loaded + 1].body.page.ids, syntheticIds.slice(200, 400), 'then the least recently read');
  // A slow server: the pass stops when its request time is spent; the rest waits for the next pass.
  environment.advance(refresh.REFRESH_BUDGET_MS);
  calls[loaded + 1].resolve(synthetic('rows', calls[loaded + 1].body.page.ids, { views: '2' })); await flush();
  environment.advance(5_000); await flush();
  assert.equal(calls.length, loaded + 2, 'the budget of one pass is spent');
  assert.equal(host.current.state.payload.videos.filter((video) => video.views === '2').length, 400);
  assert.deepEqual(shownIds(host), syntheticIds.slice(0, 450));
  // The next pass: the viewed cards again, then the rows the last pass did not reach. No row waits forever.
  environment.advance(5 * MINUTE); environment.tick(); await flush();
  assert.equal(calls.length, loaded + 3);
  assert.deepEqual(calls[loaded + 2].body.page.ids, syntheticIds.slice(0, 200));
  environment.advance(refresh.REFRESH_BUDGET_MS + 1);
  calls[loaded + 2].resolve(synthetic('rows', calls[loaded + 2].body.page.ids, { views: '3' })); await flush();
  environment.advance(2_000); await flush();
  assert.equal(calls.length, loaded + 4, 'a pass always makes its second read');
  assert.deepEqual(calls[loaded + 3].body.page.ids.slice(0, 50), syntheticIds.slice(400, 450));
  calls[loaded + 3].resolve(synthetic('rows', calls[loaded + 3].body.page.ids, { views: '3' })); await flush();
  assert.ok(host.current.state.payload.videos.every((video) => video.views !== '1'));
  host.unmount();
  console.log('Sleep and wake: due by the wall clock; a long list: 200 per read, one after another, a bounded pass.');
}

// 7b. A short absence from the tab: one read (the cards around the viewed one), not a full pass; the full pass keeps its period.
{
  const environment = testEnvironment(at('2026-11-26T10:00:00Z'));
  const { host, calls } = mountBrowse({ parsePage: (raw) => raw, environment });
  await flush();
  calls[0].resolve(synthetic('read1', syntheticIds.slice(0, 50), { list: syntheticIds, more: true })); await flush();
  for (let from = 50; from < 450; from += 50) {
    host.current.onLoadMore(); await flush();
    calls[calls.length - 1].resolve(synthetic('rows', syntheticIds.slice(from, from + 50))); await flush();
  }
  const loaded = calls.length;
  host.current.onAnchorChange({ videoId: syntheticIds[300], offsetPx: 0 });
  environment.show(false); environment.advance(2 * MINUTE); environment.show(true); await flush();
  assert.equal(calls.length, loaded + 1);
  assert.deepEqual([...calls[loaded].body.page.ids].sort(), syntheticIds.slice(200, 400), 'the 200 cards around the viewed one');
  calls[loaded].resolve(synthetic('rows', calls[loaded].body.page.ids, { views: '2' })); await flush();
  environment.advance(5_000); await flush();
  assert.equal(calls.length, loaded + 1, 'one read, not a pass');
  environment.show(false); environment.show(true); await flush();
  assert.equal(calls.length, loaded + 1, 'and not again within a minute');
  environment.advance(3 * MINUTE); environment.tick(); await flush();
  assert.equal(calls.length, loaded + 2, 'the full pass is due a period after the list was read');
  host.unmount();
  console.log('Short absence: one read around the viewed card; the full pass keeps its period.');
}

// 8. The whole path with real wire rows on the test clock: a value ends while the page is open. The card
// stays and shows Needs refresh; the row is read again soon; while the program has nothing newer it stays
// Needs refresh; when the collector has brought new values they take their place.
{
  const environment = testEnvironment(READ_AT);
  const stamp = (milliseconds) => new Date(milliseconds).toISOString().replace('Z', '000Z');
  const withoutGrowth = (response, request) => {
    request.select = request.select.filter((field) => !field.startsWith('observation.'));
    response.columns = request.select;
    for (const row of response.rows) for (const field of Object.keys(row.cells)) if (field.startsWith('observation.')) delete row.cells[field];
  };
  // The list as the server returns it for a first page without a remembered card: these four rows at its top.
  const opened = firstPage((response, request) => {
    withoutGrowth(response, request);
    request.page = { size: 4, growth_lifetime: 'page', order: 'ids' };
    response.page.order.ids = [...firstRows, ...listIds.filter((id) => !firstRows.includes(id))].join('');
  });
  const { host, calls } = mountBrowse({ environment, channelId: opened.request.scope.id,
    plan: { ...opened.request, page: { size: 4 } },
    parsePage: (raw, expect) => videos.parseLibraryPage(delivered(raw, environment.monotonic()), expect) });
  await flush();
  assert.deepEqual(calls[0].body.page, opened.request.page);
  calls[0].resolve(opened.response); await flush();
  assert.equal(host.current.state.kind, 'ready');
  assert.deepEqual(shownIds(host), firstRows);
  assert.deepEqual([host.current.state.payload.videos[0].title, host.current.state.payload.videos[0].views], ['Growth gLlatemeta0', '6120']);
  // What the server answers for these rows at the test clock's instant; `collected` rows have new values by then.
  const answer = (call, collected = []) => {
    const reply = rowsRead(call.body.page.ids, (response) => {
      response.columns = call.body.select;
      const evaluated = environment.now(), until = evaluated + 2 * MINUTE;
      Object.assign(response.context, { evaluated_at: stamp(evaluated), valid_before: stamp(until) });
      Object.assign(response, { source_read_at: stamp(evaluated), source_expires_at: stamp(until) });
      for (const row of response.rows) {
        for (const field of Object.keys(row.cells)) if (field.startsWith('observation.')) delete row.cells[field];
        for (const value of Object.values(row.cells)) {
          if (collected.includes(row.id)) Object.assign(value, { observed_at: stamp(evaluated - MINUTE), expires_at: stamp(evaluated + 696 * HOUR) });
          else if (at(value.expires_at) <= until) Object.assign(value, { state: 'expired', value: null, observed_at: null, expires_at: null });
        }
      }
    });
    call.resolve(reply.response);
  };
  // The page stays open. 16 hours later the first row's metadata ends (03:44:15 the next day).
  environment.advance(at('2026-10-31T03:44:15.221Z') - READ_AT - MINUTE); await flush();
  assert.equal(calls.length, 1, 'nothing ended yet, no check ran: nothing is read');
  assert.equal(host.current.state.payload.videos[0].title, 'Growth gLlatemeta0');
  environment.advance(2 * MINUTE); await flush();
  let shown = host.current.state.payload.videos;
  assert.deepEqual(shown.map((video) => video.video_id), firstRows, 'the card stays in its place');
  assert.deepEqual([shown[0].title, shown[0].value_states.published_at, shown[0].views], [undefined, 'expired', '6120'],
    'the ended value shows Needs refresh at once; the others stay');
  assert.equal(calls.length, 2, 'and the loaded rows are read again');
  assert.equal(calls[1].body.page.ids[0], firstRows[0], 'the row with the ended value first');
  assert.deepEqual([...calls[1].body.page.ids].sort(), [...firstRows].sort());
  assert.deepEqual(calls[1].body.select, opened.request.select, 'the same fields');
  // The program has nothing newer: the server says expired, the card keeps showing Needs refresh.
  answer(calls[1]); await flush();
  shown = host.current.state.payload.videos;
  assert.deepEqual(shown.map((video) => video.video_id), firstRows);
  assert.deepEqual([shown[0].title, shown[0].value_states.published_at, shown[0].views, shown[0].stale], [undefined, 'expired', '6120', true]);
  assert.equal(shown[1].title, 'Growth gLbg0800000');
  environment.advance(MINUTE); environment.tick(); await flush();
  assert.equal(calls.length, 2, 'no re-read loop while nothing newer exists');
  // The collector has brought new values: the next pass puts them in place.
  environment.advance(4 * MINUTE); environment.tick(); await flush();
  assert.equal(calls.length, 3);
  answer(calls[2], [firstRows[0]]); await flush();
  shown = host.current.state.payload.videos;
  assert.deepEqual(shown.map((video) => video.video_id), firstRows, 'still the same order');
  assert.deepEqual([shown[0].title, shown[0].value_states, shown[0].stale, shown[0].views], ['Growth gLlatemeta0', undefined, undefined, '6120']);
  assert.equal(host.current.sessionKey.includes('\n'), true, 'the same read of the list throughout');
  assert.equal(host.current.departedShown, 0);
  host.unmount();
  assert.equal(environment.watchers + environment.timers, 0, 'no timer survives the page');
  console.log('Wire rows on the test clock: the card stays, Needs refresh while nothing newer, new values in place.');
}

console.log('Library Videos refresh passed.');
