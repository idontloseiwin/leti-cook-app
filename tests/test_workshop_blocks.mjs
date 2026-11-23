// Research workshop blocks: the source inspector and result blocks as real HTML,
// templates, the block registry against the server fixture, Undo/Redo, paging of one block (current cursor stack and
// fixed-list cursors), per-block expiry texts and the graph run wire with run options (frozen_ref). No network.
import assert from 'node:assert/strict';
import * as savedLibrary from '../application/frontend/src/explorer/libraryData/savedLibrary.ts';
import { readFileSync } from 'node:fs';
import ts from '../application/frontend/node_modules/typescript/lib/typescript.js';
import * as lifetime from '../application/frontend/src/sourceLifetime.ts';
import * as React from '../application/frontend/node_modules/react/index.js';
import * as jsxRuntime from '../application/frontend/node_modules/react/jsx-runtime.js';
import { renderToStaticMarkup } from '../application/frontend/node_modules/react-dom/server.node.js';
function compiled(file, imports = {}) {
  const { outputText } = ts.transpileModule(readFileSync(new URL('../application/frontend/src/explorer/' + file, import.meta.url), 'utf8'),
    { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX } });
  const exports = {}; new Function('require', 'exports', outputText)(name => { assert.ok(name in imports, file + ': ' + name); return imports[name]; }, exports); return exports;
}
class ExplorerApiError extends Error { constructor(message, status, code) { super(message); this.status = status; this.code = code; } }
let server = async () => { throw new Error('no request expected'); };
const sent = [];
const api = { ExplorerApiError, async requestJson(path, signal, init) { sent.push({ path, body: init?.body ? JSON.parse(init.body) : undefined }); return server(path, init?.body ? JSON.parse(init.body) : undefined); } };
const jsx = { 'react/jsx-runtime': jsxRuntime, react: React };
const saved = compiled('savedWork.ts');
const research = compiled('researchQuery.ts', { './api': api, '../sourceLifetime': lifetime });
const definition = compiled('savedQueryDefinition.ts', { './researchQuery': research, './savedWork': saved, './timeZones': compiled('timeZones.ts') });
const operations = compiled('researchOperations.ts', { './api': api, '../sourceLifetime': lifetime });
const graph = compiled('researchGraph.ts', { './api': api, './savedWork': saved, './savedQueryDefinition': definition, './researchQuery': research, './researchOperations': operations, '../sourceLifetime': lifetime });
const views = compiled('savedViews.ts', { './researchGraph': graph, './api': api, './savedQueryDefinition': definition, '../explorerRoutes': { EXPLORER_V3_ROOT: '/app-explorer-v3' }, './catalogueQuery': {}, './savedWork': saved, './timeZones': compiled('timeZones.ts'), './libraryData/savedLibrary.ts': savedLibrary });
const handoff = compiled('workshopHandoff.ts', { './savedQueryDefinition': definition, './researchQuery': research, './researchGraph': graph, './savedViews': views, './savedWork': saved });
const model = compiled('workshopModel.ts', { './api': api, './researchGraph': graph, './researchQuery': research, './workshopHandoff': handoff });
const videoGrowth = compiled('videoGrowth.ts');
const rows = compiled('ResearchRows.tsx', { ...jsx, './researchQuery': research, './videoGrowth': videoGrowth });
const ageNote = compiled('ObservationAgeLimitNote.tsx', jsx);
const controls = compiled('ResearchControls.tsx', { ...jsx, '../sourceLifetime': lifetime, './ObservationAgeLimitNote': ageNote, './ResearchRows': rows, './researchQuery': research, './researchOperations': operations });
const conditions = compiled('ResearchConditions.tsx', { ...jsx, './researchQuery': research });
const blocks = compiled('WorkshopBlocks.tsx', { ...jsx, './api': api, '../sourceLifetimeReact': { useSourceExpiry() {} }, './ObservationAgeLimitNote': ageNote, './ResearchConditions': conditions,
  './ResearchControls': controls, './ResearchRows': rows, './researchGraph': graph, './researchQuery': research, './workshopModel': model });
const html = (component, props) => renderToStaticMarkup(React.createElement(component, props));

const channel = 'UC' + 'a'.repeat(22), folder = '22222222-2222-4222-8222-222222222222';
const iso = ms => new Date(ms).toISOString().replace(/\.\d{3}Z$/, 'Z');
const field = (caps, id, extra = {}) => ({ id, value_type: 'text', sortable: true, operators: ['eq', 'is_known'], ...extra });
const videoCaps = { source_id: research.videoSource, video_types: ['long', 'short'], scopes: ['channel', 'folder', 'label', 'collection'], operations: [operations.summaryOperation],
  observation_age_limits: { long: { '3600': 0, '86400': 604800 } },
  fields: ['video.id', 'video.channel_id', 'video.type', 'video.title', 'video.thumbnail_url', 'channel.title', 'video.published_at', 'video.views', 'video.likes', 'video.comments', 'observation.views_delta']
    .map(id => field(null, id)).concat([field(null, 'video.description', { enabled_video_types: ['long'] })]) };

// ---- 1. Block registry = server operation blocks (fixture pinned by tests/test_research_operation_blocks.py). ----
{
  const fixture = JSON.parse(readFileSync(new URL('./fixtures/research_operation_blocks.json', import.meta.url), 'utf8'));
  assert.deepEqual(Object.fromEntries(graph.NODE_KINDS.flatMap(type => model.WORKSHOP_BLOCKS[type].operation ? [[type, model.WORKSHOP_BLOCKS[type].operation]] : [])), fixture);
  assert.deepEqual(Object.keys(model.WORKSHOP_BLOCKS).sort(), [...graph.NODE_KINDS].sort(), 'every block type has a registry entry');
  for (const type of graph.NODE_KINDS) {
    const spec = model.WORKSHOP_BLOCKS[type];
    assert.equal(spec.acquisition, false, 'this node never acquires data');
    assert.equal(spec.display !== null, graph.OUTPUT_KINDS.includes(type), type + ': every result block has a display');
    assert.equal(spec.display === 'operation', type in graph.OPERATION_OF_BLOCK, type);
  }
  assert.deepEqual(model.offeredBlocks('video', [operations.summaryOperation, operations.calendarOperation, operations.historyOperation]), ['filter', 'selection', 'table', 'gallery', 'summary', 'calendar']);
  assert.deepEqual(model.offeredBlocks('channel', []), ['filter', 'selection', 'table', 'gallery']);
  // A new calendar block counts in the study's zone (time.timezone): no zone of its own.
  assert.deepEqual(model.WORKSHOP_BLOCKS.calendar.defaults('Europe/Moscow'), { year: new Date().getUTCFullYear() });
}
console.log('Block registry: every type described, calculations match the server fixture, offered only when granted.');

