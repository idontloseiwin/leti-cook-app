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
import { reactHookHost, flush } from './react_hook_host.mjs';

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
  assert.equal(card.thumbnail_url, `https://img.youtube.com/vi/${raw.id}/mqdefault.jpg`);
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
  contract(() => page('long_first_anchor', (r) => { r.context.consistency = 'current'; }), /frozen/);
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
  // The panel's buttons: "Open in research workshop" and "Send selected to workshop" replace Compare / Send to Canvas.
  const panel = read('../application/frontend/src/explorer/ExplorerVideos.tsx');
  assert.match(panel, />Open in research workshop</);
  assert.match(panel, />Send selected to workshop</);
  assert.doesNotMatch(panel, /Send to Canvas|>Compare /);
  const section = read('../application/frontend/src/explorer/LibraryVideosSection.tsx');
  assert.match(section, /isWholeFrozenList\(new Set\(selected\.map\(\(item\) => item\.id\)\), browse\.frozenIds\)\) return openWhole\(\)/);
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
  const module = compiled('ExplorerVideos.tsx', {
    react: React, 'react/jsx-runtime': jsxRuntime, 'lucide-react': icons,
    '../sourceLifetimeReact': { useSourceExpiry: () => {} }, '../sourceLifetime.ts': lifetime,
    './format': format, './videoGrowth': compiled('videoGrowth.ts'), './localTime': { formatDateLocal: (value) => value ?? 'Unknown' },
    './ModuleState': { ErrorState: ({ title }) => React.createElement('p', null, title), LoadingState: ({ label }) => React.createElement('p', { role: 'status' }, label) },
    './resourceState': { resourcePayload: (state) => (state.kind === 'ready' || state.kind === 'updating' ? state.payload : state.previous) },
    './VirtualVideoGrid': { VirtualVideoGrid: ({ videos: rows, renderCard }) => React.createElement('div', null, rows.map(renderCard)) },
    './catalogueSelection': { addSelectedRecords: (current, records) => new Map([...current, ...records.map((row) => [row.video_id, row])]) },
    './VideoThumbnail': { VideoThumbnail: () => null, videoTitle: (video) => video.title || 'Title not reported' },
    './VideoInspector': { VideoInspector: () => null }, './libraryData/errors.ts': await import('../application/frontend/src/explorer/libraryData/errors.ts'),
  });
  assert.equal(module.LOAD_AHEAD_MARGIN, '2400px 0px', 'the next page loads about three screens ahead');
  // Without source clocks (plain): the render does not depend on today's date.
  const payload = plain(page('long_first_anchor', (r) => {
    r.coverage.reasons = { source_rows_unavailable: 1, rows_with_expired_values: 1 };
    Object.assign(r.rows[0].cells['video.views'], { state: 'expired', value: null });
  }));
  const render = (props) => renderToStaticMarkup(React.createElement(module.ExplorerVideosPanel, {
    state: { kind: 'ready', scopeKey: 's', payload }, channelId: payload.channel.channel_id, growthAvailable: true,
    selectionScopeKey: 's', onRetry() {}, hasMore: true, hasPrevious: true, ...props }));
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
  assert.match(idle, /title="Needs refresh"><strong>—<\/strong> total views/);
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
  // The browser never scrolls the catalogue by itself, and the grid's height and row tops apply at
  // once even under the reduced-motion rule (0.01 ms transitions for every element), so pages load only as the
  // user reaches the boundary: End at the list top loads one page, not 7 in 0.55 s.
  const videosCss = read('../application/frontend/src/explorer/videos.css');
  assert.ok(videosCss.includes('.explorer-app.is-updated section.explorer-v2-video-catalogue {\n  overflow-anchor: none;\n}'), 'no scroll anchoring on the catalogue');
  assert.ok(videosCss.includes('.explorer-app.is-updated .explorer-v2-video-grid.is-virtual,\n.explorer-app.is-updated .explorer-v2-video-virtual-row {\n  transition: none;\n}'),
    'the virtual grid is never animated');
  assert.ok(read('../application/frontend/src/explorer/redesign.css').includes('transition-duration: 0.01ms !important;'), 'the reduced-motion rule this guards against');
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
      react: host.React, '../sourceLifetimeReact': { useSourceExpiry() {} },
      '../sourceLifetime.ts': { freshSourceRows: (rows) => rows, sourceContainer: (value) => value },
      './catalogueBrowse': compiled('catalogueBrowse.ts'), './catalogueQuery': { catalogueUsesGrowthFilters: () => false },
      './format': { videoNoun: (count) => (count === 1 ? 'video' : 'videos') }, './libraryData/wire.ts': wire,
      './libraryData/savedLibrary.ts': { planLibraryOpen: async () => ({ status: 'run', note: '',
        translation: { status: 'translated', query: { where: null } }, request: { page: { size: 50 }, select: [] } }) },
      './libraryData/errors.ts': errors,
      './libraryData/videos.ts': { libraryFrozenFields: () => [], libraryGrowthFields: () => [], parseLibraryPage: (raw) => raw,
        mergeLibraryPages: (current, next) => ({ ...next, videos: [...current.videos, ...next.videos] }),
        parseFrozenIds: () => [], selectionRecords: () => new Map() },
    });
    const inputs = { channelId: 'UC' + 'a'.repeat(22), videoType: 'long', conditions: catalogue.createInitialCatalogueConditions(), timeZone: 'UTC',
      scopeKey: 'scope', selectionScopeKey: 'selection', enabled: true, gates: null, growthAllowed: false, onGrowthUnavailable() {},
      onAnnouncement() {}, translate: async () => { throw new Error('planLibraryOpen is replaced'); },
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

// 13. "Select all matching" refused for size, time or a turned-off field says so in plain words, keeps the
// selection and never shows "HTTP 422". The real hook, driven offline.
{
  const errors = await import('../application/frontend/src/explorer/libraryData/errors.ts');
  class ApiError extends Error { constructor(message, status, code, limit) { super(message); this.name = 'ExplorerApiError'; this.status = status; this.code = code; if (limit) this.limit = limit; } }
  const future = new Date(Date.now() + 600_000).toISOString();
  const first = { videos: [{ video_id: 'v0000000000' }], browse: { session_key: 's1', frozen_ref: 'ref0', expires_at: future,
    next_cursor: 'after-50', previous_cursor: null, start_index: 0, total_count: 5761 } };
  const http422 = 'The request was not accepted (HTTP 422).';
  for (const [failure, text] of [
    [new ApiError(http422, 422, 'query_budget_exceeded'), 'This selection is too large to browse at once. Narrow the filters.'],
    [new ApiError(http422, 422, 'query_budget_exceeded', 'time'), 'This selection took too long to prepare. Narrow the filters or try again.'],
    [new ApiError(http422, 422, 'field_disabled'), 'A field in these filters is turned off in Collection fields. Remove that condition or turn the field on.'],
  ]) {
    const host = reactHookHost();
    const calls = [];
    const browse = compiled('useCatalogueBrowse.ts', {
      react: host.React, '../sourceLifetimeReact': { useSourceExpiry() {} },
      '../sourceLifetime.ts': { freshSourceRows: (rows) => rows, sourceContainer: (value) => value },
      './catalogueBrowse': compiled('catalogueBrowse.ts'), './catalogueQuery': { catalogueUsesGrowthFilters: () => false },
      './format': { videoNoun: (count) => (count === 1 ? 'video' : 'videos') }, './libraryData/wire.ts': wire,
      './libraryData/savedLibrary.ts': { planLibraryOpen: async () => ({ status: 'run', note: '',
        translation: { status: 'translated', query: { where: null } }, request: { page: { size: 50 }, select: [] } }) },
      './libraryData/errors.ts': errors,
      './libraryData/videos.ts': { libraryFrozenFields: () => [], libraryGrowthFields: () => [], parseLibraryPage: (raw) => raw,
        mergeLibraryPages: (current, next) => ({ ...next, videos: [...current.videos, ...next.videos] }),
        parseFrozenIds: () => [], selectionRecords: () => new Map() },
    });
    host.mount(browse.useCatalogueBrowse, { channelId: 'UC' + 'a'.repeat(22), videoType: 'long',
      conditions: catalogue.createInitialCatalogueConditions(), timeZone: 'UTC', scopeKey: 'scope', selectionScopeKey: 'selection',
      enabled: true, gates: null, growthAllowed: false, onGrowthUnavailable() {}, onAnnouncement() {},
      translate: async () => { throw new Error('planLibraryOpen is replaced'); },
      request: (path, body, signal) => new Promise((resolve, reject) => calls.push({ path, body, signal, resolve, reject })) });
    await flush();
    calls[0].resolve(first); await flush();
    const selecting = host.current.collectMatching(new AbortController().signal, () => {});
    await flush();
    assert.equal(calls[1].path, wire.ROUTES.frozenIds);
    calls[1].reject(failure);
    await assert.rejects(selecting, (error) => error.message === text && !/HTTP 422/.test(error.message), `${failure.code}:${failure.limit}`);
    assert.equal(host.current.frozenIds, undefined, 'the existing selection is not replaced');
    host.unmount();
  }
  console.log('Select all matching refused for size, time or a turned-off field shows plain words, not HTTP 422.');
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
    './ExplorerVideosWorkspace': { ExplorerVideosWorkspace: ({ rawState }) => String(rawState.payload.videos.length) + ' cards' },
  }));
  const grid = renderToStaticMarkup(React.createElement(LibraryVideosSection, { context: shell, videos: {
    visibleRawState: { kind: 'ready', payload: { videos: cards } }, browse: { translated: {} },
    conditions: {}, applied: {}, growthSortAvailable: true,
  } }));
  assert.match(grid, /75 cards/);
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
