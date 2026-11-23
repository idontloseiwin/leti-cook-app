// Research workshop entry contract: Library hand-off, budgets, fixed-list lifetime,
// tab history mirror, opening Saved items and workshop addresses. Pure functions, no network.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from '../application/frontend/node_modules/typescript/lib/typescript.js';
import * as lifetime from '../application/frontend/src/sourceLifetime.ts';
import * as routes from '../application/frontend/src/explorerRoutes.ts';
import * as catalogue from '../application/frontend/src/explorer/catalogueQuery.ts';
import * as savedLibrary from '../application/frontend/src/explorer/libraryData/savedLibrary.ts';
function compiled(file, imports = {}) {
  const { outputText } = ts.transpileModule(readFileSync(new URL('../application/frontend/src/explorer/' + file, import.meta.url), 'utf8'),
    { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } });
  const exports = {}; new Function('require', 'exports', outputText)(name => { assert.ok(name in imports, name); return imports[name]; }, exports); return exports;
}
const saved = compiled('savedWork.ts');
const research = compiled('researchQuery.ts', { './api': {}, '../sourceLifetime': lifetime });
const definition = compiled('savedQueryDefinition.ts', { './researchQuery': research, './savedWork': saved, './timeZones': compiled('timeZones.ts') });
const operations = compiled('researchOperations.ts', { './api': {}, '../sourceLifetime': lifetime });
const graph = compiled('researchGraph.ts', { './api': {}, './savedWork': saved, './savedQueryDefinition': definition, './researchQuery': research, './researchOperations': operations, '../sourceLifetime': lifetime });
const views = compiled('savedViews.ts', { './researchGraph': graph, './api': {}, './savedQueryDefinition': definition, '../explorerRoutes': routes, './catalogueQuery': catalogue, './savedWork': saved, './timeZones': compiled('timeZones.ts'), './libraryData/savedLibrary.ts': savedLibrary });
const handoff = compiled('workshopHandoff.ts', { './savedQueryDefinition': definition, './researchQuery': research, './researchGraph': graph, './savedViews': views, './savedWork': saved });

const channel = 'UC' + 'a'.repeat(22), other = 'UC' + 'b'.repeat(22);
const videoId = n => n.toString(36).padStart(11, '_');
const ids = n => Array.from({ length: n }, (_, i) => videoId(i));
const origin = { surface: 'library', channel_id: channel, channel_title: 'Cooking Lab', format: 'long', view: 'grid' };
// What Library executed for a frozen first page (9 columns, 3 conditions, viewer zone).
const libraryQuery = { query_version: 1, source: { id: research.videoSource }, scope: { kind: 'channel', id: channel }, video_types: ['long'],
  select: ['video.id', 'video.channel_id', 'video.type', 'video.title', 'video.thumbnail_url', 'video.published_at', 'video.duration_seconds', 'video.views', 'video.likes'],
  where: { all: [{ field: 'video.views', op: 'gte', value: '1000' }, { field: 'video.duration_seconds', op: 'gte', value: '60' }, { field: 'video.title', op: 'contains_casefold_v1', value: 'soup' }] },
  order_by: [{ field: 'video.published_at', direction: 'desc', nulls: 'last' }], time: { mode: 'current', timezone: 'Europe/Moscow' },
  coverage: { mode: 'available' }, consistency: 'frozen', page: { size: 50, anchor_id: 'abcdefghijk' } };
const frozenPage = { context: { frozen_ref: 'sealed.' + 'x'.repeat(300), evaluated_at: '2026-10-29T10:00:12Z', valid_before: '2026-10-29T10:10:12Z' }, page: { total_count: 1234 } };
const noRef = value => assert.doesNotMatch(JSON.stringify(value), /sealed\.|frozen_ref/);