// ---- 2. Source edits, templates, rows switch, study problems. ----
const base = graph.withScope(graph.newStudy(), { kind: 'folder', id: folder });
{
  const shorts = graph.withSourceQuery(base, { video_types: ['short'], coverage: { mode: 'require_complete' }, time: { mode: 'current', timezone: 'Europe/Moscow' } });
  assert.deepEqual(shorts.query.video_types, ['short']); assert.deepEqual(shorts.graph, base.graph, 'blocks, notes and layout stay');
  assert.equal(graph.researchDefinition(shorts).conditions.query.time.timezone, 'Europe/Moscow');
  const windowed = graph.withSourceQuery(base, { observations: controls.observationWindow(86400) });
  assert.deepEqual(windowed.query.observations, { version: 1, window_seconds: 86400, tolerance_seconds: 7200, age_seconds: 86400, age_tolerance_seconds: 7200 });
  assert.equal(Object.hasOwn(graph.withSourceQuery(windowed, { observations: undefined }).query, 'observations'), false, 'undefined removes a key');
  assert.deepEqual(base.query.video_types, ['long'], 'the original document is not changed');
  // Starting views of the catalogue: columns and sort set, conditions cleared, source and format kept.
  const filtered = graph.withSourceQuery(shorts, { where: { field: 'video.views', op: 'gte', value: '1000' } });
  const most = graph.researchTemplate(filtered.query, 'most_viewed');
  assert.deepEqual(most.query.select, ['video.title', 'channel.title', 'video.views', 'video.likes', 'video.comments']);
  assert.deepEqual(most.query.order_by, [{ field: 'video.views', direction: 'desc', nulls: 'last' }]);
  assert.equal(most.query.where, null); assert.deepEqual(most.query.video_types, ['short']); assert.deepEqual(most.query.scope, shorts.query.scope);
  const thumbs = graph.researchTemplate(filtered.query, 'thumbnails');
  assert.equal(thumbs.graph.nodes[1].type, 'gallery'); assert.ok(thumbs.query.select.includes('video.thumbnail_url'));
  assert.deepEqual(graph.researchTemplate(filtered.query, 'latest').query.select, ['video.title', 'channel.title', 'video.published_at', 'video.views']);
  // Overview of this selection keeps the conditions; its blocks only when granted.
  const overview = graph.researchTemplate(filtered.query, 'overview', [operations.calendarOperation]);
  assert.deepEqual(overview.query.where, filtered.query.where);
  assert.deepEqual(overview.graph.nodes.map(n => n.type), ['source', 'calendar']);
  assert.deepEqual(Object.keys(overview.graph.nodes[1].parameters), ['year'], 'the calendar counts in the study zone (time.timezone)');
  // A study saved with the block's own zone still opens and keeps it (the server still names that zone).
  const zoned = { ...overview, graph: { ...overview.graph, nodes: overview.graph.nodes.map((n) => n.type === 'calendar' ? { ...n, parameters: { ...n.parameters, time_zone: 'Asia/Tokyo' } } : n) } };
  assert.equal(graph.parseResearchDefinition(graph.researchDefinition(zoned).conditions).graph.nodes[1].parameters.time_zone, 'Asia/Tokyo');
  for (const bad of [{ year: 2026, time_zone: 7 }, { year: 2026, time_zone: 'x'.repeat(65) }, { year: 2026, zone: 'UTC' }]) {
    const broken = { ...overview, graph: { ...overview.graph, nodes: overview.graph.nodes.map((n) => n.type === 'calendar' ? { ...n, parameters: bad } : n) } };
    assert.throws(() => graph.parseResearchDefinition(graph.researchDefinition(broken).conditions), undefined, JSON.stringify(bad));
  }
  assert.deepEqual(graph.researchTemplate(filtered.query, 'overview').graph.nodes.map(n => n.type), ['source', 'summary', 'calendar']);
  assert.throws(() => graph.researchTemplate(filtered.query, 'overview', []));
  for (const t of graph.VIDEO_TEMPLATES) graph.researchDefinition(graph.researchTemplate(filtered.query, t));
  // Channels: Channel list, Compare channels, Channel history; rows switch keeps notes; collections fall back to a folder.
  const noted = { ...base, graph: { ...base.graph, notes: 'Why do these channels grow?' } };
  const channels = graph.studyForRows(noted, research.channelSource);
  assert.equal(channels.query.source.id, research.channelSource); assert.equal(Object.hasOwn(channels.query, 'video_types'), false);
  assert.deepEqual(channels.query.select, graph.CHANNEL_COLUMNS); assert.equal(channels.graph.notes, noted.graph.notes);
  assert.deepEqual(channels.query.order_by, [{ field: 'channel.subscribers', direction: 'desc', nulls: 'last' }]);
  const fromCollection = graph.studyForRows(graph.withScope(base, { kind: 'collection', id: '33333333-3333-4333-8333-333333333333' }), research.channelSource);
  assert.deepEqual(fromCollection.query.scope, { kind: 'folder', id: '', include_descendants: false });
  assert.deepEqual(graph.studyForRows(channels, research.videoSource).query.video_types, ['long']);
  for (const t of graph.CHANNEL_TEMPLATES) graph.researchDefinition(graph.researchTemplate(channels.query, t));
  const history = graph.researchTemplate(channels.query, 'channel_history');
  assert.deepEqual(history.graph.nodes.map(n => n.type), ['source', 'history']);
  // History needs one channel; the exact size in words.
  assert.deepEqual(graph.studyProblems(history), ['Channel history needs one channel. Choose a channel or remove the history block.']);
  assert.deepEqual(graph.studyProblems(graph.withScope(history, { kind: 'channel', id: channel })), []);
  const big = structuredClone(base); big.graph.notes = 'x'.repeat(4000);
  big.graph.nodes.splice(1, 0, { id: 'picked', type: 'selection', parameters: { mode: 'include', ids: Array.from({ length: 600 }, (_, i) => 'UC' + i.toString(36).padStart(22, '_')) } });
  big.graph.edges = [{ from: 'source', to: 'picked', input: 'query', output: 'query' }, { from: 'picked', to: 'result', input: 'query', output: 'query' }]; big.graph.layout.picked = { x: 0, y: 0 };
  assert.match(graph.studyProblems(big)[0], /^The study is too large to save: \d{2},\d{3} of 16,384 bytes\. Shorten notes or the ID list\.$/);
  // Fields used anywhere in the study (root and Conditions blocks).
  const evidence = graph.researchTemplate(base.query, 'evidence'); evidence.graph.nodes[1].parameters.where = { not: { field: 'video.description', op: 'is_known' } };
  assert.ok(graph.studyFields(evidence).includes('video.description'));
  const shortEvidence = graph.withSourceQuery(evidence, { video_types: ['short'] });
  assert.equal(blocks.studyFieldIssue(shortEvidence, videoCaps), 'Disabled for Shorts: Description. Remove them or enable them in Collection fields.');
  assert.equal(blocks.studyFieldIssue(evidence, videoCaps), '');
  const unknown = graph.withSourceQuery(base, { select: ['video.title', 'video.future_field'] });
  assert.equal(blocks.studyFieldIssue(unknown, videoCaps), 'Not available on this server: video.future_field. Remove them to run this study.');
  assert.match(html(controls.ResearchColumnsPicker, { columns: unknown.query.select, fields: videoCaps.fields, channels: false, loaded: true, onChange() {} }), /video\.future_field \(not available on this server\)/);
  assert.match(html(conditions.ResearchConditions, { value: { field: 'video.future_field', op: 'eq', value: 'x' }, fields: videoCaps.fields, onChange() {} }), /<option value="video\.future_field" selected="">video\.future_field \(not available on this server\)<\/option>/);
}
console.log('Source edits, starting views, overview, channel templates, rows switch, history rule and exact size text passed.');

// ---- 3. Undo / Redo: 50 steps, one step per drag or typing run, shortcuts outside text fields. ----
{
  let h = model.startHistory(0);
  for (let i = 1; i <= 60; i++) h = model.recordEdit(h, i);
  assert.equal(h.past.length, 50); assert.equal(h.past[0], 10);
  for (let i = 0; i < 50; i++) h = model.undoEdit(h);
  assert.equal(h.present, 10); assert.equal(model.undoEdit(h), h, 'nothing older than 50 steps');
  h = model.redoEdit(model.redoEdit(h)); assert.equal(h.present, 12);
  h = model.recordEdit(h, 99); assert.deepEqual(h.future, [], 'a new edit drops redo');
  let drag = model.recordEdit(model.startHistory('a'), 'b', 'move:x:1'); drag = model.recordEdit(drag, 'c', 'move:x:1'); drag = model.recordEdit(drag, 'd', 'move:x:1');
  assert.deepEqual([drag.past, drag.present], [['a'], 'd'], 'a drag is one step');
  assert.equal(model.undoEdit(model.recordEdit(drag, 'e', 'move:x:2')).present, 'd');
  const key = (k, extra = {}) => model.shortcutAction({ key: k, ctrlKey: true, metaKey: false, shiftKey: false, altKey: false, target: { tagName: 'BODY' }, ...extra });
  assert.equal(key('z'), 'undo'); assert.equal(key('Z', { shiftKey: true }), 'redo'); assert.equal(key('y'), 'redo');
  assert.equal(key('z', { metaKey: true, ctrlKey: false }), 'undo');
  for (const tagName of ['INPUT', 'TEXTAREA', 'SELECT']) assert.equal(key('z', { target: { tagName } }), null, 'a text field keeps its own undo');
  assert.equal(key('z', { ctrlKey: false }), null);
}
console.log('Undo/Redo: 50 steps, drag and typing runs as one step, shortcuts outside fields.');

