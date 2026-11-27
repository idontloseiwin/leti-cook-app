// Saved in Library: Saved v1 views and Library v2 views open through the server
// translation; a saved Library v2 query reopens as Library filters (closed grammar, change to calendar fields);
// Saved v1 with facets.time_zone; Library vs workshop routing; collection members of every
// type with their reasons, removal and the base Inspector. Offline: no network.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from '../application/frontend/node_modules/typescript/lib/typescript.js';
import * as React from '../application/frontend/node_modules/react/index.js';
import * as jsxRuntime from '../application/frontend/node_modules/react/jsx-runtime.js';
import { renderToStaticMarkup } from '../application/frontend/node_modules/react-dom/server.node.js';
import { reactHookHost, flush } from './react_hook_host.mjs';
import * as lifetime from '../application/frontend/src/sourceLifetime.ts';
import * as routes from '../application/frontend/src/explorerRoutes.ts';
import * as catalogue from '../application/frontend/src/explorer/catalogueQuery.ts';
import * as contracts from '../application/frontend/src/explorer/contracts.ts';
import * as saved from '../application/frontend/src/explorer/libraryData/savedLibrary.ts';
import * as conditions from '../application/frontend/src/explorer/libraryData/conditions.ts';
import * as wire from '../application/frontend/src/explorer/libraryData/wire.ts';

const read = (path) => readFileSync(new URL(path, import.meta.url), 'utf8');
function compiled(file, imports = {}) {
  const { outputText } = ts.transpileModule(read('../application/frontend/src/explorer/' + file),
    { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX } });
  const exports = {}; new Function('require', 'exports', outputText)((name) => { assert.ok(name in imports, `${file}: ${name}`); return imports[name]; }, exports);
  return exports;
}
const work = compiled('savedWork.ts');
const zones = compiled('timeZones.ts');
const research = compiled('researchQuery.ts', { './api': {}, '../sourceLifetime': lifetime });
const definition = compiled('savedQueryDefinition.ts', { './researchQuery': research, './savedWork': work, './timeZones': zones });
const operations = compiled('researchOperations.ts', { './api': {}, '../sourceLifetime': lifetime });
const graph = compiled('researchGraph.ts', { './api': {}, './savedWork': work, './savedQueryDefinition': definition, './researchQuery': research, './researchOperations': operations, '../sourceLifetime': lifetime });
const views = compiled('savedViews.ts', { './researchGraph': graph, './api': {}, './savedQueryDefinition': definition, '../explorerRoutes': routes,
  './catalogueQuery': catalogue, './savedWork': work, './timeZones': zones, './libraryData/savedLibrary.ts': saved });

const channel = 'UC' + 'a'.repeat(22), origin = 'http://localhost:8002', at = '2026-10-01T00:00:00Z';
const id = '11111111-1111-4111-8111-111111111111';
const row = (conditionsValue, extra = {}) => ({ id, kind: 'view', name: 'My view', channel_id: channel, conditions: conditionsValue, revision: 1,
  created_at: at, updated_at: at, channel_available: true, ...extra });
const table = JSON.parse(read('./fixtures/library_translation_cases.json'));
const forms = new Map(JSON.parse(read('./fixtures/library_recognized_forms.json')).cases.map((form) => [form.name, form.recognized]));
const libraryDefinition = (query, presentation = {}) => definition.parseSavedDefinition({ version: 2, query,
  presentation: { version: 1, surface: 'library', view: 'grid', ...presentation } });
const queryOf = (where, order = [{ field: 'video.published_at', direction: 'desc' }], type = 'long', extra = {}) => ({
  query_version: 1, source: { id: 'public.videos.current.v1' }, scope: { kind: 'channel', id: '$channel' }, video_types: [type],
  select: table.query_shell.select, ...(where === undefined ? {} : { where }), order_by: order, time: { mode: 'current', timezone: 'UTC' }, ...extra });
const recognize = (where, presentation = {}, ...rest) => saved.recognizeLibraryView(libraryDefinition(queryOf(where, ...rest), presentation));
const v1 = (filters = {}, facets = {}) => catalogue.normalizeCatalogueConditions({ version: 1,
  filters: { ...catalogue.createInitialCatalogueConditions().filters, ...filters }, facets: { version: 1, ...facets } });

// 1. The shared table backwards: recognize(translation) == conditions, or the listed equivalent form.
{
  let identical = 0, equivalent = 0, workshop = 0;
  for (const item of table.cases) {
    if (item.expected?.status !== 'translated') continue;
    // The saved shape is what the Saved list gives: parseSavedDefinition adds its defaults.
    const result = saved.recognizeLibraryView(libraryDefinition({ ...queryOf(item.expected.where, item.expected.order_by, item.video_type),
      ...(item.expected.where === null ? { where: null } : {}) }, { time_zone: item.time_zone }));
    const expected = forms.has(item.name) ? forms.get(item.name) : item.conditions;
    if (expected === null) { assert.equal(result.status, 'unrecognized', item.name); workshop += 1; continue; }
    assert.equal(result.status, 'recognized', item.name);
    assert.deepEqual(result.conditions, expected, item.name);
    assert.equal(result.videoType, item.video_type, item.name);
    assert.equal(result.savedZone, item.time_zone, item.name);
    // What the channel address will carry is canonical browser state.
    assert.deepEqual(catalogue.normalizeCatalogueConditions(result.conditions), result.conditions, item.name);
    if (forms.has(item.name)) equivalent += 1; else identical += 1;
  }
  for (const name of forms.keys()) assert.equal(table.cases.find((item) => item.name === name)?.expected?.status, 'translated', name);
  assert.equal(identical + equivalent + workshop, table.cases.filter((item) => item.expected?.status === 'translated').length);
  assert.ok(identical >= 60 && equivalent === forms.size - 1 && workshop === 1);
  console.log(`Recognizer on the shared table: ${identical} identical, ${equivalent} equivalent forms, ${workshop} opens in the workshop.`);
}

