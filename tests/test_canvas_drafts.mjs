// This tab's second copy of unsaved canvas work: the draft in sessionStorage under the history
// entry's id, written after a pause and at once when the page is left; the newer of the two copies opens; a damaged
// copy is said, never replaced silently; the typed name travels with the canvas. the tab saves against the
// revision its edits started from. Offline: no browser or server.
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
const drafts = compiled('canvasDrafts.ts', { './researchGraph': graph, './savedWork': saved, './workshopHandoff': handoff });

const folder = '22222222-2222-4222-8222-222222222222', item = '33333333-3333-4333-8333-333333333333';
const entry = '44444444-4444-4444-8444-444444444444', other = '66666666-6666-4666-8666-666666666666';
const start = () => canvas.setParameters(canvas.newCanvas('UTC'), 'videos', { ...canvas.newCanvas('UTC').nodes[0].parameters, scope: { kind: 'folder', id: folder, include_descendants: false } });
/** A Storage of the browser's shape; `quota` makes a write of more than that many characters fail like a full store. */
function memoryStore(quota = Infinity) {
  const map = new Map();
  return { map, get length() { return map.size; }, key: i => [...map.keys()][i] ?? null, getItem: k => map.has(k) ? map.get(k) : null,
    setItem: (k, v) => { const used = [...map].reduce((n, [key, value]) => n + (key === k ? 0 : value.length), 0); if (used + String(v).length > quota) throw new Error('QuotaExceededError'); map.set(k, String(v)); },
    removeItem: k => map.delete(k) };
}

// A draft round-trips; what cannot be read is no draft (wrong entry, damaged canvas, unknown keys, a long name).
{
  const store = memoryStore(), g = { ...start(), notes: 'Заметка' };
  assert.equal(drafts.writeDraft({ version: 1, entry, seq: 3, at: 10, canvas: g, name: 'Draft', saved_id: item }, store), true);
  assert.deepEqual(drafts.readDraft(entry, store), { version: 1, entry, seq: 3, at: 10, canvas: g, name: 'Draft', saved_id: item });
  assert.equal(drafts.readDraft(other, store), null);
  const key = drafts.DRAFT_PREFIX + entry, kept = JSON.parse(store.getItem(key));
  for (const damaged of [{ ...kept, canvas: { ...g, edges: [{ from: 'videos', to: 'nowhere', output: 'rows', input: 'rows' }] } }, { ...kept, frozen_ref: 'x' },
    { ...kept, name: 'n'.repeat(121) }, { ...kept, entry: other }, { ...kept, seq: 0 }]) {
    store.setItem(key, JSON.stringify(damaged)); assert.equal(drafts.readDraft(entry, store), null);
  }
  store.setItem(key, '{'); assert.equal(drafts.readDraft(entry, store), null);
  assert.equal(drafts.readDraft(entry, null), null, 'no store: no draft, no error');
  assert.equal(drafts.writeDraft({ version: 1, entry, seq: 1, at: 1, canvas: g }, null), false);
}

// A tab keeps its 20 newest drafts; a full store makes room by dropping the oldest once, and never throws.
{
  const store = memoryStore(), g = start();
  for (let i = 0; i < 25; i++) drafts.writeDraft({ version: 1, entry: `44444444-4444-4444-8444-${String(i).padStart(12, '0')}`, seq: 1, at: 100 + i, canvas: g }, store);
  assert.equal(store.length, 20);
  assert.equal(drafts.readDraft('44444444-4444-4444-8444-000000000000', store), null, 'the oldest went');
  assert.ok(drafts.readDraft('44444444-4444-4444-8444-000000000024', store));
  const one = JSON.stringify({ version: 1, entry, seq: 1, at: 1, canvas: g }).length;
  const tight = memoryStore(one * 3 + 10);
  for (let i = 0; i < 3; i++) drafts.writeDraft({ version: 1, entry: `55555555-5555-4555-8555-${String(i).padStart(12, '0')}`, seq: 1, at: i, canvas: g }, tight);
  assert.equal(drafts.writeDraft({ version: 1, entry, seq: 1, at: 9, canvas: g }, tight), true, 'older drafts made room');
  assert.ok(drafts.readDraft(entry, tight));
  assert.equal(drafts.writeDraft({ version: 1, entry, seq: 2, at: 10, canvas: { ...g, notes: 'n'.repeat(one * 4) } }, memoryStore(one)), false, 'too large: kept nowhere, no error');
  drafts.clearDrafts(store); assert.equal(store.length, 0);
}

