// The research canvas model (graph version 2) and the saved canvas (Saved conditions version 4): the branch
// query mirrors the server's composition, connections are explained before a run, removing a node keeps its
// children as drafts, a palette step slots in between, "Continue with selected" carries exact IDs, an old
// study opens without loss, and a saved canvas opens as itself. Offline: no browser, server or network.
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
const model = compiled('workshopModel.ts', { './api': {}, './researchGraph': graph, './researchQuery': research, './workshopHandoff': handoff });
const canvas = compiled('canvas/canvasModel.ts', { '../api': {}, '../savedQueryDefinition': definition, '../savedWork': saved, '../researchQuery': research,
  '../researchOperations': operations, '../researchGraph': graph, '../workshopModel': model, '../../sourceLifetime': lifetime });

const folder = '22222222-2222-4222-8222-222222222222', view = '33333333-3333-4333-8333-333333333333';
const chosen = (g, id = 'videos') => canvas.setParameters(g, id, { ...g.nodes.find(n => n.id === id).parameters, scope: { kind: 'folder', id: folder, include_descendants: false } });
const start = () => chosen(canvas.newCanvas('UTC'));
const views100 = { field: 'video.views', op: 'gte', value: '100' };

// A new canvas: a Long-form source still to be chosen, feeding Cards; the source is a draft-free root.
{
  const g = canvas.newCanvas('Europe/Kyiv');
  assert.deepEqual(g.nodes.map(n => n.type), ['source', 'gallery']);
  assert.equal(g.nodes[0].parameters.scope.id, '');
  assert.equal(g.nodes[0].parameters.time.timezone, 'Europe/Kyiv');
  assert.equal(canvas.isDraft(g, 'cards'), false);
}

// Source → Filter → Sort → Cards composes one query like the server: conditions add up, the last sort wins,
// the identity column comes first, the page is the block's own.
{
  let g = start();
  let made = canvas.addNode(g, 'filter', { x: 400, y: 120 }, 'UTC', 'videos'); g = canvas.insertBetween(made.graph, 'videos', made.id);
  g = canvas.setParameters(g, made.id, { where: views100 });
  const sort = canvas.addNode(g, 'sort', { x: 800, y: 120 }, 'UTC', made.id); g = canvas.insertBetween(sort.graph, made.id, sort.id);
  g = canvas.setParameters(g, sort.id, { order_by: [{ field: 'video.duration_seconds', direction: 'asc' }] });
  assert.deepEqual(canvas.pathTo(g, 'cards').map(n => n.type), ['source', 'filter', 'sort', 'gallery']);
  const q = canvas.branchQuery(g, 'cards');
  assert.deepEqual(q.where, views100);
  assert.deepEqual(q.order_by, [{ field: 'video.duration_seconds', direction: 'asc' }]);
  assert.equal(q.select[0], 'video.id');
  assert.equal(q.page.size, 24);
  assert.deepEqual(q.scope, { kind: 'folder', id: folder, include_descendants: false });
  // A step slotted in between moves the result (and what follows it) one step right, never on top of another node.
  assert.ok(g.layout.cards.x > g.layout[sort.id].x && g.layout[sort.id].x > g.layout[made.id].x);
}

// New nodes never cover an existing one.
{
  const g = start(), a = canvas.addNode(g, 'table', { x: 420, y: 120 }, 'UTC', 'videos');
  assert.notDeepEqual(a.graph.layout[a.id], g.layout.cards);
  assert.ok(Math.abs(a.graph.layout[a.id].y - g.layout.cards.y) >= 130);
}

// Connections are explained before a run: a view feeds nothing, a source has no input, no loops, entity rules.
{
  let g = start();
  const f = canvas.addNode(g, 'filter', { x: 400, y: 400 }, 'UTC', 'videos'); g = f.graph;
  assert.match(canvas.connectionProblem(g, 'cards', f.id), /feeds nothing/);
  assert.match(canvas.connectionProblem(g, f.id, 'videos'), /no input/);
  const f2 = canvas.addNode(g, 'filter', { x: 800, y: 400 }, 'UTC', f.id); g = f2.graph;
  assert.match(canvas.connectionProblem(g, f2.id, f.id), /loop/);
  const channels = canvas.addNode(g, 'source', { x: 0, y: 800 }, 'UTC'); g = canvas.setParameters(channels.graph, channels.id, canvas.sourceParams('UTC', 'channel'));
  const cards = canvas.addNode(g, 'gallery', { x: 400, y: 800 }, 'UTC'); g = cards.graph;
  assert.match(canvas.connectionProblem(g, channels.id, cards.id), /works with videos/);
  assert.equal(canvas.connect(g, channels.id, cards.id), g);
}