// 1. The whole selection: Library's exact query, never visible page IDs; the reference only in memory.
{
  const whole = handoff.selectionHandoff(libraryQuery, frozenPage, origin);
  assert.equal(whole.kind, 'selection');
  const expected = structuredClone(libraryQuery); delete expected.page.anchor_id; expected.consistency = 'current';
  assert.deepEqual(whole.query, expected);
  assert.deepEqual(whole.frozen, { ref: frozenPage.context.frozen_ref, evaluated_at: '2026-10-29T10:00:12Z', valid_before: '2026-10-29T10:10:12Z', total_count: 1234 });
  assert.equal(Object.hasOwn(whole, 'ids'), false);
  const after = handoff.selectionHandoff({ ...libraryQuery, page: { size: 50, after: 'cursor-2' } }, null, origin);
  assert.deepEqual(after.query.page, { size: 50 });
  assert.equal(Object.hasOwn(after, 'frozen'), false);
  assert.deepEqual(libraryQuery.page, { size: 50, anchor_id: 'abcdefghijk' }, 'Library state is not changed');
  const study = handoff.studyFromHandoff(whole);
  assert.deepEqual(study.document.query, whole.query, 'the study root query is Library\'s query');
  assert.deepEqual(study.document.graph.nodes.map(n => n.type), ['source', 'gallery']);
  assert.deepEqual(study.run, { consistency: 'frozen', frozen_ref: frozenPage.context.frozen_ref });
  assert.equal(study.autoRun, true);
  assert.equal(study.name, 'Cooking Lab · Long-form selection');
  noRef(study.document); noRef(study.name); noRef(study.notice);
  assert.deepEqual(handoff.studyFromHandoff(after).run, { consistency: 'frozen' });
  assert.deepEqual(handoff.studyFromHandoff(handoff.selectionHandoff(libraryQuery, null, { ...origin, view: 'table' })).document.graph.nodes[1].type, 'table');
  // The study is a valid saved v3 and passes the browser's own checks.
  assert.equal(graph.researchDefinition(study.document).conditions.query.scope.id, '$channel');
  const kept = handoff.withoutReference(whole);
  assert.equal(Object.hasOwn(kept, 'frozen'), false); noRef(kept);
  assert.ok(whole.frozen, 'withoutReference copies; the in-memory hand-off keeps its reference');
  for (const [query, from] of [[{ ...libraryQuery, scope: { kind: 'channel', id: other } }, origin], [{ ...libraryQuery, video_types: ['short'] }, origin],
    [{ ...libraryQuery, source: { id: research.channelSource } }, origin], [{ ...libraryQuery, select: [] }, origin], [null, origin],
    [libraryQuery, { ...origin, surface: 'canvas' }], [libraryQuery, { ...origin, channel_id: 'UC1' }], [libraryQuery, { ...origin, format: 'live' }]])
    assert.deepEqual(handoff.selectionHandoff(query, null, from), { refused: 'unsupported_query' });
  for (const bad of [{ ...frozenPage, context: { ...frozenPage.context, frozen_ref: '' } }, { ...frozenPage, context: { ...frozenPage.context, frozen_ref: 'a b' } },
    { ...frozenPage, context: { ...frozenPage.context, valid_before: frozenPage.context.evaluated_at } }, { ...frozenPage, page: { total_count: -1 } },
    { ...frozenPage, context: { ...frozenPage.context, evaluated_at: '29.10.2026' } }])
    assert.deepEqual(handoff.selectionHandoff(libraryQuery, bad, origin), { refused: 'unsupported_query' });
}
console.log('Whole selection: Library query without page tokens, reference only in memory.');