// ---- 4. Source inspector as real HTML (every function of the catalogue's source panel). ----
{
  const props = { caps: videoCaps, sources: { folder: [{ id: folder, name: 'Music' }], label: [], channel: [], collection: [] }, mode: 'current', modeNote: 'Every block reads current data.',
    onChange() {}, onRows() {}, onMode() {} };
  const study = graph.withSourceQuery(base, { where: { all: [{ field: 'video.views', op: 'eq', value: '5' }] }, select: ['video.title', 'observation.views_delta'] });
  const out = html(blocks.ResearchSourcePicker, { ...props, document: study });
  assert.match(out, /role="group" aria-label="Rows of this study"><span>Rows<\/span><button type="button" aria-pressed="true">Videos<\/button><button type="button" aria-pressed="false">Channels<\/button>/);
  assert.match(out, /<option value="folder" selected="">Folder<\/option>/);
  assert.match(out, /<option value="22222222-2222-4222-8222-222222222222" selected="">Music<\/option>/);
  assert.match(out, /<option value="stream" disabled="">Streams<\/option>/, 'a format the server does not offer is off');
  assert.match(out, /<summary>Conditions \(1\)<\/summary>/); assert.match(out, /<summary>Sort \(1\)<\/summary>/);
  assert.match(out, /Missing values sort last\. Video ID breaks remaining ties\./);
  assert.match(out, /<input type="checkbox"\/>Require complete data/);
  assert.match(out, /View change over <select><option value="3600" disabled="" selected="">1 hour<\/option><option value="86400">1 day<\/option>/, 'the window and its schedule limit');
  assert.match(out, /Observations use real saved snapshots/);
  assert.match(out, /Dates and years<select><option value="UTC" selected="">UTC<\/option>/);
  assert.match(out, /<button type="button" aria-pressed="true">Current reads<\/button><button type="button" aria-pressed="false">Fixed list \(10 min\)<\/button>/);
  assert.doesNotMatch(html(blocks.ResearchSourcePicker, { ...props, document: base }), /View change over/, 'no window without a change field');
  const moscow = html(blocks.ResearchSourcePicker, { ...props, document: graph.withSourceQuery(base, { time: { mode: 'current', timezone: 'Asia/Tokyo' } }) });
  assert.match(moscow, /<option value="Asia\/Tokyo" selected="">Asia\/Tokyo<\/option>/, 'a hand-off zone stays selectable');
  const collection = graph.withScope(base, { kind: 'collection', id: '33333333-3333-4333-8333-333333333333' });
  const withMore = html(blocks.ResearchSourcePicker, { ...props, document: collection, collections: { loading: false, error: 'Collections could not be loaded. Try again.', more: true, load() {} } });
  assert.match(withMore, /Collections could not be loaded\. Try again\.<\/span><button type="button">Refresh collections<\/button><button type="button">More collections<\/button>/);
  assert.match(withMore, /<option value="33333333-3333-4333-8333-333333333333" selected="">33333333-3333-4333-8333-333333333333<\/option>/, 'a collection not on loaded pages is not dropped');
  const many = { ...props.sources, folder: Array.from({ length: 13 }, (_, i) => ({ id: `0000000${i.toString(16)}-0000-4000-8000-000000000000`.slice(-36), name: 'Folder ' + i })) };
  assert.match(html(blocks.ResearchSourcePicker, { ...props, sources: many, document: base }), /<label>Find folder<input type="search"/);
  const channelCaps = { ...videoCaps, source_id: research.channelSource, video_types: [], scopes: ['channel', 'folder', 'label'], fields: ['channel.id', ...graph.CHANNEL_COLUMNS].map(id => field(null, id)) };
  const channelHtml = html(blocks.ResearchSourcePicker, { ...props, caps: channelCaps, document: graph.studyForRows(base, research.channelSource) });
  assert.doesNotMatch(channelHtml, /value="collection"|<label>Format|Disabled|View change over|Observations use/); assert.match(channelHtml, /Channel ID breaks remaining ties/);
  assert.match(channelHtml, /<option value="channel\.subscribers" selected="">Subscribers<\/option>/);
  const shorts = html(blocks.ResearchSourcePicker, { ...props, document: graph.withSourceQuery(graph.researchTemplate(base.query, 'evidence'), { video_types: ['short'], select: ['video.title', 'video.description'] }) });
  assert.match(shorts, /Disabled for Shorts: Description\. Remove them or enable them in Collection fields\./);
  assert.match(html(blocks.ResearchSourcePicker, { ...props, document: base, mode: 'frozen', modeNote: 'Fixed list from Library · 1,234 videos' }), /aria-pressed="true">Fixed list \(10 min\)<\/button><\/div><small>Fixed list from Library · 1,234 videos<\/small>/);
}
console.log('Source inspector: rows, source, format, conditions, sort, completeness, window, zone, read mode — real HTML.');