// Removing a node keeps its children as drafts; a canvas keeps at least one node; budgets are explained.
{
  let g = start();
  const f = canvas.addNode(g, 'filter', { x: 400, y: 120 }, 'UTC', 'videos'); g = canvas.insertBetween(f.graph, 'videos', f.id);
  const removed = canvas.removeNodes(g, [f.id]);
  assert.deepEqual(removed.nodes.map(n => n.id), ['videos', 'cards']);
  assert.equal(canvas.isDraft(removed, 'cards'), true);
  assert.equal(canvas.removeNodes(removed, ['videos', 'cards']), removed);
  let full = g; while (full.nodes.length < canvas.CANVAS_NODES) full = canvas.addNode(full, 'filter', { x: 0, y: 0 }, 'UTC').graph;
  assert.match(canvas.budgetProblem(full, ['sort']), /up to 16 nodes/);
}

// "Continue with selected": a Selected items node on the result's input with the exact IDs, and Cards after it.
{
  const g = start(), ids = ['aaaaaaaaaaa', 'bbbbbbbbbbb'];
  const next = canvas.continueWithSelected(g, 'cards', ids, 'UTC');
  const picked = next.graph.nodes.find(n => n.id === next.id);
  assert.equal(picked.type, 'selection');
  assert.deepEqual(picked.parameters, { mode: 'include', ids });
  assert.equal(canvas.parentOf(next.graph, next.id), 'videos');
  const after = next.graph.edges.find(e => e.from === next.id).to;
  assert.deepEqual(canvas.branchQuery(next.graph, after).where, { field: 'video.id', op: 'in', value: ids });
  assert.match(canvas.continueWithSelected(g, 'cards', [], 'UTC').problem, /at least one/);
  assert.match(canvas.continueWithSelected(g, 'cards', Array.from({ length: 1001 }, (_, i) => String(i).padStart(11, 'a')), 'UTC').problem, /1,000/);
}

// Size: the canvas counts UTF-8 bytes like the server. Its limits are the server's.
{
  const source = readFileSync(new URL('../application/research_query/graph.py', import.meta.url), 'utf8');
  assert.equal(Number(/^MAX_DEFINITION = (\d+)/m.exec(source)[1]), canvas.CANVAS_BYTES);
  assert.equal(Number(/^CANVAS_NOTES_BYTES = (\d+)/m.exec(source)[1]), canvas.CANVAS_NOTES);
  assert.equal(canvas.CANVAS_NOTES, 8192);
  // A fixed canvas with Cyrillic notes: 693 bytes by research_query.validation.canonical({version: 4, graph}).
  const fixed = { version: 2, nodes: [
    { id: 'videos', type: 'source', parameters: { source: { id: research.videoSource }, scope: { kind: 'folder', id: folder, include_descendants: false },
      video_types: ['long'], time: { mode: 'current', timezone: 'UTC' } } },
    { id: 'cards', type: 'gallery', parameters: { select: ['video.title'], page_size: 24 } }],
  edges: [{ from: 'videos', to: 'cards', output: 'rows', input: 'rows' }],
  notes: 'Почему растут длинные видео? '.repeat(3) + 'ё', layout: { videos: { x: 40, y: 120 }, cards: { x: 420, y: 120 } } };
  assert.equal(canvas.canvasBytes(fixed), 693);
  assert.equal(canvas.textBytes(fixed.notes), 161);
}

