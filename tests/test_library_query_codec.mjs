// Library address codec with the media type, the shared translation table and
// the per-type presets. Offline: no browser, network or React.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import * as query from '../application/frontend/src/explorer/catalogueQuery.ts';
import { INITIAL_EXPLORER_FILTERS, STATIC_CATALOGUE_FILTER_KEYS } from '../application/frontend/src/explorer/filters.ts';
import { parseTranslation } from '../application/frontend/src/explorer/libraryData/conditions.ts';
import { PRESET_GROUPS, presetsFor } from '../application/frontend/src/explorer/libraryData/presets.ts';

const read = (path) => readFileSync(new URL(path, import.meta.url), 'utf8');
const channel = 'UC' + 'a'.repeat(22);
const base = `https://example.test/app-explorer-v3/channels/${channel}`;
const initial = query.createInitialCatalogueConditions();
const filtered = query.normalizeCatalogueConditions({
  version: 1,
  filters: { ...INITIAL_EXPLORER_FILTERS, q: 'Straße  guide', min_views: '10', sort_by: 'views', sort_order: 'asc' },
  facets: { version: 1, duration_buckets: ['under_5m'], published_years: [2024], cohort: null },
});

// The writer before the `type` parameter existed, kept here as the reference for Long addresses.
function writeBeforeType(input, state) {
  const output = new URL(input.href);
  output.searchParams.set('section', state.section);
  output.searchParams.set('overviewPeriod', state.overviewPeriod);
  output.searchParams.set('videos', query.serializeCatalogueConditions(state.conditions));
  return output;
}

const inputs = [
  base,
  `${base}?section=videos`,
  `${base}?from=home&section=overview&overviewPeriod=max`,
  `${base}?channel=x&a=%20b+c&videos=%7B%7D&section=videos`,
  `${base}?from=saved&videos=${encodeURIComponent(query.serializeCatalogueConditions(filtered))}&overviewPeriod=28d`,
  `${base}#fragment`,
];
const states = [];
for (const section of ['overview', 'videos']) {
  for (const overviewPeriod of ['7d', '28d', '3m', '1y', 'max']) {
    for (const conditions of [initial, filtered]) states.push({ section, overviewPeriod, conditions });
  }
}

// 1. Long addresses are byte-identical to the writer before `type`, with the type left out or given.
let compared = 0;
for (const href of inputs) {
  for (const state of states) {
    const expected = writeBeforeType(new URL(href), state).href;
    assert.equal(query.writeCatalogueUrl(new URL(href), state).href, expected, href);
    assert.equal(query.writeCatalogueUrl(new URL(href), { ...state, type: 'long' }).href, expected, href);
    assert.equal(new URL(expected).searchParams.has('type'), false);
    compared += 2;
  }
}

// 2. Shorts and Streams write `type`; returning to Long removes it and restores the Long address exactly.
for (const href of inputs) {
  for (const type of ['short', 'stream']) {
    const state = { section: 'videos', overviewPeriod: 'max', conditions: filtered };
    const typed = query.writeCatalogueUrl(new URL(href), { ...state, type });
    assert.deepEqual(typed.searchParams.getAll('type'), [type]);
    const back = query.writeCatalogueUrl(typed, { ...state, type: 'long' });
    assert.equal(back.href, writeBeforeType(new URL(href), state).href);
    assert.equal(query.writeCatalogueUrl(typed, { ...state, type }).href, typed.href, 'rewriting is stable');
  }
}

// 3. Old links read unchanged (type = long); typed links read their tab.
const oldLink = new URL(`${base}?section=videos&overviewPeriod=28d&videos=${encodeURIComponent(query.serializeCatalogueConditions(filtered))}`);
assert.deepEqual(query.readCatalogueUrl(oldLink), { section: 'videos', overviewPeriod: '28d', type: 'long', conditions: filtered });
assert.deepEqual(query.readCatalogueUrl(new URL(base)), { section: 'overview', overviewPeriod: 'max', type: 'long', conditions: initial });
assert.equal(query.readCatalogueUrl('section=videos&type=short').type, 'short');
assert.equal(query.readCatalogueUrl('?type=stream').type, 'stream');
assert.equal(query.readCatalogueUrl(new URLSearchParams('type=long')).type, 'long');
for (const bad of ['type=Short', 'type=shorts', 'type=', 'type=long&type=short', 'type=%20short']) {
  assert.throws(() => query.readCatalogueUrl(bad), (error) => error instanceof query.CatalogueQueryError && error.field === 'type', bad);
}
assert.throws(() => query.writeCatalogueUrl(new URL(base), { section: 'videos', overviewPeriod: 'max', type: 'shorts', conditions: initial }),
  (error) => error instanceof query.CatalogueQueryError && error.field === 'type');