// ---- 5. Result pages and blocks: current and fixed pages, Details, compare, paging, errors, expiry texts. ----
const now = Date.now(), at = iso(now - 5000), until = iso(now + 110000), listAt = iso(now - 60000), listUntil = iso(now + 540000);
const columns = ['video.title', 'video.views'];
function page({ ids, frozen = false, start = 0, total = ids.length, more = false, evaluated = at, valid = until, reasons = {} }) {
  const cell = (f, id) => ({ value: f === 'video.views' ? '1234' : 'Title ' + id, state: 'known', provenance_ref: 'p', observed_at: evaluated, expires_at: valid });
  return { query_version: 1, query_fingerprint: 'a'.repeat(64), columns,
    context: { source_id: research.videoSource, evaluated_at: evaluated, valid_before: valid, consistency: frozen ? 'frozen' : 'current', time_zone: 'UTC', ...(frozen ? { frozen_ref: 'list-' + evaluated } : {}) },
    rows: ids.map(id => ({ id, cells: Object.fromEntries(columns.map(f => [f, cell(f, id)])) })), provenance: { p: { origin: 'youtube_reported', source_paths: ['videos.statistics.viewCount'], method: 'stored' } },
    coverage: { status: Object.values(reasons).some(Boolean) ? 'partial' : 'complete', basis: 'channel_inventory', channels_in_scope: 1, channels_complete: 1, reasons },
    page: frozen ? { has_more: more, next_cursor: more ? 'next-' + start : null, previous_cursor: start ? 'prev-' + start : null, start_index: start, total_count: total, anchor_id: null }
      : { has_more: more, next_cursor: more ? 'cur-' + start : null },
    ...(frozen ? { warnings: ['frozen_values_as_of_evaluated_at'] } : {}) };
}
const vid = i => 'v' + String(i).padStart(10, '0');
const tagged = value => lifetime.captureSourceLifetime({ ...value, source_read_at: iso(now), source_expires_at: value.context.valid_before }, undefined, performance.now());
const study = graph.withSourceQuery(base, { select: columns, page: { size: 2 } });
const filters = graph.blockFilters(study, 'result');
{
  // Fixed-list pages: position and session checks; rows and cells pass the same checks as current pages.
  const first = graph.parseBlockPage(tagged(page({ ids: [vid(1), vid(2)], frozen: true, total: 5, more: true, evaluated: listAt, valid: listUntil })), filters);
  assert.equal(model.pagePosition(first, 1, false), '1–2 of 5');
  for (const broken of [{ start_index: 1 }, { total_count: 1 }, { has_more: false }, { previous_cursor: 'x' }]) {
    const bad = page({ ids: [vid(1), vid(2)], frozen: true, total: 5, more: true, evaluated: listAt, valid: listUntil }); Object.assign(bad.page, broken);
    assert.throws(() => graph.parseBlockPage(tagged(bad), filters), JSON.stringify(broken));
  }
  const noRef = page({ ids: [vid(1)], frozen: true, evaluated: listAt, valid: listUntil }); delete noRef.context.frozen_ref;
  assert.throws(() => graph.parseBlockPage(tagged(noRef), filters));
  assert.equal(model.blockTimeLabel(first.context), `Fixed list · fixed at ${listAt.slice(11, 19)} UTC · until ${listUntil.slice(11, 19)} UTC`);
  assert.equal(model.blockTimeLabel({ evaluated_at: at, valid_before: until, consistency: 'current' }), `Current read at ${at.slice(11, 19)} UTC · valid until ${until.slice(11, 19)}`);
  const other = graph.parseBlockPage(tagged(page({ ids: [vid(3)], frozen: true, start: 2, total: 5, more: true, evaluated: iso(now - 1000), valid: listUntil })), filters);
  assert.equal(model.samePageSession(first, other), false, 'a page of another fixed list is refused');
  // Error texts in words.
  // The fixed-list refusal names its bound; without one no number is claimed.
  assert.equal(model.blockError('query_budget_exceeded', 'consistency', 'rows'), 'Too many rows to fix (more than 50,000). Use current reads.');
  assert.equal(model.blockError('query_budget_exceeded', 'consistency', 'memory'), 'Too much data to fix as one list (over the server’s memory limit). Use current reads.');
  for (const limit of [undefined, 'time', 'bytes']) assert.equal(model.blockError('query_budget_exceeded', 'consistency', limit), 'Too large to fix as one list. Use current reads.');
  assert.equal(model.blockError('catalogue_preparing'), 'Stored data is being prepared in the background. Run again shortly.');
  assert.equal(model.blockError('something_new'), 'This block could not run. No result is shown.');
  assert.equal(controls.coverageReasonLabel('source_rows_expiring'), 'Rows left out because their source data expires before this fixed list ends');
  assert.equal(controls.coverageReasonLabel('new_reason'), 'Other source limitation (new_reason)');
  // A table block: read time, coverage, position, Details column, Compare, paging buttons.
  const current = graph.parseBlockPage(tagged(page({ ids: [vid(1), vid(2)], more: true, reasons: { inventory_incomplete: 1, source_rows_expiring: 0 } })), filters);
  const node = { state: 'ready', output: 'page', consistency: 'current', query: { ...study.query, page: { size: 2 } }, value: current };
  const table = html(blocks.ResearchBlockResult, { id: 'result', type: 'table', document: study, result: node, fixedStudy: false, onKeepComparison() {} });
  assert.match(table, new RegExp(`Current read at ${at.slice(11, 19)} UTC · valid until ${until.slice(11, 19)} · Partial source · 1 of 1 channels complete · Page 1 · 2 videos`));
  assert.match(table, /<summary>Why data is incomplete<\/summary><ul><li>Channels whose inventory is incomplete: 1<\/li><\/ul>/, 'zero reasons are not listed');
  assert.match(table, /<th scope="col">Details<\/th><th scope="col">Compare<\/th>/); assert.match(table, /<summary>Data details<\/summary>/);
  assert.match(table, /<tr data-video-id="v0000000001">/); assert.match(table, /aria-label="Compare v0000000001"/);
  assert.match(table, /Live pages: updates may move rows between pages\.<\/small><button type="button" disabled="">← Previous<\/button><button type="button">Next →<\/button>/);
  const fixedNode = { ...node, consistency: 'frozen', value: first, query: { ...node.query, consistency: 'frozen' } };
  const fixedTable = html(blocks.ResearchBlockResult, { id: 'result', type: 'gallery', document: study, result: fixedNode, fixedStudy: true });
  assert.match(fixedTable, /Fixed list · fixed at .* UTC · until .* UTC · Complete source · 1 of 1 channels complete · 1–2 of 5/);
  assert.match(fixedTable, /<article data-video-id="v0000000001"><a class="research-thumbnail"/); assert.match(fixedTable, /Pages come from this fixed list/);
  assert.match(html(blocks.ResearchBlockResult, { id: 'result', type: 'table', document: study, result: { state: 'error', code: 'query_budget_exceeded', path: 'consistency', limit: 'rows', consistency: 'frozen' }, fixedStudy: true, onUseCurrentReads() {} }),
    /Too many rows to fix \(more than 50,000\)\. Use current reads\.<\/p><button type="button">Use current reads<\/button>/);
  assert.match(html(blocks.ResearchBlockResult, { id: 'result', type: 'table', document: study, result: { state: 'error', code: 'query_budget_exceeded', path: 'consistency', limit: 'memory', consistency: 'frozen' }, fixedStudy: true, onUseCurrentReads() {} }),
    /Too much data to fix as one list \(over the server’s memory limit\)\. Use current reads\.<\/p><button type="button">Use current reads<\/button>/);
  // Selected IDs feeding a table: "N selected IDs · rows found: M"; 2–4 IDs show side by side.
  const picked = structuredClone(study); picked.graph.nodes.splice(1, 0, { id: 'picked', type: 'selection', parameters: { mode: 'include', ids: [vid(1), vid(2), vid(9)] } });
  picked.graph.edges = [{ from: 'source', to: 'picked', input: 'query', output: 'query' }, { from: 'picked', to: 'result', input: 'query', output: 'query' }]; picked.graph.layout.picked = { x: 0, y: 0 };
  const lastPage = graph.parseBlockPage(tagged(page({ ids: [vid(1), vid(2)] })), filters);
  const side = html(blocks.ResearchBlockResult, { id: 'result', type: 'table', document: picked, result: { ...node, value: lastPage }, fixedStudy: false });
  assert.match(side, /3 selected IDs · rows found: 2/); assert.match(side, /<h4>Side by side<\/h4>/); assert.match(side, /No score or rank is calculated\./);
  // An operation block in a fixed-list study says it reads current data.
  const summary = lifetime.captureSourceLifetime({ version: 1, operation: operations.summaryOperation, operation_fingerprint: 'f'.repeat(64),
    context: { source_id: research.videoSource, evaluated_at: at, valid_before: until, consistency: 'current' }, coverage: { status: 'complete', reasons: {} },
    result: { population: 0, metrics: Object.fromEntries(operations.metrics.map(m => [m, { definition_epoch: null, state: 'no_known_values', known_count: 0, missing_count: 0, total: null, mean: null, median: null, q1: null, q3: null, min: null, max: null, buckets: [] }])) },
    source_read_at: iso(now), source_expires_at: until }, undefined, performance.now());
  const calc = html(blocks.ResearchBlockResult, { id: 'fp', type: 'summary', document: study, result: { state: 'ready', output: 'operation', consistency: 'current', value: operations.parseOperation(summary, operations.summaryOperation, research.videoSource) }, fixedStudy: true });
  assert.match(calc, /Calculated from current data at .* UTC with the same conditions, not from the fixed list\./);
  // Comparison of selected video rows.
  const compare = html(controls.ResearchCompare, { rows: current.rows, columns, channels: false, level: 4 });
  assert.match(compare, /aria-label="Selected video comparison"><h4>Compare selected videos<\/h4>/); assert.match(compare, /<th>Title v0000000001 · v0000000001<\/th>/);
  assert.doesNotMatch(compare, /Subscribers may be rounded/);
}
console.log('Result blocks: time labels, coverage in words, Details, compare, side by side, fixed positions, errors and calculation notes.');