// Notes: 4,096 Cyrillic letters (8,192 bytes) are kept; one more letter is not, and the canvas says why. An edit that
// shortens notes that are already too long is made.
{
  const g = start(), full = { ...g, notes: 'ж'.repeat(4096) };
  assert.equal(canvas.editSizeProblem(g, full), null);
  assert.equal(canvas.sizeProblem(full), null);
  const over = canvas.editSizeProblem(full, { ...g, notes: 'ж'.repeat(4097) });
  assert.match(over, /^Notes are limited to 8,192 bytes \(about 4,096 Cyrillic letters\); this edit would make them 8,194\./);
  assert.equal(canvas.editSizeProblem(g, { ...g, notes: 'n'.repeat(8192) }), null, 'Latin letters take one byte');
  const old = { ...g, notes: 'ж'.repeat(5000) };
  assert.match(canvas.sizeProblem(old), /Notes are limited to 8,192 bytes .*they take 10,000/);
  assert.equal(canvas.editSizeProblem(old, { ...g, notes: 'ж'.repeat(4999) }), null, 'shortening is always allowed');
  assert.match(canvas.editSizeProblem(old, { ...g, notes: 'ж'.repeat(5001) }), /would make them 10,002/);
  // A saved canvas follows the server: notes up to 8,192 bytes.
  assert.deepEqual(graph.parseSavedCanvas({ version: 4, graph: full }).graph.notes, full.notes);
  assert.throws(() => graph.parseSavedCanvas({ version: 4, graph: { ...g, notes: 'ж'.repeat(4097) } }));
}

// The whole canvas: an edit past 16,384 bytes is not made (duplicating 1,000 picked IDs, a Filter on top of them).
{
  const ids = Array.from({ length: 1000 }, (_, i) => 'v' + String(i).padStart(10, '0'));
  const g = canvas.continueWithSelected(start(), 'cards', ids, 'UTC').graph;
  const picked = g.nodes.find(n => n.type === 'selection').id;
  assert.ok(canvas.canvasBytes(g) <= canvas.CANVAS_BYTES);
  const twice = canvas.duplicateNodes(g, [picked]).graph;
  assert.match(canvas.editSizeProblem(g, twice), /^A canvas is limited to 16,384 bytes; this edit would make it [\d,]+\. Shorten the notes, pick fewer items or remove a node\.$/);
  assert.equal(canvas.editSizeProblem(twice, canvas.removeNodes(twice, [picked])), null, 'removing is always allowed');
}

// "Continue with selected" refuses what does not fit and says how many do; that many fit, one more does not.
{
  const ids = Array.from({ length: 1000 }, (_, i) => 'v' + String(i).padStart(10, '0'));
  const g = { ...start(), notes: 'ж'.repeat(900) };
  const refused = canvas.continueWithSelected(g, 'cards', ids, 'UTC');
  const [, total, fit] = /^These 1,000 items do not fit: the canvas would take ([\d,]+) of 16,384 bytes\. Up to ([\d,]+) fit; pick fewer or shorten the notes\.$/.exec(refused.problem);
  assert.ok(Number(total.replaceAll(',', '')) > canvas.CANVAS_BYTES);
  const n = Number(fit.replaceAll(',', '')), made = canvas.continueWithSelected(g, 'cards', ids.slice(0, n), 'UTC');
  assert.ok(made.graph && canvas.canvasBytes(made.graph) <= canvas.CANVAS_BYTES);
  assert.ok('problem' in canvas.continueWithSelected(g, 'cards', ids.slice(0, n + 1), 'UTC'));
  // A canvas filled to its last bytes has no room for one more Selected items node and its Cards.
  const brim = canvas.continueWithSelected(g, 'cards', ids.slice(0, n), 'UTC').graph, next = brim.nodes.find(x => x.type === 'gallery' && x.id !== 'cards').id;
  assert.match(canvas.continueWithSelected(brim, next, ['aaaaaaaaaaa'], 'UTC').problem, /^No picked items fit: the canvas would take [\d,]+ of 16,384 bytes\. Shorten the notes or remove a node\.$/);
}