// The two copies: after a pause both are written under one number; when the page is left at once — history.state only
// while its entry is still the current one, the tab's draft always (Back, another page).
{
  const store = memoryStore(), timers = [];
  const history = { state: handoff.writeWorkshopHistory({ scroll: 1 }, { version: 1, entry }), replaceState(state) { this.state = state; } };
  const copies = new drafts.CanvasCopies({ history, href: () => 'http://local/app-explorer-v3/research', store: () => store, now: () => 1000,
    schedule: run => { timers.push(run); return timers.length; }, cancel: () => {} });
  let g = { ...start(), notes: 'one' }; const mirror = () => handoff.workshopMirror({ entry, document: graph.newStudy(), canvas: g, name: 'Renamed draft' });
  copies.edited(mirror); g = { ...g, notes: 'two' }; // the latest edit is what is written
  timers.at(-1)();
  assert.equal(history.state.scroll, 1);
  assert.equal(history.state.workshop.canvas.notes, 'two'); assert.equal(history.state.workshop.name, 'Renamed draft'); assert.equal(history.state.workshop.seq, 1);
  assert.deepEqual(drafts.readDraft(entry, store), { version: 1, entry, seq: 1, at: 1000, canvas: g, name: 'Renamed draft' });
  // An edit 100 ms before Back: the address already shows another entry, so only the tab copy takes it.
  g = { ...g, notes: 'three' }; copies.edited(mirror);
  history.state = handoff.writeWorkshopHistory(null, { version: 1, entry: other });
  copies.flush(false);
  assert.equal(history.state.workshop.entry, other, 'the other entry is not written');
  assert.equal(drafts.readDraft(entry, store).canvas.notes, 'three'); assert.equal(drafts.readDraft(entry, store).seq, 2);
  copies.flush(true); assert.equal(drafts.readDraft(entry, store).seq, 2, 'nothing waiting: nothing written');
  // Reload or closing: this entry is current, both copies are written now, not after the pause.
  history.state = handoff.writeWorkshopHistory(null, { version: 1, entry, canvas: g, seq: 2 });
  g = { ...g, notes: 'four' }; copies.edited(mirror); copies.flush(true);
  assert.equal(history.state.workshop.canvas.notes, 'four'); assert.equal(drafts.readDraft(entry, store).canvas.notes, 'four');
  copies.reset(7); copies.edited(mirror); copies.flush(true); assert.equal(history.state.workshop.seq, 8, 'numbering goes on from the opened copy');
  const refusing = new drafts.CanvasCopies({ history: { state: history.state, replaceState() { throw new Error('SecurityError'); } }, href: () => '', store: () => store, now: () => 2000 });
  assert.doesNotThrow(() => refusing.write(mirror(), true)); assert.equal(drafts.readDraft(entry, store).at, 2000, 'the draft still keeps it');
}