// ---- 6. Paging one block: current cursor stack, fixed-list cursors, preparation retry. ----
{
  server = async (path, body) => { assert.equal(path, '/research/queries/execute'); return tagged(page({ ids: [vid(3)], more: false })); };
  sent.length = 0;
  const next = await model.loadBlockPage({ ...study.query, consistency: 'current', page: { size: 2 } }, filters, 'cur-0', new AbortController().signal);
  assert.equal(sent[0].body.page.after, 'cur-0'); assert.equal(sent[0].body.page.size, 2); assert.equal(next.rows[0].id, vid(3));
  await assert.rejects(() => model.loadBlockPage({ ...study.query, consistency: 'frozen', page: { size: 2 } }, filters, undefined, new AbortController().signal), /pages with its own cursors/);
  let attempts = 0;
  server = async () => { if (++attempts < 2) throw new ExplorerApiError('preparing', 503, 'catalogue_preparing'); return tagged(page({ ids: [vid(4)], frozen: true, start: 2, total: 3, evaluated: listAt, valid: listUntil })); };
  const realTimeout = globalThis.setTimeout; globalThis.setTimeout = (fn) => realTimeout(fn, 0);
  try {
    let prepared = 0;
    const frozenPage = await model.loadBlockPage({ ...study.query, consistency: 'frozen', page: { size: 2 } }, filters, 'next-0', new AbortController().signal, () => prepared++);
    assert.equal(attempts, 2); assert.equal(prepared, 1); assert.equal(frozenPage.page.start_index, 2); assert.equal(model.pagePosition(frozenPage, 2, false), '3–3 of 3');
  } finally { globalThis.setTimeout = realTimeout; }
  server = async () => { throw new ExplorerApiError('expired', 409, 'cursor_expired'); };
  await assert.rejects(() => model.loadBlockPage({ ...study.query, consistency: 'frozen', page: { size: 2 } }, filters, 'next-0', new AbortController().signal), e => e.code === 'cursor_expired');
  // The retry's bounds (checked on the removed catalogue page's loader before): three attempts and two 2 s
  // waits at most, no retry for any other refusal, and a wait cancelled by the reader leaves no timer behind.
  const realSet = globalThis.setTimeout, realClear = globalThis.clearTimeout, delays = [], timers = new Set();
  let hold = false, calls = 0;
  globalThis.setTimeout = (fn, ms) => { const id = {}; timers.add(id); delays.push(ms); if (!hold) queueMicrotask(() => { timers.delete(id); fn(); }); return id; };
  globalThis.clearTimeout = (id) => { timers.delete(id); };
  const currentQuery = { ...study.query, consistency: 'current', page: { size: 2 } };
  try {
    server = async () => { calls++; throw new ExplorerApiError('preparing', 503, 'catalogue_preparing'); };
    await assert.rejects(model.loadBlockPage(currentQuery, filters, undefined, new AbortController().signal), e => e.code === 'catalogue_preparing');
    assert.equal(calls, 3); assert.deepEqual(delays, [2000, 2000]);
    for (const code of ['policy_denied', 'backend_unavailable', 'query_busy', 'query_budget_exceeded', 'cursor_expired']) {
      calls = 0; delays.length = 0;
      server = async () => { calls++; throw new ExplorerApiError(code, 409, code); };
      await assert.rejects(model.loadBlockPage(currentQuery, filters, undefined, new AbortController().signal), e => e.code === code);
      assert.equal(calls, 1); assert.equal(delays.length, 0);
    }
    hold = true; calls = 0;
    server = async () => { calls++; throw new ExplorerApiError('preparing', 503, 'catalogue_preparing'); };
    const controller = new AbortController(), pending = model.loadBlockPage(currentQuery, filters, undefined, controller.signal);
    for (let tick = 0; tick < 20 && !timers.size; tick++) await Promise.resolve();
    assert.equal(timers.size, 1);
    controller.abort(); await assert.rejects(pending, /abort/i);
    assert.equal(timers.size, 0); assert.equal(calls, 1);
  } finally { globalThis.setTimeout = realSet; globalThis.clearTimeout = realClear; }
}
console.log('Paging: the block query with page.after, fixed lists only by their cursors, bounded preparation retry.');