// 2. The grammar: preferences where one query has several readings, and refusals.
{
  const ok = (result) => { assert.equal(result.status, 'recognized', JSON.stringify(result)); return result.conditions; };
  // A custom bound equal to an "X+" preset opens as the preset; other bounds stay custom.
  assert.deepEqual(ok(recognize({ field: 'video.views', op: 'gte', value: '10000000' })).facets.view_buckets, ['10m_plus']);
  const custom = ok(recognize({ all: [{ field: 'video.views', op: 'gte', value: '5' }, { field: 'video.views', op: 'lte', value: '50' }] }));
  assert.deepEqual([custom.filters.min_views, custom.filters.max_views, custom.facets.view_buckets], ['5', '50', []]);
  // A whole Jan 1 → Jan 1 range is a year; other two-day ranges are custom dates (one node vs two leaves).
  const year = ok(recognize({ all: [{ field: 'video.published_at', op: 'gte', value: '2023-12-31T15:00:00Z' }, { field: 'video.published_at', op: 'lt', value: '2024-12-31T15:00:00Z' }] }, { time_zone: 'Asia/Tokyo' }));
  assert.deepEqual([year.facets.published_years, year.filters.published_from], [[2024], '']);
  const dates = ok(recognize({ all: [{ field: 'video.title', op: 'contains_casefold_v1', value: 'soup' }, { field: 'video.published_at', op: 'gte', value: '2024-01-01T00:00:00Z' },
    { field: 'video.published_at', op: 'lt', value: '2025-01-01T00:00:00Z' }] }));
  assert.deepEqual([dates.filters.published_from, dates.filters.published_before, dates.facets.published_years], ['2024-01-01', '2025-01-01', []]);
  // "Older than" with separate years below it: the translator keeps them as their own ranges.
  const older = ok(recognize({ any: [{ field: 'video.published_at', op: 'lt', value: '2023-01-01T00:00:00Z' },
    { all: [{ field: 'video.published_at', op: 'gte', value: '2010-01-01T00:00:00Z' }, { field: 'video.published_at', op: 'lt', value: '2012-01-01T00:00:00Z' }] }] }));
  assert.deepEqual([older.facets.published_older_than, older.facets.published_years], [2023, [2011, 2010]]);
  // The same instant is a midnight in one zone only: the saved zone decides.
  assert.equal(ok(recognize({ field: 'video.published_at', op: 'gte', value: '2026-03-28T23:00:00Z' }, { time_zone: 'Europe/Berlin' })).filters.published_from, '2026-03-29');
  assert.equal(recognize({ field: 'video.published_at', op: 'gte', value: '2026-03-28T23:00:00Z' }, { time_zone: 'UTC' }).status, 'unrecognized');
  // Without presentation.time_zone the query's zone is the saved zone.
  assert.equal(saved.savedLibraryZone(libraryDefinition(queryOf(undefined, undefined, 'long', { time: { mode: 'current', timezone: 'Asia/Tokyo' } }))), 'Asia/Tokyo');
  // Cohort, with and without a start; the address keeps its six-digit fraction.
  assert.deepEqual(ok(recognize({ any: [{ field: 'video.published_at', op: 'is_unknown' }, { field: 'video.published_at', op: 'lte', value: '2026-09-30T22:00:00Z' }] })).facets.cohort,
    { kind: 'overview', published_from: null, published_through: '2026-09-30T22:00:00.000000Z' });
  // Types: Shorts and Streams open on their tab; their duration presets wait for (no set yet).
  const shorts = recognize({ all: [{ field: 'video.views', op: 'gte', value: '1000' }, { field: 'video.views', op: 'lt', value: '10000' }] }, {}, undefined, 'short');
  assert.deepEqual([shorts.videoType, shorts.conditions.facets.view_buckets], ['short', ['1k_10k']]);
  assert.equal(recognize({ field: 'video.duration_seconds', op: 'lt', value: '300' }, {}, undefined, 'stream').status, 'unrecognized');
  assert.equal(ok(recognize({ field: 'video.duration_seconds', op: 'lte', value: '300' }, {}, undefined, 'stream')).filters.max_duration_seconds, '300');
  // No conditions: the initial filters with the saved sort.
  assert.deepEqual(ok(recognize(undefined, {}, [{ field: 'video.views', direction: 'asc' }])), v1({ sort_by: 'views', sort_order: 'asc' }));
  // 28-day growth (growth.legacy_conditions) and its sort; calculated values in table order.
  const gained = 'observation.views_gained_28d', interval = 'observation.growth_28d_interval_seconds', status = 'observation.growth_28d_status';
  const growthNode = (...leaves) => ({ all: [{ field: gained, op: 'is_known' }, ...leaves] });
  assert.deepEqual(ok(recognize(growthNode({ field: gained, op: 'gte', value: '-5' }, { field: gained, op: 'lte', value: '9' },
    { field: interval, op: 'gte', value: '604800' }, { field: status, op: 'eq', value: 'ready' }))).facets.growth,
  { min_views_gained: '-5', max_views_gained: '9', min_tracked_days: '7', full_history: true });
  assert.deepEqual(ok(recognize(undefined, {}, [{ field: gained, direction: 'asc' }])), v1({ sort_by: 'views_gained', sort_order: 'asc' }));
  const mixed = ok(recognize({ all: [{ field: 'video.title', op: 'contains_casefold_v1', value: 'soup' }, { field: 'video.like_rate', op: 'gte', value: '2.5' },
    { field: 'video.comment_rate', op: 'lte', value: '1' }, { any: [{ field: 'video.title_length', op: 'lt', value: '40' }, { field: 'video.title_length', op: 'gte', value: '80' }] },
    growthNode({ field: status, op: 'eq', value: 'ready' })] }));
  assert.deepEqual([mixed.filters.q, mixed.filters.min_like_rate, mixed.filters.max_comment_rate, mixed.facets.title_length_buckets, mixed.facets.growth],
    ['soup', '2.5', '1', ['under_40', '80_plus'], { full_history: true }]);
  [growthNode(), growthNode({ field: interval, op: 'gte', value: '18446744073709551615' }), // the cap of an enormous bound is not a day count
    growthNode({ field: gained, op: 'lte', value: '9' }, { field: gained, op: 'gte', value: '1' }), // not the translator's order
    growthNode({ field: status, op: 'eq', value: 'partial' }), { all: [{ field: gained, op: 'gte', value: '1' }, { field: gained, op: 'lte', value: '9' }] },
    { all: [growthNode({ field: status, op: 'eq', value: 'ready' }), { field: 'video.title', op: 'contains_casefold_v1', value: 'x' }] },
  ].forEach((where, index) => assert.equal(recognize(where).status, 'unrecognized', `growth refusal ${index}`));
  // Refusals: Library cannot show it, the workshop opens it.
  const refused = [
    recognize({ all: [{ field: 'video.views', op: 'gte', value: '5' }, { field: 'video.title', op: 'contains_casefold_v1', value: 'x' }] }), // not in table order
    recognize({ field: 'video.tags', op: 'contains', value: 'x' }),
    recognize({ not: { field: 'video.views', op: 'gte', value: '5' } }),
    recognize({ field: 'video.views', op: 'gt', value: '5' }),
    recognize({ field: 'video.published_at', op: 'gte', value: '2026-03-29T00:00:00Z' }, { time_zone: 'Europe/Berlin' }), // not a local midnight
    recognize({ any: [{ field: 'video.views', op: 'lt', value: '1000' }, { field: 'video.views', op: 'gte', value: '1000' }] }), // touching: never the translator's output
    recognize(undefined, {}, [{ field: 'video.views', direction: 'desc' }, { field: 'video.title', direction: 'asc' }]),
    recognize(undefined, {}, [{ field: 'video.id', direction: 'asc' }]),
    recognize(undefined, {}, undefined, 'long', { coverage: { mode: 'require_complete' } }),
    recognize(undefined, {}, undefined, 'long', { coverage: { mode: 'available', required_fields: ['video.views'] } }),
    recognize({ field: 'video.title', op: 'contains_casefold_v1', value: 'x'.repeat(101) }),
  ];
  refused.forEach((result, index) => assert.equal(result.status, 'unrecognized', String(index)));
  assert.deepEqual(saved.recognizeLibraryView(definition.parseSavedDefinition({ version: 2, query: queryOf(undefined) })),
    { status: 'unrecognized', reason: 'This view was not saved in Library.' });
  console.log('Recognizer grammar: presets before custom bounds, years before dates, saved zone, types, refusals.');
}

