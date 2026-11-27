// Library Videos on the shared backend: frozen pages → the cards
// of the grid for Long, Shorts and Streams; growth through the one parser
// (growthFromCells); "Select all matching" as one id export; group counts
// (video.facets.v1) with each group's own condition removed; capabilities and
// presets; the Save view v2 writer; the workshop hand-off; the honest growth
// labels; (no "Load more" button while a page loads); filters
// in the codec. Offline: real wire examples of a temporary PostgreSQL 17 (fixture),
// no browser or network.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from '../application/frontend/node_modules/typescript/lib/typescript.js';
import * as React from '../application/frontend/node_modules/react/index.js';
import * as jsxRuntime from '../application/frontend/node_modules/react/jsx-runtime.js';
import { renderToStaticMarkup } from '../application/frontend/node_modules/react-dom/server.node.js';
import { ExplorerContractError } from '../application/frontend/src/explorer/contracts.ts';
import * as lifetime from '../application/frontend/src/sourceLifetime.ts';
import * as videos from '../application/frontend/src/explorer/libraryData/videos.ts';
import * as wire from '../application/frontend/src/explorer/libraryData/wire.ts';
import * as inspector from '../application/frontend/src/explorer/libraryData/inspector.ts';
import * as saved from '../application/frontend/src/explorer/libraryData/savedLibrary.ts';
import * as conditions from '../application/frontend/src/explorer/libraryData/conditions.ts';
import * as catalogue from '../application/frontend/src/explorer/catalogueQuery.ts';
import * as presets from '../application/frontend/src/explorer/libraryData/presets.ts';
import { libraryGates } from '../application/frontend/src/explorer/libraryData/capabilities.ts';
import * as refresh from '../application/frontend/src/explorer/catalogueRefresh.ts';
import { reactHookHost, flush } from './react_hook_host.mjs';

// The background update of loaded cards has its own test (test_library_videos_refresh.mjs); here it stays idle.
const idleRefresh = { now: () => Date.now(), monotonic: () => performance.now(), visible: () => true, watch: () => () => {}, after: () => () => {} };

const read = (path) => readFileSync(new URL(path, import.meta.url), 'utf8');
const clone = (value) => structuredClone(value);
const contract = (fn, pattern) => assert.throws(fn, (error) => error instanceof ExplorerContractError
  && (!pattern || pattern.test(error.message)), String(pattern ?? 'contract error'));
const fixture = JSON.parse(read('./fixtures/library_videos_wire.json'));
const example = (name) => clone(fixture[name]);
// As requestJson delivers a reply: the source clock of the HTTP layer (the session's valid_before).
// The fixture replies carry the HTTP read envelope (tests/library_wire_fixtures_pg.py); the headers repeat it.
const delivered = (response) => lifetime.captureSourceLifetime(response, { get: (name) => (name === 'X-Source-Expires-At'
  ? response.source_expires_at ?? response.context?.valid_before ?? null
  : name === 'X-Source-Read-At' ? response.source_read_at ?? response.context?.evaluated_at ?? null : null) }, 0);
const page = (name, mutate) => {
  const { request, response } = example(name);
  mutate?.(response, request);
  return videos.parseLibraryPage(delivered(response), { request, channelId: request.scope.id });
};
const plain = (value) => lifetime.withoutSourceLifetime(value);
const capabilities = JSON.parse(read('./fixtures/research_capabilities_window_limits.json'));

function compiled(file, imports = {}) {
  const { outputText } = ts.transpileModule(read('../application/frontend/src/explorer/' + file),
    { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX } });
  const exports = {};
  new Function('require', 'exports', outputText)((name) => { assert.ok(name in imports, `${file}: ${name}`); return imports[name]; }, exports);
  return exports;
}

// 1. The frozen request: card fields, frozen Inspector values, growth (growth order).
{
  const gates = libraryGates(capabilities, 'long');
  // The tag count is frozen with the other calculations (the tag group counts the frozen list).
  assert.deepEqual(videos.libraryFrozenFields(gates), ['video.title_length', 'video.tag_count', 'video.like_rate', 'video.comment_rate']);
  // measured over 64 MiB with YouTube-limit tags: the tags themselves stay current unless switched on.
  assert.equal(videos.FROZEN_TAGS, false);
  assert.deepEqual(videos.libraryFrozenFields(gates, true), ['video.title_length', 'video.tag_count', 'video.like_rate', 'video.comment_rate', 'video.tags']);
  assert.deepEqual(videos.libraryFrozenFields({ ...gates, derivedFilters: false }), [], 'no calculations without the Baseline gate');
  assert.deepEqual(videos.libraryFrozenFields(null), []);
  assert.deepEqual(videos.libraryGrowthFields(true), [...inspector.GROWTH_FIELDS]);
  assert.deepEqual(videos.libraryGrowthFields(false), []);
  const memory = fixture.frozen_session_bytes_20000_rows;
  assert.ok(memory.base9_derived4_growth5.bytes < 64 * 1048576, 'the shipped select fits one result');
  // Repeat the measurement with the browser-built select (tag count frozen): 20,000 rows under the shipped store limit.
  assert.deepEqual(memory.shipped_select_p5.select, [...wire.LIBRARY_SELECT, ...videos.libraryFrozenFields(gates), ...inspector.GROWTH_FIELDS]);
  assert.ok(memory.shipped_select_p5.bytes < 64 * 1048576 && memory.shipped_select_p5.rows > 19000);
  assert.ok(memory.base9_derived4_tags_youtube_max35x13_growth5.bytes > 64 * 1048576, 'tags at the YouTube limit do not');
  // The translated query plus these fields is exactly the executed request of the fixture.
  const translated = example('translate').response.translation.query;
  const request = wire.libraryQuery(translated, { timeZone: 'Europe/Berlin', fields: videos.libraryFrozenFields(gates), growthFields: videos.libraryGrowthFields(true), page: { anchorId: 'gLbg0800000' } });
  assert.deepEqual(request.select, [...wire.LIBRARY_SELECT, 'video.title_length', 'video.tag_count', 'video.like_rate', 'video.comment_rate', ...inspector.GROWTH_FIELDS]);
  assert.deepEqual(request.page, { size: 50, anchor_id: 'gLbg0800000' });
  assert.equal(request.consistency, 'frozen');
  assert.deepEqual(request.where, translated.where, 'membership comes only from the translation');
  console.log('Frozen request: card fields, frozen calculations, tags measured, growth fields.');
}

// 2. Pages → cards (real replies: Long first page at an anchor, next, previous; Shorts; Streams; no growth).
{
  const first = page('long_first_anchor');
  assert.equal(first.video_type, 'long');
  assert.equal(first.videos.length, 4);
  assert.equal(first.browse.start_index, 4);
  assert.equal(first.browse.total_count, 37);
  assert.equal(first.browse.anchor_video_id, 'gLbg0800000');
  assert.ok(first.videos.some((video) => video.video_id === 'gLbg0800000'));
  assert.equal(first.growth, true);
  assert.equal(first.pagination.matched_count, 37);
  assert.deepEqual(plain(first.coverage), { complete: false, notice: { partial: true, withheldRows: 1, staleValueRows: 0, inventoryIncomplete: 0, other: [] } });
  const raw = example('long_first_anchor').response.rows[0];
  const card = first.videos[0];
  assert.equal(card.video_id, raw.id);
  assert.equal(card.title, raw.cells['video.title'].value);
  assert.equal(card.thumbnail_url, `https://img.youtube.com/vi/${raw.id}/hqdefault.jpg`);
  assert.equal(card.published_at, raw.cells['video.published_at'].value);
  assert.equal(card.duration, Number(raw.cells['video.duration_seconds'].value), 'duration in seconds');
  assert.equal(card.views, raw.cells['video.views'].value, 'counters stay decimal strings');
  assert.equal(card.like_count, raw.cells['video.likes'].value);
  assert.equal(card.comment_count, raw.cells['video.comments'].value);
  assert.equal(card.category_id, raw.cells['video.category_id'].value);
  assert.equal(card.checked_at, raw.cells['video.views'].observed_at, 'Counters checked = the views clock');
  assert.deepEqual([...card.tags], ['growth', 'fixture']);
  // Growth: exactly what the Inspector's one parser reads from the same cells.
  assert.deepEqual(plain(card.view_growth), inspector.growthFromCells(raw.cells, 'row'));
  assert.equal(card.view_growth.status, 'ready');
  assert.equal(card.view_growth.time_zone, 'Europe/Berlin');
  // A row lives as long as its own known values, never shorter than the session.
  const known = Object.values(raw.cells).filter((cell) => cell.state === 'known' && cell.expires_at).map((cell) => cell.expires_at).sort();
  assert.equal(card.source_expires_at, known[0]);
  assert.ok(Date.parse(card.source_expires_at) >= Date.parse(first.browse.expires_at));
  const next = page('long_next'), previous = page('long_previous');
  assert.equal(next.browse.session_key, first.browse.session_key, 'one session: same fingerprint, clocks and total');
  assert.notEqual(next.browse.frozen_ref, first.browse.frozen_ref, 'tokens differ on every page');
  const merged = videos.mergeLibraryPages(videos.mergeLibraryPages(first, next, 'next'), previous, 'previous');
  assert.equal(merged.videos.length, 12);
  assert.equal(merged.browse.start_index, 0);
  assert.equal(merged.browse.previous_cursor, null);
  assert.equal(merged.browse.next_cursor, next.browse.next_cursor);
  contract(() => videos.mergeLibraryPages(first, previous, 'next'), /adjacent/);
  contract(() => videos.mergeLibraryPages(first, first, 'next'), /adjacent|repeated/);
  // Another session: the same pages with a later end (instants follow the generating run's clock).
  const otherSession = page('long_next', (response) => {
    response.context.valid_before = new Date(Date.parse(response.context.valid_before) + 1000).toISOString().replace('Z', '000Z'); });
  contract(() => videos.mergeLibraryPages(first, otherSession, 'next'), /another browsing session/);
  // The lifetime of the reply's root reaches the presentation; rows keep their own.
  assert.equal(lifetime.ownSourceLifetime(first).expiresAt, Date.parse(first.browse.expires_at), 'the page ends with the session');
  assert.equal(lifetime.ownSourceLifetime(first.videos[0]).expiresAt, Date.parse(card.source_expires_at), 'a row lives as its own values');
  assert.ok(lifetime.ownSourceLifetime(first.videos[0]).expiresAt > lifetime.ownSourceLifetime(first).expiresAt, 'a row outlives the session');
  for (const [name, type, count] of [['short_first', 'short', 23], ['stream_first', 'stream', 25]]) {
    const typed = page(name);
    assert.equal(typed.video_type, type);
    assert.equal(typed.videos.length, count);
    assert.ok(typed.videos.every((video) => video.video_type === type && video.view_growth));
    assert.ok(new Set(typed.videos.map((video) => video.view_growth.status)).size >= 4, `${type}: all kinds of growth`);
  }
  const noGrowth = page('long_plain');
  assert.equal(noGrowth.growth, false);
  assert.ok(noGrowth.videos.every((video) => video.view_growth === undefined));
  assert.equal(noGrowth.time_zone, 'UTC');
  console.log('Pages: Long (anchor, next, previous), Shorts, Streams, no growth; one session key; growth = Inspector parser.');
}

// 3. Strictness: nothing is shown partially.
{
  contract(() => page('long_first_anchor', (r) => { r.extra = 1; }), /unknown key/);
  contract(() => page('long_first_anchor', (r) => { r.context.time_zone = 'UTC'; }), /time zone/);
  contract(() => page('long_first_anchor', (r) => { r.context.consistency = 'current'; }), /consistency/);
  contract(() => page('long_first_anchor', (r) => { r.columns = [...r.columns].reverse(); }), /requested fields/);
  contract(() => page('long_first_anchor', (r) => { r.rows[1] = clone(r.rows[0]); }), /repeated/);
  contract(() => page('long_first_anchor', (r) => { r.rows[0].cells['video.views'].extra = 1; }), /unknown key/);
  contract(() => page('long_first_anchor', (r) => { r.rows[0].cells['video.views'].state = 'stale'; }), /expected one of/);
  contract(() => page('long_first_anchor', (r) => { r.rows[0].cells['video.views'].value = null; }), /needs a value/);
  contract(() => page('long_first_anchor', (r) => { r.rows[0].cells['video.views'].value = 12; }), /unsigned decimal/);
  contract(() => page('long_first_anchor', (r) => { r.rows[0].cells['video.views'].value = '18446744073709551616'; }), /unsigned decimal/);
  contract(() => page('long_first_anchor', (r) => { r.rows[0].cells['video.thumbnail_url'].value = 'https://example.test/x.jpg'; }), /thumbnail/);
  contract(() => page('long_first_anchor', (r) => { r.rows[0].cells['video.views'].provenance_ref = 'nope'; }), /provenance/);
  contract(() => page('long_first_anchor', (r) => { r.rows[0].cells['video.views'].expires_at = r.context.evaluated_at; }), /inside the session/);
  contract(() => page('long_first_anchor', (r) => { r.page.anchor_id = 'gLhuge00000'; }), /anchor/);
  contract(() => videos.mergeLibraryPages(page('long_first_anchor'), page('long_next', (r) => { r.page.total_count = 38; }), 'next'), /another browsing session/);
  contract(() => page('short_first', (r) => { r.page.total_count = 24; }), /last page/);
  contract(() => page('long_first_anchor', (r) => { r.rows[0].cells['observation.views_gained_28d'].value = '1'; }), /growth evidence/);
  contract(() => page('long_first_anchor', (_r, q) => { q.page.size = 3; }), /bounded list/);
  contract(() => videos.parseLibraryPage(example('long_first_anchor').response, { request: example('long_plain').request, channelId: 'x' }), /time zone|requested fields|bounded/);
  // Exact counters: the largest uint64 stays a string; a duration beyond 2^53 is not shown.
  const big = page('long_first_anchor', (r) => {
    r.rows[0].cells['video.views'].value = '18446744073709551615';
    r.rows[0].cells['video.duration_seconds'].value = '9007199254740993';
  });
  assert.equal(big.videos[0].views, '18446744073709551615');
  assert.equal(big.videos[0].duration, undefined);
  // An expired value stays in the row as "—" with "Needs refresh"; unavailable likewise; unknown is "not reported".
  const stale = page('long_first_anchor', (r) => {
    Object.assign(r.rows[0].cells['video.views'], { state: 'expired', value: null });
    Object.assign(r.rows[0].cells['video.published_at'], { state: 'unavailable', value: null });
    Object.assign(r.rows[0].cells['video.title'], { state: 'unknown', value: null });
  });
  assert.deepEqual(plain(stale.videos[0].value_states), { views: 'expired', published_at: 'unavailable' });
  assert.equal(stale.videos[0].stale, true);
  assert.equal(stale.videos[0].views, undefined);
  assert.equal(stale.videos[0].title, undefined);
  // Unknown coverage reasons are kept and counted, never dropped.
  const reasons = page('long_first_anchor', (r) => { r.coverage.reasons = { source_rows_unavailable: 1, rows_with_expired_values: 3, future_reason: 2 }; });
  assert.deepEqual(plain(reasons.coverage.notice), { partial: true, withheldRows: 1, staleValueRows: 3, inventoryIncomplete: 0, other: [{ code: 'future_reason', count: 2 }] });
  console.log('Strict pages: unknown keys, states, order, anchors, totals, provenance, clocks; uint64; freshness states; open reasons.');
}