// ---- 7. Run protocol: run options sent, fixed and current blocks parsed, the reference kept for the next run. ----
{
  const run = graph.withSourceQuery(graph.researchTemplate(study.query, 'evidence'), { page: { size: 2 }, select: columns });
  run.graph.nodes.push({ id: 'fp', type: 'summary', parameters: {} }); run.graph.edges.push({ from: 'source', to: 'fp', input: 'query', output: 'query' }); run.graph.layout.fp = { x: 0, y: 200 };
  const fixedPage = page({ ids: [vid(1), vid(2)], frozen: true, total: 1234, more: true, evaluated: listAt, valid: listUntil });
  const summary = { version: 1, operation: operations.summaryOperation, operation_fingerprint: 'f'.repeat(64), context: { source_id: research.videoSource, evaluated_at: at, valid_before: until, consistency: 'current' },
    coverage: { status: 'complete', reasons: {} }, result: { population: 0, metrics: Object.fromEntries(operations.metrics.map(m => [m, { definition_epoch: null, state: 'no_known_values', known_count: 0, missing_count: 0, total: null, mean: null, median: null, q1: null, q3: null, min: null, max: null, buckets: [] }])) } };
  const reply = (overrides = {}) => ({ version: 1, graph_fingerprint: 'b'.repeat(64), status: 'ready',
    context: { evaluated_at: at, valid_before: until, consistency: 'fixed_source_list' }, budgets: {}, warnings: ['…'],
    run: { consistency: 'frozen', fixed: { reused: true, evaluated_at: listAt, valid_before: listUntil, total_count: 1234 } },
    nodes: { source: { state: 'ready', output: 'query', grain: 'video' }, conditions: { state: 'ready', output: 'query', grain: 'video' },
      result: { state: 'ready', output: 'page', consistency: 'frozen', query: { ...run.query, consistency: 'frozen', page: { size: 2 } }, value: fixedPage, elapsed_ms: 2 },
      fp: { state: 'ready', output: 'operation', consistency: 'current', value: summary, elapsed_ms: 1 } }, ...overrides });
  const answer = value => lifetime.captureSourceLifetime({ ...value, source_read_at: iso(now), source_expires_at: value.context.valid_before }, undefined, performance.now());
  sent.length = 0; server = async () => answer(reply());
  const result = await graph.runResearch(run, new AbortController().signal, { consistency: 'frozen', frozen_ref: 'library-ref' });
  assert.deepEqual(sent[0].body.run, { consistency: 'frozen', frozen_ref: 'library-ref' }); assert.equal(sent[0].path, '/research/graphs/execute');
  assert.deepEqual(Object.keys(sent[0].body).sort(), ['graph', 'query', 'run', 'version'], 'the run option travels next to the study, never inside it');
  assert.equal(result.context.consistency, 'fixed_source_list'); assert.equal(result.nodes.result.consistency, 'frozen'); assert.equal(result.nodes.fp.consistency, 'current');
  assert.equal(result.nodes.result.value.page.total_count, 1234);
  assert.deepEqual(model.fixedReferenceOf(result), { ref: 'list-' + listAt, evaluated_at: listAt, valid_before: listUntil, total_count: 1234 });
  assert.equal(model.fixedListLine(result.run.fixed, true, false), `Fixed list from Library · 1,234 videos · fixed at ${listAt.slice(11, 19)} UTC · until ${listUntil.slice(11, 19)} UTC`);
  // Each block keeps its own lifetime: the fixed list outlives the run's earliest deadline.
  assert.equal(lifetime.ownSourceLifetime(result.nodes.result.value).expiresAt, Date.parse(listUntil));
  assert.equal(lifetime.ownSourceLifetime(result.nodes.fp.value).expiresAt, Date.parse(until));
  // Strict wire: a fixed calculation, a fixed block in a current run, a reused list without a reference, a missing paging query.
  const bad = [
    r => { r.nodes.fp.consistency = 'frozen'; },
    r => { r.run.fixed = null; },
    r => { delete r.nodes.result.query; },
    r => { r.nodes.result.query.consistency = 'current'; },
    r => { r.context.consistency = 'sequential_current_reads'; },
    r => { delete r.nodes.fp.consistency; },
  ];
  for (const mutate of bad) { const r = reply(); mutate(r); server = async () => answer(r); await assert.rejects(() => graph.runResearch(run, new AbortController().signal, { consistency: 'frozen', frozen_ref: 'library-ref' }), String(mutate)); }
  { const r = reply(); server = async () => answer(r); await assert.rejects(() => graph.runResearch(run, new AbortController().signal, { consistency: 'frozen' }), 'reused needs a reference'); }
  // Without run options the reply is today's: no run, no consistency per block.
  const plain = reply({ context: { evaluated_at: at, valid_before: until, consistency: 'sequential_current_reads' } }); delete plain.run;
  plain.nodes.result = { state: 'ready', output: 'page', value: page({ ids: [vid(1)] }), elapsed_ms: 1 }; plain.nodes.fp = { state: 'ready', output: 'operation', value: summary, elapsed_ms: 1 };
  sent.length = 0; server = async () => answer(plain);
  const old = await graph.runResearch(run, new AbortController().signal);
  assert.equal(Object.hasOwn(sent[0].body, 'run'), false); assert.equal(old.nodes.result.value.rows.length, 1); assert.equal(model.fixedReferenceOf(old), undefined);
  // A block error of the fixed list carries its path; the rest of the run answers.
  const partial = reply({ status: 'partial', run: { consistency: 'frozen', fixed: null }, context: { evaluated_at: at, valid_before: until, consistency: 'sequential_current_reads' } });
  partial.nodes.result = { state: 'error', code: 'query_busy', consistency: 'frozen', elapsed_ms: 0 };
  server = async () => answer(partial);
  const busy = await graph.runResearch(run, new AbortController().signal, { consistency: 'frozen' });
  assert.equal(busy.nodes.result.code, 'query_busy'); assert.equal(model.blockError(busy.nodes.result.code), 'The server is busy with other reads or lists. Run again shortly.');
  // Refusals of the reference become the choice, never a silent replacement.
  assert.deepEqual(handoff.fixedListRefusal('cursor_expired'), { action: 'choose', notice: 'The fixed list is no longer available: data or access changed.' });
  assert.deepEqual(handoff.EXPIRY_CHOICES.map(c => c.label), ['Fix a new list', 'Use current reads']);
  assert.equal(model.studyError(new ExplorerApiError('x', 422, 'query_budget_exceeded')), 'The study reached its limits (30 seconds or 4 MiB of results). Remove blocks or narrow the source, then run again.');
  // A failed Run research speaks the workshop's words and names the action it has (Run research),
  // never the old catalogue page's "refresh"; a lost connection is said plainly.
  const offline = Object.assign(new Error('No connection to the Leti Cook server.'), { name: 'ExplorerNetworkError' });
  for (const [error, text] of [
    [new ExplorerApiError('The server could not complete the request (HTTP 500).', 500), 'The server could not complete the request (HTTP 500). Run research again.'],
    [new ExplorerApiError('The server could not complete the request (HTTP 502).', 502), 'The server could not complete the request (HTTP 502). Run research again.'],
    [offline, 'No connection to the Leti Cook server. Run research again.'],
    [new ExplorerApiError('x', 503, 'backend_unavailable'), 'Stored data is temporarily unavailable. Run again shortly.'],
    [new ExplorerApiError('x', 503, 'hot_runtime_not_ready'), 'Stored data is temporarily unavailable. Run again shortly.'],
    [new ExplorerApiError('x', 429, 'query_busy'), 'The server is busy with other reads or lists. Run again shortly.'],
    [new ExplorerApiError('x', 503, 'catalogue_preparing'), 'Stored data is being prepared in the background. Run again shortly.'],
    [new ExplorerApiError('x', 409, 'cursor_expired'), 'This list expired. Run research to read it again.'],
    [new ExplorerApiError('x', 418, 'teapot'), 'The request was not accepted (HTTP 418). Run research again.'],
    [new ExplorerApiError('x', 408, 'client_timeout'), 'The request timed out. Run research again.'],
    [new Error('Unsupported or invalid research definition.'), 'The study could not run. No result is shown. Run research again.'],
  ]) assert.equal(model.studyError(error), text, String(error.code ?? error.message));
  assert.equal(research.researchError(new Error('x')), 'The server’s answer could not be read. Reload the page to try again.');
  // A block page shows a reply check's own text: it names no Refresh either.
  for (const file of ['researchQuery.ts', 'researchOperations.ts']) {
    assert.doesNotMatch(readFileSync(new URL('../application/frontend/src/explorer/' + file, import.meta.url), 'utf8'), /Refresh to try again|catalogue response/, file);
  }
  // Elsewhere in the workshop the same reasons name that place's action.
  assert.equal(research.researchError(new ExplorerApiError('x', 500)), 'The server could not complete the request (HTTP 500). Reload the page to try again.');
  assert.equal(research.researchError(offline, 'Check connections again.'), 'No connection to the Leti Cook server. Check connections again.');
  for (const code of ['catalogue_preparing', 'query_busy', 'cursor_expired', 'cursor_context_changed', 'backend_unavailable', 'unknown_code', undefined]) {
    for (const text of [research.researchError(new ExplorerApiError('x', 503, code)), model.studyError(new ExplorerApiError('x', 503, code))]) {
      assert.doesNotMatch(text, /[Rr]efresh|catalogue could not/, `${code}: ${text}`);
    }
  }
  const workshop = readFileSync(new URL('../application/frontend/src/explorer/ResearchWorkshop.tsx', import.meta.url), 'utf8');
  assert.match(workshop, /setMessage\(researchError\(error, 'Check connections again\.'\)\)/, 'Check connections names itself');
}
console.log('Graph run options: run options next to the study, fixed and current blocks, per-block lifetimes, strict replies, refusals as a choice.');

// ---- 8. 28-day growth fields from a Library hand-off parse and show (Library sorts by views gained). ----
{
  const growthColumns = ['video.title', 'observation.views_gained_28d', 'observation.growth_28d_status'];
  const q = graph.withSourceQuery(base, { select: growthColumns, order_by: [{ field: 'observation.views_gained_28d', direction: 'desc', nulls: 'last' }], page: { size: 2 } });
  const f = graph.blockFilters(q, 'result');
  const evidence = { method: 'video.observed_growth_28d.v1', version: 1, status: 'partial', reason: 'window_shorter_than_28_days', from_at: listAt, to_at: at, from_views: '10', to_views: '25',
    actual_interval_seconds: '86400', boundary_distance_seconds: '0', tracked_days: '1', definition_epoch: 'e1', from_definition_epoch: 'e1', to_definition_epoch: 'e1', time_zone: 'UTC' };
  const cell = (value, observation) => ({ value, state: 'known', provenance_ref: 'p', observed_at: at, expires_at: until, ...(observation === undefined ? {} : { observation }) });
  const growthPage = { query_version: 1, query_fingerprint: 'c'.repeat(64), columns: growthColumns, context: { source_id: research.videoSource, evaluated_at: at, valid_before: until, consistency: 'current' },
    rows: [{ id: vid(1), cells: { 'video.title': cell('Soup'), 'observation.views_gained_28d': cell('15', evidence), 'observation.growth_28d_status': cell('partial', null) } }],
    provenance: { p: { origin: 'calculated', source_paths: [], method: 'video.observed_growth_28d.v1' } }, coverage: { status: 'complete', basis: 'channel_inventory', channels_in_scope: 1, channels_complete: 1, reasons: {} },
    page: { has_more: false, next_cursor: null } };
  const parsed = graph.parseBlockPage(tagged(growthPage), f);
  assert.equal(rows.displayResearchCell(parsed.rows[0].cells['observation.growth_28d_status'], 'observation.growth_28d_status'), 'Partial history', 'the words of the Library Inspector');
  assert.match(html(rows.SourceDetails, { row: parsed.rows[0], page: parsed }), /28-day growth: partial · observed from/);
  const wrong = structuredClone(growthPage); wrong.rows[0].cells['observation.growth_28d_status'].value = 'great';
  assert.throws(() => graph.parseBlockPage(tagged(wrong), f));
  assert.doesNotMatch(html(controls.ObservationNote, { window: 3600, columns: growthColumns }), /snapshots/, 'growth columns are not change-window observations');
}
console.log('Growth fields of a Library hand-off: parsed, labelled and explained in Data details.');