// An unsaved canvas over the save limit reopens from the tab history instead of being
// replaced by an empty one (1,000 picked + a Filter + 2,000 Latin notes, 17,324 bytes).
{
  const ids = Array.from({ length: 1000 }, (_, i) => 'v' + String(i).padStart(10, '0'));
  const base = canvas.continueWithSelected(start(), 'cards', ids, 'UTC').graph;
  const f = canvas.addNode(base, 'filter', { x: 0, y: 600 }, 'UTC', 'videos');
  const large = { ...canvas.setParameters(f.graph, f.id, { where: views100 }), notes: 'n'.repeat(2000) };
  assert.ok(canvas.canvasBytes(large) > 17000);
  const entry = '55555555-5555-4555-8555-555555555555';
  const mirror = handoff.readWorkshopHistory(handoff.writeWorkshopHistory(null, handoff.workshopMirror({ entry, document: graph.newStudy(), canvas: large })));
  assert.deepEqual(mirror.canvas, large);
  assert.match(canvas.sizeProblem(large), /^A canvas is limited to 16,384 bytes; this one takes [\d,]+\./);
  assert.throws(() => graph.parseSavedCanvas({ version: 4, graph: large }), 'Save still refuses it');
}

// An old study (version 1) opens on the canvas with the same meaning: source, its conditions, its order, its blocks.
{
  const study = graph.newStudy();
  study.query.scope = { kind: 'folder', id: folder, include_descendants: false };
  study.query.where = views100;
  const g = canvas.fromStudy(study), result = g.nodes.find(n => canvas.isResult(n.type));
  const q = canvas.branchQuery(g, result.id);
  assert.deepEqual(q.where, views100);
  assert.deepEqual(q.order_by, study.query.order_by.map(({ field, direction }) => ({ field, direction })));
  assert.deepEqual(q.scope, study.query.scope);
  assert.equal(g.notes, study.graph.notes);
  // Defaults (available data, a 1-hour change window) are not copied; a study's own rules are.
  assert.equal('coverage' in g.nodes[0].parameters || 'observations' in g.nodes[0].parameters, false);
  const strict = structuredClone(study), day = { version: 1, window_seconds: 86400, tolerance_seconds: 7200, age_seconds: 86400, age_tolerance_seconds: 7200 };
  strict.query.coverage = { mode: 'require_complete' }; strict.query.observations = day;
  const s = canvas.fromStudy(strict), sq = canvas.branchQuery(s, s.nodes.find(n => canvas.isResult(n.type)).id);
  assert.deepEqual(sq.coverage, { mode: 'require_complete' });
  assert.deepEqual(sq.observations, day);
  assert.match(canvas.nodeSummary(s, s.nodes[0]), /complete data only · 1-day change/);
}

// "Open in research workshop": the Cards branch of the opened canvas composes Library's exact query (so the server
// shows Library's fixed list there), and the run carries Library's reference.
{
  const channel = 'UC' + 'a'.repeat(22);
  const library = { query_version: 1, source: { id: research.videoSource }, scope: { kind: 'channel', id: channel }, video_types: ['long'],
    select: ['video.id', 'video.channel_id', 'video.type', 'video.title', 'video.thumbnail_url', 'video.published_at', 'video.duration_seconds', 'video.views', 'video.likes'],
    where: { all: [{ field: 'video.views', op: 'gte', value: '1000' }, { field: 'video.title', op: 'contains_casefold_v1', value: 'soup' }] },
    order_by: [{ field: 'video.published_at', direction: 'desc', nulls: 'last' }], time: { mode: 'current', timezone: 'Europe/Moscow' },
    coverage: { mode: 'available' }, consistency: 'frozen', page: { size: 50 } };
  const frozen = { context: { frozen_ref: 'sealed.' + 'x'.repeat(300), evaluated_at: '2026-10-29T10:00:12Z', valid_before: '2026-10-29T10:10:12Z' }, page: { total_count: 1234 } };
  const opened = handoff.studyFromHandoff(handoff.selectionHandoff(library, frozen,
    { surface: 'library', channel_id: channel, channel_title: 'Cooking Lab', format: 'long', view: 'grid' }));
  assert.deepEqual(opened.run, { consistency: 'frozen', frozen_ref: frozen.context.frozen_ref });
  const g = canvas.fromStudy(opened.document), cards = g.nodes.find(n => n.type === 'gallery');
  const q = canvas.branchQuery(g, cards.id);
  for (const key of ['source', 'scope', 'video_types', 'select', 'where', 'time']) assert.deepEqual(q[key], library[key], key);
  assert.deepEqual(q.order_by, library.order_by.map(({ field, direction }) => ({ field, direction })));
  assert.equal(q.page.size, 50);
  assert.equal('coverage' in q || 'observations' in q, false, 'defaults stay defaults');
}