// 2. Explicit selection: IDs only, no Library conditions, one format, nothing cut silently.
{
  const picked = [{ id: videoId(1), type: 'long' }, { id: videoId(2), type: 'short' }, { id: videoId(3) }, { id: videoId(1), type: 'long' }, { id: videoId(4), type: 'stream' }];
  const sent = handoff.selectedIdsHandoff(libraryQuery, picked, origin);
  assert.deepEqual(sent.ids, [videoId(1), videoId(3)]);
  assert.equal(sent.other_formats_excluded, 2);
  const study = handoff.studyFromHandoff(sent);
  assert.equal(study.document.query.where, null);
  assert.deepEqual(study.document.query.select, libraryQuery.select);
  assert.deepEqual(study.document.query.order_by, libraryQuery.order_by);
  assert.equal(study.document.query.time.timezone, 'Europe/Moscow');
  assert.deepEqual(study.document.graph.nodes.map(n => [n.type, n.parameters]), [['source', {}], ['selection', { mode: 'include', ids: sent.ids }], ['table', {}]]);
  assert.deepEqual(study.run, { consistency: 'current' });
  assert.match(study.notice, /^2 IDs from Library\. .* 2 selected videos of other formats are not included: a study has one format\.$/);
  assert.deepEqual(handoff.selectedIdsHandoff(libraryQuery, [], origin), { refused: 'nothing_selected' });
  assert.deepEqual(handoff.selectedIdsHandoff(libraryQuery, [{ id: videoId(9), type: 'short' }], origin), { refused: 'nothing_selected', count: 1 });
  assert.deepEqual(handoff.selectedIdsHandoff(libraryQuery, ids(1001).map(id => ({ id })), origin), { refused: 'too_many', count: 1001 });
  for (const item of [{ id: 'short' }, { id: '../../etc/xx' }, { id: videoId(1), type: 'live' }])
    assert.deepEqual(handoff.selectedIdsHandoff(libraryQuery, [item], origin), { refused: 'unsupported_query' });
  // 1000 video IDs fit a study, as on the server.
  const thousand = handoff.selectedIdsHandoff(libraryQuery, ids(1000).map(id => ({ id, type: 'long' })), origin);
  assert.equal(thousand.ids.length, 1000);
  const big = handoff.studyFromHandoff(thousand).document, size = graph.researchSize(big);
  assert.ok(size.valid && size.bytes <= graph.MAX_RESEARCH_BYTES, JSON.stringify(size));
  assert.equal(graph.researchDefinition(big).conditions.graph.nodes[1].parameters.ids.length, 1000);
  console.log(`1000 selected video IDs: ${size.bytes} of ${graph.MAX_RESEARCH_BYTES} bytes.`);
  // Long notes take the same bytes: the study is refused with its size, never trimmed.
  const noted = structuredClone(big); noted.graph.notes = 'n'.repeat(4096);
  const over = graph.researchSize(noted);
  assert.equal(over.valid, false); assert.ok(over.bytes > graph.MAX_RESEARCH_BYTES);
  // Channel IDs are longer: 600 of them do not fit (about 569 when saved).
  const channelIds = Array.from({ length: 600 }, (_, i) => 'UC' + i.toString(36).padStart(22, '_'));
  const channels = { version: 1, query: { query_version: 1, source: { id: research.channelSource }, scope: { kind: 'label', id: 'monetized' },
    select: ['channel.id', 'channel.title', 'channel.subscribers'], order_by: [{ field: 'channel.subscribers', direction: 'desc' }] },
    graph: { ...big.graph, nodes: big.graph.nodes.map(n => n.type === 'selection' ? { ...n, parameters: { mode: 'include', ids: channelIds } } : n) } };
  const channelSize = graph.researchSize(channels);
  assert.equal(channelSize.valid, false); assert.ok(channelSize.bytes > graph.MAX_RESEARCH_BYTES, String(channelSize.bytes));
  channels.graph.nodes[1].parameters.ids = channelIds.slice(0, 500);
  assert.equal(graph.researchSize(channels).valid, true);
}
console.log('Selected IDs: no conditions, one format, 1000 video IDs fit, oversize refused with bytes.');

// 3. Whole frozen list detection after "Select all matching".
assert.equal(handoff.isWholeFrozenList(new Set(['a', 'b']), ['b', 'a']), true);
assert.equal(handoff.isWholeFrozenList(new Set(['a']), ['a', 'b']), false);
assert.equal(handoff.isWholeFrozenList(new Set(['a', 'c']), ['a', 'b']), false);
assert.equal(handoff.isWholeFrozenList(new Set(), []), false);
assert.equal(handoff.isWholeFrozenList(new Set(['a']), ['a', 'a']), false);