// ---- 9. a Library hand-off of every format opens a runnable study (real server replies). ----
// tests/library_wire_fixtures_pg.py ran Library's frozen list and then the study the hand-off opens through
// execute_graph on a temporary PostgreSQL 17 (research_growth_pg seed). The capabilities document is the current
// server's (pinned by tests/test_observation_limits.py). Library's select carries its calculations (title length,
// tag count, like and comment rates) and the 28-day growth fields: the workshop must offer every one of them.
{
  const wire = JSON.parse(readFileSync(new URL('./fixtures/library_workshop_wire.json', import.meta.url), 'utf8'));
  const document = JSON.parse(readFileSync(new URL('./fixtures/research_capabilities_window_limits.json', import.meta.url), 'utf8'));
  server = async (path) => { assert.equal(path, '/research/capabilities'); return structuredClone(document); };
  const caps = await research.loadResearchCapabilities(new AbortController().signal);
  // Every field the server offers is kept and labelled (none is dropped as "not available on this server").
  assert.deepEqual(caps.fields.map(f => f.id).sort(), document.fields.map(f => f.id).sort());
  for (const f of document.fields) assert.ok(Object.hasOwn(research.researchLabels, f.id), f.id + ' has a workshop label');
  assert.deepEqual(['video.title_length', 'video.tag_count', 'video.like_rate', 'video.comment_rate'].map(id => research.researchLabels[id]),
    ['Title length', 'Tag count', 'Like rate', 'Comment rate'], 'the Library Inspector names');
  // As requestJson delivers the reply: its HTTP read envelope, received now.
  const answer = value => lifetime.captureSourceLifetime(structuredClone(value), { get: name => name === 'X-Source-Expires-At' ? value.source_expires_at
    : name === 'X-Source-Read-At' ? value.source_read_at : null }, performance.now());
  // The replies were read when the fixture was generated: the browser clock is set to that read.
  const atRead = async (reply, work) => { const realNow = Date.now; Date.now = () => Date.parse(reply.source_read_at); try { return await work(); } finally { Date.now = realNow; } };
  const channel = wire.long_selection.library_request.scope.id;
  for (const kind of ['long', 'short', 'stream']) {
    const item = wire[`${kind}_selection`];
    const origin = { surface: 'library', channel_id: channel, channel_title: 'Growth fixture', format: kind, view: 'grid' };
    const whole = handoff.selectionHandoff(item.library_request, item.library_frozen, origin);
    assert.equal(whole.kind, 'selection', kind);
    const study = handoff.studyFromHandoff(whole);
    // The study the workshop opens is exactly the one the server ran.
    assert.deepEqual({ ...study.document, run: study.run }, item.request, kind);
    for (const id of ['video.title_length', 'video.tag_count', 'video.like_rate', 'video.comment_rate']) assert.ok(study.document.query.select.includes(id), id);
    // Runnable: a chosen source, no study problems, every field offered for this format (ResearchWorkshop `runnable`).
    assert.equal(research.validScope(study.document.query.scope), true);
    assert.deepEqual(graph.studyProblems(study.document), [], kind);
    assert.equal(blocks.studyFieldIssue(study.document, caps), '', kind + ': ' + blocks.studyFieldIssue(study.document, caps));
    // The run on arrival reads the real reply: the gallery shows Library's rows with the calculated values.
    sent.length = 0; server = async (path, body) => { assert.equal(path, '/research/graphs/execute'); assert.deepEqual(body, item.request); return answer(item.response); };
    const result = await atRead(item.response, () => graph.runResearch(study.document, new AbortController().signal, study.run));
    const gallery = result.nodes.result;
    assert.equal(gallery.state, 'ready', kind);
    assert.ok(gallery.value.rows.length > 0, kind);
    const shown = gallery.value.rows.map(row => ['video.like_rate', 'video.title_length', 'video.tag_count'].map(id => rows.displayResearchCell(row.cells[id], id)));
    assert.ok(shown.some(([rate]) => /%$/.test(rate)), kind + ': like rate as a percentage');
    assert.ok(shown.every(([, length]) => /characters$|Unknown|Expired|Unavailable/.test(length)), kind);
    const page = await atRead(item.response, () => html(blocks.ResearchBlockResult, { id: 'result', type: 'gallery', document: study.document, result: gallery, fixedStudy: true }));
    assert.doesNotMatch(page, /Not available on this server/);
    assert.match(page, /Like rate/);
  }
  // "Send selected to workshop": the selected IDs as their own study, current reads, also runnable.
  const ids = wire.long_selected_ids;
  const origin = { surface: 'library', channel_id: channel, channel_title: 'Growth fixture', format: 'long', view: 'grid' };
  const selected = handoff.studyFromHandoff(handoff.selectedIdsHandoff(ids.library_request, ids.ids.map(id => ({ id, type: 'long' })), origin));
  assert.deepEqual({ ...selected.document, run: selected.run }, ids.request);
  assert.equal(blocks.studyFieldIssue(selected.document, caps), '');
  server = async () => answer(ids.response);
  const table = (await atRead(ids.response, () => graph.runResearch(selected.document, new AbortController().signal, selected.run))).nodes.result;
  assert.deepEqual(table.value.rows.map(row => row.id).sort(), [...ids.ids].sort());
  // Without the labels (the old capabilities parser kept only labelled fields) the study could not run.
  assert.equal(blocks.studyFieldIssue(wire.long_selection.request, { ...caps, fields: caps.fields.filter(f => !['video.like_rate'].includes(f.id)) }),
    'Disabled: Like rate. Remove them or enable them in Collection fields.');
}
console.log('Library hand-offs of Long, Shorts and Streams open runnable studies; every server field is labelled; real graph replies parse.');