// 4. "Select all matching": one id export of the session, checked against the pages.
{
  const first = page('long_first_anchor');
  const reply = example('long_frozen_ids').response;
  const ids = videos.parseFrozenIds(reply, first);
  assert.equal(ids.length, 37);
  assert.deepEqual(ids.slice(4, 8), first.videos.map((video) => video.video_id), 'the ids are in the order of the pages');
  contract(() => videos.parseFrozenIds({ ...clone(reply), total_count: 36 }, first), /session|every id/);
  contract(() => videos.parseFrozenIds({ ...clone(reply), ids: [...reply.ids.slice(0, 36), reply.ids[0]] }, first), /every id/);
  contract(() => videos.parseFrozenIds({ ...clone(reply), context: { ...reply.context, time_zone: 'UTC' } }, first), /frozen result/);
  contract(() => videos.parseFrozenIds({ ...clone(reply), query_fingerprint: 'f'.repeat(64) }, first), /another browsing session/);
  contract(() => videos.parseFrozenIds({ ...clone(reply), ids: [...reply.ids].reverse() }, first), /order/);
  const records = videos.selectionRecords(ids, first.videos, 'long');
  assert.equal(records.length, 37);
  assert.equal(records[4], first.videos[0], 'a loaded row keeps its values');
  assert.deepEqual(records[0], { video_id: ids[0], video_type: 'long' });
  assert.deepEqual(wire.frozenIdsRequest(first.browse.frozen_ref), { version: 1, frozen_ref: first.browse.frozen_ref });
  console.log('Select all matching: one export, order and identity checked; loaded rows keep values.');
}

// 5. Group counts: each group without its own condition; one switch for the frozen population.
{
  const translated = example('translate').response.translation.query;
  const groups = ['views', 'duration', 'likes', 'comments', 'title_length', 'tag_count', 'published', 'category'];
  const split = videos.splitFacetConditions(translated.where, groups);
  assert.deepEqual(split, { base: null, conditions: { published: translated.where.all[0], views: translated.where.all[1] } });
  const cohort = { all: [{ field: 'video.published_at', op: 'gte', value: '2026-01-01T00:00:00Z' }, { field: 'video.published_at', op: 'lte', value: '2026-02-01T00:00:00Z' }] };
  const search = { field: 'video.title', op: 'contains_casefold_v1', value: 'guide' };
  const minViews = { field: 'video.views', op: 'gte', value: '10' }, maxViews = { field: 'video.views', op: 'lte', value: '99' };
  const category = { field: 'video.category_id', op: 'eq', value: '22' };
  const rate = { field: 'video.like_rate', op: 'gte', value: '2.5' };
  const growth = { field: 'observation.views_gained_28d', op: 'is_known' };
  assert.deepEqual(videos.splitFacetConditions({ all: [search, category, minViews, maxViews, rate, cohort, growth] }, groups),
    { base: { all: [search, rate, cohort, growth] }, conditions: { category, views: { all: [minViews, maxViews] } } },
    'search, rates, growth and the Overview cohort stay base conditions');
  assert.deepEqual(videos.splitFacetConditions(search, groups), { base: search, conditions: {} });
  assert.deepEqual(videos.splitFacetConditions(undefined, groups), { base: null, conditions: {} });
  // Title length and tags count only with the Baseline calculations.
  assert.deepEqual(videos.facetGroups(libraryGates(capabilities, 'long')), groups);
  assert.deepEqual(videos.facetGroups({ ...libraryGates(capabilities, 'long'), derivedFilters: false }), groups.filter((g) => g !== 'title_length' && g !== 'tag_count'));
  const current = videos.facetsRequest(translated, { timeZone: 'Europe/Berlin', groups, frozenRef: 'ref-1', frozen: false });
  assert.equal(videos.FROZEN_FACETS, true, 'the server counts a frozen list');
  assert.deepEqual(videos.facetsRequest(translated, { timeZone: 'Europe/Berlin', groups, frozenRef: 'ref-1' }).parameters.population,
    { kind: 'frozen', frozen_ref: 'ref-1' }, 'the switch is on');
  assert.deepEqual(videos.facetsRequest(translated, { timeZone: 'Europe/Berlin', groups }).parameters.population, { kind: 'query' },
    'without a list (it is being built) the current population is counted');
  assert.deepEqual(current.parameters, { groups, conditions: split.conditions, population: { kind: 'query' } });
  assert.deepEqual(Object.keys(current), ['version', 'operation', 'query', 'parameters']);
  assert.equal(current.operation, 'video.facets.v1');
  assert.equal(current.query.consistency, undefined, 'operations read current queries');
  assert.equal(current.query.page, undefined);
  assert.equal(current.query.where, undefined);
  assert.deepEqual(current.query.time, { mode: 'current', timezone: 'Europe/Berlin' });
  const frozen = videos.facetsRequest(translated, { timeZone: 'Europe/Berlin', groups, frozenRef: 'ref-1', frozen: true });
  assert.deepEqual(frozen.parameters.population, { kind: 'frozen', frozen_ref: 'ref-1' }, 'the server key is frozen_ref');
  assert.deepEqual({ ...frozen.parameters, population: undefined }, { ...current.parameters, population: undefined }, 'same base and conditions');
  // The real reply (the panel never asks for the growth group: it is removed from the requested set).
  const reply = example('facets_query').response;
  delete reply.result.groups.growth;
  const facets = videos.parseFacets(reply, { videoType: 'long', groups, timeZone: 'Europe/Berlin' });
  assert.equal(facets.population, 'query');
  assert.equal(facets.matched_count, 37);
  assert.deepEqual(facets.facets.views.map((row) => row.key), presets.presetsFor('long', 'views').map((row) => row.key));
  assert.deepEqual(facets.facets.published, { years: [{ year: 2026, count: 35 }, { year: 2025, count: 2 }], older: null });
  assert.deepEqual(facets.categories, [{ category_id: '24', category_name: 'Category 24', count: 37 }]);
  assert.equal(facets.coverage.complete, false, 'one completeness rule: coverage.status');
  assert.deepEqual([...videos.categoryLabels(facets)], [['24', 'Category 24']]);
  const facetContract = (mutate, expect = {}, pattern) => {
    const value = clone(reply); mutate(value);
    contract(() => videos.parseFacets(value, { videoType: 'long', groups, timeZone: 'Europe/Berlin', ...expect }), pattern);
  };
  facetContract((r) => { r.result.groups.views.buckets[0].label = 'Under 1000'; }, {}, /preset/);
  facetContract((r) => { r.result.groups.duration.buckets.pop(); }, {}, /presets of this type/);
  facetContract((r) => { r.result.groups.extra = {}; }, {}, /unknown key/);
  facetContract((r) => { delete r.result.groups.category; }, {}, /missing key/);
  facetContract((r) => { r.result.time_zone = 'UTC'; }, {}, /time zone/);
  facetContract(() => {}, { videoType: 'short' }, /preset/);
  facetContract(() => {}, { frozenTotal: 37 }, /population/);
  facetContract((r) => { r.result.groups.published.older = { before_year: 2025, count: 1 }; }, {}, /older/);
  facetContract((r) => { r.result.groups.views.buckets[0].count = 999; }, {}, /more rows/);
  // A frozen reply must count exactly the frozen list.
  const frozenReply = clone(reply); frozenReply.result.population.kind = 'frozen';
  assert.equal(videos.parseFacets(frozenReply, { videoType: 'long', groups, timeZone: 'Europe/Berlin', frozenTotal: 37 }).population, 'frozen');
  contract(() => videos.parseFacets(frozenReply, { videoType: 'long', groups, timeZone: 'Europe/Berlin', frozenTotal: 36 }), /frozen total/);
  // Temporary PostgreSQL 17: the server counts the frozen list for the request the
  // browser builds. The list froze every group field (tag count included), so the reply is the frozen population.
  const frozenCase = fixture.facets_frozen;
  assert.equal(frozenCase.request.parameters.population.kind, 'frozen');
  // The generator (tests/library_wire_fixtures_pg.py) sent exactly what the browser modules build for this list.
  const listGates = libraryGates(capabilities, 'long');
  assert.deepEqual(wire.libraryQuery(frozenCase.translated, { timeZone: 'Europe/Berlin', fields: videos.libraryFrozenFields(listGates),
    growthFields: videos.libraryGrowthFields(true) }), frozenCase.list_request);
  assert.deepEqual(videos.facetsRequest(frozenCase.translated, { timeZone: 'Europe/Berlin', groups,
    frozenRef: frozenCase.request.parameters.population.frozen_ref }), frozenCase.request);
  assert.deepEqual(frozenCase.request.parameters.groups, groups);
  const frozenFacets = videos.parseFacets(clone(frozenCase.response), { videoType: 'long', groups, timeZone: 'Europe/Berlin', frozenTotal: frozenCase.list_total });
  assert.equal(frozenFacets.population, 'frozen');
  assert.equal(frozenFacets.matched_count, frozenCase.list_total);
  contract(() => videos.parseFacets(clone(frozenCase.response), { videoType: 'long', groups, timeZone: 'Europe/Berlin', frozenTotal: frozenCase.list_total + 1 }), /frozen total/);
  // Static details over the same frozen list: the count is read with the shown (current) tags, not the frozen count.
  const detailsCase = fixture.static_frozen_tag_count;
  assert.deepEqual(detailsCase.request.parameters.population.kind, 'frozen');
  assert.deepEqual(inspector.inspectorRequest(inspector.INSPECTOR_OPERATIONS.staticDetails, { channelId: frozenCase.translated.scope.id,
    videoType: 'long', frozenRef: detailsCase.request.parameters.population.frozen_ref, sessionKey: 's', checkedAt: null, active: true },
  detailsCase.request.parameters.video_id), detailsCase.request);
  assert.ok(detailsCase.response.result.inspection.frozen_fields.includes('video.tag_count'));
  assert.equal(detailsCase.response.result.inspection.frozen_fields.includes('video.tags'), false);
  const details = inspector.parseStaticDetails(clone(detailsCase.response), { videoId: detailsCase.request.parameters.video_id, videoType: 'long', mode: 'frozen' });
  assert.equal(details.tags.state, 'known');
  assert.deepEqual(details.tag_count, { state: 'known', value: details.tags.value.length });
  const mixed = clone(detailsCase.response);
  mixed.result.derived['video.tag_count'].observed_at = '2020-01-01T00:00:00Z';
  contract(() => inspector.parseStaticDetails(mixed, { videoId: detailsCase.request.parameters.video_id, videoType: 'long', mode: 'frozen' }), /read with the tags/);
  console.log('Facets: own-condition split, frozen switch on (real frozen reply), presets and years strict; Inspector tag count read with its tags.');
}

// 6. Capabilities and per-type presets: the panel draws the server's own sets.
{
  const parsed = videos.parseLibraryCapabilities(capabilities);
  const shape = (rows) => JSON.stringify(Array.isArray(rows) ? rows.map(({ key, label, min, max }) => [key, label, min, max]) : null);
  for (const type of ['long', 'short', 'stream']) {
    for (const group of presets.PRESET_GROUPS) {
      assert.equal(shape(parsed.presets.groups[group]?.buckets[type]), shape(presets.presetsFor(type, group)), `${type} ${group}`);
    }
    const gates = libraryGates(parsed, type);
    assert.equal(gates.facets && gates.growth && gates.derivedFilters && gates.frozen.available, true, type);
    assert.equal(gates.field('video.caption_available'), true);
  }
  contract(() => videos.parseLibraryCapabilities({ ...capabilities, source_id: 'public.channels.current.v1' }), /video source/);
  console.log('Capabilities: gates per type; the bundled presets equal capabilities.presets for every type.');
}

// 7. Save view: v2 + presentation, no read mode, recognised back.
{
  const translated = example('translate').response.translation.query;
  const input = example('translate').request.conditions;
  const definition = saved.librarySavedDefinition(translated, { channelId: translated.scope.id, timeZone: 'Europe/Berlin', density: 'compact' });
  assert.deepEqual(definition.presentation, { version: 1, surface: 'library', view: 'grid', density: 'compact', time_zone: 'Europe/Berlin' });
  assert.deepEqual(definition.query.scope, { kind: 'channel', id: '$channel' });
  assert.equal(definition.query.consistency, undefined, 'no read mode is saved');
  assert.deepEqual(definition.query.page, { size: 50 }, 'no page.after');
  assert.deepEqual(definition.query.where, translated.where, 'the translator node order is kept');
  assert.throws(() => saved.librarySavedDefinition(translated, { channelId: 'UC' + 'b'.repeat(22), timeZone: 'UTC' }));
  // Recognised again as the same Library filters once the calendar fields are switched on.
  const recognized = saved.recognizeLibraryView(definition, true);
  assert.equal(recognized.status, 'recognized');
  assert.deepEqual(recognized.conditions, catalogue.normalizeCatalogueConditions(input));
  assert.equal(recognized.savedZone, 'Europe/Berlin');
  console.log('Save view: v2 with presentation, without page.after or consistency; recognised back.');
}

// 8. The workshop hand-off: Library's executed query and its frozen list.
{
  const { request } = example('long_first_anchor');
  const first = page('long_first_anchor');
  const handoff = compiled('workshopHandoff.ts', {
    './savedQueryDefinition': compiled('savedQueryDefinition.ts', {
      './researchQuery': compiled('researchQuery.ts', { './api': {}, '../sourceLifetime': lifetime }),
      './savedWork': compiled('savedWork.ts'), './timeZones': compiled('timeZones.ts') }),
    './researchQuery': compiled('researchQuery.ts', { './api': {}, '../sourceLifetime': lifetime }),
    './researchGraph': { MAX_RESEARCH_BYTES: 16384, researchSize: () => ({ bytes: 100, valid: true }), sourceStudy: () => ({}), newStudy: () => ({}),
      openResearch: () => ({}), parseStudyDocument: () => ({}), studyFromSavedQuery: () => ({}) },
    './savedViews': { readWorkshopAddress: () => ({}), RESEARCH_WORKSHOP_PATH: '/r', savedOpenTarget: () => 'library', savedViewUrl: () => new URL('http://x') },
    './savedWork': compiled('savedWork.ts'),
  });
  const origin = { surface: 'library', channel_id: request.scope.id, channel_title: 'Growth fixture', format: 'long', view: 'grid' };
  const frozen = { context: { frozen_ref: first.browse.frozen_ref, evaluated_at: first.browse.created_at, valid_before: first.browse.expires_at },
    page: { total_count: first.browse.total_count } };
  const whole = handoff.selectionHandoff(request, frozen, origin);
  assert.equal(whole.kind, 'selection');
  assert.deepEqual(whole.frozen, { ref: first.browse.frozen_ref, evaluated_at: first.browse.created_at, valid_before: first.browse.expires_at, total_count: 37 });
  assert.equal(whole.query.consistency, 'current');
  assert.deepEqual(whole.query.page, { size: 4 }, 'no page.after or anchor');
  assert.deepEqual(whole.query.select, request.select, 'exactly the executed fields');
  const selected = handoff.selectedIdsHandoff(request, first.videos.map((video) => ({ id: video.video_id, type: video.video_type })), origin);
  assert.deepEqual(selected.ids, first.videos.map((video) => video.video_id));
  const many = Array.from({ length: 1001 }, (_, index) => ({ id: `a${String(index).padStart(10, '0')}`, type: 'long' }));
  assert.deepEqual(handoff.selectedIdsHandoff(request, many, origin), { refused: 'too_many', count: 1001 });
  assert.deepEqual(handoff.selectedIdsHandoff(request, [{ id: 'gLhuge00000', type: 'short' }], origin), { refused: 'nothing_selected', count: 1 });
  const ids = videos.parseFrozenIds(example('long_frozen_ids').response, first);
  assert.equal(handoff.isWholeFrozenList(new Set(ids), ids), true, 'after Select all matching the selection is the whole list');
  // Library's list position, its ordered ID list and its slices never travel into the study.
  for (const page of [{ size: 4, growth_lifetime: 'page', order: 'ids', anchor_id: ids[3] }, { size: 4, growth_lifetime: 'page', ids: ids.slice(0, 4) }]) {
    assert.deepEqual(handoff.selectionHandoff({ ...request, consistency: 'current', page }, frozen, origin).query.page, { size: 4 });
    assert.deepEqual(handoff.selectedIdsHandoff({ ...request, consistency: 'current', page }, [{ id: ids[0], type: 'long' }], origin).query.page, { size: 4 });
  }
  // The panel's buttons: "Open in research workshop" and "Send selected to workshop" replace Compare / Send to Canvas.
  const panel = read('../application/frontend/src/explorer/ExplorerVideos.tsx');
  assert.match(panel, />Open in research workshop</);
  assert.match(panel, />Send selected to workshop</);
  assert.doesNotMatch(panel, /Send to Canvas|>Compare /);
  const section = read('../application/frontend/src/explorer/LibraryVideosSection.tsx');
  // The whole list selected goes only as the fixed list checked against the selection (useCatalogueBrowse freezeForWorkshop).
  assert.match(section, /const chosen = new Set\(selected\.map\(\(item\) => item\.id\)\);\s+if \(browse\.listIds && isWholeFrozenList\(chosen, browse\.listIds\)\) return openWhole\(chosen\);/);
  assert.match(section, /settings|handoffRefusalText/);
  console.log('Workshop hand-off: the executed query and frozen list; selected ids up to 1,000; whole-list selection.');
}