// Opening an entry: the newer copy wins; a damaged history copy opens from the draft; with neither, the canvas says so.
{
  const g = { ...start(), notes: 'history copy' }, newer = { ...start(), notes: 'draft copy' };
  const state = seq => handoff.writeWorkshopHistory(null, handoff.workshopMirror({ entry, document: graph.newStudy(), canvas: g, name: 'Old name', seq }));
  const open = async (value, draft) => (await handoff.resolveWorkshopStudy({ state: value, search: '', origin: 'http://local', signal: new AbortController().signal,
    loadSavedView: async () => { throw new Error('not used'); }, handoffFor: () => undefined, readDraft: id => id === entry ? draft : null })).study;
  const tab = { version: 1, entry, seq: 5, at: 1, canvas: newer, name: 'Typed name' };
  let study = await open(state(3), tab);
  assert.deepEqual([study.canvas, study.name, study.seq, study.entry], [newer, 'Typed name', 5, entry]);
  study = await open(state(6), tab);
  assert.deepEqual([study.canvas, study.name, study.seq], [g, 'Old name', 6], 'history.state is newer');
  study = await open(state(6), null);
  assert.deepEqual(study.canvas, g);
  const damaged = state(9); damaged.workshop.canvas.edges.push({ from: 'videos', to: 'nowhere', output: 'rows', input: 'rows' });
  assert.deepEqual(handoff.readWorkshopHistory(damaged), { version: 1, entry, damaged: true });
  study = await open(damaged, tab);
  assert.deepEqual([study.canvas, study.name, study.notice], [newer, 'Typed name', ''], 'the draft keeps it');
  study = await open(damaged, null);
  assert.equal(study.canvas, undefined);
  assert.equal(study.notice, handoff.DAMAGED_CANVAS_NOTE, 'said, not silent');
  assert.equal('damaged' in handoff.writeWorkshopHistory(null, { version: 1, entry, damaged: true }).workshop, false, 'never written');
  const badSeq = state(1); badSeq.workshop.seq = -1;
  assert.equal(handoff.readWorkshopHistory(badSeq).damaged, true);
}
// The tab remembers the Saved revision its edits started from and saves against it, so a newer save in
// another tab is refused (409), never overwritten; reopening says so too. A canvas without unsaved changes is not kept
// in the tab and reopens as its newest saved version.
{
  const g = start(), theirs = { ...start(), notes: 'edited in tab 2' }, mine = { ...start(), notes: 'unsaved note' };
  const view = (revision, value) => ({ id: item, kind: 'view', name: 'Audit canvas', channel_id: null, conditions: { version: 4, graph: value }, revision,
    created_at: '2026-11-01T00:00:00Z', updated_at: '2026-11-01T00:00:00Z', channel_available: true });
  assert.deepEqual(handoff.savedConflict(view(3, theirs), 2, mine), { conflict: true, base: 2 });
  assert.deepEqual(handoff.savedConflict(view(3, mine), 2, mine), { conflict: false, base: 3 }, 'the same graph: continue from the newest');
  assert.deepEqual(handoff.savedConflict(view(2, theirs), 2, mine), { conflict: false, base: 2 });
  assert.deepEqual(handoff.savedConflict(view(3, theirs), undefined, mine), { conflict: false, base: 3 });
  assert.deepEqual(handoff.savedConflict(undefined, 2, mine), { conflict: false, base: undefined });
  const kept = handoff.tabMirror({ entry, document: graph.newStudy(), canvas: mine, name: 'Audit canvas', saved: view(2, g), baseRevision: 2 });
  assert.deepEqual([kept.canvas, kept.base_revision, kept.saved_id], [mine, 2, item]);
  assert.equal('canvas' in handoff.tabMirror({ entry, document: graph.newStudy(), canvas: g, saved: view(2, g), baseRevision: 2 }), false);
  assert.equal(handoff.readWorkshopHistory(handoff.writeWorkshopHistory(null, kept)).base_revision, 2);
  const badBase = handoff.writeWorkshopHistory(null, kept); badBase.workshop.base_revision = 0;
  assert.equal(handoff.readWorkshopHistory(badBase).damaged, true);
  // Tab B saved (revision 3); tab A reloads with its own unsaved note.
  const open = async (state, draft = null) => (await handoff.resolveWorkshopStudy({ state, search: '', origin: 'http://local', signal: new AbortController().signal,
    loadSavedView: async id => { assert.equal(id, item); return view(3, theirs); }, handoffFor: () => undefined, readDraft: () => draft })).study;
  let study = await open(handoff.writeWorkshopHistory(null, kept));
  assert.deepEqual([study.canvas, study.saved.revision, study.baseRevision], [mine, 3, 2]);
  assert.equal(handoff.savedConflict(study.saved, study.baseRevision, study.canvas).conflict, true, 'said on reopening');
  study = await open(handoff.writeWorkshopHistory(null, handoff.tabMirror({ entry, document: graph.newStudy(), canvas: g, saved: view(2, g), baseRevision: 2 })));
  assert.deepEqual([study.canvas, study.baseRevision], [theirs, 3], 'no unsaved changes: the newest saved version');
  assert.equal(handoff.savedConflict(study.saved, study.baseRevision, study.canvas).conflict, false);
  // ?saved=<id> opens at its revision.
  study = (await handoff.resolveWorkshopStudy({ state: null, search: '?saved=' + item, origin: 'http://local', signal: new AbortController().signal,
    loadSavedView: async () => view(3, theirs), handoffFor: () => undefined })).study;
  assert.equal(study.baseRevision, 3);
  // The tab's draft keeps the base revision; an entry equal to its saved canvas has no draft.
  const store = memoryStore(), copies = new drafts.CanvasCopies({ history: { state: null, replaceState() {} }, href: () => '', store: () => store, now: () => 5 });
  copies.write(kept, false);
  assert.equal(drafts.readDraft(entry, store).base_revision, 2);
  const damaged = handoff.writeWorkshopHistory(null, kept); damaged.workshop.canvas = { broken: true };
  study = await open(damaged, drafts.readDraft(entry, store));
  assert.deepEqual([study.canvas, study.baseRevision], [mine, 2], 'the draft carries the base revision');
  copies.write(handoff.tabMirror({ entry, document: graph.newStudy(), canvas: g, saved: view(2, g), baseRevision: 2 }), false);
  assert.equal(drafts.readDraft(entry, store), null, 'saved: the draft goes');
  // Saving sends the base revision; the canvas never advises a reload that would overwrite (the old text is gone).
  const sent = [];
  const api = { requestJson: async (path, signal, init) => { sent.push([path, JSON.parse(init.body)]); const error = new Error('changed'); error.status = 409; throw error; } };
  const writing = compiled('savedViews.ts', { './researchGraph': graph, './api': api, './savedQueryDefinition': definition, '../explorerRoutes': routes, './catalogueQuery': catalogue,
    './savedWork': saved, './timeZones': compiled('timeZones.ts'), './libraryData/savedLibrary.ts': savedLibrary });
  await assert.rejects(writing.updateSavedResearch({ ...view(3, theirs), revision: 2 }, 'Audit canvas', { version: 4, graph: mine }));
  assert.deepEqual([sent[0][0], sent[0][1].expected_revision], ['/saved/views/' + item, 2]);
  const source = readFileSync(new URL('../application/frontend/src/explorer/canvas/CanvasWorkshop.tsx', import.meta.url), 'utf8');
  assert.match(source, /updateSavedResearch\(base !== undefined \? \{ \.\.\.saved, revision: base \} : saved,/);
  for (const label of ['Save mine as a new canvas', 'Open the saved version', 'Replace the saved version with mine', 'Replace the saved version']) assert.ok(source.includes(`>${label}</button>`), label);
  assert.equal(source.includes('Reload it before saving again'), false);
}
// The tab marks the entry it left with unsaved work; a fresh entry at the same address (sign-in came back
// here) takes that entry's draft back, once, within a session's 12 hours. Signing out ends both.
{
  const store = memoryStore(), path = '/app-explorer-v3/research?saved=' + item, now = 1_000_000;
  const g = { ...start(), notes: 'before the session ended' };
  drafts.writeDraft({ version: 1, entry, seq: 4, at: now, canvas: g, name: 'Kept', saved_id: item, base_revision: 2 }, store);
  drafts.markResume(entry, path, now, store);
  assert.equal(drafts.takeResume('/app-explorer-v3/research', now, store), null, 'another address');
  drafts.markResume(entry, path, now, store);
  assert.equal(drafts.takeResume(path, now + 12 * 3600 * 1000 + 1, store), null, 'older than a session');
  drafts.markResume(entry, path, now, store);
  assert.equal(drafts.takeResume(path, now + 60_000, store), entry);
  assert.equal(drafts.takeResume(path, now + 60_000, store), null, 'taken once');
  store.setItem(drafts.RESUME_KEY, '{"version":1,"entry":"nope","path":"' + path + '","at":1}');
  assert.equal(drafts.takeResume(path, 2, store), null, 'damaged mark');
  const view = { id: item, kind: 'view', name: 'Kept', channel_id: null, conditions: { version: 4, graph: start() }, revision: 2,
    created_at: '2026-11-01T00:00:00Z', updated_at: '2026-11-01T00:00:00Z', channel_available: true };
  const open = async (state, resume) => (await handoff.resolveWorkshopStudy({ state, search: '?saved=' + item, origin: 'http://local', signal: new AbortController().signal,
    loadSavedView: async () => view, handoffFor: () => undefined, readDraft: id => drafts.readDraft(id, store), resume })).study;
  let study = await open(null, entry);
  assert.deepEqual([study.entry, study.canvas, study.name, study.baseRevision, study.notice], [entry, g, 'Kept', 2, handoff.RESTORED_CANVAS_NOTE]);
  study = await open(null, null);
  assert.deepEqual([study.canvas, study.notice], [view.conditions.graph, ''], 'no mark: the address decides');
  study = await open(handoff.writeWorkshopHistory(null, { version: 1, entry: other, document: graph.newStudy() }), entry);
  assert.equal(study.entry, other, 'an entry with its own history copy is never replaced by a mark');
  drafts.markResume(entry, path, now, store);
  drafts.clearDrafts(store);
  assert.deepEqual([store.length, drafts.readDraft(entry, store)], [0, null], 'signing out ends the drafts and the mark');
}
// The name is part of what a saved canvas keeps. A name-only change is an unsaved change everywhere — kept in
// both copies, back after Back, Reload and a new sign-in — and a rename saved in another tab is a conflict, never
// replaced silently. Unchanged content follows the newest save; the same content in both tabs asks nothing.
{
  const g = start(), changed = { ...start(), notes: 'changed graph' };
  const view = (revision, name, value = g) => ({ id: item, kind: 'view', name, channel_id: null, conditions: { version: 4, graph: value }, revision,
    created_at: '2026-11-01T00:00:00Z', updated_at: '2026-11-01T00:00:00Z', channel_available: true });
  const study = (name, value = g) => ({ entry, document: graph.newStudy(), canvas: value, name, saved: view(1, 'Original'), baseRevision: 1 });
  // One notion, normalised as Save normalises the name.
  assert.equal(handoff.sameSavedContent(view(1, 'Original'), g, 'Original'), true);
  assert.equal(handoff.sameSavedContent(view(1, 'Original'), g, '  Original  '), true, 'spaces around the name do not count');
  assert.equal(handoff.sameSavedContent(view(1, 'Untitled canvas'), g, ''), true, 'an empty name saves as Untitled canvas');
  assert.equal(handoff.sameSavedContent(view(1, 'Original'), g, 'Unsaved rename'), false, 'a new name alone is a change');
  assert.equal(handoff.sameSavedContent(view(1, 'Original'), changed, 'Original'), false);
  assert.equal(handoff.savedCanvasName('  Kept  '), 'Kept');
  // Name only: kept in both copies, with the start revision.
  const store = memoryStore(), history = { state: handoff.writeWorkshopHistory(null, { version: 1, entry }), replaceState(state) { this.state = state; } };
  const copies = new drafts.CanvasCopies({ history, href: () => '', store: () => store, now: () => 7 });
  copies.write(handoff.tabMirror(study('Unsaved rename')), true);
  assert.deepEqual([history.state.workshop.name, history.state.workshop.base_revision, 'canvas' in history.state.workshop], ['Unsaved rename', 1, true]);
  assert.deepEqual([drafts.readDraft(entry, store)?.name, drafts.readDraft(entry, store)?.base_revision], ['Unsaved rename', 1]);
  const open = async (state, server, extra = {}) => (await handoff.resolveWorkshopStudy({ state, search: '?saved=' + item, origin: 'http://local',
    signal: new AbortController().signal, loadSavedView: async () => server, handoffFor: () => undefined, readDraft: id => drafts.readDraft(id, store), ...extra })).study;
  // Back and Reload open the entry's history copy: the rename is back, on its start revision.
  let opened = await open(history.state, view(1, 'Original'));
  assert.deepEqual([opened.name, opened.baseRevision], ['Unsaved rename', 1]);
  assert.deepEqual(handoff.savedConflict(opened.saved, opened.baseRevision, opened.canvas, opened.name), { conflict: false, base: 1 });
  // A new sign-in opens a fresh entry with the resume mark: the rename comes back too.
  const path = '/app-explorer-v3/research?saved=' + item;
  drafts.markResume(entry, path, 7, store);
  opened = await open(null, view(1, 'Original'), { resume: drafts.takeResume(path, 8, store) });
  assert.deepEqual([opened.name, opened.baseRevision, opened.notice], ['Unsaved rename', 1, handoff.RESTORED_CANVAS_NOTE]);
  // Renamed in two tabs: B saved "Saved in other tab" (revision 2); A reloads and saves — a conflict on revision 1.
  opened = await open(history.state, view(2, 'Saved in other tab'));
  assert.deepEqual([opened.name, opened.baseRevision], ['Unsaved rename', 1]);
  const decision = handoff.savedConflict(opened.saved, opened.baseRevision, opened.canvas, opened.name);
  assert.deepEqual(decision, { conflict: true, base: 1 });
  const sent = [];
  const api = { requestJson: async (url, signal, init) => { sent.push(JSON.parse(init.body)); const error = new Error('changed'); error.status = 409; throw error; } };
  const writing = compiled('savedViews.ts', { './researchGraph': graph, './api': api, './savedQueryDefinition': definition, '../explorerRoutes': routes, './catalogueQuery': catalogue,
    './savedWork': saved, './timeZones': compiled('timeZones.ts'), './libraryData/savedLibrary.ts': savedLibrary });
  await assert.rejects(writing.updateSavedResearch({ ...opened.saved, revision: decision.base }, handoff.savedCanvasName(opened.name), { version: 4, graph: opened.canvas }));
  assert.deepEqual([sent[0].expected_revision, sent[0].name], [1, 'Unsaved rename'], 'Save names the start revision, so the server refuses (409)');
  // The graph changed too, or only the graph: the same conflict.
  assert.equal(handoff.savedConflict(view(2, 'Saved in other tab'), 1, changed, 'Unsaved rename').conflict, true);
  assert.equal(handoff.savedConflict(view(2, 'Original'), 1, changed, 'Original').conflict, true);
  // Both tabs hold exactly the same content: nothing to ask; the tab moves on to the newest revision.
  assert.deepEqual(handoff.savedConflict(view(2, 'Unsaved rename'), 1, g, 'Unsaved rename'), { conflict: false, base: 2 });
  assert.deepEqual(handoff.savedConflict(view(2, 'Unsaved rename'), 1, g, ' Unsaved rename  '), { conflict: false, base: 2 });
  // No unsaved change here: nothing is kept, and Reload shows the newest save — B's name, not this tab's older one.
  copies.write(handoff.tabMirror(study('Original')), true);
  assert.deepEqual([drafts.readDraft(entry, store), 'canvas' in history.state.workshop], [null, false]);
  opened = await open(history.state, view(2, 'Saved in other tab'));
  assert.deepEqual([opened.name, opened.baseRevision, handoff.savedConflict(opened.saved, opened.baseRevision, opened.canvas, opened.name).conflict],
    ['Saved in other tab', 2, false]);
  // The canvas's "Unsaved changes" and its opening conflict use the same rule.
  const source = readFileSync(new URL('../application/frontend/src/explorer/canvas/CanvasWorkshop.tsx', import.meta.url), 'utf8');
  assert.match(source, /const unsaved = !sameSavedContent\(saved, graph, name\);/);
  assert.match(source, /savedConflict\(initialSaved, baseRevision, initial, initialName\)/);
}
console.log('Canvas drafts: tab copy, newer copy wins, damaged copies said, name kept, conflicts never overwrite (name included), sign-in takes it back: ok');