// A broken condition keeps a valid tab readable on its own (the page then stays on that tab).
const broken = new URL(`${base}?type=short&videos=%7Bbad`);
assert.throws(() => query.readCatalogueUrl(broken), query.CatalogueQueryError);
assert.equal(query.readCatalogueVideoType(broken), 'short');
assert.equal(query.readCatalogueVideoType(new URL(`${base}?videos=%7Bbad`)), 'long');

// 4. Round trip of every state and type: write then read gives the same state.
let roundTrips = 0;
for (const href of inputs) {
  for (const state of states) {
    for (const type of ['long', 'short', 'stream']) {
      const written = query.writeCatalogueUrl(new URL(href), { ...state, type });
      assert.deepEqual(query.readCatalogueUrl(written), { ...state, type });
      roundTrips += 1;
    }
  }
}
// The shell changes tabs through navigation only: no other code writes `type`.
const shell = read('../application/frontend/src/explorer/ExplorerWorkspace.tsx');
assert.match(shell, /navigation\.navigate\(\{ \.\.\.navigation\.state, type \}\)/);
assert.match(shell, /mediaTabs\.find\(\(tab\) => tab\.type === navigation\.state\.type\)/);
for (const tab of ["{ id: 'videos', type: 'long'", "{ id: 'shorts', type: 'short'", "{ id: 'streams', type: 'stream'"]) assert.ok(shell.includes(tab), tab);
console.log(`Library address codec: ${compared} Long writes byte-identical, ${roundTrips} round trips, typed tabs and old links passed.`);

// 5. The shared translation table: canonical browser inputs, every v1 key, well-formed expectations.
const fixture = JSON.parse(read('./fixtures/library_translation_cases.json'));
assert.equal(fixture.version, 1);
assert.match(fixture.channel_id, /^UC[A-Za-z0-9_-]{22}$/);
assert.deepEqual(fixture.query_shell.select, ['video.id', 'video.title', 'video.thumbnail_url', 'video.published_at',
  'video.duration_seconds', 'video.views', 'video.likes', 'video.comments', 'video.category_id']);