// The tab history keeps the canvas (unsaved edits included); a damaged canvas keeps only the entry id and says so.
{
  const g = start(), entry = '44444444-4444-4444-8444-444444444444';
  const state = handoff.writeWorkshopHistory({ other: 1 }, handoff.workshopMirror({ entry, document: graph.newStudy(), canvas: g, name: 'Draft' }));
  assert.equal(state.other, 1);
  const mirror = handoff.readWorkshopHistory(state);
  assert.deepEqual(mirror.canvas, g);
  assert.equal(mirror.name, 'Draft');
  const damaged = structuredClone(state); damaged.workshop.canvas.edges.push({ from: 'videos', to: 'nowhere', output: 'rows', input: 'rows' });
  assert.deepEqual(handoff.readWorkshopHistory(damaged), { version: 1, entry, damaged: true });
  assert.equal('canvas' in handoff.workshopMirror({ entry, document: graph.newStudy() }), false);
}

// Templates start a canvas on the same source: the workshop's starting views, as nodes.
{
  const g = start(), q = canvas.branchQuery(g, 'videos');
  const thumbs = canvas.fromStudy(graph.researchTemplate(q, 'thumbnails'));
  const cards = thumbs.nodes.find(n => n.type === 'gallery');
  assert.ok(cards.parameters.select.includes('video.thumbnail_url'));
  assert.deepEqual(canvas.branchQuery(thumbs, cards.id).order_by, [{ field: 'video.views', direction: 'desc' }]);
  assert.deepEqual(canvas.branchQuery(thumbs, cards.id).scope, q.scope);
  const overview = canvas.fromStudy(graph.researchTemplate(q, 'overview', ['video.summary.v1']));
  assert.deepEqual(overview.nodes.filter(n => canvas.isResult(n.type)).map(n => n.type), ['summary']);
  const channels = canvas.fromStudy(graph.researchTemplate(q, 'channel_list'));
  assert.equal(canvas.entityOf(channels, channels.nodes.find(n => canvas.isResult(n.type)).id), 'channel');
}

// The canvas palette lists every template once. Evidence and notes keeps the Source's rows (videos or
// channels), so it has its own group instead of appearing under both Videos and Channels.
{
  const groups = graph.CANVAS_TEMPLATE_GROUPS, listed = groups.flatMap(group => group.templates);
  assert.equal(new Set(listed).size, listed.length, 'no template listed twice');
  assert.deepEqual([...listed].sort(), [...new Set([...graph.VIDEO_TEMPLATES, ...graph.CHANNEL_TEMPLATES])].sort(), 'none left out');
  assert.deepEqual(groups.map(group => [group.label, [...group.templates]]), [
    ['Videos', ['latest', 'most_viewed', 'thumbnails', 'overview', 'observations']],
    ['Channels', ['channel_list', 'compare_channels', 'channel_history']],
    ['Videos or channels', ['evidence']]]);
  const videoQuery = canvas.branchQuery(start(), 'videos');
  const channelQuery = { ...videoQuery, source: { id: research.channelSource } };
  assert.equal(graph.researchTemplate(videoQuery, 'evidence').query.source.id, research.videoSource);
  assert.equal(graph.researchTemplate(channelQuery, 'evidence').query.source.id, research.channelSource);
  // The palette's template list is drawn from these groups (it was two lists with Evidence and notes in both).
  const workshop = readFileSync(new URL('../application/frontend/src/explorer/canvas/CanvasWorkshop.tsx', import.meta.url), 'utf8');
  assert.match(workshop, /CANVAS_TEMPLATE_GROUPS\.filter\(group => group\.templates\.some\(offered\)\)\.map/);
  assert.doesNotMatch(workshop, /VIDEO_TEMPLATES|CHANNEL_TEMPLATES/);
}