// 4. Fixed-list lifetime: reuse while it lives, ask when it expired or the server refused; never replace silently.
{
  const reference = handoff.selectionHandoff(libraryQuery, frozenPage, origin).frozen;
  const live = handoff.fixedListDecision(reference, Date.parse('2026-10-29T10:05:00Z'));
  assert.deepEqual(live, { action: 'reuse', run: { consistency: 'frozen', frozen_ref: reference.ref },
    notice: 'Fixed list from Library · 1,234 videos · fixed at 10:00:12 UTC · until 10:10:12 UTC' });
  assert.deepEqual(handoff.fixedListDecision(reference, Date.parse('2026-10-29T10:10:12Z')), { action: 'choose', notice: 'Library\'s fixed list expired at 10:10 UTC.' });
  assert.deepEqual(handoff.fixedListDecision(undefined, Date.now()), { action: 'new_list', run: { consistency: 'frozen' }, notice: 'Opened again: Run fixes a new list.' });
  for (const code of ['cursor_expired', 'cursor_context_changed'])
    assert.deepEqual(handoff.fixedListRefusal(code), { action: 'choose', notice: 'The fixed list is no longer available: data or access changed.' });
  for (const code of ['query_busy', 'invalid_query', undefined]) assert.equal(handoff.fixedListRefusal(code), null);
  assert.deepEqual(handoff.EXPIRY_CHOICES.map(c => c.label), ['Fix a new list', 'Use current reads']);
}
console.log('Fixed list: reuse, expiry and refusal choices.');

// 5. Tab history mirror: the document survives reload/Back/Forward; the reference never does.
{
  const whole = handoff.selectionHandoff(libraryQuery, frozenPage, origin), study = handoff.studyFromHandoff(whole);
  const entry = handoff.newWorkshopEntry();
  const state = handoff.writeWorkshopHistory({ scroll: 3 }, { version: 1, entry, document: study.document, name: study.name, saved_id: '11111111-1111-4111-8111-111111111111', handoff: whole });
  assert.equal(state.scroll, 3, 'other history keys are kept');
  noRef(state);
  const copy = structuredClone(state); // what the browser keeps for the entry
  const back = handoff.readWorkshopHistory(copy);
  assert.deepEqual(back, { version: 1, entry, document: study.document, name: study.name, saved_id: '11111111-1111-4111-8111-111111111111', handoff: handoff.withoutReference(whole) });
  assert.equal(handoff.readWorkshopHistory({}), null);
  assert.equal(handoff.readWorkshopHistory({ workshop: { version: 1, entry: 'not-an-entry' } }), null);
  for (const damage of [s => s.workshop.document.graph.rows = [], s => s.workshop.name = 'x'.repeat(121), s => s.workshop.saved_id = 'nope',
    s => s.workshop.handoff.frozen = whole.frozen, s => s.workshop.handoff.query.scope.id = other, s => s.workshop.results = []]) {
    const bad = structuredClone(state); damage(bad);
    assert.deepEqual(handoff.readWorkshopHistory(bad), { version: 1, entry, damaged: true }, 'a damaged mirror keeps only its entry and says so');
  }
  // A new study whose source is not chosen yet is still mirrored; it cannot run or be saved until it is.
  const fresh = graph.newStudy();
  assert.equal(fresh.query.scope.id, '');
  assert.deepEqual(handoff.readWorkshopHistory(handoff.writeWorkshopHistory(null, { version: 1, entry, document: fresh })).document, fresh);
  assert.throws(() => graph.researchDefinition(fresh));
  const chosen = graph.withScope(fresh, { kind: 'folder', id: '22222222-2222-4222-8222-222222222222' });
  assert.deepEqual(graph.researchDefinition(chosen).conditions.graph, fresh.graph);
  assert.throws(() => graph.parseStudyDocument({ ...fresh, query: { ...fresh.query, scope: { kind: 'channel', id: 'UC1' } } }));
  assert.throws(() => graph.parseStudyDocument({ ...fresh, version: 3 }));
  const ids1000 = handoff.selectedIdsHandoff(libraryQuery, ids(1000).map(id => ({ id })), origin);
  assert.equal(handoff.readWorkshopHistory(handoff.writeWorkshopHistory({}, { version: 1, entry, handoff: ids1000 })).handoff.ids.length, 1000);
}
console.log('History mirror: document, name, Saved identity and hand-off round-trip without the reference.');