const names = new Set();
const covered = new Set();
for (const item of fixture.cases) {
  assert.deepEqual(Object.keys(item).filter((key) => !['name', 'covers', 'video_type', 'time_zone', 'conditions', 'expected', 'changes_with'].includes(key)), [], item.name);
  assert.ok(!names.has(item.name), `duplicate case ${item.name}`); names.add(item.name);
  assert.ok(['long', 'short', 'stream'].includes(item.video_type), item.name);
  assert.ok(item.time_zone === 'UTC' || /^[A-Za-z]+(?:\/[A-Za-z0-9_+-]+){1,2}$/.test(item.time_zone), item.name);
  // Inputs are what the browser sends: the codec leaves them unchanged.
  assert.deepEqual(query.normalizeCatalogueConditions(item.conditions), item.conditions, item.name);
  item.covers.forEach((key) => covered.add(key));
  if (item.changes_with !== undefined) assert.ok(typeof item.changes_with === 'string' && item.changes_with.length > 8, item.name);
  if (item.expected === null) { assert.ok(item.changes_with, `${item.name}: a row without expectation names what it waits for`); continue; }
  if (item.expected.status === 'translated') {
    assert.deepEqual(Object.keys(item.expected).sort(), ['order_by', 'status', 'video_types', 'where'], item.name);
    assert.deepEqual(item.expected.video_types, [item.video_type], item.name);
    assert.ok(item.expected.order_by.length >= 1, item.name);
    // The browser client accepts the server reply this row describes (the translator carries the viewer zone).
    const reply = { contract_version: 1, translation: { status: 'translated', time_zone: item.time_zone, missing: [], query: {
      query_version: 1, source: fixture.query_shell.source, scope: { kind: 'channel', id: fixture.channel_id },
      video_types: item.expected.video_types, select: fixture.query_shell.select,
      ...(item.expected.where === null ? {} : { where: item.expected.where }),
      order_by: item.expected.order_by, time: { mode: 'current', timezone: item.time_zone }, page: { size: 50, after: null } } } };
    const parsed = parseTranslation(reply, { channel_id: fixture.channel_id, conditions: item.conditions, time_zone: item.time_zone, video_type: item.video_type });
    assert.equal(parsed.status, 'translated', item.name);
  } else {
    assert.equal(item.expected.status, 'needs_capability', item.name);
    assert.deepEqual(Object.keys(item.expected).sort(), ['missing', 'status'], item.name);
    assert.ok(item.expected.missing.length >= 1, item.name);
    const reply = { contract_version: 1, translation: { status: 'needs_capability', query: null, time_zone: item.time_zone, missing: item.expected.missing } };
    assert.equal(parseTranslation(reply, { channel_id: fixture.channel_id, conditions: item.conditions, time_zone: item.time_zone, video_type: item.video_type }).status, 'needs_capability');
  }
}
const v1Keys = new Set([
  ...Object.keys(INITIAL_EXPLORER_FILTERS).filter((key) => key !== 'sort_by' && key !== 'sort_order').map((key) => `filters.${key}`),
  ...STATIC_CATALOGUE_FILTER_KEYS.map((key) => `filters.${key}`),
  'filters.sort_order',
  ...['published_at', 'views', 'like_count', 'comment_count', 'duration', 'title', 'views_gained'].map((sort) => `filters.sort_by=${sort}`),
  ...query.CATALOGUE_NUMERIC_GROUPS.filter((group) => group.facet).map((group) => `facets.${group.facet}`),
  'facets.published_years', 'facets.published_older_than', 'facets.cohort',
  ...['min_views_gained', 'max_views_gained', 'min_tracked_days', 'full_history'].map((key) => `facets.growth.${key}`),
  'video_type',
]);
assert.deepEqual([...v1Keys].filter((key) => !covered.has(key)), [], 'every v1 key has a row');
assert.deepEqual([...covered].filter((key) => !v1Keys.has(key)), [], 'rows cover only known keys');
// Every sort key the codec accepts is in the table (no sort can be added silently).
const sorts = /const sortFields = new Set\(\[([^\]]*)\]\)/.exec(read('../application/frontend/src/explorer/catalogueQuery.ts'))[1].match(/'([a-z_]+)'/g).map((item) => item.slice(1, -1));
assert.deepEqual(sorts.filter((sort) => !covered.has(`filters.sort_by=${sort}`)), []);
console.log(`Translation table: ${fixture.cases.length} rows, ${v1Keys.size} keys covered, inputs canonical, replies accepted by the client.`);

// 6. Presets by type: Long = shared JSON; Shorts and Streams share all but duration (by_video_type).
const registry = JSON.parse(read('../application/shared/catalogue-filter-presets.json'));
for (const group of PRESET_GROUPS) {
  assert.deepEqual(presetsFor('long', group), registry[group], group);
  if (group !== 'duration') for (const type of ['short', 'stream']) assert.deepEqual(presetsFor(type, group), registry[group]);
}
assert.deepEqual(presetsFor('short', 'duration'), registry.by_video_type.short.duration);
assert.deepEqual(presetsFor('stream', 'duration'), registry.by_video_type.stream.duration);
const preset = (type, group, key) => presetsFor(type, group)?.find((item) => item.key === key);
assert.deepEqual(preset('short', 'duration', '1m_3m'), { key: '1m_3m', label: '1–3 min', min: 60, max: 181 });
assert.deepEqual(preset('long', 'views', 'under_1k'), { key: 'under_1k', label: 'Under 1K', min: null, max: 1000 });
assert.equal(preset('short', 'duration', 'under_5m'), undefined);
assert.deepEqual(query.CATALOGUE_NUMERIC_GROUPS.filter((group) => group.presets).map((group) => [group.group, group.presets]),
  PRESET_GROUPS.map((group) => [group, presetsFor('long', group)]), 'the Long panel uses the same sets');
assert.throws(() => presetsFor('long', 'rates'));
console.log('Presets by type: Long matches the shared registry; Shorts/Streams have their own duration sets.');