// A saved canvas: the graph only; it opens in the workshop as itself, with its Saved item.
{
  const g = start(), conditions = { version: 4, graph: g };
  assert.deepEqual(graph.parseSavedCanvas(conditions), conditions);
  assert.equal(views.serializeSavedConditions(conditions), definition.canonicalSaved(conditions));
  for (const bad of [{ ...conditions, query: {} }, { version: 4 }, { version: 4, graph: { ...g, version: 1 } },
    { version: 4, graph: { ...g, edges: [{ from: 'videos', to: 'nowhere', output: 'rows', input: 'rows' }] } },
    { version: 4, graph: { ...g, layout: { videos: g.layout.videos } } }]) assert.throws(() => graph.parseSavedCanvas(bad));
  assert.equal(views.savedOpenTarget(conditions), 'canvas');
  const item = { id: view, kind: 'view', name: 'My canvas', channel_id: null, conditions, revision: 2, created_at: '2026-11-01T00:00:00Z',
    updated_at: '2026-11-01T00:00:00Z', channel_available: true };
  assert.equal(views.savedLibraryTarget(item, 'http://local').kind, 'workshop');
  const plan = handoff.savedStudyPlan(item, 'http://local');
  assert.equal(plan.action, 'open');
  assert.deepEqual(plan.canvas, g);
  assert.equal(plan.saved, item);
  assert.equal(plan.notice, '');
  const gone = handoff.savedStudyPlan({ ...item, channel_available: false }, 'http://local');
  assert.equal(gone.sourceMissing, true); assert.equal(gone.notice, '', 'stated once, by the canvas');
}