// 6. Opening /research?saved=<id>: v1 and Library v2 views go to Library, a v2 query opens as a first study, v3 as itself.
{
  const at = '2026-10-01T00:00:00Z', id = '11111111-1111-4111-8111-111111111111';
  const row = (conditions, channelId = channel, available = true) => ({ id, kind: 'view', name: 'My view', channel_id: channelId, conditions, revision: 2, created_at: at, updated_at: at, channel_available: available });
  const v1 = catalogue.createInitialCatalogueConditions();
  const v2 = definition.parseSavedDefinition({ version: 2, query: { ...structuredClone(libraryQuery), scope: { kind: 'channel', id: '$channel' }, consistency: 'current', page: { size: 50 } } });
  const v3 = graph.researchDefinition(graph.researchTemplate({ ...v2.query, scope: { kind: 'channel', id: channel } }, 'evidence')).conditions;
  assert.equal(views.savedOpenTarget(v1), 'library');
  assert.equal(views.savedOpenTarget(v2), 'query_study');
  assert.equal(views.savedOpenTarget({ ...v2, presentation: { version: 1, surface: 'library', view: 'grid' } }), 'library');
  assert.equal(views.savedOpenTarget(v3), 'study');
  const leave = handoff.savedStudyPlan(views.parseSavedView(row(v1)), 'http://localhost');
  assert.equal(leave.action, 'leave'); assert.equal(leave.url.pathname, `/app-explorer-v3/channels/${channel}`);
  assert.deepEqual(handoff.savedStudyPlan(views.parseSavedView(row(v1, channel, false)), 'http://localhost'), { action: 'refuse', notice: 'This saved view’s channel is unavailable. The saved view is kept.' });
  const first = handoff.savedStudyPlan(views.parseSavedView(row(v2)), 'http://localhost');
  assert.equal(first.action, 'open'); assert.equal(first.autoRun, true); assert.equal(first.fromQuery, true); assert.equal(first.saved, undefined);
  assert.deepEqual(first.document.query, { ...v2.query, scope: { kind: 'channel', id: channel } }, 'the query opens without loss');
  assert.deepEqual(first.document.graph.nodes.map(n => n.type), ['source', 'table']);
  const gone = handoff.savedStudyPlan(views.parseSavedView(row(v2, channel, false)), 'http://localhost');
  assert.equal(gone.autoRun, false); assert.equal(gone.sourceMissing, true); assert.equal(gone.notice, '', 'stated once, by the Source block');
  const folder = definition.parseSavedDefinition({ version: 2, query: { ...v2.query, scope: { kind: 'folder', id: '22222222-2222-4222-8222-222222222222', include_descendants: false } } });
  assert.equal(handoff.savedStudyPlan(views.parseSavedView(row(folder, null)), 'http://localhost').document.query.scope.id, '22222222-2222-4222-8222-222222222222');
  const study = handoff.savedStudyPlan(views.parseSavedView(row(v3)), 'http://localhost');
  assert.equal(study.action, 'open'); assert.equal(study.autoRun, false); assert.equal(study.saved.id, id); assert.equal(study.fromQuery, false);
  assert.deepEqual(study.document.graph, v3.graph);
  // A Library v2 view goes to Library when it can show it; otherwise it opens here with the same query, never in a loop.
  const library = { ...v2, presentation: { version: 1, surface: 'library', view: 'grid' } };
  const libraryView = { ...row(v2), conditions: library };
  const target = views.savedViewUrl(libraryView, 'http://localhost'), plan = handoff.savedStudyPlan(libraryView, 'http://localhost');
  if (target.pathname === views.RESEARCH_WORKSHOP_PATH) {
    assert.equal(plan.action, 'open'); assert.equal(plan.document.graph.nodes[1].type, 'gallery');
    assert.equal(plan.notice, 'This view was saved in Library. It opens here as a study with the same conditions.');
    assert.equal(Object.hasOwn(plan.document.query, 'presentation'), false);
  } else assert.deepEqual(plan, { action: 'leave', url: target });
}
console.log('Saved opening: v1/Library v2 → Library, v2 → first study with one run, v3 → study without a run.');