// 3. Local midnights exactly as midnight (Python zoneinfo, fold 0), from the real translator.
{
  const reference = [['America/Havana', 2026, 3, 8, '2026-03-08T05:00:00Z'], ['America/Havana', 2026, 11, 1, '2026-11-01T04:00:00Z'],
    ['Asia/Beirut', 2026, 3, 29, '2026-03-28T22:00:00Z'], ['Asia/Beirut', 2026, 10, 25, '2026-10-24T22:00:00Z'],
    ['America/Asuncion', 2024, 10, 6, '2024-10-06T04:00:00Z'], ['America/Asuncion', 2024, 3, 24, '2024-03-24T04:00:00Z'],
    ['Europe/Moscow', 1850, 6, 1, '1850-05-31T21:29:43Z'], ['Asia/Kolkata', 1900, 1, 1, '1899-12-31T18:38:50Z'],
    ['Australia/Lord_Howe', 2026, 4, 5, '2026-04-04T13:00:00Z'], ['America/Santiago', 2026, 9, 6, '2026-09-06T04:00:00Z'],
    ['Pacific/Kiritimati', 1, 1, 1, '0001-01-01T10:29:20Z'], ['Pacific/Pago_Pago', 9999, 12, 31, '9999-12-31T11:00:00Z'],
    ['Asia/Gaza', 2026, 3, 28, '2026-03-27T22:00:00Z'], ['America/Godthab', 2026, 3, 29, '2026-03-29T01:00:00Z']];
  for (const [zone, year, month, day, expected] of reference) {
    assert.equal(new Date(saved.libraryMidnight(year, month, day, zone)).toISOString().replace('.000Z', 'Z'), expected, `${zone} ${year}-${month}-${day}`);
  }
  assert.equal(saved.libraryMidnight(10000, 1, 1, 'UTC'), Infinity);
  console.log(`Local midnights: ${reference.length} gap, fold and local-mean-time cases equal the Python translator.`);
}