// ---- 10. Workshop layout regressions. ----
{
  const wire = JSON.parse(readFileSync(new URL('./fixtures/library_workshop_wire.json', import.meta.url), 'utf8'));
  const css = readFileSync(new URL('../application/frontend/src/explorer/researchWorkshop.css', import.meta.url), 'utf8');
  const rule = selector => { const start = css.indexOf(selector + '{'); assert.ok(start >= 0, selector); return css.slice(start, css.indexOf('}', start)); };
  // long titles wrap inside their column (the cell is marked; the class lets it wrap, other cells stay on one line).
  const item = wire.long_selection;
  const origin = { surface: 'library', channel_id: item.library_request.scope.id, format: 'long', view: 'table' };
  const study = handoff.studyFromHandoff(handoff.selectionHandoff(item.library_request, item.library_frozen, origin));
  assert.equal(study.document.graph.nodes[1].type, 'table');
  const answer = value => lifetime.captureSourceLifetime(structuredClone(value), { get: name => name === 'X-Source-Expires-At' ? value.source_expires_at
    : name === 'X-Source-Read-At' ? value.source_read_at : null }, performance.now());
  const realNow = Date.now; Date.now = () => Date.parse(item.response.source_read_at);
  try {
    server = async () => answer(item.response);
    const table = (await graph.runResearch(study.document, new AbortController().signal, study.run)).nodes.result;
    const markup = html(blocks.ResearchBlockResult, { id: 'result', type: 'table', document: study.document, result: table, fixedStudy: true });
    assert.equal((markup.match(/<td class="research-text-cell">/g) ?? []).length, table.value.rows.length, 'one wrapped title cell per row');
    assert.match(markup, /<td>\d[^<]*<\/td>/, 'numbers and dates stay plain cells');
  } finally { Date.now = realNow; }
  assert.deepEqual(blocks.WRAPPED_FIELDS, ['video.title', 'channel.title', 'video.description', 'video.tags', 'video.topic_urls']);
  assert.match(rule('.research-workshop .research-table-wrap td.research-text-cell'), /white-space:normal!important;overflow-wrap:anywhere/);
  // A gallery shows images: the study's columns gain the row image when a block becomes a gallery.
  const plainStudy = graph.withScope(graph.newStudy(), { kind: 'folder', id: folder });
  assert.equal(plainStudy.query.select.includes('video.thumbnail_url'), false, 'a new table study keeps its starting columns');
  const shown = graph.withGalleryImage(plainStudy.query);
  assert.deepEqual(shown.select, [...plainStudy.query.select, 'video.thumbnail_url']);
  assert.equal(graph.withGalleryImage(shown), shown, 'once is enough');
  assert.deepEqual(graph.withGalleryImage(graph.entityQuery(plainStudy.query, research.channelSource)).select.at(-1), 'channel.avatar_url');
  assert.ok(graph.newStudy({ view: 'gallery', columns: ['video.title', 'video.views'] }).query.select.includes('video.thumbnail_url'));
  const workshopSource = readFileSync(new URL('../application/frontend/src/explorer/ResearchWorkshop.tsx', import.meta.url), 'utf8');
  assert.match(workshopSource, /query:type==='gallery'\?withGalleryImage\(document\.query\):document\.query/, 'Show as Gallery adds the image column');
  // The calendar block's hour rows keep a gap and a right-aligned count: "23" and "01:00" never read as "2301:00".
  assert.match(rule('.research-workshop .research-bars'), /column-gap:32px/);
  assert.match(rule('.research-workshop .research-bar>span:last-child'), /min-width:4ch;text-align:right/);
  // "Import definition" has the size of the buttons beside it (the app's controls: 11 px, 36 px high).
  assert.match(rule('.explorer-app.is-updated .research-workshop .research-file'), /min-height:36px;.*font-size:11px/);
  // and in their place: the row centres its buttons (beside the taller Name field), so the label is
  // centred too; at the row's end it stood 11 px lower (858 vs 869 px).
  const catalogueCss = readFileSync(new URL('../application/frontend/src/explorer/researchCatalogue.css', import.meta.url), 'utf8');
  assert.ok(catalogueCss.includes('.research-actions,.research-sort,.research-condition{display:flex;align-items:center;'), 'the row centres its items');
  for (const selector of ['.research-workshop .research-file', '.explorer-app.is-updated .research-workshop .research-file']) {
    assert.match(rule(selector), /align-self:center;/, selector);
  }
  assert.doesNotMatch(css, /research-file\{[^}]*align-self:end/);
}
console.log('Workshop block layout and definition import controls passed.');

// ---- 11. Library's growth fields read in the workshop as Library shows them. ----
{
  const wire = JSON.parse(readFileSync(new URL('./fixtures/library_workshop_wire.json', import.meta.url), 'utf8'));
  const [STATUS, GAINED, PER_DAY, INTERVAL, DAYS] = ['observation.growth_28d_status', 'observation.views_gained_28d', 'observation.views_per_day_28d',
    'observation.growth_28d_interval_seconds', 'observation.growth_28d_tracked_days'];
  const fields = [STATUS, GAINED, PER_DAY, INTERVAL, DAYS];
  const show = (row, id) => rows.displayResearchCell(row.cells[id], id);
  const answer = value => lifetime.captureSourceLifetime(structuredClone(value), { get: name => name === 'X-Source-Expires-At' ? value.source_expires_at
    : name === 'X-Source-Read-At' ? value.source_read_at : null }, performance.now());
  const realNow = Date.now;
  let compared = 0;
  try {
    for (const [name, format] of [['long_selection', 'long'], ['short_selection', 'short'], ['stream_selection', 'stream']]) {
      const item = wire[name];
      const origin = { surface: 'library', channel_id: item.library_request.scope.id, format, view: 'table' };
      const study = handoff.studyFromHandoff(handoff.selectionHandoff(item.library_request, item.library_frozen, origin));
      Date.now = () => Date.parse(item.response.source_read_at);
      server = async () => answer(item.response);
      const table = (await graph.runResearch(study.document, new AbortController().signal, study.run)).nodes.result;
      for (const row of table.value.rows) {
        const status = row.cells[STATUS].value;
        // The Inspector's status words and interval; the card's signed compact change ("+583.3K over ~26.6 days").
        assert.equal(show(row, STATUS), videoGrowth.GROWTH_STATUS_LABELS[status], row.id);
        if (row.cells[INTERVAL].state === 'known') assert.equal(show(row, INTERVAL), videoGrowth.observedInterval(Number(row.cells[INTERVAL].value)), row.id);
        if (status !== 'ready' && status !== 'partial') continue;
        const card = videoGrowth.presentVideoViewGrowth({ status, delta_views: row.cells[GAINED].value, actual_interval_seconds: Number(row.cells[INTERVAL].value),
          tracked_days: Number(row.cells[DAYS].value), observed_start_at: null, observed_end_at: null, boundary_distance_seconds: null, reason: null });
        assert.ok(card.text.startsWith(show(row, GAINED) + ' over '), `${row.id}: ${card.text} / ${show(row, GAINED)}`);
        assert.match(show(row, PER_DAY), /^(~[+-][\d,.]+|0) views\/day$/, row.id);
        assert.match(show(row, DAYS), /^\d[\d,]* days?$/, row.id);
        compared += 1;
      }
      if (format === 'long') {
        const byId = Object.fromEntries(table.value.rows.map(row => [row.id, row]));
        assert.deepEqual(fields.map(id => show(byId.gLbg0100000, id)), ['Partial history', '+196', '~+168 views/day', '1d 4h', '1 day']);
        assert.deepEqual(fields.map(id => show(byId.gLtie100000, id)), ['Partial history', '+1', '~+0.12 views/day', '8d', '8 days']);
        assert.deepEqual(fields.map(id => show(byId.gLtoday0000, id)), ['Tracking started today', 'Unavailable', 'Unavailable', '3h 54m 45s', 'Unavailable']);
        assert.deepEqual(fields.map(id => show(byId.gLwaitone00, id)), ['Waiting for next snapshot', 'Unavailable', 'Unavailable', '0s', 'Unavailable']);
      }
    }
  } finally { Date.now = realNow; }
  assert.ok(compared >= 12, 'growth rows of every format were compared');
  // Format counts and rates for display.
  const known = value => ({ value, state: 'known', provenance_ref: 'p', observed_at: null, expires_at: null });
  assert.deepEqual([[GAINED, '5431'], [PER_DAY, '386.53'], [INTERVAL, '1213967'], [DAYS, '14']].map(([id, value]) => rows.displayResearchCell(known(value), id)),
    ['+5.4K', '~+386.53 views/day', '14d 1h 12m 47s', '14 days']);
  assert.equal(rows.displayResearchCell(known('-120'), GAINED), '-120', 'a correction keeps its sign');
  assert.equal(rows.displayResearchCell(known('0'), GAINED), '0');
  assert.equal(rows.displayResearchCell(known('-2.5'), PER_DAY), '~-2.5 views/day');
  assert.equal(rows.displayResearchCell(known('0.00'), PER_DAY), '0 views/day');
  assert.equal(rows.displayResearchCell(known('1234567.5'), PER_DAY), '~+1,234,567.5 views/day');
  assert.equal(rows.displayResearchCell(known('ready'), STATUS), '28-day comparison available');
  assert.equal(research.researchLabels[INTERVAL], 'Growth interval', 'the column label does not say seconds');
  const conditionSource = readFileSync(new URL('../application/frontend/src/explorer/ResearchConditions.tsx', import.meta.url), 'utf8');
  assert.match(conditionSource, /field\?\.id === 'observation\.growth_28d_interval_seconds' \? 'Seconds \(whole number\)'/, 'a condition on it still takes seconds');
}
console.log('Library growth fields in workshop rows read as in Library (status and interval as the Inspector, views gained as the card).');