// wires without a mouse. "Connect to…" lists the nodes a node can feed by the rules of a dragged wire, names
// them in words, says which input a wire replaces, and makes the same wire as one Undo step that marks the canvas
// unsaved. The menus take the focus, move with the arrow keys and close on Escape with the focus given back.
{
  let g = start();
  const f = canvas.addNode(g, 'filter', { x: 400, y: 400 }, 'UTC'); g = f.graph; // a draft, not connected
  const s = canvas.addNode(g, 'sort', { x: 800, y: 400 }, 'UTC'); g = s.graph;
  // The source feeds Cards already: only the two drafts are offered. A result feeds nothing; nothing feeds a source.
  assert.deepEqual(canvas.connectionTargets(g, 'videos'), [{ id: f.id }, { id: s.id }]);
  assert.deepEqual(canvas.connectionTargets(g, 'cards'), []);
  assert.deepEqual(canvas.connectionTargets(g, f.id), [{ id: 'cards', replaces: 'videos' }, { id: s.id }]);
  for (const from of g.nodes.map(n => n.id)) {
    const offered = canvas.connectionTargets(g, from).map(t => t.id);
    for (const to of g.nodes.map(n => n.id)) {
      // Offered exactly when a dragged wire would be accepted (and is not there yet).
      const fresh = !g.edges.some(e => e.from === from && e.to === to) && canvas.connectionProblem(g, from, to) === null;
      assert.equal(offered.includes(to), fresh, `${from} → ${to}`);
    }
  }
  // No loops: after Filter → Sort, Sort cannot feed Filter.
  const chained = canvas.connect(g, f.id, s.id);
  assert.deepEqual(canvas.connectionTargets(chained, s.id), [{ id: 'cards', replaces: 'videos' }]);
  // Entity rules: a Channels source cannot feed Cards (videos only), so Cards is not offered.
  const ch = canvas.addNode(g, 'source', { x: 0, y: 800 }, 'UTC'), channels = canvas.setParameters(ch.graph, ch.id, canvas.sourceParams('UTC', 'channel'));
  assert.deepEqual(canvas.connectionTargets(channels, ch.id).map(t => t.id), [f.id, s.id]);
  // Names in words; equal ones are numbered, so two empty Filters can be told apart.
  const twin = canvas.addNode(g, 'filter', { x: 1200, y: 400 }, 'UTC').graph, named = canvas.nodeNames(twin);
  assert.equal(named.cards, 'Cards: 6 fields · 24 per page');
  assert.deepEqual([named[f.id], named.filter_2], ['Filter: No condition yet (1)', 'Filter: No condition yet (2)']);
  assert.equal(canvas.nodeNames(g, { scope: () => 'Cooking' }).videos, 'Source: Long-form · folder Cooking');
  // The keys of a menu: arrows wrap, Home and End go to the ends, Escape closes, anything else is not the menu's.
  assert.deepEqual(['ArrowDown', 'ArrowDown', 'ArrowUp', 'Home', 'End', 'Escape', 'Tab', 'a'].map(key => canvas.menuMove(key, 2, 3)), [0, 0, 1, 0, 2, 'close', null, null]);
  assert.deepEqual([canvas.menuMove('ArrowDown', -1, 3), canvas.menuMove('ArrowUp', -1, 3), canvas.menuMove('ArrowDown', 0, 0), canvas.menuMove('Escape', 0, 0)], [0, 2, null, 'close']);

  // The real menus of CanvasWorkshop.tsx, compiled alone (the canvas itself needs React Flow and a browser).
  const file = readFileSync(new URL('../application/frontend/src/explorer/canvas/CanvasWorkshop.tsx', import.meta.url), 'utf8');
  const tree = ts.createSourceFile('CanvasWorkshop.tsx', file, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const declared = name => { let text; const visit = node => { if (ts.isFunctionDeclaration(node) && node.name?.text === name) text = node.getText(tree); ts.forEachChild(node, visit); }; visit(tree); assert.ok(text, name); return text; };
  const jsx = (type, props) => ({ type, props: props ?? {} });
  const component = (name, globals) => {
    const { outputText } = ts.transpileModule('export ' + declared(name), { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX } });
    const exports = {};
    new Function('require', 'exports', ...Object.keys(globals), outputText)(n => { assert.equal(n, 'react/jsx-runtime'); return { jsx, jsxs: jsx, Fragment: 'fragment' }; }, exports, ...Object.values(globals));
    return exports[name];
  };
  const text = node => node === null || node === undefined || typeof node === 'boolean' ? '' : typeof node !== 'object' ? String(node)
    : Array.isArray(node) ? node.map(text).join('') : text(node.props.children);
  const buttonsOf = node => !node || typeof node !== 'object' ? [] : Array.isArray(node) ? node.flatMap(buttonsOf)
    : node.type === 'button' ? [node] : buttonsOf(node.props.children);

  // CanvasMenu: focus on open, arrows, Escape; the focus comes back only when it was inside the menu.
  {
    const { reactHookHost } = await import('./react_hook_host.mjs');
    const page = { activeElement: null };
    const element = name => ({ name, focus() { page.activeElement = this; } });
    const items = [element('Filter'), element('Sort'), element('Cancel')], opener = element('opener'), notes = element('notes');
    const box = { querySelectorAll: selector => { assert.equal(selector, 'button:not(:disabled)'); return items; }, contains: el => items.includes(el) };
    const mount = () => {
      const host = reactHookHost(); let first = true, closed = 0;
      const React = { ...host.React, useRef: initial => { const ref = host.React.useRef(first ? box : initial); first = false; return ref; } };
      const Menu = component('CanvasMenu', { useRef: React.useRef, useLayoutEffect: React.useLayoutEffect, menuMove: canvas.menuMove, document: page });
      host.mount(() => Menu({ label: 'Connect Filter to', returnTo: () => opener, onClose: () => { closed++; }, children: [] }), null);
      const key = k => { let stopped = false; host.current.props.onKeyDown({ key: k, preventDefault() {}, stopPropagation() { stopped = true; } }); return stopped; };
      return { host, key, closed: () => closed };
    };
    let menu = mount();
    assert.equal(menu.host.current.props.role, 'menu');
    assert.equal(menu.host.current.props['aria-label'], 'Connect Filter to');
    assert.equal(page.activeElement, items[0], 'the menu takes the focus when it opens');
    assert.ok(menu.key('ArrowDown')); assert.equal(page.activeElement, items[1]);
    menu.key('End'); assert.equal(page.activeElement, items[2]);
    menu.key('ArrowDown'); assert.equal(page.activeElement, items[0], 'the arrows wrap');
    menu.key('ArrowUp'); assert.equal(page.activeElement, items[2]);
    assert.equal(menu.key('Tab'), false, 'other keys keep their usual meaning');
    assert.ok(menu.key('Escape')); assert.equal(menu.closed(), 1, 'Escape closes it');
    menu.host.unmount(); assert.equal(page.activeElement, opener, 'closed from inside: the focus goes back');
    menu = mount(); notes.focus(); menu.host.unmount();
    assert.equal(page.activeElement, notes, 'closed by an edit elsewhere: the focus stays there');
  }

  // ConnectMenu: the offered nodes, by name; a choice is the dragged wire, one Undo step, and unsaved until undone.
  {
    const Stub = props => props;
    const Connect = component('ConnectMenu', { CanvasMenu: Stub, connectionTargets: canvas.connectionTargets });
    const names = canvas.nodeNames(g, { scope: () => 'Cooking' }), chosen = [];
    const menuTree = Connect({ graph: g, from: f.id, names, returnTo: () => null, onClose() {}, onConnect: (to, said) => chosen.push([to, said]) });
    assert.equal(menuTree.type, Stub);
    assert.equal(menuTree.props.label, 'Connect Filter: No condition yet to');
    assert.equal(menuTree.props.id, 'canvas-connect-menu');
    const items = buttonsOf(menuTree.props.children);
    assert.deepEqual(items.map(b => [b.props.role, text(b.props.children)]), [
      ['menuitem', 'Cards: 6 fields · 24 per page (replaces the wire from Source: Long-form · folder Cooking)'],
      ['menuitem', 'Sort: Views ↓'], [undefined, 'Cancel']]);
    items[0].props.onClick();
    assert.deepEqual(chosen[0][0], 'cards');
    assert.match(chosen[0][1], /^Connected: Filter: No condition yet → Cards: 6 fields · 24 per page \(instead of Source: Long-form · folder Cooking\)\. Undo removes the wire\.$/);
    // CanvasEditor's connectNodes: the same check and edit as onConnect (a dragged wire).
    assert.match(file, /function onConnect\(c: Connection\) \{ connectNodes\(c\.source, c\.target\); \}/);
    assert.match(file, /onConnect=\{\(to, said\) => connectNodes\(node\.id, to, said\)\}/);
    assert.match(file, /if \(!change\(connect\(graph, from, to\)\)\) setMessage\(done\);/);
    const saved = { id: view, kind: 'view', name: 'Mine', channel_id: null, conditions: { version: 4, graph: g }, revision: 1,
      created_at: '2026-11-01T00:00:00Z', updated_at: '2026-11-01T00:00:00Z', channel_available: true };
    let history = model.recordEdit(model.startHistory(g), canvas.connect(g, f.id, 'cards'));
    assert.deepEqual(canvas.pathTo(history.present, 'cards').map(n => n.id), [], 'Cards now reads the draft Filter');
    assert.equal(handoff.sameSavedContent(saved, history.present, 'Mine'), false, 'Unsaved changes');
    history = model.undoEdit(history);
    assert.equal(history.present, g, 'one Undo takes the wire back');
    assert.equal(handoff.sameSavedContent(saved, history.present, 'Mine'), true);
    // With nothing to feed, the menu says so instead of an empty list.
    const none = Connect({ graph: g, from: 'cards', names, returnTo: () => null, onClose() {}, onConnect() {} });
    assert.match(text(none.props.children), /^No node can take these rows\. Add one from the palette, then connect it\.Cancel$/);
  }
  // Wiring of the canvas: the button names its menu; the Add-after menu is a CanvasMenu that returns to its node; nodes
  // carry their names for screen readers.
  assert.match(file, /aria-haspopup="menu" aria-expanded=\{connecting === node\.id\}/);
  assert.match(file, />Connect to…<\/button>/);
  assert.match(file, /<CanvasMenu style=\{\{ left: menu\.x, top: menu\.y \}\} label="Add a connected node"/);
  assert.match(file, /\.react-flow__node\[data-id="\$\{menu\.parent\}"\]/);
  assert.match(file, /ariaLabel: `\$\{names\[node\.id\]\}\. \$\{state\.text\}`/);
  assert.match(file, /setMenu\(null\); setConnecting\(null\); setMessage\(problem \?\? ''\);/);
}

console.log('research canvas UI: ok');