// 4. Calendar fields: one function behind one constant, which is on. The field shape opens; views with UTC-instant dates keep opening.
{
  assert.equal(saved.PUBLICATION_DATE_FIELDS, true);
  const source = read('../application/frontend/src/explorer/libraryData/savedLibrary.ts');
  assert.equal(source.match(/publicationDateCandidates\(/g).length, 2, 'declared once, called once');
  assert.match(source, /export const PUBLICATION_DATE_FIELDS = true;/);
  const fields = { all: [{ field: 'video.published_date', op: 'gte', value: '2026-03-29' }, { field: 'video.published_date', op: 'lt', value: '2026-04-01' }] };
  const years = { any: [{ field: 'video.published_year', op: 'in', value: ['2022', '2024'] }, { field: 'video.published_year', op: 'lt', value: '2001' }] };
  const target = (where, useCalendarFields = undefined) => saved.recognizeLibraryView(libraryDefinition(queryOf(where, undefined, 'long', { time: { mode: 'current', timezone: 'Europe/Berlin' } })), useCalendarFields);
  assert.deepEqual([target(fields).conditions.filters.published_from, target(fields).conditions.filters.published_before], ['2026-03-29', '2026-04-01']);
  assert.deepEqual([target(years).conditions.facets.published_years, target(years).conditions.facets.published_older_than], [[2024, 2022], 2001]);
  assert.equal(target(years).savedZone, 'Europe/Berlin');
  assert.equal(target(fields, false).status, 'unrecognized', 'the switch alone decides the field shape');
  assert.equal(target({ field: 'video.published_year', op: 'in', value: ['2024', '2022'] }).status, 'unrecognized', 'ascending as the translator writes it');
  // Views whose dates are UTC instants of local midnights of the saved zone still open, by default.
  assert.equal(target({ field: 'video.published_at', op: 'lt', value: '2022-12-31T23:00:00Z' }).conditions.facets.published_older_than, 2023);
  const before = JSON.parse(read('./fixtures/library_saved_utc_instants.json'));
  assert.equal(before.cases.length, 11);
  for (const item of before.cases) {
    const result = saved.recognizeLibraryView(libraryDefinition(queryOf(item.where, item.order_by, item.video_type), { time_zone: item.time_zone }));
    assert.equal(result.status, 'recognized', item.name);
    assert.deepEqual(result.conditions, item.conditions, item.name);
    assert.equal(result.savedZone, item.time_zone, item.name);
  }
  console.log(`calendar fields are on; the field shape opens and ${before.cases.length} views with UTC-instant dates keep opening.`);
}

// 5. A Saved v1 view with facets.time_zone does not break the Saved list or a backup.
{
  const tokyo = { ...v1({}, { published_years: [2024] }), facets: { ...v1({}, { published_years: [2024] }).facets, time_zone: 'Asia/Tokyo' } };
  assert.throws(() => catalogue.normalizeCatalogueConditions(tokyo), /time_zone/, 'the address codec still has no zone');
  const page = views.parseSavedViewsPage({ contract_version: 1, views: [row(v1()), { ...row(tokyo), id: '22222222-2222-4222-8222-222222222222', name: 'Tokyo years' }], next_cursor: null });
  assert.deepEqual(page.views.map((view) => view.name), ['My view', 'Tokyo years']);
  assert.equal(page.views[1].conditions.facets.time_zone, 'Asia/Tokyo');
  const serialized = views.serializeSavedConditions(page.views[1].conditions);
  assert.equal(JSON.parse(serialized).facets.time_zone, 'Asia/Tokyo');
  assert.deepEqual(views.normalizeSavedConditions(JSON.parse(serialized)), page.views[1].conditions);
  assert.equal(views.serializeSavedConditions(page.views[0].conditions), catalogue.serializeCatalogueConditions(v1()), 'views without a zone retain their UTC date boundaries');
  // The Library address carries the conditions without the zone; the target remembers it for the note.
  const target = views.savedLibraryTarget(page.views[1], origin);
  assert.equal(target.kind, 'channel'); assert.equal(target.savedZone, 'Asia/Tokyo');
  assert.deepEqual(catalogue.readCatalogueUrl(new URL(target.url)).conditions, v1({}, { published_years: [2024] }));
  assert.equal(saved.libraryZoneNote(target.conditions, target.savedZone, 'Europe/Moscow'), 'Saved with dates in Asia/Tokyo; shown in Europe/Moscow.');
  for (const zone of ['UTC', 'local', '+09:00', 5, '', 'Asia/' + 'x'.repeat(70), 'Asia/../x']) {
    const bad = { ...tokyo, facets: { ...tokyo.facets, time_zone: zone } };
    // The item's grammar stays strict; such an item is listed as unreadable instead of refusing the page.
    assert.throws(() => views.parseSavedView(row(bad)), String(zone));
    assert.deepEqual(views.parseSavedViewsPage({ contract_version: 1, views: [row(bad)], next_cursor: null }).views,
      [{ kind: 'unreadable', id, name: 'My view' }], String(zone));
  }
  // The backup codec keeps the zone (a restore re-sends the parsed file).
  const backup = compiled('userWorkBackup.ts', { './researchFieldPreferences': compiled('researchFieldPreferences.ts'), './savedViews': views, './savedWork.ts': work,
    './catalogueQuery.ts': catalogue, './cataloguePreferences.ts': { catalogueDensityPreference: { isValid: (x) => ['large', 'compact'].includes(x) } },
    '../state/browserPreferences.ts': {}, './bookmarks.ts': {}, './folderCollapse.ts': {} });
  const file = { format: 'leti-cook-user-work', version: 5, created_at: at, browser_preferences: { catalogue_density: 'large', bookmarked_channel_refs: [], collapsed_folder_ids: [] },
    user_work: { folders: [], labels: [], collections: [], reviews: [], queries: [], field_preferences: { version: 1, defaults_profile: 'public-current-v1',
      lifecycle_profile: 'public-696h-v1', workspace: {}, sources: {}, description_setup: 'pending' }, channels: [{ id: '55555555-5555-4555-8555-555555555555', user_input: null, folder_id: null,
      parent_ref: null, is_hidden: false, added_at: null, label_keys: [], saved_views: [{ id: '66666666-6666-4666-8666-666666666666', name: 'Tokyo years', conditions: tokyo,
        revision: 1, created_at: at, updated_at: at }] }] } };
  assert.equal(JSON.parse(JSON.stringify(backup.parseUserWorkBackup(file))).user_work.channels[0].saved_views[0].conditions.facets.time_zone, 'Asia/Tokyo');
  console.log('Saved v1 with facets.time_zone reads in the list and the backup; the zone is kept, the address carries none.');
}

// 6. Where a Saved item opens: v1 and recognised Library v2 in the channel's Library, the rest in the workshop.
{
  const workshopUrl = `${origin}/app-explorer-v3/research?saved=${id}`;
  const legacy = views.savedLibraryTarget(views.parseSavedView(row(v1({ q: 'soup' }))), origin);
  assert.deepEqual([legacy.kind, legacy.videoType, new URL(legacy.url).pathname], ['channel', 'long', `/app-explorer-v3/channels/${channel}`]);
  assert.equal(new URL(legacy.url).searchParams.get('section'), 'videos');
  assert.equal(new URL(legacy.url).searchParams.has('type'), false, 'a Long address stays byte-identical');
  // A v1 view stores no format; duration presets of one format name it (keys never repeat across formats).
  for (const [keys, type] of [[['under_15s', '15s_30s'], 'short'], [['4h_plus'], 'stream'], [['under_5m'], 'long'], [['under_5m', 'under_15s'], 'long'], [[], 'long']]) {
    const typed = views.savedLibraryTarget(views.parseSavedView(row(v1({}, { duration_buckets: keys }))), origin);
    assert.equal(typed.videoType, type, keys.join(','));
    assert.equal(new URL(typed.url).searchParams.get('type'), type === 'long' ? null : type, keys.join(','));
    assert.deepEqual(catalogue.readCatalogueUrl(new URL(typed.url)).conditions.facets.duration_buckets, keys, 'the keys travel unchanged');
  }
  assert.equal(saved.savedLibraryVideoType(v1({}, { duration_buckets: ['1m_3m'] })), 'short');
  const shortsQuery = queryOf({ field: 'video.views', op: 'gte', value: '10000000' }, [{ field: 'video.views', direction: 'desc' }], 'short');
  const library = { version: 2, query: shortsQuery, presentation: { version: 1, surface: 'library', view: 'grid', time_zone: 'Europe/Berlin' } };
  const libraryRow = views.parseSavedView(row(library));
  const target = views.savedLibraryTarget(libraryRow, origin), url = views.savedViewUrl(libraryRow, origin);
  assert.equal(target.kind, 'channel'); assert.equal(url.href, target.url);
  assert.deepEqual(catalogue.readCatalogueUrl(url), { section: 'videos', overviewPeriod: 'max', type: 'short',
    conditions: v1({ sort_by: 'views', sort_order: 'desc' }, { view_buckets: ['10m_plus'] }) });
  assert.deepEqual(views.readExplorerDestination(url.pathname, url.search), { kind: 'channel', channelId: channel });
  // The channel is gone: the workshop opens the query and states the missing source.
  assert.deepEqual(views.savedLibraryTarget(views.parseSavedView(row(library, { channel_available: false })), origin),
    { kind: 'workshop', url: workshopUrl, reason: 'The source channel is unavailable.' });
  const odd = { ...library, query: { ...shortsQuery, where: { not: { field: 'video.views', op: 'gte', value: '1' } } } };
  assert.deepEqual(views.savedLibraryTarget(views.parseSavedView(row(odd)), origin), { kind: 'workshop', url: workshopUrl, reason: 'Library filters cannot show these conditions.' });
  assert.equal(views.savedViewUrl(views.parseSavedView(row({ version: 2, query: shortsQuery })), origin).href, workshopUrl, 'a v2 query is a workshop study');
  const study = graph.researchDefinition(graph.researchTemplate({ ...definition.parseSavedDefinition({ version: 2, query: shortsQuery }).query, scope: { kind: 'channel', id: channel } }, 'evidence')).conditions;
  assert.equal(views.savedViewUrl(views.parseSavedView(row(study)), origin).href, workshopUrl, 'a v3 study is a workshop study');
  assert.equal(views.savedOpenTarget(library), 'library');
  // ExplorerApp takes the destination from the address (a Library v2 view is a channel, not research).
  const app = read('../application/frontend/src/explorer/ExplorerApp.tsx');
  const open = app.slice(app.indexOf('const openSavedView = '), app.indexOf('return (', app.indexOf('const openSavedView = ')));
  assert.match(open, /const target = savedViewUrl\(view, window\.location\.origin\);\s*window\.history\.pushState\(\{\}, '', target\);\s*setDestination\(readExplorerDestination\(target\.pathname, target\.search\)\);/);
  console.log('Saved routing: v1 and Library v2 open the channel tab of their type; unavailable, unrecognised, v2 queries and v3 open the workshop.');
}

// 7. Opening runs only a translated query; needs_capability names the conditions and runs nothing.
{
  const sent = [];
  const reply = (body) => {
    const missing = body.conditions.facets.tag_count_buckets ? [{ capability: 'video.tag_count', conditions: ['facets.tag_count_buckets'] }]
      : body.conditions.facets.growth ? [{ capability: 'observation.growth_28d_status', conditions: ['facets.growth.full_history'] },
        { capability: 'observation.views_gained_28d', conditions: ['filters.sort_by'] }] : [];
    return { contract_version: 1, translation: missing.length ? { status: 'needs_capability', query: null, time_zone: body.time_zone, missing }
      : { status: 'translated', time_zone: body.time_zone, missing: [], query: { query_version: 1, source: { id: wire.VIDEO_SOURCE },
        scope: { kind: 'channel', id: body.channel_id }, video_types: [body.video_type], select: [...wire.LIBRARY_SELECT],
        where: { field: 'video.published_at', op: 'gte', value: '2024-12-31T15:00:00Z' }, order_by: [{ field: 'video.views', direction: 'desc', nulls: 'last' }],
        time: { mode: 'current', timezone: 'UTC' }, page: { size: 50, after: null } } } };
  };
  const client = conditions.createTranslationClient(async (path, signal, init) => { assert.equal(path, '/saved/views/translate'); const body = JSON.parse(init.body); sent.push(body); return reply(body); });
  const translate = (...args) => client.translate(...args);
  const recognized = v1({ published_from: '2025-01-01', sort_by: 'views' });
  const plan = await saved.planLibraryOpen(translate, { channelId: channel, conditions: recognized, videoType: 'short', viewerZone: 'Asia/Tokyo', savedZone: 'Europe/Berlin' });
  assert.equal(plan.status, 'run');
  assert.deepEqual(sent.at(-1), { channel_id: channel, conditions: recognized, time_zone: 'Asia/Tokyo', video_type: 'short' }, 'the type and the viewer zone travel');
  assert.deepEqual(plan.request, wire.libraryQuery(plan.translation.query, { timeZone: 'Asia/Tokyo' }), 'exactly the translated query, frozen, first page');
  assert.equal(plan.request.consistency, 'frozen'); assert.equal(plan.request.time.timezone, 'Asia/Tokyo');
  assert.equal(plan.note, 'Saved with dates in Europe/Berlin; shown in Asia/Tokyo.');
  assert.equal((await saved.planLibraryOpen(translate, { channelId: channel, conditions: v1({ sort_by: 'views' }), videoType: 'long', viewerZone: 'Asia/Tokyo', savedZone: 'Europe/Berlin' })).note, '', 'no dates, no note');
  const blocked = await saved.planLibraryOpen(translate, { channelId: channel, conditions: v1({ sort_by: 'views_gained' }, { growth: { full_history: true } }), videoType: 'long', viewerZone: 'UTC' });
  assert.equal(blocked.status, 'blocked'); assert.equal(Object.hasOwn(blocked, 'request'), false, 'nothing is built to run');
  assert.deepEqual(blocked.missing.flatMap((item) => item.labels), ['Full history (28-day comparison available)', 'Sort: Views gained']);
  assert.equal(blocked.message, 'This saved view cannot open yet: Full history (28-day comparison available), Sort: Views gained need data this server does not provide. The saved view is kept.');
  assert.equal(blocked.blockedText, conditions.BLOCKED_TEXT.growthFilters);
  const tags = await saved.planLibraryOpen(translate, { channelId: channel, conditions: v1({}, { tag_count_buckets: ['zero'] }), videoType: 'long', viewerZone: 'UTC' });
  assert.deepEqual([tags.status, tags.blockedText], ['blocked', conditions.BLOCKED_TEXT.staticFilters]);
  assert.equal(saved.conditionLabel('facets.future'), 'facets.future');
  console.log('Opening through translation: type and zone sent, translated query runs frozen, needs_capability lists conditions and runs nothing.');
}

// 8. The Saved list: open flow, zone note, labels of v2 fields.
{
  const list = read('../application/frontend/src/explorer/SavedViewsList.tsx');
  assert.match(list, /const plan = await planLibraryOpen\(\(\.\.\.args\) => translation\.translate\(\.\.\.args\), \{\s*channelId: target\.channelId, conditions: target\.conditions, videoType: target\.videoType, viewerZone, savedZone: target\.savedZone,\s*\}\);/);
  assert.match(list, /if \(plan\.status === 'blocked'\) setError\(plan\.message\);\s*else if \(plan\.note\) setPendingOpen\(\{ view: updated, note: plan\.note \}\);\s*else onOpen\(updated\);/);
  assert.match(list, /<span>\{pendingOpen\.note\}<\/span>/);
  const component = compiled('SavedViewsList.tsx', { react: React, 'react/jsx-runtime': jsxRuntime, '../components/YouTubeAttribution': { YouTubeAttribution: () => null },
    './api': { isAbortError: () => false, requestJson: async () => { throw new Error('offline'); } }, './savedViews': views, './savedWork': work,
    './filters': { EXPLORER_SORT_LABELS: { published_at: 'Published date', views: 'Views', title: 'Title' } }, './localTime': { formatTimestampLocal: (value) => value },
    './researchQuery': research, './timeBasis': { useTimeBasis: () => ({ zone: 'UTC' }) }, './libraryData/conditions.ts': conditions, './libraryData/savedLibrary.ts': saved,
    './savedCollections': { loadSavedCollections: async () => ({ collections: [], next_cursor: null }) } });
  const sortOf = (value) => component.savedViewSort(views.parseSavedView(row(value)), origin);
  assert.deepEqual(sortOf(v1({ sort_by: 'views_gained', sort_order: 'asc' })), { label: 'Views gained', descending: false });
  const libraryQuery = queryOf(undefined, [{ field: 'video.published_at', direction: 'desc' }]);
  assert.deepEqual(sortOf({ version: 2, query: libraryQuery, presentation: { version: 1, surface: 'library', view: 'grid' } }), { label: 'Published date', descending: true });
  assert.deepEqual(sortOf({ version: 2, query: { ...libraryQuery, order_by: [{ field: 'video.views', direction: 'asc' }, { field: 'video.published_at', direction: 'desc' }] } }),
    { label: 'Views, Published (UTC)', descending: false }, 'a query shows field labels, not raw ids');
  // The list renders with the module-level translation client (no request before Open).
  assert.match(renderToStaticMarkup(React.createElement(component.SavedViewsList, { channels: [], onOpen() {} })), /<h1 id="saved-views-title">Saved<\/h1>/);
  // A study on a folder, label or collection names that source; the kind alone when the name is unknown or deleted.
  const folderId = '22222222-2222-4222-8222-222222222222', collectionId = '33333333-3333-4333-8333-333333333333';
  const study = (kind, scopeId, channelId = null) => ({ channel_id: channelId, conditions: { version: 3, query: { scope: { kind, id: scopeId } } } });
  const names = { folder: [{ id: folderId, name: 'Music' }], label: [{ id: 'monetized', name: 'Monetized' }], collection: [{ id: collectionId, name: 'acc34 mixed 3 types' }] };
  const source = (view, known = names) => component.savedViewSource(view, [{ channel_id: channel, title: 'Kitchen' }], known);
  assert.equal(source(study('folder', folderId)), 'Folder · Music');
  assert.equal(source(study('label', 'monetized')), 'Label · Monetized');
  assert.equal(source(study('collection', collectionId)), 'Collection · acc34 mixed 3 types');
  assert.equal(source(study('folder', '44444444-4444-4444-8444-444444444444')), 'Folder', 'a deleted folder');
  assert.equal(source(study('collection', collectionId), {}), 'Collection', 'names not loaded (or not readable)');
  assert.equal(source(study('channel', channel, channel)), 'Kitchen', 'a channel study names its channel');
  assert.equal(source({ channel_id: channel, conditions: { version: 1 } }), 'Kitchen');
  assert.equal(source({ channel_id: 'UC' + 'z'.repeat(22), conditions: { version: 1 } }), 'UC' + 'z'.repeat(22), 'an unknown channel keeps its id');
  assert.match(list, /<p>\{savedViewSource\(view, channels, sourceNames\)\}\{!view\.channel_available/);
  // Folder and label names come from the sidebar lists the workshop uses; collection names from Saved's first page.
  assert.match(read('../application/frontend/src/explorer/SavedPage.tsx'), /<SavedViewsList channels=\{channels\} folders=\{folders\} labels=\{labels\}/);
  assert.match(read('../application/frontend/src/explorer/ExplorerApp.tsx'), /<SavedPage destination=\{destination\} channels=\{channels\} folders=\{folderChoices\} labels=\{labelChoices\}/);
  console.log('Saved list: opening checks the translation first, one zone note, Library and field labels.');
}

// 8b. one item this page cannot read is a row without buttons; the rest of the list and Load more work.
{
  const ids = Array.from({ length: 50 }, (_, n) => `11111111-1111-4111-8111-${String(n).padStart(12, '0')}`);
  const items = ids.map((itemId, n) => row(v1(), { id: itemId, name: `View ${n}` }));
  items[17] = { ...items[17], name: 'From a newer version', conditions: { version: 9, future: true } };
  const page = views.parseSavedViewsPage({ contract_version: 1, views: items, next_cursor: 'next-page' });
  assert.equal(page.views.length, 50);
  assert.deepEqual(page.views[17], { kind: 'unreadable', id: ids[17], name: 'From a newer version' }, 'kept at its place');
  assert.equal(page.views.filter((view) => view.kind === 'view').length, 49);
  assert.equal(page.next_cursor, 'next-page');
  // What cannot be read of the item itself is left out, never invented.
  assert.deepEqual(views.parseSavedViewsPage({ contract_version: 1, views: [{ id: 'not-an-id', name: '' }, 'text', null], next_cursor: null }).views,
    [{ kind: 'unreadable', id: null, name: null }, { kind: 'unreadable', id: null, name: null }, { kind: 'unreadable', id: null, name: null }]);
  // The page's own checks still refuse the whole page.
  const refused = (value, why) => assert.throws(() => views.parseSavedViewsPage(value, 'previous'), /saved|cursor|contract/, why);
  refused({ contract_version: 1, views: items, next_cursor: 'previous' }, 'a cursor that did not advance');
  refused({ contract_version: 1, views: items, next_cursor: 'bad cursor' }, 'a cursor with a space');
  refused({ contract_version: 1, views: [...items, items[0]], next_cursor: null }, 'more than 50');
  refused({ contract_version: 1, views: [items[0], { ...items[0], conditions: { version: 9 } }], next_cursor: null }, 'a repeated identity, readable or not');
  refused({ contract_version: 2, views: items, next_cursor: null }, 'another contract');
  refused({ contract_version: 1, views: {}, next_cursor: null }, 'no list');

  // The list itself: the real component, its effects driven offline. Its row has the name and the sentence, no buttons.
  const host = reactHookHost(), reads = [];
  const jsx = (type, props) => ({ type, props: props ?? {} });
  const second = { contract_version: 1, views: [row(v1(), { id: '22222222-2222-4222-8222-000000000001', name: 'Second page' })], next_cursor: null };
  const list = compiled('SavedViewsList.tsx', { react: host.React, 'react/jsx-runtime': { jsx, jsxs: jsx, Fragment: 'fragment' },
    '../components/YouTubeAttribution': { YouTubeAttribution: () => null },
    './api': { isAbortError: () => false, requestJson: async () => { throw new Error('offline'); } },
    './savedViews': { ...views, loadSavedViews: async (cursor) => { reads.push(cursor ?? null); return cursor ? views.parseSavedViewsPage(second, cursor)
      : views.parseSavedViewsPage({ contract_version: 1, views: items, next_cursor: 'next-page' }); } },
    './savedWork': work, './filters': { EXPLORER_SORT_LABELS: { published_at: 'Published date', views: 'Views', title: 'Title' } },
    './localTime': { formatTimestampLocal: (value) => value }, './researchQuery': research, './timeBasis': { useTimeBasis: () => ({ zone: 'UTC' }) },
    './libraryData/conditions.ts': conditions, './libraryData/savedLibrary.ts': saved,
    './savedCollections': { loadSavedCollections: async () => ({ collections: [], next_cursor: null }) } });
  const oldWindow = globalThis.window;
  globalThis.window = { location: { origin }, confirm: () => false };
  try {
  host.mount(() => list.SavedViewsList({ channels: [], onOpen() {} }), {});
  await flush();
  const all = (node, test, found = []) => {
    if (Array.isArray(node)) { node.forEach((child) => all(child, test, found)); return found; }
    if (!node || typeof node !== 'object') return found;
    if (test(node)) found.push(node);
    all(node.props.children, test, found);
    return found;
  };
  const text = (node) => (node === null || node === undefined || typeof node === 'boolean' ? '' : typeof node === 'string' || typeof node === 'number' ? String(node)
    : Array.isArray(node) ? node.map(text).join(' ') : typeof node.type === 'function' ? '' : text(node.props.children));
  const rows = all(host.current, (node) => node.type === 'li');
  assert.equal(rows.length, 50, 'every item has its row');
  assert.match(text(rows[17]), /From a newer version/);
  assert.match(text(rows[17]), /This item cannot be opened by this version of the page\. Reload the page; if it stays, it was saved by a different version\./);
  assert.equal(all(rows[17], (node) => node.type === 'button').length, 0, 'no Open, Rename or Delete');
  assert.deepEqual(all(rows[16], (node) => node.type === 'button').map(text), ['Open', 'Rename', 'Delete']);
  assert.equal(all(host.current, (node) => node.props?.role === 'alert').length, 0, 'the list is not refused');
  const more = all(host.current, (node) => node.type === 'button' && text(node) === 'Load more saved views')[0];
  assert.ok(more && !more.props.disabled);
  more.props.onClick();
  await flush();
  assert.deepEqual(reads, [null, 'next-page']);
  const after = all(host.current, (node) => node.type === 'li');
  assert.equal(after.length, 51);
  assert.match(text(after[17]), /cannot be opened by this version/, 'the unreadable row keeps its place');
  assert.match(text(after[50]), /Second page/);
  host.unmount();
  } finally { globalThis.window = oldWindow; }
  console.log('one unreadable Saved item is a row without buttons; the other 49, Load more and page checks are unchanged.');
}

// 9. Collections: members of all three types, the 11 reasons, removal and the base Inspector.
{
  const collections = compiled('savedCollections.ts', { './api': { requestJson: async () => ({}) }, '../explorerRoutes': routes, './contracts': contracts, './savedWork': work });
  const page = compiled('SavedCollectionDetail.tsx', { react: React, 'react/jsx-runtime': jsxRuntime, '../components/YouTubeAttribution': { YouTubeAttribution: () => null },
    './api': { isAbortError: () => false }, './CollectionTransferDialog': { CollectionTransferDialog: () => null },
    './ExplorerVideos': { VideoCard: ({ video }) => React.createElement('article', { 'data-card': video.video_type, 'data-video-id': video.video_id }), VideoInspector: () => null },
    './VirtualVideoGrid': { VirtualVideoGrid: () => null }, './savedCollections': collections, './savedWork': work, './format': { videoNoun: (n) => (n === 1 ? 'video' : 'videos') } });
  const checked = '2026-10-29T10:00:00Z';
  const available = (type, index) => collections.parseCollectionMember({ video_id: `${type}_video_${index}`.slice(0, 11).padEnd(11, 'x'), added_at: at, status: 'available', reason: null,
    channel_id: channel, source_expires_at: '2026-10-29T12:00:00Z', video: { video_id: `${type}_video_${index}`.slice(0, 11).padEnd(11, 'x'), video_type: type, title: `A ${type}`,
      thumbnail_url: null, published_at: '2026-10-01T00:00:00Z', checked_at: checked, duration: 'PT1M', views: '10', like_count: '1', comment_count: '0', category_id: '22',
      category_name: 'People', duration_seconds: 60 } });
  const reasons = ['channel_hidden', 'channel_unavailable', 'inventory_missing', 'source_mismatch', 'source_unpublished', 'source_stale',
    'type_conflict', 'type_unverified', 'source_missing', 'source_unavailable', 'stream_incomplete'];
  assert.deepEqual(Object.keys(collections.COLLECTION_SOURCE_REASONS).filter((key) => key !== 'api_paused').sort(), [...reasons].sort(), 'the 11 collection membership reasons');
  const removed = [], toggled = [];
  const card = (member) => page.CollectionMemberCard({ member, selected: false, removeDisabled: false, onInspect() {},
    onToggleSelected: (video, value) => toggled.push([video.video_id, value]), onRemove: (videoId) => removed.push(videoId) });
  for (const [index, type] of ['long', 'short', 'stream'].entries()) {
    const member = available(type, index), html = renderToStaticMarkup(card(member));
    assert.match(html, new RegExp(`data-card="${type}"`), type);
    assert.equal(card(member).props.video, member.video, 'the Library card gets the member video of its type');
    // One mixed grid: every type in the Long card at Large density (equal heights), without growth.
    assert.equal(card(member).props.density, 'large', type);
    assert.equal(card(member).props.form, undefined, `${type}: no Shorts form in a collection, the Long card for every type`);
    assert.equal(card(member).props.growthAvailable, false, type);
    assert.equal(page.CollectionMemberCard({ member, selected: false, selecting: true, removeDisabled: false, onInspect() {},
      onToggleSelected() {}, onRemove() {} }).props.selecting, true, 'a selection keeps every checkbox visible');
  }
  for (const reason of [...reasons, 'api_paused']) {
    const member = collections.parseCollectionMember({ video_id: 'unavailabl1', added_at: at, status: 'unavailable', reason, channel_id: null, video: null, source_expires_at: null });
    const html = renderToStaticMarkup(card(member));
    assert.ok(html.includes(collections.COLLECTION_SOURCE_REASONS[reason].replace(/'/g, '&#x27;')), reason);
    assert.match(html, />Remove from collection<\/button>/);
    assert.match(html, reason === 'api_paused' ? /Select saved video/ : /Select unavailable video/);
  }
  // Remove and select work by ID, whatever the type.
  const element = card(collections.parseCollectionMember({ video_id: 'streamvideo', added_at: at, status: 'unavailable', reason: 'stream_incomplete', channel_id: null, video: null, source_expires_at: null }));
  const children = element.props.children;
  children.at(-1).props.onClick(); children[0].props.children[0].props.onChange({ target: { checked: true } });
  assert.deepEqual(removed, ['streamvideo']); assert.deepEqual(toggled, [['streamvideo', true]]);
  // The collection Inspector is the base one (no frozen-list panels).
  const detail = read('../application/frontend/src/explorer/SavedCollectionDetail.tsx');
  assert.match(detail, /\{inspected && <VideoInspector video=\{inspected\} onClose=\{closeInspector\} onRestoreFocus=\{restoreInspector\} \/>\}/);
  assert.doesNotMatch(detail, /staticDetails=|launchContext=|history=\{|growth=\{/);
  assert.match(detail, /selecting=\{selected\.size > 0\}/, 'the grid knows when a selection is open');
  assert.match(detail, /<VirtualVideoGrid ref=\{grid\} videos=\{gridVideos\} density="large"/, 'the grid estimates Large Long cards');
  assert.doesNotMatch(detail, /videoType=|form=/, 'the collection grid has no video type, so no Shorts columns or rows');
  console.log('Collections: Long, Shorts and Streams members as Library cards, 11 reasons, removal by ID, base Inspector.');
}
console.log('Saved in Library passed.');