// 9. Growth labels: only a full window reads "~28 days"; "today" names its zone.
{
  const growth = compiled('videoGrowth.ts');
  const partial = { status: 'partial', delta_views: '2801', tracked_days: 27, observed_start_at: 'a', observed_end_at: 'b',
    actual_interval_seconds: 27.5 * 86400, boundary_distance_seconds: null, reason: 'full_window_history_unavailable' };
  assert.deepEqual(growth.presentVideoViewGrowth(partial), { text: '+2.8K over ~27.5 days', tone: 'observed' });
  assert.deepEqual(growth.presentVideoViewGrowth({ ...partial, status: 'ready', actual_interval_seconds: 2419200 + 3600, boundary_distance_seconds: 3600 }),
    { text: '+2.8K over ~28 days', tone: 'positive' });
  assert.equal(growth.presentVideoViewGrowth({ ...partial, actual_interval_seconds: 9 * 86400 }).text, '+2.8K over 9 days');
  const today = { status: 'started_today', delta_views: null, tracked_days: null, observed_start_at: 'a', observed_end_at: 'b',
    actual_interval_seconds: 600, boundary_distance_seconds: null, reason: 'tracking_started_today' };
  assert.deepEqual(growth.presentVideoViewGrowth({ ...today, time_zone: 'Europe/Berlin' }),
    { text: 'Tracking started today', tone: 'muted', title: 'Tracking started today (Europe/Berlin)' });
  assert.equal(growth.presentVideoViewGrowth(today).title, 'Tracking started today (UTC)', 'old rows: UTC');
  // Every partial card of the real Shorts page names its own days, never "~28".
  for (const video of page('short_first').videos.filter((row) => row.view_growth.status === 'partial')) {
    assert.doesNotMatch(growth.presentVideoViewGrowth(video.view_growth).text, /~28 days/);
  }
  console.log('Growth labels: partial windows show their actual days; "today" names the zone.');
}