// 7. Workshop addresses: ?saved=<id> (one, well-formed) and ?settings=fields; the route stays research.
assert.equal(views.RESEARCH_WORKSHOP_PATH, '/app-explorer-v3/research');
assert.deepEqual(views.readWorkshopAddress(''), { saved: null, fieldSettings: false });
assert.deepEqual(views.readWorkshopAddress('?settings=fields'), { saved: null, fieldSettings: true });
assert.deepEqual(views.readWorkshopAddress('?saved=11111111-1111-4111-8111-11111111111A&settings=fields'), { saved: '11111111-1111-4111-8111-11111111111a', fieldSettings: true });
for (const search of ['?saved=a&saved=b', '?saved=11111111-1111-4111-8111-111111111111&saved=11111111-1111-4111-8111-111111111111', '?saved=../x', '?saved='])
  assert.deepEqual(views.readWorkshopAddress(search), { saved: null, error: 'Invalid saved query address.', fieldSettings: false }, search);
for (const search of ['', '?saved=11111111-1111-4111-8111-111111111111', '?settings=fields', '?saved=a&saved=b'])
  assert.deepEqual(views.readExplorerDestination('/app-explorer-v3/research', search), { kind: 'research' });

// 8. Which study an entry shows: mirror (reload/Back/Forward) > Library hand-off > ?saved= > new study.
{
  const at = '2026-10-01T00:00:00Z', origin8 = 'http://localhost', id = '11111111-1111-4111-8111-111111111111';
  const v2 = definition.parseSavedDefinition({ version: 2, query: { ...structuredClone(libraryQuery), scope: { kind: 'channel', id: '$channel' }, consistency: 'current', page: { size: 50 } } });
  const v3 = graph.researchDefinition(graph.researchTemplate({ ...v2.query, scope: { kind: 'channel', id: channel } }, 'evidence')).conditions;
  const row = conditions => views.parseSavedView({ id, kind: 'view', name: 'Stored', channel_id: channel, conditions, revision: 1, created_at: at, updated_at: at, channel_available: true });
  const store = new Map([[id, row(v3)]]), asked = [];
  const loadSavedView = async (savedIdValue, signal) => { asked.push(savedIdValue); signal.throwIfAborted(); const view = store.get(savedIdValue); if (!view) throw new Error('404'); return view; };
  const resolve = (state, search = '', handoffFor = () => undefined, signal = new AbortController().signal) =>
    handoff.resolveWorkshopStudy({ state, search, origin: origin8, signal, loadSavedView, handoffFor });
  // A new study, then reload: the same entry and the same unsaved document.
  const fresh = (await resolve(null)).study;
  assert.equal(fresh.query, undefined); assert.equal(fresh.document.query.scope.id, ''); assert.equal(fresh.autoRun, false);
  const edited = graph.withScope(fresh.document, { kind: 'folder', id: '22222222-2222-4222-8222-222222222222' }); edited.graph.notes = 'my question';
  const state = handoff.writeWorkshopHistory({}, handoff.workshopMirror({ ...fresh, document: edited, name: 'Draft' }));
  const reloaded = (await resolve(structuredClone(state), '?saved=' + id)).study;
  assert.equal(reloaded.entry, fresh.entry); assert.deepEqual(reloaded.document, edited); assert.equal(reloaded.name, 'Draft');
  assert.deepEqual(asked, [], 'the mirror wins over ?saved= and needs no read');
  // A saved study reopened from its mirror gets its current Saved identity; a deleted one keeps the work as new.
  const savedState = handoff.writeWorkshopHistory({}, handoff.workshopMirror({ ...fresh, document: edited, saved: store.get(id) }));
  assert.equal((await resolve(savedState)).study.saved.revision, 1);
  store.delete(id);
  const orphan = (await resolve(savedState)).study;
  assert.equal(orphan.saved, undefined); assert.match(orphan.notice, /deleted or is unavailable\. Your study is kept/);
  // A Library hand-off: memory (with the reference) for its own entry only, else history without it.
  const whole = handoff.selectionHandoff(libraryQuery, frozenPage, origin), entry = handoff.newWorkshopEntry();
  const arrived = handoff.writeWorkshopHistory({}, { version: 1, entry, handoff: whole });
  noRef(arrived);
  const live = (await resolve(arrived, '', e => e === entry ? whole : undefined)).study;
  assert.deepEqual(live.run, { consistency: 'frozen', frozen_ref: frozenPage.context.frozen_ref }); assert.equal(live.autoRun, true);
  // The workshop states the fixed list itself (Library's, a new one, or the choice once expired).
  assert.doesNotMatch(live.notice, /reads current data/); assert.equal(live.entry, entry);
  // Reloaded (the mirror has the document): still a fixed list, its reference gone, a new one on Run.
  const reloadedSelection = (await resolve(handoff.writeWorkshopHistory({}, handoff.workshopMirror(live)), '', () => whole)).study;
  assert.deepEqual(reloadedSelection.run, { consistency: 'frozen' }); assert.equal(reloadedSelection.notice, handoff.REOPENED_SELECTION_NOTE);
  assert.equal(reloadedSelection.autoRun, false); assert.equal(reloadedSelection.handoff.frozen, undefined);
  noRef(handoff.writeWorkshopHistory({}, handoff.workshopMirror(live)));
  const later = (await resolve(arrived, '', () => whole)).study, other = (await resolve(arrived, '', e => e === 'x' ? whole : undefined)).study;
  assert.deepEqual(later.run, { consistency: 'frozen', frozen_ref: frozenPage.context.frozen_ref });
  assert.deepEqual(other.run, { consistency: 'frozen' }, 'another entry\'s memory is not used');
  const picked = handoff.selectedIdsHandoff(libraryQuery, ids(3).map(id => ({ id })), origin);
  const sent = (await resolve(handoff.writeWorkshopHistory({}, { version: 1, entry, handoff: picked }))).study;
  assert.deepEqual(sent.run, { consistency: 'current' }); assert.doesNotMatch(sent.notice, /reads current data/);
  // ?saved=: v2 query → first study with one run; v1 → Library; missing, malformed, cancelled.
  store.set(id, row(v2));
  const query = (await resolve(null, '?saved=' + id)).study;
  assert.equal(query.fromQuery, true); assert.equal(query.autoRun, true); assert.equal(query.name, 'Stored'); assert.equal(query.saved, undefined);
  store.set(id, row(catalogue.createInitialCatalogueConditions()));
  assert.equal((await resolve(null, '?saved=' + id)).leave.pathname, `/app-explorer-v3/channels/${channel}`);
  store.clear();
  assert.equal((await resolve(null, '?saved=' + id)).study.notice, 'This saved item was deleted or is unavailable.');
  assert.equal((await resolve(null, '?saved=a&saved=b')).study.notice, 'Invalid saved query address.');
  const stop = new AbortController(); stop.abort();
  assert.equal(await resolve(null, '?saved=' + id, undefined, stop.signal), null);
  assert.equal((await resolve({ workshop: { version: 1, entry, document: { broken: true } } })).study.entry, entry, 'a damaged mirror keeps its entry');
}
console.log('Workshop hand-off, budgets, history and Saved routing passed.');