// 10. The grid (server render): automatic paging, the workshop buttons, the freshness notice and the type's loading text.
{
  const sharedFormat = compiled('../format.ts');
  const format = compiled('format.ts', { '../format': sharedFormat, '../format.ts': sharedFormat });
  const icons = new Proxy({}, { get: () => () => null });
  const panelImports = {
    react: React, 'react/jsx-runtime': jsxRuntime, 'lucide-react': icons,
    '../sourceLifetimeReact': { useSourceExpiry: () => {} }, '../sourceLifetime.ts': lifetime,
    './format': format, './videoGrowth': compiled('videoGrowth.ts'),
    './localTime': { formatDateLocal: (value) => value ?? 'Unknown', formatExactLocal: (value) => value ?? 'Not recorded', formatTimestampLocal: (value) => `at ${value}` },
    './videoCardFormat': await import('../application/frontend/src/explorer/videoCardFormat.ts'),
    './videoPresentation': { safeVideoThumbnailUrl: (value) => value || undefined },
    './libraryData/videos.ts': videos,
    './ModuleState': { ErrorState: ({ title }) => React.createElement('p', null, title), LoadingState: ({ label }) => React.createElement('p', { role: 'status' }, label) },
    './resourceState': { resourcePayload: (state) => (state.kind === 'ready' || state.kind === 'updating' ? state.payload : state.previous) },
    './VirtualVideoGrid': { VirtualVideoGrid: ({ videos: rows, renderCard, videoType }) => React.createElement('div', { 'data-video-type': videoType }, rows.map(renderCard)) },
    './videoGridGeometry': await import('../application/frontend/src/explorer/videoGridGeometry.ts'),
    './catalogueSelection': { addSelectedRecords: (current, records) => new Map([...current, ...records.map((row) => [row.video_id, row])]) },
    './VideoThumbnail': { VideoThumbnail: () => null, videoTitle: (video) => video.title || 'Title not reported' },
    './VideoInspector': { VideoInspector: () => null }, './libraryData/errors.ts': await import('../application/frontend/src/explorer/libraryData/errors.ts'),
  };
  const module = compiled('ExplorerVideos.tsx', panelImports);
  assert.equal(module.LOAD_AHEAD_MARGIN, '2400px 0px', 'the next page loads about three screens ahead');
  // Without source clocks (plain): the render does not depend on today's date.
  const payload = plain(page('long_first_anchor', (r) => {
    r.coverage.reasons = { source_rows_unavailable: 1, rows_with_expired_values: 1 };
    Object.assign(r.rows[0].cells['video.views'], { state: 'expired', value: null });
  }));
  const render = (props) => renderToStaticMarkup(React.createElement(module.ExplorerVideosPanel, {
    state: { kind: 'ready', scopeKey: 's', payload }, channelId: payload.channel.channel_id, growthAvailable: true,
    selectionScopeKey: 's', onRetry() {}, hasMore: true, hasPrevious: true, ...props }));
  // Open a real card, renew the live query, then remove it. Read the newly rendered
  // Inspector props and its React key to verify the previous panels cannot be reused.
  {
    const host = reactHookHost();
    const panel = compiled('ExplorerVideos.tsx', { ...panelImports, react: { ...host.React, memo: value => value } });
    const elements = element => !React.isValidElement(element) ? [] :
      [element, ...React.Children.toArray(element.props.children).flatMap(elements)];
    const find = type => elements(host.current).find(element => element.type === type);
    const props = (key, rows) => ({
      state: { kind: 'ready', scopeKey: 's', payload: { ...payload, browse: { consistency: 'current' }, videos: rows } },
      selectionScopeKey: 's', channelId: payload.channel.channel_id, onRetry() {}, growthAvailable: true,
      session: { sessionKey: key, pending: false }, inspector: { gates: {}, timeZone: 'UTC' },
    });
    const video = payload.videos[0], other = payload.videos[1];
    globalThis.window = new EventTarget();
    host.mount(panel.ExplorerVideosPanel, props('read1', [video]));
    const grid = find(panelImports['./VirtualVideoGrid'].VirtualVideoGrid);
    grid.props.renderCard(video).props.onInspect(video, 'title');
    const inspector = () => find(panelImports['./VideoInspector'].VideoInspector);
    const originalKey = inspector().key;
    assert.equal(inspector().props.library.opening.active, true);
    host.update(props('read2', [{ ...video, title: 'Refreshed title' }]));
    assert.notEqual(inspector().key, originalKey);
    assert.equal(inspector().props.library.opening.active, true);
    assert.equal(inspector().props.video.title, 'Refreshed title');
    host.update(props('read3', [other]));
    assert.equal(inspector(), undefined);
    assert.match(renderToStaticMarkup(host.current), /no longer in these results/);
    host.unmount();
    delete globalThis.window;
  }
  // Time alone never closes the Inspector or refuses to open it: a card whose values' lifetimes have ended
  // opens like any other, and stays open while its card is replaced by later reads of the same list.
  {
    const host = reactHookHost();
    const panel = compiled('ExplorerVideos.tsx', { ...panelImports, react: { ...host.React, memo: value => value } });
    const elements = element => !React.isValidElement(element) ? [] :
      [element, ...React.Children.toArray(element.props.children).flatMap(elements)];
    const find = type => elements(host.current).find(element => element.type === type);
    const inspector = () => find(panelImports['./VideoInspector'].VideoInspector);
    const ended = page('long_first_anchor');
    const future = Date.parse('2027-01-01T00:00:00Z');
    const rows = videos.shownCatalogueVideos(ended, future, 0);
    assert.ok(rows.every((video) => lifetime.sourceExpired(video, future)), 'every lifetime of these rows has ended');
    const props = (shown) => ({
      state: { kind: 'ready', scopeKey: 's', payload: { ...payload, browse: { consistency: 'current' }, videos: shown } },
      selectionScopeKey: 's', channelId: payload.channel.channel_id, onRetry() {}, growthAvailable: true,
      session: { sessionKey: 'read1', pending: false }, inspector: { gates: {}, timeZone: 'UTC' },
    });
    globalThis.window = new EventTarget();
    const realNow = Date.now;
    Date.now = () => future;
    try {
      host.mount(panel.ExplorerVideosPanel, props(rows));
      const grid = find(panelImports['./VirtualVideoGrid'].VirtualVideoGrid);
      assert.equal(grid.props.videos.length, 4, 'the grid gets every loaded card');
      grid.props.renderCard(rows[0]).props.onInspect(rows[0], 'title');
      assert.equal(inspector().props.video.video_id, rows[0].video_id, 'the Inspector opens for a card that needs refresh');
      const key = inspector().key;
      // A later read of the same list replaced the card (new values): the same Inspector, not a new one.
      host.update(props([{ ...rows[0], views: '7000' }, ...rows.slice(1)]));
      assert.equal(inspector().key, key);
      assert.equal(inspector().props.video.views, '7000');
      assert.equal(inspector().props.library.opening.active, true);
    } finally { Date.now = realNow; }
    host.unmount();
    delete globalThis.window;
    assert.doesNotMatch(read('../application/frontend/src/explorer/ExplorerVideos.tsx'), /useSourceExpiry\(inspectedVideo|freshSourceRows/,
      'no timer closes the Inspector and no filter drops loaded cards');
  }
  // Another sort of the same matched set: the Inspector stays on its video while it is in the new list,
  // even before its card loads there; one grid per read of the list.
  {
    const host = reactHookHost();
    const panel = compiled('ExplorerVideos.tsx', { ...panelImports, react: { ...host.React, memo: value => value } });
    // The rendered elements as written: React.Children.toArray would re-key them (".$" + key), and the grid's own key is checked here.
    const children = (value) => (Array.isArray(value) ? value.flatMap(children) : [value]);
    const elements = element => !React.isValidElement(element) ? [] :
      [element, ...children(element.props.children).flatMap(elements)];
    const find = type => elements(host.current).find(element => element.type === type);
    const grid = () => find(panelImports['./VirtualVideoGrid'].VirtualVideoGrid);
    const inspector = () => find(panelImports['./VideoInspector'].VideoInspector);
    const props = (key, rows, listIds) => ({
      state: { kind: 'ready', scopeKey: 's', payload: { ...payload, videos: rows,
        browse: { consistency: 'current', order: { ids: listIds, total: listIds.length, limit: 200000 } } } },
      selectionScopeKey: 's', channelId: payload.channel.channel_id, onRetry() {}, growthAvailable: true,
      session: { sessionKey: key, pending: false }, inspector: { gates: {}, timeZone: 'UTC' },
    });
    const video = payload.videos[0], other = payload.videos[1];
    globalThis.window = new EventTarget();
    host.mount(panel.ExplorerVideosPanel, props('sorted1', [video], [video.video_id, other.video_id]));
    grid().props.renderCard(video).props.onInspect(video, 'thumbnail');
    assert.equal(grid().key, 'sorted1');
    assert.equal(inspector().props.video.video_id, video.video_id);
    host.update(props('sorted2', [other], [other.video_id, video.video_id]));
    assert.equal(inspector().props.video.video_id, video.video_id, 'still in the list: kept open');
    assert.equal(grid().key, 'sorted2', 'a new grid for the new order');
    host.update(props('filtered', [other], [other.video_id]));
    assert.equal(inspector(), undefined);
    assert.match(renderToStaticMarkup(host.current), /no longer in these results/);
    host.unmount();
    delete globalThis.window;
  }
  // The list: its total, Refresh order, and plain words for what is left out.
  {
    const listed = { ...payload, browse: { ...payload.browse, consistency: 'current', order: { ids: [], total: 12408, limit: 200000 } } };
    const text = render({ state: { kind: 'ready', scopeKey: 's', payload: listed }, onRefreshOrder() {}, leftResults: 2, departedShown: 1,
      refreshProblem: refresh.refreshProblemText({ last: { now: 0, monotonic: 0 }, lastFull: { now: 0, monotonic: 0 }, started: { now: 0, monotonic: 0 }, failures: 1, paused: false }) });
    assert.match(text, /<strong>12,408<\/strong> videos/);
    assert.match(text, />Refresh order</);
    assert.match(text, /2 videos of the loaded part no longer match these filters or are no longer available\./);
    assert.match(text, /1 shown video no longer matches these\s+filters or is no longer available\. Its card stays with the last\s+values\. Refresh order to update the list\./);
    assert.match(text, /could not be updated in the background\. The cards keep their last values; the next attempt is in 5 minutes\./);
    assert.doesNotMatch(text, /(is|are) hidden/, 'no card is hidden by time');
    const past = { ...payload, browse: { ...payload.browse, consistency: 'current', order: { ids: null, total: 250000, limit: 200000 } } };
    const pastText = render({ state: { kind: 'ready', scopeKey: 's', payload: past } });
    assert.match(pastText, /<strong>250,000<\/strong> videos/);
    assert.match(pastText, /more than the 200,000 that Library can keep in one ordered list/);
    assert.doesNotMatch(render({}), /Refresh order|ordered list/, 'a fixed list has neither');
    // Rows whose lifetimes have all ended are still drawn: every card, with "—" and Needs refresh for the ended values.
    const ended = page('long_first_anchor');
    assert.ok(ended.videos.every((video) => lifetime.sourceExpired(video, Date.parse('2027-01-01T00:00:00Z'))), 'the fixture rows end in November 2026');
    const endedRows = videos.shownCatalogueVideos(ended, Date.parse('2027-01-01T00:00:00Z'), 0);
    const kept = render({ state: { kind: 'ready', scopeKey: 's', payload: { ...listed, videos: endedRows } }, onRefreshOrder() {} });
    assert.equal(kept.match(/data-video-id=/g).length, 4, 'every loaded card is drawn');
    assert.equal(kept.match(/>Needs refresh</g).length, 4, 'each with its views marked');
    assert.match(kept, /Data is out of date\. New numbers appear after the next refresh\./);
    assert.doesNotMatch(kept, /No stored videos match|(is|are) hidden/);
    assert.match(render({ state: { kind: 'ready', scopeKey: 's', payload: { ...listed, videos: [] } } }), /No stored videos match the applied filters\./);
  }
  const idle = render({});
  assert.match(idle, />Load more videos</);
  assert.match(idle, />Load previous videos</);
  const loading = render({ isLoadingMore: true });
  assert.doesNotMatch(loading, /Load more videos|Load previous videos/, 'no button where cards are about to arrive');
  assert.match(loading, /Loading videos…/);
  assert.match(idle, /1 video shows — for values that need refresh\./);
  // The count agrees with its noun.
  assert.match(idle, /1 video row was withheld\./);
  assert.doesNotMatch(idle, /1 video rows/);
  const several = plain(page('long_first_anchor', (r) => { r.coverage.reasons = { source_rows_unavailable: 3, future_reason: 1 }; }));
  const severalText = render({ state: { kind: 'ready', scopeKey: 's', payload: several } });
  assert.match(severalText, /3 video rows were withheld\./);
  assert.match(severalText, /1 other row excluded \(future_reason\)\./);
  // The card (Long form): an expired value is "—" with its word, never 0.
  assert.match(idle, /<span class="video-card-views" title="Data is out of date\. New numbers appear after the next refresh\."><b>—<\/b><em class="video-card-state is-warning">Needs refresh<\/em><\/span>/);
  assert.match(idle, /aria-label="Video: Growth gLlatemeta0\. Views: needs refresh\./, 'the spoken summary names the state');
  assert.doesNotMatch(idle, /total views|Thumbnail unavailable|explorer-v2-video-card/, 'the compact card omits placeholder text and uses current video-card markup');
  // Counts as YouTube writes them (cut: 1,392 → 1.3K) with the exact value on hover; likes and comments from the
  // stored row: the Videos session already selects them (no YouTube request, no quota).
  assert.ok(['video.likes', 'video.comments'].every((field) => wire.LIBRARY_SELECT.includes(field)));
  const card = (html, id) => html.match(new RegExp(`<article class="video-card[^"]*" aria-label="[^"]*" data-video-id="${id}">.*?</article>`, 's'))?.[0] ?? '';
  const second = card(idle, 'gLbg0800000');
  assert.match(second, /^<article class="video-card is-long is-large" aria-label="Video: Growth gLbg0800000\. 1\.3 thousand views/);
  assert.match(second, /<span class="video-card-views" title="1,392 views"><b>1\.3K<\/b><i>views<\/i><\/span>/);
  assert.match(second, /<span class="video-card-stat" title="34 likes">/);
  assert.match(second, /<span class="video-card-stat" title="3 comments">/);
  assert.match(second, /<span class="video-card-badge">10:00<\/span>/, 'the duration on the thumbnail');
  const zeroLength = plain(page('long_first_anchor', (r) => { r.rows[1].cells['video.duration_seconds'].value = '0'; }));
  assert.match(card(render({ state: { kind: 'ready', scopeKey: 's', payload: zeroLength } }), 'gLbg0800000'), /<span class="video-card-badge">—<\/span>/,
    'a length YouTube reports as 0 (live or upcoming) is "—", not 0:00');
  assert.match(second, /<span class="video-card-date" title="Published 2026-10-01T06:48:15\.221465Z">\d+(?:min|h|d|w|mo|y) ago<\/span>/,
    'a relative date, the exact time on hover');
  assert.match(second, /<span class="video-card-growth is-(?:up|down|flat)" title="[^"]*">[^<]+<\/span>/);
  assert.match(second, /<img src="https:\/\/img\.youtube\.com\/vi\/gLbg0800000\/hqdefault\.jpg" alt="" loading="lazy"/);
  // Focus targets in Tab order (thumbnail, title, checkbox, link); the Inspector triggers and the link stay.
  assert.deepEqual([...second.matchAll(/data-grid-focus="([a-z]+)"/g)].map((match) => match[1]), ['thumbnail', 'title', 'selection', 'youtube']);
  assert.deepEqual([...second.matchAll(/data-inspector-trigger="([a-z]+)"/g)].map((match) => match[1]), ['thumbnail', 'title']);
  assert.match(second, /href="https:\/\/www\.youtube\.com\/watch\?v=gLbg0800000"[^>]*title="Open on YouTube"/);
  // Compact: views · date in the first row, growth and the link in the second; no likes or comments.
  const compact = card(render({ density: 'compact' }), 'gLbg0800000');
  assert.match(compact, /^<article class="video-card is-long is-compact"/);
  assert.match(compact, /<div class="video-card-row"><span class="video-card-views"[^>]*>.*?<\/span><span class="video-card-date"/);
  assert.match(compact, /<div class="video-card-row"><span class="video-card-growth[^>]*>[^<]+<\/span><a class="video-card-youtube"/);
  assert.doesNotMatch(compact, /video-card-stat/);
  // Without growth (refused, or a collection) the card has no growth chip; the rows keep their height in CSS.
  assert.doesNotMatch(render({ growthAvailable: false }), /video-card-growth/);
  // Shorts (the Shorts tab): the grid gets the type; the 2:3 form with the views alone under the title, then
  // growth; Large adds likes and comments, then the date with the link; Compact keeps views with the link, then
  // growth. The thumbnail is the same 480x360 image (the CSS frame crops it); over it only the duration.
  {
    const shortsPayload = plain(page('short_first', (r) => {
      Object.assign(r.rows[0].cells['video.views'], { state: 'expired', value: null });
      Object.assign(r.rows[2].cells['video.views'], { state: 'unavailable', value: null });
      Object.assign(r.rows[3].cells['video.likes'], { state: 'unavailable', value: null });
    }));
    const shorts = (props) => render({ state: { kind: 'ready', scopeKey: 's', payload: shortsPayload },
      channelId: shortsPayload.channel.channel_id, videoType: 'short', ...props });
    const rows = (html) => [...html.matchAll(/<div class="(video-card-row[^"]*)">(.*?)<\/div>/g)].map((match) => [match[1], match[2]]);
    const large = shorts({});
    assert.match(large, /<div data-video-type="short"><article class="video-card is-shorts is-large"/, 'the grid knows its type');
    assert.match(render({}), /<div data-video-type="long"><article class="video-card is-long is-large"/);
    const short = card(large, 'gSbg0100000');
    assert.match(short, /^<article class="video-card is-shorts is-large" aria-label="Short: Growth gSbg0100000\. 312 views, up 196 over ~1\.2 days\. 10 minutes\./);
    assert.match(short, /<div class="video-card-media"><button type="button" class="video-card-thumbnail"[^>]*><span class="video-card-placeholder">.*?<\/span>(<img [^>]*>)<\/button><span class="video-card-badge">10:00<\/span><\/div>/,
      'over the frame only the duration; the checkbox sits outside the image');
    assert.match(short, /<img src="https:\/\/img\.youtube\.com\/vi\/gSbg0100000\/hqdefault\.jpg" alt=""/);
    const largeRows = rows(short);
    assert.deepEqual(largeRows.map(([name]) => name), ['video-card-row', 'video-card-row', 'video-card-row is-small video-card-meta', 'video-card-row video-card-meta']);
    assert.match(largeRows[0][1], /^<span class="video-card-views" title="312 views"><b>312<\/b><i>views<\/i><\/span>$/, 'the views alone');
    assert.match(largeRows[1][1], /^<span class="video-card-growth is-up" title="\+196 views over ~1\.2 days">\+196 · 1d<\/span>$/, 'growth with its actual days');
    assert.match(largeRows[2][1], /^<span class="video-card-stat" title="YouTube no longer returns this value\.">.*?<span class="explorer-sr-only"> likes: unavailable<\/span><\/span><span class="video-card-stat" title="0 comments">/);
    assert.match(largeRows[3][1], /^<span class="video-card-date" title="Published 2026-10-29T05:37:15\.221465Z">\d+(?:min|h|d|w|mo|y) ago<\/span><a class="video-card-youtube" href="https:\/\/www\.youtube\.com\/watch\?v=gSbg0100000"[^>]*title="Open on YouTube"/);
    assert.deepEqual([...short.matchAll(/data-grid-focus="([a-z]+)"/g)].map((match) => match[1]), ['thumbnail', 'title', 'selection', 'youtube']);
    assert.deepEqual([...short.matchAll(/data-inspector-trigger="([a-z]+)"/g)].map((match) => match[1]), ['thumbnail', 'title']);
    // Value states: "—" and the state word stay in the views row (the growth row keeps its own status), never 0.
    const expired = card(large, 'gSbg0000000');
    assert.match(expired, /aria-label="Short: Growth gSbg0000000\. Views: needs refresh\./);
    assert.match(rows(expired)[0][1], /^<span class="video-card-views" title="Data is out of date\. New numbers appear after the next refresh\."><b>—<\/b><em class="video-card-state is-warning">Needs refresh<\/em><\/span>$/);
    assert.match(rows(expired)[1][1], /^<span class="video-card-growth is-flat" title="Tracking started today \([^)]+\)">Tracking started today<\/span>$/);
    const gone = card(large, 'gSwaitone00');
    assert.match(gone, /^<article class="video-card is-shorts is-large is-unavailable"/, 'an unavailable Short is dimmed');
    assert.doesNotMatch(gone, /<img /, 'and shows the placeholder');
    assert.match(rows(gone)[0][1], /<b>—<\/b><em class="video-card-state">Unavailable<\/em>/);
    for (const html of [short, expired, gone]) assert.equal(rows(html).length, 4, 'every Large Short has the same four rows');
    // Compact: views and the link, then growth; no date, likes or comments (the title's hover has the date).
    const compactShorts = shorts({ density: 'compact' });
    const small = card(compactShorts, 'gSbg0100000');
    assert.match(small, /^<article class="video-card is-shorts is-compact"/);
    assert.match(small, /title="Growth gSbg0100000 · Published 2026-10-29T05:37:15\.221465Z"/);
    assert.deepEqual(rows(small).map(([name]) => name), ['video-card-row', 'video-card-row']);
    assert.match(rows(small)[0][1], /^<span class="video-card-views" title="312 views">.*?<\/span><a class="video-card-youtube"/);
    assert.match(rows(small)[1][1], /^<span class="video-card-growth is-up"[^>]*>\+196 · 1d<\/span>$/);
    assert.doesNotMatch(small, /video-card-stat|video-card-date/);
    assert.deepEqual([...small.matchAll(/data-grid-focus="([a-z]+)"/g)].map((match) => match[1]), ['thumbnail', 'title', 'selection', 'youtube'], 'Tab order kept');
    assert.match(rows(card(compactShorts, 'gSbg0000000'))[0][1], /<b>—<\/b><em class="video-card-state is-warning">Needs refresh<\/em><\/span><a class="video-card-youtube"/);
    // Without growth the growth row stays (empty): the height does not change.
    assert.deepEqual(rows(card(shorts({ growthAvailable: false }), 'gSbg0100000'))[1], ['video-card-row', '']);
    assert.deepEqual(rows(card(shorts({ growthAvailable: false, density: 'compact' }), 'gSbg0100000'))[1], ['video-card-row', '']);
    // Streams keep the Long form.
    assert.match(render({ videoType: 'stream' }), /<div data-video-type="stream"><article class="video-card is-long is-large"/);
    console.log('Shorts cards: 2:3 form, views under the title, rows per density, value states, Tab order; streams stay Long.');
  }
  assert.doesNotMatch(idle, /Open in research workshop/, 'the hand-off needs the workshop prop');
  const withWorkshop = render({ workshop: { openSelection: () => undefined, sendSelected: () => undefined } });
  assert.match(withWorkshop, /Open in research workshop/);
  const empty = renderToStaticMarkup(React.createElement(module.ExplorerVideosPanel, {
    state: { kind: 'loading', scopeKey: 's' }, videoType: 'short', growthAvailable: false, selectionScopeKey: 's', onRetry() {} }));
  assert.match(empty, /Loading stored Shorts catalogue…/);
  const long = renderToStaticMarkup(React.createElement(module.ExplorerVideosPanel, {
    state: { kind: 'loading', scopeKey: 's' }, growthAvailable: false, selectionScopeKey: 's', onRetry() {} }));
  assert.match(long, /Loading stored long-form catalogue…/, 'Long keeps its words');
  const preparing = renderToStaticMarkup(React.createElement(module.ExplorerVideosPanel, {
    state: { kind: 'loading', scopeKey: 's' }, growthAvailable: false, selectionScopeKey: 's', onRetry() {}, preparing: true }));
  assert.match(preparing, /Preparing stored catalogue…/, 'catalogue_preparing: retries run with this text');

  // while the matching ids load, "Select all matching" keeps keyboard focus (aria-disabled, not disabled).
  assert.deepEqual(module.selectAllMatchingButtonState(true, false), { disabled: false, 'aria-disabled': true });
  assert.deepEqual(module.selectAllMatchingButtonState(false, false), { disabled: false });
  assert.deepEqual(module.selectAllMatchingButtonState(false, true), { disabled: true });
  const selectable = render({ onCollectMatching: async () => [] });
  assert.match(selectable, /<button type="button" class="explorer-secondary-button">Select all matching<\/button>/);
  const panelSource = read('../application/frontend/src/explorer/ExplorerVideos.tsx');
  assert.doesNotMatch(panelSource, /disabled=\{Boolean\(selectionProgress\) \|\| expired/, 'the busy state never disables the focused button');
  assert.match(panelSource, /selectAllRef\.current\?\.focus\(\{ preventScroll: true \}\)/, 'focus returns when the progress controls go');
  assert.ok(read('../application/frontend/src/explorer/styles.css').includes('.explorer-app button:disabled,\n.explorer-app button[aria-disabled="true"] {'),
    'the busy button has the disabled style');

  // new cards that appear under a resting pointer ignore its click for a moment.
  const guard = { firstIndex: 50, until: 1800, x: 400, y: 600 };
  const click = (change = {}) => module.insertedCardClickBlocked(guard, { index: 50, now: 1300, x: 400, y: 600, detail: 1, ...change });
  assert.equal(module.INSERTED_CARD_GUARD_MS, 800);
  assert.equal(click(), true, 'the click aimed at "Load more" does not open a new card');
  assert.equal(click({ x: 404, y: 603 }), true, 'a tremor is not a new aim');
  assert.equal(click({ x: 420 }), false, 'a moved pointer clicks what it sees');
  assert.equal(click({ now: 1800 }), false, 'the guard ends');
  assert.equal(click({ index: 49 }), false, 'cards that were already shown are not guarded');
  assert.equal(click({ index: -1 }), false, 'buttons outside the cards are not guarded');
  assert.equal(click({ detail: 0 }), false, 'keyboard activation is never blocked');
  assert.equal(module.insertedCardClickBlocked(null, { index: 50, now: 0, x: 0, y: 0, detail: 1 }), false);
  assert.match(panelSource, /onClickCapture=\{guardInsertedCards\}/, 'the guard sits on the catalogue section (no new layout box)');

  // After a jump to a new session the remembered card is shown whole, below the sticky bars.
  const geometry = await import('../application/frontend/src/explorer/videoGridGeometry.ts');
  const ids = Array.from({ length: 12 }, (_, index) => `v${String(index).padStart(10, '0')}`);
  const layout = geometry.buildVideoGridLayout(ids, 3, 350, 15, new Map());
  const at = (offsetPx, inset = 120) => geometry.visibleAnchorOffset({ videoId: ids[7], offsetPx }, ids, layout, 900, inset);
  assert.equal(at(823), 900 - 350 - 16, 'a partially visible card fits after scrolling');
  assert.equal(at(300), 300, 'an anchor already shown whole keeps its place');
  assert.equal(at(-200), 136, 'a card cut at the top comes below the sticky bars');
  assert.equal(geometry.visibleAnchorOffset({ videoId: ids[7], offsetPx: 823 }, ids,
    geometry.buildVideoGridLayout(ids, 3, 1200, 15, new Map()), 900, 120), 136, 'a card taller than the room starts below the bars');
  assert.equal(geometry.visibleAnchorOffset({ videoId: 'missing0000', offsetPx: 823 }, ids, layout, 900, 120), 823);
  // Until a row is measured it is placed from the card's own sizes: 2 × padding + the 16:9 thumbnail + the text block.
  assert.equal(geometry.estimatedVideoRowHeight(360, 'large'), 16 + 344 * 9 / 16 + 112);
  assert.equal(geometry.estimatedVideoRowHeight(178, 'compact'), 12 + 166 * 9 / 16 + 104);
  // Shorts: the 2:3 thumbnail (3/2 of its width) and the Shorts text block (Large 8 + 40 + 30 + 30 + 24 + 30 = 162,
  // Compact 104): 463 px for a Large Short of a 1440 px grid, as the mockup measured.
  assert.equal(geometry.estimatedVideoRowHeight(1440 / 7, 'large', 'shorts'), 16 + (1440 / 7 - 16) * 3 / 2 + 162);
  assert.equal(Math.round(geometry.estimatedVideoRowHeight(1440 / 7, 'large', 'shorts')), 463);
  assert.equal(geometry.estimatedVideoRowHeight(131, 'compact', 'shorts'), 12 + 119 * 3 / 2 + 104);
  assert.equal(geometry.estimatedVideoRowHeight(360, 'large', 'long'), geometry.estimatedVideoRowHeight(360, 'large'));
  assert.deepEqual(['long', 'short', 'stream', undefined].map(geometry.videoCardForm), ['long', 'shorts', 'long', 'long'],
    'only the Shorts grid has the Shorts form; a grid without a type (a collection) keeps Long');
  // The real grid names its type for the CSS (Shorts columns); a collection grid names none.
  {
    const realGrid = compiled('VirtualVideoGrid.tsx', { react: React, 'react/jsx-runtime': jsxRuntime,
      '../discoveryJournal': { reportDiscoveryRendered() {} }, './videoGridGeometry': geometry });
    const draw = (props) => renderToStaticMarkup(React.createElement(realGrid.VirtualVideoGrid, {
      videos: payload.videos.slice(0, 3), density: 'large', scopeKey: 's',
      renderCard: (video) => React.createElement('article', { key: video.video_id, 'data-video-id': video.video_id }), ...props }));
    assert.match(draw({ videoType: 'short' }), /^<div class="explorer-v2-video-grid is-virtual" data-density="large" data-video-type="short" style="height:/);
    assert.match(draw({ videoType: 'short', density: 'compact' }), /^<div class="explorer-v2-video-grid is-virtual" data-density="compact" data-video-type="short"/);
    assert.match(draw({ videoType: 'long' }), /data-video-type="long"/);
    assert.match(draw({}), /^<div class="explorer-v2-video-grid is-virtual" data-density="large" style="height:/, 'no type: the Long form');
  }
  // The jump showed the card whole, then the next restore undid it. In this case the remembered card
  // (sort "Views gained") was the 11th of the last page (5,750..5,760, 3 columns); the previous page (50 cards)
  // arrived above it and moved every card two places, and the grid restored the first card of the top row.
  {
    const name = (prefix, index) => `${prefix}${String(index).padStart(10, '0')}`;
    const last = Array.from({ length: 11 }, (_, index) => name('l', index));
    const merged = [...Array.from({ length: 50 }, (_, index) => name('p', index)), ...last];
    const anchorId = last[10];
    const gridDocTop = 624, viewport = 900, inset = 0;
    const lay = (list) => geometry.buildVideoGridLayout(list, 3, 346, 15, new Map());
    const shown = (list, layout, scrollY) => {
      const row = layout.rows[Math.floor(list.indexOf(anchorId) / layout.columns)];
      return { top: gridDocTop + row.top - scrollY, bottom: gridDocTop + row.top + row.height - scrollY };
    };
    const whole = ({ top, bottom }) => top >= inset + 16 && bottom <= viewport - 16;
    const restore = (list, layout, anchor, settle) => geometry.videoAnchorScrollTop(settle
      ? { ...anchor, offsetPx: geometry.visibleAnchorOffset(anchor, list, layout, viewport, inset) } : anchor, list, layout, gridDocTop);
    // 1. The jump of the new session (remembered offset 823): whole.
    const before = lay(last);
    const jumped = restore(last, before, { videoId: anchorId, offsetPx: 823 }, true);
    assert.ok(whole(shown(last, before, jumped)), 'the jump shows the remembered card whole');
    // 2. What the grid keeps there for its next restore.
    const gridTop = gridDocTop - jumped;
    const topRow = geometry.gridAnchorAt(last, before, gridTop);
    assert.deepEqual(topRow, { videoId: last[3], offsetPx: gridTop + before.rows[1].top }, 'without a settling card: the first card of the top row');
    const settling = geometry.gridAnchorAt(last, before, gridTop, anchorId);
    assert.deepEqual(settling, { videoId: anchorId, offsetPx: gridTop + before.rows[3].top }, 'while the session settles: the remembered card');
    assert.equal(geometry.gridAnchorAt(last, before, gridTop, 'missing0000').videoId, last[3]);
    assert.equal(geometry.gridAnchorAt([], lay([]), 0), null);
    // 3. The previous page arrives above: restoring the top row dropped the card one row out of the window.
    const after = lay(merged);
    assert.ok(!whole(shown(merged, after, restore(merged, after, topRow, false))), 'restoring the top row clips the bottom of the card (823..1183 px)');
    assert.ok(shown(merged, after, restore(merged, after, topRow, false)).bottom > viewport);
    const settled = shown(merged, after, restore(merged, after, settling, true));
    assert.ok(whole(settled), 'the settling card stays whole after the previous page');
    assert.equal(Math.round(settled.top), Math.round(shown(last, before, jumped).top), 'and where the jump put it');
  }
  const gridSource = read('../application/frontend/src/explorer/VirtualVideoGrid.tsx');
  assert.match(gridSource, /settling\.current = restoreAnchor\.videoId;/, 'the new session anchor settles');
  assert.match(gridSource, /gridAnchorAt\(currentIds, currentLayout, bounds\.top, settling\.current\)/, 'captures keep the settling card');
  assert.match(gridSource, /const whole = settling\.current !== null && pending\.videoId === settling\.current;/, 'every restore of it is whole');
  assert.match(gridSource, /else settling\.current = null;/, 'a move by the user ends the settling');
  assert.match(gridSource, /rect\.top <= top \+ 1 && rect\.bottom > 0/, 'only bars held at the top count as covering it');
  assert.doesNotMatch(gridSource, /pendingWhole/);
  assert.match(gridSource, /estimate: estimatedVideoRowHeight\(cardWidth, density, form\)/, 'the estimate follows the card and its form');
  assert.match(gridSource, /density !== geometry\.density \|\| form !== geometry\.form/, 'a new type re-estimates and drops measured rows');
  // The browser never scrolls the catalogue by itself, and the grid's height and row tops apply at
  // once even under the reduced-motion rule (0.01 ms transitions for every element), so pages load only as the
  // user reaches the boundary: End at the list top loads one page, not 7 in 0.55 s.
  const videosCss = read('../application/frontend/src/explorer/videos.css');
  assert.ok(videosCss.includes('.explorer-app.is-updated section.explorer-v2-video-catalogue {\n  overflow-anchor: none;\n}'), 'no scroll anchoring on the catalogue');
  assert.ok(videosCss.includes('.explorer-app.is-updated .explorer-v2-video-grid.is-virtual,\n.explorer-app.is-updated .explorer-v2-video-virtual-row {\n  transition: none;\n}'),
    'the virtual grid is never animated');
  assert.ok(read('../application/frontend/src/explorer/redesign.css').includes('transition-duration: 0.01ms !important;'), 'the reduced-motion rule this guards against');
  // Columns owned by CSS (the grid reads the resolved tracks): as many as fit at the minimum
  // width with its gap, the rest shared. auto-fill keeps the tracks of a grid whose rows are placed absolutely.
  assert.ok(videosCss.includes('.explorer-app.is-updated .explorer-v2-video-grid {\n  --vc-col: 320px;\n  --vc-gap: 8px;\n  display: grid;\n'
    + '  grid-template-columns: repeat(auto-fill, minmax(min(var(--vc-col), 100%), 1fr));\n  gap: var(--vc-gap);'), 'Large: 320 px, gap 8');
  assert.ok(videosCss.includes('.explorer-app.is-updated .explorer-v2-video-grid[data-density="compact"] {\n  --vc-col: 224px;\n  --vc-gap: 4px;\n}'),
    'Compact: 224 px, gap 4');
  assert.ok(videosCss.includes('@container explorer-v2-videos (max-width: 479px) {\n  .explorer-app.is-updated .explorer-v2-video-grid[data-density="compact"] {\n    --vc-col: 160px;'),
    'Compact: 160 px up to a 479 px grid');
  assert.doesNotMatch(videosCss, /explorer-v2-video-grid[^{]*\{\s*grid-template-columns: (?:repeat\(\d|1fr)/, 'no fixed column counts left');
  const fit = (width, min, gap) => Math.max(1, Math.floor((width + gap) / (min + gap)));
  // Common content widths → Long Large and Long Compact columns.
  for (const [width, large, compact] of [[360, 1, 2], [768, 2, 3], [1280, 3, 5], [1440, 4, 6], [1920, 5, 8], [2560, 7, 11]]) {
    assert.equal(fit(width, 320, 8), large, `Large at ${width} px`);
    assert.equal(fit(width, width <= 479 ? 160 : 224, 4), compact, `Compact at ${width} px`);
  }
  // One more column fits from n × min + (n − 1) × gap.
  for (const [min, gap, from] of [[320, 8, [648, 976, 1304, 1632, 1960, 2288, 2616]], [224, 4, [452, 680, 908, 1136, 1364, 1592, 1820, 2048, 2276, 2504]]]) {
    from.forEach((width, index) => { assert.equal(fit(width, min, gap), index + 2); assert.equal(fit(width - 1, min, gap), index + 1); });
  }
  // Shorts: 2n − 1 columns where the Long grid of the same density fits n, at least 2, edge to edge (gap 0).
  // The container queries are evaluated at every grid width from 160 to 3,840 px against the Long rule above.
  const shortsGrid = '.explorer-app.is-updated .explorer-v2-video-grid[data-video-type="short"] {\n  --vc-gap: 0px;\n  --vc-shorts: 2;\n'
    + '  grid-template-columns: repeat(var(--vc-shorts), minmax(0, 1fr));\n}';
  assert.ok(videosCss.includes(shortsGrid), 'Shorts: no gap, at least 2 columns, the count from the container queries');
  assert.ok(videosCss.indexOf(shortsGrid) > videosCss.indexOf('[data-density="compact"] {\n  --vc-col: 224px;\n  --vc-gap: 4px;\n}'),
    'the Shorts gap comes after the Compact one (same specificity)');
  const shortsQueries = { large: [], compact: [] };
  for (const [, from, density, count] of videosCss.matchAll(/@container explorer-v2-videos \(min-width: (\d+)px\) \{ \.explorer-app\.is-updated \.explorer-v2-video-grid\[data-video-type="short"\]\[data-density="(large|compact)"\] \{ --vc-shorts: (\d+); \} \}/g)) {
    shortsQueries[density].push([Number(from), Number(count)]);
  }
  assert.deepEqual(shortsQueries.large.map(([from]) => from), [648, 976, 1304, 1632, 1960, 2288, 2616, 2944, 3272, 3600], 'Large: where Long gains a column');
  assert.deepEqual(shortsQueries.compact.map(([from]) => from), [324, 680, 908, 1136, 1364, 1592, 1820, 2048, 2276, 2504, 2732, 2960, 3188, 3416, 3644],
    'Compact: 324 (160 px columns up to 479), then where Long gains a column');
  // Ascending, so the widest matching query is the last one and wins (equal specificity).
  const shortsColumns = (width, density) => shortsQueries[density].filter(([from]) => width >= from).at(-1)?.[1] ?? 2;
  for (let width = 160; width <= 3840; width += 1) {
    assert.equal(shortsColumns(width, 'large'), Math.max(2, 2 * fit(width, 320, 8) - 1), `Shorts Large at ${width} px`);
    assert.equal(shortsColumns(width, 'compact'), Math.max(2, 2 * fit(width, width <= 479 ? 160 : 224, 4) - 1), `Shorts Compact at ${width} px`);
  }
  // The README's columns table.
  for (const [width, large, compact] of [[360, 2, 3], [768, 3, 5], [1280, 5, 9], [1440, 7, 11], [1920, 9, 15], [2560, 13, 21]]) {
    assert.equal(shortsColumns(width, 'large'), large, `Shorts Large at ${width} px`);
    assert.equal(shortsColumns(width, 'compact'), compact, `Shorts Compact at ${width} px`);
  }
  // The Shorts card: a 2:3 frame over the 9:16 middle of the 480x360 thumbnail (the image only scaled and
  // centred), so the pillarbox bars of an upright video (138.75 px each side) stay outside; its text block's sizes.
  assert.ok(videosCss.includes('.explorer-app.is-updated .video-card.is-shorts .video-card-media {\n  aspect-ratio: 2 / 3;\n}'), 'Shorts: a 2:3 frame');
  const shortsImage = '.explorer-app.is-updated .video-card.is-shorts .video-card-thumbnail img {\n  inset: 50% auto auto 50%;\n'
    + '  width: calc(100% * 64 / 27);\n  max-width: none;\n  height: auto;\n  aspect-ratio: 4 / 3;\n  object-fit: cover;\n  transform: translate(-50%, -50%);\n}';
  assert.ok(videosCss.includes(shortsImage), 'the image scaled to 64/27 of the frame width and centred both ways');
  assert.doesNotMatch(shortsImage, /display/, 'a failed image still hides itself (img[hidden]) over the placeholder');
  {
    const [, times, over] = shortsImage.match(/width: calc\(100% \* (\d+) \/ (\d+)\);/).map(Number);
    const visibleWidth = 480 * over / times; // thumbnail px across the frame
    assert.equal(visibleWidth, 360 * 9 / 16, 'the frame spans exactly the 9:16 middle of the thumbnail');
    assert.equal(visibleWidth * 3 / 2, 303.75, 'and 303.75 of its 360 px down, centred');
    assert.ok((480 - visibleWidth) / 2 >= 138, 'pillarbox bars (measured 138 px a side) fall outside the frame');
  }
  assert.ok(videosCss.includes('.explorer-app.is-updated .video-card-thumbnail img {\n  position: absolute;\n  inset: 0;\n  display: block;\n  width: 100%;\n  height: 100%;\n  object-fit: cover;\n}'),
    'Long and every 16:9 frame keep the plain cover crop');
  assert.ok(videosCss.includes('.explorer-app.is-updated .video-card.is-shorts.is-large .video-card-title {\n  height: 40px;\n  font-size: 14px;\n  line-height: 20px;\n}'));
  assert.ok(videosCss.includes('.explorer-app.is-updated .video-card-row.is-small {\n  height: 20px;\n  margin-top: 4px;\n}'));
  console.log('Grid: (no Load button while loading, 2400 px ahead), freshness notice and "—", workshop button, type texts.');
  console.log('Focus kept on Select all matching; new cards guard a resting pointer; the jump shows the anchor whole and keeps it whole; no self-scroll.');
}

// 11. filters in the codec: optional keys, absent when empty, validated like the server.
{
  const base = catalogue.createInitialCatalogueConditions();
  assert.equal(JSON.stringify(catalogue.normalizeCatalogueConditions(base)), JSON.stringify(base), 'older objects keep their exact form');
  const more = catalogue.normalizeCatalogueConditions({ ...base, filters: { ...base.filters, captions: 'with', description_q: '  pasta  ', paid_promotion: '',
    stream_ended_from: '2026-03-29', stream_ended_before: '2026-04-01' } });
  assert.deepEqual(Object.fromEntries(['captions', 'description_q', 'paid_promotion', 'stream_ended_from', 'stream_ended_before'].map((key) => [key, more.filters[key]])),
    { captions: 'with', description_q: 'pasta', paid_promotion: undefined, stream_ended_from: '2026-03-29', stream_ended_before: '2026-04-01' });
  for (const [key, value] of [['captions', 'maybe'], ['paid_promotion', 'yes'], ['description_q', 'x'.repeat(101)], ['stream_ended_from', '2026-02-30'], ['stream_ended_before', '0001-01-01']]) {
    assert.throws(() => catalogue.normalizeCatalogueConditions({ ...base, filters: { ...base.filters, [key]: value } }), catalogue.CatalogueQueryError, key);
  }
  assert.notEqual(catalogue.catalogueAnalysisKey('UC' + 'a'.repeat(22), more), catalogue.catalogueAnalysisKey('UC' + 'a'.repeat(22), base), 'a further filter changes the matched set');
  assert.deepEqual(catalogue.clearCatalogueFilterGroup(more, 'stream_ended').filters.stream_ended_from, undefined);
  // Every duration preset of every format is a valid address value; the tab checks its own set.
  for (const type of ['long', 'short', 'stream']) {
    const keys = presets.presetsFor(type, 'duration').map((row) => row.key);
    assert.deepEqual(catalogue.normalizeCatalogueConditions({ ...base, facets: { ...base.facets, duration_buckets: keys } }).facets.duration_buckets, keys);
  }
  // The panel (server render): the format's duration presets, the further filters where their field is collected,
  // Stream ended only on Streams, category counts and the Collection fields link.
  const sharedFormat = compiled('../format.ts');
  const toolbar = compiled('FilterToolbar.tsx', {
    react: React, 'react/jsx-runtime': jsxRuntime, 'lucide-react': new Proxy({}, { get: () => () => null }),
    './filters': compiled('filters.ts'), './catalogueQuery': catalogue, './libraryData/presets.ts': presets,
    './savedViews': { RESEARCH_WORKSHOP_PATH: '/app-explorer-v3/research' },
    './resourceState': { resourcePayload: (state) => (state.kind === 'ready' ? state.payload : undefined) },
    './videoPresentation': { formatVideoCategory: (value) => value?.trim() || 'Unknown' },
    './format': compiled('format.ts', { '../format': sharedFormat, '../format.ts': sharedFormat }),
    './timeBasis': { useTimeBasis: () => ({ zone: 'Europe/Berlin' }) }, './timeZones': compiled('timeZones.ts'),
  });
  const gatesFor = (type, enabled) => ({ ...libraryGates(capabilities, type), field: (id) => enabled.includes(id) });
  const draw = (type, enabled, extra = {}) => renderToStaticMarkup(React.createElement(toolbar.FilterToolbar, {
    draft: base, applied: base, categories: [{ category_id: '22', category_name: 'People & Blogs', count: 12 }], errors: {}, error: '',
    videoType: type, gates: gatesFor(type, enabled), growthSortAvailable: true, derivedFiltersAvailable: true, facetState: { kind: 'idle' },
    facetsAvailable: true, incomplete: false, resetKey: 'k', onChange() {}, onApply: () => true, onCancel() {}, onImmediate() {}, onRemove() {},
    onClear() {}, onOpen() {}, onRetryFacets() {}, ...extra }));
  // A failed group-count read says why next to "Retry counts"; buckets stay "—", never 0.
  const status = (html) => html.match(/<div class="catalogue-facet-status" role="status">(.*?)<\/div>/)[1];
  for (const [kind, message, shown] of [
    ['error', 'The server could not complete the request (HTTP 500).', 'Group counts could not load. The server could not complete the request (HTTP 500).'],
    ['error', 'No connection to the Leti Cook server.', 'Group counts could not load. No connection to the Leti Cook server.'],
    ['error', 'Group counts are unavailable on this server.', 'Group counts are unavailable on this server.'],
    ['incompatible', 'Unexpected result.page.', 'Group counts could not load. Unexpected result.page.'],
  ]) {
    const failed = draw('long', [], { facetState: { kind, scopeKey: 'k', message } });
    assert.equal(status(failed), `${shown}<button type="button">Retry counts</button>`, `${kind}: ${message}`);
    assert.doesNotMatch(failed, /Loading group counts/);
    assert.match(failed, /<span>Under 5 min<\/span><small>—<\/small>/, 'no count is invented');
    assert.doesNotMatch(failed, /<small>0<\/small>/);
  }
  assert.equal(status(draw('long', [], { facetState: { kind: 'loading', scopeKey: 'k' } })), 'Loading group counts…', 'a running read still says so, without Retry');
  const shorts = draw('short', ['video.caption_available']);
  assert.match(shorts, /<legend>More filters<\/legend>/);
  assert.match(shorts, /<option value="with">With captions<\/option>/);
  assert.doesNotMatch(shorts, /Description contains|Declared paid promotion|Stream ended/);
  for (const preset of presets.presetsFor('short', 'duration')) assert.ok(shorts.includes(`<span>${preset.label}</span>`), preset.label);
  assert.doesNotMatch(shorts, /Under 5 min/, 'not the Long duration presets');
  assert.match(shorts, /People &amp; Blogs \(12\)/, 'categories with their counts');
  assert.match(shorts, /href="\/app-explorer-v3\/research\?settings=fields">Collection fields…<\/a>/);
  const streams = draw('stream', ['video.caption_available', 'video.description', 'video.paid_promotion']);
  assert.match(streams, /Description contains/);
  assert.match(streams, /Declared paid promotion/);
  assert.match(streams, /Stream ended from · local time/);
  assert.match(draw('long', []), /Under 5 min/);
  assert.doesNotMatch(draw('long', []), /More filters/, 'nothing offered: no empty fieldset');
  // One translation client per page: the Saved list and the Videos tab share the cache.
  const request = async () => ({});
  assert.equal(conditions.pageTranslationClient(request), conditions.pageTranslationClient(request));
  assert.notEqual(conditions.pageTranslationClient(request), conditions.pageTranslationClient(async () => ({})));
  console.log('Filters: optional codec keys, validation, scope key; per-format duration keys; Collection fields link; shared translation client.');
}

// 12. a next page that failed once and then loads on "Retry loading videos" clears its error,
// so the grid's automatic loading (paused while an error is shown) resumes. The real hook, driven offline.
{
  const errors = await import('../application/frontend/src/explorer/libraryData/errors.ts');
  class ApiError extends Error { constructor(message, status, code) { super(message); this.name = 'ExplorerApiError'; this.status = status; this.code = code; } }
  const future = new Date(Date.now() + 600_000).toISOString();
  const cards = (from, count) => Array.from({ length: count }, (_, i) => ({ video_id: `v${String(from + i).padStart(10, '0')}` }));
  const page = (from, next) => ({ videos: cards(from, 50), browse: { session_key: 's1', frozen_ref: `ref${from}`, expires_at: future,
    next_cursor: next, previous_cursor: null, start_index: from, total_count: 5761 } });
  const failures = {
    'HTTP 500': new ApiError('The server could not complete the request (HTTP 500).', 500),
    '503 backend_unavailable': new ApiError('The service is unavailable right now. Try again in a moment.', 503, 'backend_unavailable'),
    '429 query_busy': new ApiError('Too many requests. Wait a moment and try again.', 429, 'query_busy'),
    offline: new Error('No connection to the Leti Cook server.'),
    '422 query_budget_exceeded': new ApiError('The request was not accepted (HTTP 422).', 422, 'query_budget_exceeded'),
  };
  // A refusal without server text is shown in plain words, not as "HTTP 422".
  const shown = { '422 query_budget_exceeded': 'This selection is too large to browse at once. Narrow the filters.' };
  for (const [name, failure] of Object.entries(failures)) {
    const host = reactHookHost();
    const calls = [];
    const browse = compiled('useCatalogueBrowse.ts', {
      react: host.React, './catalogueRefresh.ts': refresh,
      '../sourceLifetime.ts': { sourceContainer: (value) => value },
      './catalogueBrowse': compiled('catalogueBrowse.ts'), './catalogueQuery': { catalogueUsesGrowthFilters: () => false },
      './format': { videoNoun: (count) => (count === 1 ? 'video' : 'videos') }, './libraryData/wire.ts': wire,
      './libraryData/savedLibrary.ts': { planLibraryOpen: async () => ({ status: 'run', note: '',
        translation: { status: 'translated', query: { where: null } }, request: { page: { size: 50 }, select: [] } }) },
      './libraryData/errors.ts': errors,
      './libraryData/videos.ts': { libraryFrozenFields: () => [], libraryGrowthFields: () => [], parseLibraryPage: (raw) => raw,
        shownCatalogueVideos: (p) => p.videos, nextCatalogueExpiry: () => Infinity, mergeLibraryPages: (current, next) => ({ ...next, videos: [...current.videos, ...next.videos] }),
        listHasMore: videos.listHasMore, listSlice: videos.listSlice, mergeLibrarySlice: videos.mergeLibrarySlice,
        selectionRecords: () => new Map() },
    });
    const inputs = { channelId: 'UC' + 'a'.repeat(22), videoType: 'long', conditions: catalogue.createInitialCatalogueConditions(), timeZone: 'UTC',
      scopeKey: 'scope', selectionScopeKey: 'selection', enabled: true, gates: null, growthAllowed: false, onGrowthUnavailable() {},
      refreshEnvironment: idleRefresh, onAnnouncement() {}, translate: async () => { throw new Error('planLibraryOpen is replaced'); },
      request: (path, body, signal) => new Promise((resolve, reject) => calls.push({ path, body, signal, resolve, reject })) };
    host.mount(browse.useCatalogueBrowse, inputs);
    await flush();
    calls[0].resolve(page(0, 'after-50')); await flush();
    assert.equal(host.current.state.kind, 'ready', name);
    host.current.onLoadMore(); await flush();
    assert.equal(calls[1].body.page.after, 'after-50');
    calls[1].reject(failure); await flush();
    assert.equal(host.current.loadMoreError, shown[name] ?? failure.message, `${name}: the failure is shown with Retry`);
    assert.doesNotMatch(host.current.loadMoreError, /HTTP 422/, name);
    assert.equal(host.current.loadMoreErrorDirection, 'next');
    host.current.onLoadMore(); await flush();   // "Retry loading videos"
    assert.equal(host.current.isLoadingMore, true);
    assert.equal(host.current.loadMoreError, undefined, `${name}: no error while the retry runs`);
    calls[2].resolve(page(50, 'after-100')); await flush();
    assert.equal(host.current.state.payload.videos.length, 100, name);
    assert.equal(host.current.loadMoreError, undefined, `${name}: a successful retry clears the error`);
    assert.equal(host.current.loadMoreErrorDirection, undefined, `${name}: and its direction (the button says Load more again)`);
    assert.equal(host.current.hasMore, true, `${name}: the next page can load automatically`);
    // The automatic load after the retry is the same request as any other next page.
    host.current.onLoadMore(); await flush();
    assert.equal(calls[3].body.page.after, 'after-100');
    calls[3].resolve(page(100, null)); await flush();
    assert.equal(host.current.state.payload.videos.length, 150);
    assert.equal(host.current.loadMoreError, undefined);
    host.unmount();
  }
  // The grid pauses automatic loading only while an error is shown (guard unchanged).
  assert.match(read('../application/frontend/src/explorer/ExplorerVideos.tsx'), /const autoLoading = typeof IntersectionObserver !== 'undefined' && !loadMoreError && !expired/);
  console.log('after a failed next page, a successful retry clears the error and automatic loading resumes (500, 503, 429, offline, 422).');
}

// Current pages carry their own clock and one forward cursor. Changes to
// counters between reads can move a video across the keyset boundary.
{
  const { request, response } = example('long_first_anchor');
  request.consistency = 'current';
  request.page = { size: response.rows.length };
  response.context.consistency = 'current';
  delete response.context.frozen_ref;
  response.page = { has_more: true, next_cursor: 'next-page' };
  const parse = () => videos.parseLibraryPage(response, { request, channelId: request.scope.id });
  const first = parse();
  request.page.anchor_id = first.videos[0].video_id;
  assert.equal(parse().browse.anchor_video_id, request.page.anchor_id);
  request.page.anchor_id = 'Missing0000';
  assert.equal(parse().browse.anchor_video_id, null, 'ordinary first page after a missing anchor');
  delete request.page.anchor_id;
  assert.equal(first.browse.total_count, undefined);
  assert.equal(first.browse.frozen_ref, undefined);
  request.page.after = 'next-page';
  response.page = { has_more: false, next_cursor: null };
  const last = parse();
  assert.equal(videos.mergeLibraryPages(first, last, 'next').videos.length, first.videos.length);
  // A second continuation of an earlier immutable page owns its own ID index.
  const forkPage = id => ({ ...first, videos: [{ ...first.videos[0], video_id: id }] });
  const a = videos.mergeLibraryPages(first, forkPage('freshA00000'), 'next');
  const ab = videos.mergeLibraryPages(a, forkPage('freshB00000'), 'next');
  const ac = videos.mergeLibraryPages(a, forkPage('freshC00000'), 'next');
  assert.ok(!a.videos.some(v => v.video_id === 'freshB00000'));
  assert.equal(videos.mergeLibraryPages(ac, forkPage('freshB00000'), 'next').videos.length, ab.videos.length + 1);
  // Wall and monotonic deadlines stay independent when earlier pages remain on screen.
  const now = Date.now(), mono = performance.now();
  const timed = (id, ttl) => lifetime.captureSourceLifetime({ ...forkPage(id),
    source_read_at: new Date(now).toISOString(), source_expires_at: new Date(now + ttl).toISOString(),
    videos: [{ video_id: id }] }, undefined, mono);
  // Time alone removes no loaded row: both stay when the earlier page's lifetime has passed on either clock.
  const mixed = videos.mergeLibraryPages(timed('freshA00000', 2000), timed('freshB00000', 9000), 'next');
  assert.equal(videos.shownCatalogueVideos(mixed, now, mono), mixed.videos);
  for (const [wall, monotonic] of [[now + 2500, mono], [now, mono + 2500], [now + 86_400_000, mono + 86_400_000]]) {
    assert.deepEqual(videos.shownCatalogueVideos(mixed, wall, monotonic).map(v => v.video_id), ['freshA00000', 'freshB00000']);
  }
  contract(() => videos.pageIdentity(first), /fixed list/);
  response.context.frozen_ref = 'unexpected';
  contract(parse);
  delete response.context.frozen_ref;
  response.page.has_more = true;
  contract(parse, /continuation/);
}

// 13. "Select all matching" takes the whole list without a read; past the list limit it says so in plain
// words. "Open in research workshop" refused for size, time or a turned-off field says so in plain words,
// never "HTTP 422". The real hook, driven offline.
{
  const errors = await import('../application/frontend/src/explorer/libraryData/errors.ts');
  class ApiError extends Error { constructor(message, status, code, limit) { super(message); this.name = 'ExplorerApiError'; this.status = status; this.code = code; if (limit) this.limit = limit; } }
  const future = new Date(Date.now() + 600_000).toISOString();
  const listIds = Array.from({ length: 5761 }, (_, i) => 'v' + String(i).padStart(10, '0'));
  const first = (order) => ({ videos: [{ video_id: listIds[0] }], browse: { consistency: 'current', session_key: 's1', expires_at: future,
    next_cursor: 'after-50', previous_cursor: null, start_index: 0, end_index: 1, left_results: 0, query_fingerprint: 'q', order } });
  const http422 = 'The request was not accepted (HTTP 422).';
  const mount = (calls) => {
    const host = reactHookHost();
    const browse = compiled('useCatalogueBrowse.ts', {
      react: host.React, './catalogueRefresh.ts': refresh,
      '../sourceLifetime.ts': { sourceContainer: (value) => value },
      './catalogueBrowse': compiled('catalogueBrowse.ts'), './catalogueQuery': { catalogueUsesGrowthFilters: () => false },
      './format': { videoNoun: (count) => (count === 1 ? 'video' : 'videos') }, './libraryData/wire.ts': wire,
      './libraryData/savedLibrary.ts': { planLibraryOpen: async () => ({ status: 'run', note: '',
        translation: { status: 'translated', query: { where: null } }, request: { page: { size: 50 }, select: [] } }) },
      './libraryData/errors.ts': errors,
      './libraryData/videos.ts': { libraryFrozenFields: () => [], libraryGrowthFields: () => [], parseLibraryPage: (raw) => raw,
        shownCatalogueVideos: (p) => p.videos, nextCatalogueExpiry: () => Infinity, mergeLibraryPages: (current, next) => ({ ...next, videos: [...current.videos, ...next.videos] }),
        listHasMore: videos.listHasMore, listSlice: videos.listSlice, mergeLibrarySlice: videos.mergeLibrarySlice,
        selectionRecords: videos.selectionRecords },
    });
    host.mount(browse.useCatalogueBrowse, { channelId: 'UC' + 'a'.repeat(22), videoType: 'long',
      conditions: catalogue.createInitialCatalogueConditions(), timeZone: 'UTC', scopeKey: 'scope', selectionScopeKey: 'selection',
      enabled: true, gates: null, growthAllowed: false, onGrowthUnavailable() {}, onAnnouncement() {}, refreshEnvironment: idleRefresh,
      translate: async () => { throw new Error('planLibraryOpen is replaced'); },
      request: (path, body, signal) => new Promise((resolve, reject) => calls.push({ path, body, signal, resolve, reject })) });
    return host;
  };
  {
    const calls = [], host = mount(calls);
    await flush();
    calls[0].resolve(first({ ids: listIds, total: listIds.length, limit: 200000 })); await flush();
    const progress = [];
    const records = await host.current.collectMatching(new AbortController().signal, (value) => progress.push(value));
    assert.deepEqual(records.map((row) => row.video_id), listIds, 'every video of the list, in its order');
    assert.equal(calls.length, 1, 'no further read');
    assert.deepEqual(progress, [{ received: 5761, total: 5761 }]);
    assert.deepEqual(host.current.listIds, listIds);
    host.unmount();
  }
  {
    const calls = [], host = mount(calls);
    await flush();
    calls[0].resolve(first({ ids: null, total: 250000, limit: 200000 })); await flush();
    await assert.rejects(host.current.collectMatching(new AbortController().signal, () => {}),
      (error) => /This list has 250,000 videos, more than the 200,000/.test(error.message) && /Existing selection is kept\./.test(error.message));
    assert.equal(calls.length, 1);
    assert.equal(host.current.listIds, undefined);
    host.unmount();
  }
  for (const [failure, text] of [
    [new ApiError(http422, 422, 'query_budget_exceeded'), 'This selection is too large to browse at once. Narrow the filters.'],
    [new ApiError(http422, 422, 'query_budget_exceeded', 'time'), 'This selection took too long to prepare. Narrow the filters or try again.'],
    [new ApiError(http422, 422, 'field_disabled'), 'A field in these filters is turned off in Collection fields. Remove that condition or turn the field on.'],
  ]) {
    const calls = [], host = mount(calls);
    await flush();
    calls[0].resolve(first({ ids: listIds, total: listIds.length, limit: 200000 })); await flush();
    const opening = host.current.freezeForWorkshop();
    await flush();
    assert.equal(calls[0].body.consistency, 'current');
    assert.equal(calls[1].path, wire.ROUTES.execute);
    assert.equal(calls[1].body.consistency, 'frozen');
    assert.deepEqual(calls[1].body.page, { size: 50 }, 'the fixed list has no list or slice options');
    calls[1].reject(failure);
    await assert.rejects(opening, (error) => errors.libraryErrorState(error, 'select_all').message === text
      && !/HTTP 422/.test(errors.libraryErrorState(error, 'select_all').message), `${failure.code}:${failure.limit}`);
    host.unmount();
  }
  console.log('Select all matching takes the whole list without a read; past the limit and workshop refusals are plain words.');
}


// One list per query (the real hook, driven offline). Its first page brings the ordered IDs; later pages are
// slices of them; a sort change opens the new list at its top; Reload and Refresh order of the same list open at
// the remembered card with everything above it reachable; time alone never reads the list again.
{
  const host = reactHookHost();
  const ids = Array.from({ length: 1001 }, (_, i) => 'v' + String(i).padStart(10, '0'));
  const calls = [];
  const browse = compiled('useCatalogueBrowse.ts', {
    react: host.React, './catalogueRefresh.ts': refresh,
    '../sourceLifetime.ts': lifetime,
    './catalogueBrowse': compiled('catalogueBrowse.ts'), './catalogueQuery': { catalogueUsesGrowthFilters: () => false },
    './format': { videoNoun: () => 'videos' }, './libraryData/wire.ts': wire,
    './libraryData/savedLibrary.ts': { planLibraryOpen: async () => ({ status: 'run', translation: { query: {} }, request: { page: { size: 50 } } }) },
    './libraryData/errors.ts': await import('../application/frontend/src/explorer/libraryData/errors.ts'),
    './libraryData/videos.ts': { libraryFrozenFields: () => [], libraryGrowthFields: () => [], parseLibraryPage: raw => raw,
      shownCatalogueVideos: videos.shownCatalogueVideos, nextCatalogueExpiry: videos.nextCatalogueExpiry,
      expiringCatalogueIds: videos.expiringCatalogueIds, refreshLibraryRows: videos.refreshLibraryRows,
      selectionRecords: videos.selectionRecords, mergeLibraryPages: videos.mergeLibraryPages,
      listHasMore: videos.listHasMore, listSlice: videos.listSlice, mergeLibrarySlice: videos.mergeLibrarySlice,
      listPosition: videos.listPosition, parseFrozenIds: (raw) => raw.ids },
  });
  const initial = { channelId: 'UC' + 'a'.repeat(22), videoType: 'long', scopeKey: 'views-desc\nreload:0', listKey: 'views-desc',
    selectionScopeKey: 'channel-A', enabled: true, gates: null, growthAllowed: false, onAnnouncement() {}, onGrowthUnavailable() {},
    refreshEnvironment: idleRefresh,
    request: (path, body) => new Promise((resolve, reject) => calls.push({ path, body, resolve, reject })) };
  host.mount(browse.useCatalogueBrowse, initial);
  await flush();
  const until = new Date(Date.now() + 600_000).toISOString();
  const order = (list = ids) => ({ ids: list, total: list.length, limit: 200000 });
  // What parseLibraryPage returns for a list page (from `start`) or a slice (`list` undefined).
  const reply = (session, rows, { list, start = 0, more = false } = {}) => ({
    contract_version: 1, video_type: 'long', channel: { channel_id: initial.channelId }, growth: false, columns: ['video.id'], time_zone: 'UTC',
    videos: rows.map((id) => ({ video_id: id, video_type: 'long' })),
    browse: { consistency: 'current', session_key: session, query_fingerprint: 'same-query', created_at: until, expires_at: until,
      start_index: start, end_index: start + rows.length, previous_cursor: null, next_cursor: more ? 'cursor' : null,
      anchor_video_id: null, left_results: 0, ...(list ? { order: order(list) } : {}) },
    pagination: { matched_count: list?.length, offset: start, returned_count: rows.length, has_more_in_scan: more },
    coverage: { complete: true },
  });
  assert.deepEqual(calls[0].body.page, { size: 50, growth_lifetime: 'page', order: 'ids' }, 'a new list starts at its top');
  calls[0].resolve(reply('read1', ids.slice(0, 50), { list: ids, more: true })); await flush();
  assert.equal(host.current.order.total, 1001);
  assert.equal(host.current.hasMore, true);
  assert.equal(host.current.hasPrevious, false);
  assert.equal(host.current.startsBelowTop, false);
  host.current.onLoadMore(); await flush();
  assert.deepEqual(calls[1].body.page, { size: 50, growth_lifetime: 'page', ids: ids.slice(50, 100) }, 'the next slice of the list');
  // A video of the slice left the results meanwhile: counted, never replaced, and the list goes on after it.
  calls[1].resolve(reply('slice', ids.slice(50, 100).filter((id) => id !== ids[60]))); await flush();
  assert.equal(host.current.state.payload.videos.length, 99);
  assert.equal(host.current.leftResults, 1);
  host.current.onLoadMore(); await flush();
  assert.deepEqual(calls[2].body.page.ids, ids.slice(100, 150));
  calls[2].resolve(reply('slice', ids.slice(100, 150))); await flush();
  assert.equal(host.current.state.payload.videos.length, 149);
  assert.equal(host.current.sessionKey, 'read1', 'slices belong to the read of the list');
  // Select all matching: the whole list without a read.
  const selected = await host.current.collectMatching(new AbortController().signal, () => {});
  assert.deepEqual(selected.map((row) => row.video_id), ids);
  assert.equal(calls.length, 3);
  // Time alone never reads the list again, and the loaded cards stay (their background update: test_library_videos_refresh.mjs).
  assert.equal(calls.length, 3);
  assert.equal(host.current.state.payload.videos.length, 149, 'loaded cards stay');
  // Reload of the same list opens at the remembered card; everything above it is reachable.
  const visibleId = ids[177];
  host.current.onAnchorChange({ videoId: visibleId, offsetPx: 17 });
  host.update({ ...initial, scopeKey: 'views-desc\nreload:1' }); await flush();
  assert.deepEqual(calls[3].body.page, { size: 50, growth_lifetime: 'page', order: 'ids', anchor_id: visibleId });
  const resumed = reply('read2', ids.slice(177, 227), { list: ids, start: 177, more: true });
  resumed.browse.anchor_video_id = visibleId;
  calls[3].resolve(resumed); await flush();
  assert.equal(host.current.restoreAnchor.videoId, visibleId);
  assert.equal(host.current.startsBelowTop, true);
  assert.equal(host.current.hasPrevious, true);
  host.current.onLoadPrevious(); await flush();
  assert.deepEqual(calls[4].body.page.ids, ids.slice(127, 177), 'the slice above the remembered card');
  calls[4].resolve(reply('slice', ids.slice(127, 177))); await flush();
  assert.equal(host.current.state.payload.browse.start_index, 127);
  assert.equal(host.current.state.payload.videos[0].video_id, ids[127]);
  // A sort change (same matched set, same selection) opens the new list at its top, never at the remembered card.
  host.current.onAnchorChange({ videoId: ids[150], offsetPx: 0 });
  const reversed = [...ids].reverse();
  host.update({ ...initial, scopeKey: 'views-asc\nreload:1', listKey: 'views-asc' }); await flush();
  assert.equal(host.current.state.kind, 'loading');
  assert.deepEqual(calls[5].body.page, { size: 50, growth_lifetime: 'page', order: 'ids' }, 'no anchor after a sort change');
  calls[5].resolve(reply('read3', reversed.slice(0, 50), { list: reversed, more: true })); await flush();
  assert.equal(host.current.restoreAnchor, undefined);
  assert.equal(host.current.anchorMissing, false);
  assert.equal(host.current.state.payload.videos[0].video_id, reversed[0]);
  // Refresh order keeps the remembered card of this list; Show from the beginning is a plain reset.
  host.current.onAnchorChange({ videoId: reversed[60], offsetPx: 0 });
  host.current.onRefreshOrder(); await flush();
  assert.equal(host.current.state.kind, 'updating', 'the loaded cards stay while the list is read again');
  assert.equal(calls[6].body.page.anchor_id, reversed[60]);
  calls[6].resolve(reply('read4', reversed.slice(0, 50), { list: reversed, more: true })); await flush();
  assert.equal(host.current.anchorMissing, true, 'the remembered card is gone: the top, said so');
  host.current.onStartFromBeginning(); await flush();
  assert.equal(calls[7].body.page.anchor_id, undefined);
  calls[7].resolve(reply('read5', reversed.slice(0, 50), { list: reversed, more: true })); await flush();
  assert.equal(host.current.anchorMissing, false);
  // Remembered at the list's very top: reading it again starts at the top, so videos that arrived above are in view.
  host.current.onAnchorChange({ videoId: reversed[0], offsetPx: 0 });
  host.current.onRefreshOrder(); await flush();
  assert.equal(calls[8].body.page.anchor_id, undefined);
  const newest = ['vNEW0000000', ...reversed];
  calls[8].resolve(reply('read6', newest.slice(0, 50), { list: newest, more: true })); await flush();
  assert.equal(host.current.state.payload.videos[0].video_id, 'vNEW0000000');
  // "Open in research workshop" fixes the same query without list options.
  const fixedList = { ...reply('fixed', []), browse: { consistency: 'frozen', session_key: 'fixed', frozen_ref: 'reference', total_count: 1002,
    expires_at: until, query_fingerprint: 'same-query' } };
  const opening = host.current.freezeForWorkshop(); await flush();
  assert.deepEqual([calls[9].body.consistency, calls[9].body.page], ['frozen', { size: 50 }]);
  calls[9].resolve(fixedList);
  assert.equal((await opening).browse.frozen_ref, 'reference');
  assert.equal(calls.length, 10, 'no ID export without a selection to check');
  // "Send selected" of the whole list: the fixed list goes only when it holds exactly the selected videos.
  const sending = host.current.freezeForWorkshop(new Set(newest)); await flush();
  calls[10].resolve(fixedList); await flush();
  assert.deepEqual([calls[11].path, calls[11].body], [wire.ROUTES.frozenIds, { version: 1, frozen_ref: 'reference' }]);
  calls[11].resolve({ ids: [...newest].reverse() });
  assert.equal((await sending).browse.frozen_ref, 'reference', 'the same videos in another order are the selection');
  const changed = host.current.freezeForWorkshop(new Set(newest)); await flush();
  calls[12].resolve(fixedList); await flush();
  calls[13].resolve({ ids: [...newest.slice(1), 'vLATE000000'] });
  await assert.rejects(changed, (error) => error.message === browse.selectionChangedText(1002, 1002)
    && /Nothing was sent\./.test(error.message));
  // Another channel: a busy slice keeps the cards; withdrawn access removes them.
  host.update({ ...initial, scopeKey: 'channel-B\nreload:1', listKey: 'channel-B', selectionScopeKey: 'channel-B' });
  await flush();
  const permitted = reply('read7', [visibleId], { list: [visibleId, ids[1]], more: true });
  permitted.videos[0].title = 'Visible while permitted';
  calls[14].resolve(permitted); await flush();
  host.current.onLoadMore(); await flush();
  assert.deepEqual(calls[15].body.page.ids, [ids[1]]);
  calls[15].reject(Object.assign(new Error('Busy'), { code: 'query_busy', status: 503 })); await flush();
  assert.equal(host.current.state.payload.videos[0].title, permitted.videos[0].title);
  assert.ok(host.current.loadMoreError);
  host.current.onLoadMore(); await flush();
  calls[16].reject(Object.assign(new Error('Permission withdrawn'), { status: 403 })); await flush();
  assert.equal(host.current.state.kind, 'error');
  assert.equal(host.current.state.previous, undefined);
  assert.equal(host.current.executed, undefined);
  assert.equal(host.current.sessionKey, undefined);
  assert.ok(host.current.accessRevision > 0);
  host.unmount();
  console.log('One list: slices, nothing restarts by time, Reload at the remembered card, a sort change at the top.');
}

// Past the ordered-list limit (no IDs): pages continue by cursor, and after the cursor's lifetime from the last loaded
// card with the same page size (the size is part of the query; the next cursor continues it). Reload at a remembered
// card offers the way back to the top.
{
  const host = reactHookHost();
  const calls = [];
  const browse = compiled('useCatalogueBrowse.ts', {
    react: host.React, './catalogueRefresh.ts': refresh, '../sourceLifetime.ts': lifetime,
    './catalogueBrowse': compiled('catalogueBrowse.ts'), './catalogueQuery': { catalogueUsesGrowthFilters: () => false },
    './format': { videoNoun: () => 'videos' }, './libraryData/wire.ts': wire,
    './libraryData/savedLibrary.ts': { planLibraryOpen: async () => ({ status: 'run', translation: { query: {} }, request: { page: { size: 50 } } }) },
    './libraryData/errors.ts': await import('../application/frontend/src/explorer/libraryData/errors.ts'),
    './libraryData/videos.ts': { libraryFrozenFields: () => [], libraryGrowthFields: () => [], parseLibraryPage: raw => raw,
      shownCatalogueVideos: videos.shownCatalogueVideos, nextCatalogueExpiry: videos.nextCatalogueExpiry,
      expiringCatalogueIds: videos.expiringCatalogueIds, refreshLibraryRows: videos.refreshLibraryRows,
      selectionRecords: videos.selectionRecords, mergeLibraryPages: videos.mergeLibraryPages,
      listHasMore: videos.listHasMore, listSlice: videos.listSlice, mergeLibrarySlice: videos.mergeLibrarySlice,
      listPosition: videos.listPosition, parseFrozenIds: (raw) => raw.ids },
  });
  const ids = Array.from({ length: 200 }, (_, i) => 'w' + String(i).padStart(10, '0'));
  const initial = { channelId: 'UC' + 'a'.repeat(22), videoType: 'long', scopeKey: 'big\nreload:0', listKey: 'big',
    selectionScopeKey: 'big', enabled: true, gates: null, growthAllowed: false, onAnnouncement() {}, onGrowthUnavailable() {},
    refreshEnvironment: idleRefresh,
    request: (path, body) => new Promise((resolve, reject) => calls.push({ path, body, resolve, reject })) };
  host.mount(browse.useCatalogueBrowse, initial);
  await flush();
  const live = new Date(Date.now() + 600_000).toISOString(), gone = new Date(Date.now() - 1000).toISOString();
  const reply = (rows, { cursor = null, expires = live, anchor = null, listed = false } = {}) => ({
    contract_version: 1, video_type: 'long', channel: { channel_id: initial.channelId }, growth: false, columns: ['video.id'], time_zone: 'UTC',
    videos: rows.map((id) => ({ video_id: id, video_type: 'long' })),
    browse: { consistency: 'current', session_key: 'big-read', query_fingerprint: 'same-query', created_at: live, expires_at: expires,
      start_index: 0, end_index: rows.length, previous_cursor: null, next_cursor: cursor, anchor_video_id: anchor, left_results: 0,
      ...(listed ? { order: { ids: null, total: 250000, limit: 200000 } } : {}) },
    pagination: { matched_count: listed ? 250000 : undefined, offset: 0, returned_count: rows.length, has_more_in_scan: Boolean(cursor) },
    coverage: { complete: true },
  });
  calls[0].resolve(reply(ids.slice(0, 50), { cursor: 'c1', expires: gone, listed: true })); await flush();
  assert.deepEqual([host.current.order.total, host.current.listIds, host.current.hasMore, host.current.hasPrevious, host.current.startsBelowTop],
    [250000, undefined, true, false, false]);
  // The cursor's lifetime has passed: the next page starts at the last loaded card, with the same size.
  host.current.onLoadMore(); await flush();
  assert.deepEqual(calls[1].body.page, { size: 50, growth_lifetime: 'page', anchor_id: ids[49] });
  const resumed = reply(ids.slice(49, 99), { cursor: 'c2', anchor: ids[49] });
  calls[1].resolve(resumed); await flush();
  assert.deepEqual(host.current.state.payload.videos.map((video) => video.video_id), ids.slice(0, 99), 'the card itself is not repeated');
  assert.equal(host.current.state.payload.pagination.matched_count, 250000);
  assert.equal(host.current.loadMoreError, undefined);
  // ... and its cursor continues the same query.
  host.current.onLoadMore(); await flush();
  assert.deepEqual(calls[2].body.page, { size: 50, growth_lifetime: 'page', after: 'c2' });
  calls[2].resolve(reply(ids.slice(99, 149), { cursor: 'c3' })); await flush();
  assert.equal(host.current.state.payload.videos.length, 149);
  // Reload at a remembered card: nothing above it can load by slices, so "Show from the beginning" is offered.
  host.current.onAnchorChange({ videoId: ids[120], offsetPx: 0 });
  host.update({ ...initial, scopeKey: 'big\nreload:1' }); await flush();
  assert.equal(calls[3].body.page.anchor_id, ids[120]);
  calls[3].resolve(reply(ids.slice(120, 170), { cursor: 'c4', anchor: ids[120], listed: true })); await flush();
  assert.deepEqual([host.current.startsBelowTop, host.current.hasPrevious], [true, false]);
  host.current.onStartFromBeginning(); await flush();
  assert.equal(calls[4].body.page.anchor_id, undefined);
  calls[4].resolve(reply(ids.slice(0, 50), { cursor: 'c5', listed: true })); await flush();
  assert.equal(host.current.startsBelowTop, false);
  // At the very top of such a list, reading it again starts at the top too.
  host.current.onAnchorChange({ videoId: ids[0], offsetPx: 0 });
  host.current.onRefreshOrder(); await flush();
  assert.equal(calls[5].body.page.anchor_id, undefined);
  host.unmount();
  console.log('Past the limit: cursor pages, resumption at the last card with the same size, the way back to the top.');
}

// One ordered list on the wire (real rows of the fixture as current replies): the first page is the list's
// slice at its first row, slices are the requested videos in their order, nothing repeats or is skipped.
{
  const ids = example('long_frozen_ids').response.ids;
  const current = (name, page, mutate) => {
    const { request, response } = example(name);
    request.consistency = 'current';
    request.page = page;
    response.context.consistency = 'current';
    delete response.context.frozen_ref;
    mutate?.(response, request);
    return { request, response };
  };
  const parse = ({ request, response }) => videos.parseLibraryPage(response, { request, channelId: request.scope.id });
  const order = { total_count: ids.length, ids: ids.join(''), limit: 200000 };
  const first = current('long_first_anchor', { size: 4, growth_lifetime: 'page', order: 'ids', anchor_id: example('long_first_anchor').response.rows[0].id },
    (response) => { response.page = { has_more: true, next_cursor: 'next-page', order: clone(order) }; });
  const a = parse(first);
  assert.deepEqual([a.browse.start_index, a.browse.end_index, a.browse.order.total, a.pagination.matched_count], [4, 8, 37, 37]);
  assert.deepEqual([videos.listHasMore(a, 'next'), videos.listHasMore(a, 'previous')], [true, true]);
  assert.deepEqual(videos.listSlice(a, 'next', 4), ids.slice(8, 12));
  assert.deepEqual(videos.listSlice(a, 'previous', 4), ids.slice(0, 4));
  assert.equal(videos.listPosition(a.browse.order, ids[20]), 20);
  assert.equal(videos.listPosition(a.browse.order, 'zzzzzzzzzzz'), -1);
  const broken = (change) => { const value = clone(first); change(value); return () => parse(value); };
  contract(broken((v) => { delete v.request.page.anchor_id; }), /not a slice/);
  contract(broken((v) => { v.response.page.order.ids = [...ids].reverse().join(''); }), /not a slice/);
  contract(broken((v) => { v.response.page.order.ids = [...ids.slice(0, 36), ids[0]].join(''); }), /repeated/);
  contract(broken((v) => { v.response.page.order.total_count = 36; }), /every video once/);
  contract(broken((v) => { v.response.page.order = { total_count: 37, ids: null, limit: 200000 }; }), /withheld/);
  contract(broken((v) => { delete v.response.page.order; }), /missing key/);
  contract(broken((v) => { v.response.page.has_more = false; v.response.page.next_cursor = null; }), /not a slice/);
  const withheld = clone(first);
  withheld.response.page.order = { total_count: 250000, ids: null, limit: 200000 };
  const past = parse(withheld);
  assert.deepEqual([past.browse.order.ids, past.pagination.matched_count, videos.listHasMore(past, 'previous')], [null, 250000, false]);
  // Slices: the frozen next and previous pages' rows, as current replies of exactly those IDs.
  const nextIds = ids.slice(8, 12), previousIds = ids.slice(0, 4);
  const slice = (name, wanted) => current(name, { size: 4, growth_lifetime: 'page', ids: wanted },
    (response) => { response.page = { has_more: false, next_cursor: null }; });
  const b = videos.mergeLibrarySlice(a, parse(slice('long_next', nextIds)), 'next', nextIds);
  const c = videos.mergeLibrarySlice(b, parse(slice('long_previous', previousIds)), 'previous', previousIds);
  assert.deepEqual(c.videos.map((video) => video.video_id), ids.slice(0, 12), 'nothing skipped or repeated');
  assert.deepEqual([c.browse.start_index, c.browse.end_index, c.browse.left_results, c.pagination.matched_count], [0, 12, 0, 37]);
  const gap = slice('long_next', nextIds);
  gap.response.rows.splice(1, 1);
  const d = videos.mergeLibrarySlice(a, parse(gap), 'next', nextIds);
  assert.deepEqual([d.videos.length, d.browse.end_index, d.browse.left_results], [7, 12, 1], 'a video that left is counted, not replaced');
  assert.deepEqual(videos.listSlice(d, 'next', 4), ids.slice(12, 16));
  const swapped = slice('long_next', nextIds);
  swapped.response.rows.reverse();
  contract(() => parse(swapped), /requested videos/);
  const foreign = slice('long_next', ids.slice(20, 24));
  contract(() => parse(foreign), /requested videos/);
  contract(() => videos.mergeLibrarySlice(a, parse(slice('long_next', nextIds)), 'previous', nextIds), /next slice/);
  contract(() => videos.mergeLibrarySlice(c, parse(slice('long_next', nextIds)), 'next', nextIds), /next slice/);
  console.log('Ordered list on the wire: slice of its list, strict IDs, slices in order, gaps counted.');
}

// An expired first-loading report coexists with fresh stored videos. Both Library
// sections must hand the current sample to their display components.
{
  const shell = {
    channelId: 'channel', videoType: 'short', activeSection: 'videos', timeZone: 'UTC',
    navigation: { state: {}, restored: 1 }, profileReady: false,
    typeReadiness: { status: 'unknown' },
  };
  const cards = Array.from({ length: 75 }, (_, index) => ({ id: String(index) }));
  const common = { 'react/jsx-runtime': jsxRuntime, react: React };
  // Unused imports are deliberately empty. Only view children are replaced.
  const importsFor = extra => new Proxy({ ...common, ...extra }, {
    has: () => true, get: (target, key) => target[key] ?? {},
  });
  const { LibraryVideosSection } = compiled('LibraryVideosSection.tsx', importsFor({
    './AlertEditor': { WatchSelection: () => 'Watch selection' },
    './ExplorerVideosWorkspace': { ExplorerVideosWorkspace: ({ rawState }) => String(rawState.payload.videos.length) + ' cards' },
  }));
  const grid = renderToStaticMarkup(React.createElement(LibraryVideosSection, { context: shell, videos: {
    visibleRawState: { kind: 'ready', payload: { videos: cards } }, visibleFacetState: { kind: 'idle' }, browse: { translated: {} },
    conditions: {}, applied: {}, growthSortAvailable: true,
  } }));
  assert.match(grid, /75 cards/);
  assert.match(grid, /Watch selection/);
  const { OverviewPanel } = compiled('ExplorerPanels.tsx', importsFor({
    '../sourceLifetime': lifetime, './resourceState': { resourcePayload: state => state.payload },
    './ChannelFingerprint': { ChannelFingerprint: ({ payload }) => String(payload.sample.matched_video_count) + ' matched' },
    './PublishingRhythm.tsx': { PublishingRhythm: () => null },
    './OverviewContentBreakdown.tsx': { OverviewContentBreakdown: () => null },
  }));
  const overview = renderToStaticMarkup(React.createElement(OverviewPanel, {
    videoType: shell.videoType, profileReady: false, typeReadiness: shell.typeReadiness, availability: 'available',
    overviewState: { kind: 'ready', payload: { sample: { matched_video_count: cards.length } } },
  }));
  assert.match(overview, /75 matched/);
}
console.log('Library Videos passed.');

{
  const props = { channelId: 'UC'+'A'.repeat(22), type: 'short', coverage: 'complete',
    check: {epoch: 1, estimated_general: 2, request: null, unavailable: null, quota_retry_at: null},
    onRead() {}, onComplete() {} };
  const make = (runtime, send) => compiled('ManualListCheckControl.tsx', {
    react: runtime, 'react/jsx-runtime': jsxRuntime, './api': { requestJson: send },
    './savedWork': { newSavedWorkId: () => '01234567-abcd-4123-abcd-0123456789ab' },
  }).ManualListCheckControl;
  const view = make(React, () => assert.fail('opening must not schedule a check'));
  const markup = extra => renderToStaticMarkup(React.createElement(view, {...props, ...extra}));
  assert.match(markup({}), /About 2 General.*0 Stats.*Verify full list/);
  assert.match(markup({coverage:'absent'}), /1 General.*More pages/);
  assert.match(markup({check:{...props.check,estimated_general:null}}), /From 1 General/);
  assert.doesNotMatch(markup({check:{...props.check,unavailable:'format_off'}}), /Verify full list/);
  const host = reactHookHost();
  const calls = [];
  const control = make(host.React, async (url, _, init) => {
    calls.push({url,init}); return {request:{id:JSON.parse(init.body).request_id}, joined:false};
  });
  host.mount(() => control(props));
  const button = (node,label) => Array.isArray(node) ? node.map(x => button(x,label)).find(Boolean)
    : node?.type === 'button' && node.props.children === label ? node : node?.props && button(node.props.children,label);
  button(host.current,'Verify full list').props.onClick(); await flush();
  assert.equal(calls.length,0);
  assert.match(renderToStaticMarkup(host.current), /Coverage is already confirmed/);
  await button(host.current,'Confirm check').props.onClick(); await flush();
  assert.equal(calls.length,1);
  assert.equal(calls[0].url,'/channels/'+props.channelId+'/explorer/list-check/short');
  assert.equal(calls[0].init.headers['X-Saved-Work'],'1');
  assert.equal(JSON.parse(calls[0].init.body).previous,null);
}

{
  const defaults = { name:'Views', event:'video.views', direction:'up', threshold:'100', scope:{kind:'channel',id:'UC'+'a'.repeat(22)} };
  const mod = (react, requestJson) => compiled('AlertEditor.tsx', {
    './alerts.css': {}, react, 'react/jsx-runtime':jsxRuntime, './api': {requestJson},
    '../sourceLifetime':lifetime, '../sourceLifetimeReact': {useSourceExpiry:()=>{}},
  });
  const {WatchSelection}=mod(React,()=>assert.fail('opening Library must not create a rule'));
  const watch = scope => renderToStaticMarkup(React.createElement(WatchSelection, {query:{scope,video_types:['short']}}));
  for (const scope of [{kind:'library'},{kind:'saved',id:'view'}]) {
    assert.match(watch(scope),/<button[^>]*disabled/);
  }
  for (const kind of ['channel','folder','label']) {
    assert.doesNotMatch(watch({kind,id:'fixture'}),/disabled/);
  }
  for (const formats of [['short'],['long','short'],[]]) {
    const host=reactHookHost(), sent=[];
    const {Editor}=mod(host.React, async (url,_,init)=>{
      if (url.endsWith('/capabilities')) return {formats};
      assert.ok(url.endsWith('/rules')); sent.push(JSON.parse(init.body).rule);
      return {};
    });
    const props={initial:defaults,onClose:()=>{},onSaved:()=>{}};
    host.mount(()=>Editor(props));
    await flush();
    await host.current.props.onSubmit({preventDefault() {}});
    await flush();
    assert.deepEqual(sent.map(rule=>rule.video_type),formats.slice(0,1));
    host.unmount();
  }
}
