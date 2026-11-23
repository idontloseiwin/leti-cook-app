// Library → research workshop hand-off and the workshop's tab history.
// Pure functions: no requests, no storage. The fixed-list reference (frozen_ref) lives only in this
// tab's memory: never in the URL, history.state, localStorage, an export or Saved.
import { parseSavedDefinition, type SavedQuery } from './savedQueryDefinition';
import { researchTypeNames, videoSource, type ResearchType } from './researchQuery';
import { MAX_RESEARCH_BYTES, newStudy, openResearch, parseCanvasDraft, parseStudyDocument, researchSize, sourceStudy, studyFromSavedQuery,
  type GraphNode, type ResearchDocument, type RunOptions } from './researchGraph';
import { readWorkshopAddress, RESEARCH_WORKSHOP_PATH, savedOpenTarget, savedViewUrl, type SavedView } from './savedViews';
import { canonicalSaved } from './savedQueryDefinition';
import { newSavedWorkId, savedId } from './savedWork';
import type { CanvasGraph } from './canvas/canvasModel';
import type { CanvasDraft } from './canvasDrafts';

export type HandoffOrigin = { surface: 'library'; channel_id: string; channel_title?: string; format: ResearchType; view: 'grid' | 'table' };
// Library's live frozen list: context.frozen_ref with its clocks and page.total_count.
export type FixedListReference = { ref: string; evaluated_at: string; valid_before: string; total_count: number };
export type FrozenPageContext = { context: { frozen_ref: string; evaluated_at: string; valid_before: string }; page: { total_count: number } };
export type WorkshopHandoff = {
  version: 1;
  kind: 'selection' | 'selected_ids';
  origin: HandoffOrigin;
  // Exactly what Library executed for its first page: scope bound to the channel id,
  // consistency 'current', without page.after / page.anchor_id.
  query: SavedQuery;
  // 'selection' only, while Library holds a live frozen list.
  frozen?: FixedListReference;
  // 'selected_ids' only: 1..1000 unique IDs of query.video_types[0], in selection order.
  ids?: string[];
  other_formats_excluded?: number;
};
export type HandoffRefusal = { refused: 'nothing_selected' | 'too_many' | 'too_large' | 'unsupported_query'; count?: number; bytes?: number; capabilities?: string[] };
// The graph run mode: current reads, or one fixed list, reused while its reference lives (researchGraph.runResearch).
export type { RunOptions } from './researchGraph';
export const MAX_SELECTED_IDS = 1000;

const CHANNEL_ID = /^UC[A-Za-z0-9_-]{22}$/, VIDEO_ID = /^[A-Za-z0-9_-]{11}$/;
const STAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$/;
const TOKEN = /^[\x21-\x7e]{1,16384}$/; // opaque, like the server's cursor limit
const ENTRY = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const types: ResearchType[] = ['long', 'short', 'stream'];
function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null;
}
function keys(value: Record<string, unknown>, required: string[], optional: string[] = []): boolean {
  return required.every(key => Object.hasOwn(value, key)) && Object.keys(value).every(key => required.includes(key) || optional.includes(key));
}
const instant = (value: unknown): number => typeof value === 'string' && STAMP.test(value) ? Date.parse(value) : NaN;
const unsupported = (): HandoffRefusal => ({ refused: 'unsupported_query' });

function parseOrigin(value: unknown): HandoffOrigin | null {
  const o = record(value);
  if (!o || !keys(o, ['surface', 'channel_id', 'format', 'view'], ['channel_title']) || o.surface !== 'library'
    || typeof o.channel_id !== 'string' || !CHANNEL_ID.test(o.channel_id) || !types.includes(o.format as ResearchType)
    || !['grid', 'table'].includes(String(o.view))
    || o.channel_title !== undefined && (typeof o.channel_title !== 'string' || [...o.channel_title].length > 1000)) return null;
  return { surface: 'library', channel_id: o.channel_id, format: o.format as ResearchType, view: o.view as 'grid' | 'table',
    ...(o.channel_title !== undefined ? { channel_title: o.channel_title as string } : {}) };
}
/** Library's query as the study's root query: paging tokens removed, current reads, same channel and format. */
function studyQuery(raw: unknown, origin: HandoffOrigin): SavedQuery | HandoffRefusal {
  const source = record(raw);
  if (!source) return unsupported();
  const query = structuredClone(source), page = record(query.page), scope = record(query.scope);
  if (page) { delete page.after; delete page.anchor_id; if (!Object.keys(page).length) delete query.page; }
  query.consistency = 'current';
  if (record(query.source)?.id !== videoSource || !scope || scope.kind !== 'channel' || scope.id !== origin.channel_id
    || !Array.isArray(query.video_types) || query.video_types.length !== 1 || query.video_types[0] !== origin.format) return unsupported();
  try { parseSavedDefinition({ version: 2, query: { ...query, scope: { ...scope, id: '$channel' } } }); } catch { return unsupported(); }
  return query as SavedQuery;
}
function fixedReference(value: FrozenPageContext): FixedListReference | null {
  const context = record(value?.context), total = record(value?.page)?.total_count;
  if (!context || typeof context.frozen_ref !== 'string' || !TOKEN.test(context.frozen_ref)
    || !(instant(context.valid_before) > instant(context.evaluated_at)) || !Number.isSafeInteger(total) || Number(total) < 0) return null;
  return { ref: context.frozen_ref, evaluated_at: context.evaluated_at as string, valid_before: context.valid_before as string, total_count: total as number };
}
function sized(handoff: WorkshopHandoff): WorkshopHandoff | HandoffRefusal {
  const size = researchSize(studyFromHandoff(handoff).document);
  if (size.bytes > MAX_RESEARCH_BYTES) return { refused: 'too_large', bytes: size.bytes };
  return size.valid ? handoff : unsupported();
}

/** "Open in research workshop": the whole selection Library executed, never the visible page's IDs. */
export function selectionHandoff(libraryQuery: unknown, frozen: FrozenPageContext | null, origin: HandoffOrigin): WorkshopHandoff | HandoffRefusal {
  const from = parseOrigin(origin);
  if (!from) return unsupported();
  const query = studyQuery(libraryQuery, from);
  if ('refused' in query) return query;
  const reference = frozen ? fixedReference(frozen) : undefined;
  if (reference === null) return unsupported();
  return sized({ version: 1, kind: 'selection', origin: from, query, ...(reference ? { frozen: reference } : {}) });
}
/** "Send selected to workshop": only the explicitly selected IDs of the query's format; nothing is cut silently. */
export function selectedIdsHandoff(libraryQuery: unknown, selected: Iterable<{ id: string; type?: ResearchType }>, origin: HandoffOrigin): WorkshopHandoff | HandoffRefusal {
  const from = parseOrigin(origin);
  if (!from) return unsupported();
  const query = studyQuery(libraryQuery, from);
  if ('refused' in query) return query;
  const ids: string[] = [], seen = new Set<string>();
  let other = 0;
  for (const item of selected) {
    if (!item || typeof item.id !== 'string' || !VIDEO_ID.test(item.id) || item.type !== undefined && !types.includes(item.type)) return unsupported();
    if (item.type !== undefined && item.type !== from.format) { other++; continue; }
    if (!seen.has(item.id)) { seen.add(item.id); ids.push(item.id); }
  }
  if (!ids.length) return { refused: 'nothing_selected', ...(other ? { count: other } : {}) };
  if (ids.length > MAX_SELECTED_IDS) return { refused: 'too_many', count: ids.length };
  return sized({ version: 1, kind: 'selected_ids', origin: from, query, ids, ...(other ? { other_formats_excluded: other } : {}) });
}
/** After "Select all matching": the selection is Library's whole frozen list of the same session. */
export function isWholeFrozenList(selected: ReadonlySet<string>, frozenIds: readonly string[]): boolean {
  return frozenIds.length > 0 && new Set(frozenIds).size === frozenIds.length && selected.size === frozenIds.length && frozenIds.every(id => selected.has(id));
}
const plainName = (text: string) => [...text.replace(/[\u0000-\u001f\u007f]/g, ' ').trim()].slice(0, 120).join('');
/** The study a hand-off opens: one run on arrival; the reference only travels as a run option. */
export function studyFromHandoff(handoff: WorkshopHandoff): { document: ResearchDocument; name: string; run: RunOptions; autoRun: true; notice: string } {
  const { origin } = handoff, format = researchTypeNames[origin.format], who = origin.channel_title?.trim() || origin.channel_id;
  if (handoff.kind === 'selection') return {
    document: sourceStudy(handoff.query, origin.view === 'grid' ? 'gallery' : 'table'), name: plainName(`${who} · ${format} selection`),
    run: handoff.frozen ? { consistency: 'frozen', frozen_ref: handoff.frozen.ref } : { consistency: 'frozen' }, autoRun: true,
    notice: `Opened from Library: ${who} · ${format}, with Library's conditions, sorting and columns.`,
  };
  const ids = handoff.ids ?? [], excluded = handoff.other_formats_excluded ?? 0;
  // Library keeps a selection across filter changes, so its conditions are not applied here:
  // with them, some selected IDs could disappear without a word.
  const query = structuredClone(handoff.query); query.where = null;
  const nodes: GraphNode[] = [{ id: 'source', type: 'source', parameters: {} }, { id: 'selected', type: 'selection', parameters: { mode: 'include', ids: [...ids] } },
    { id: 'result', type: 'table', parameters: {} }];
  const document: ResearchDocument = { version: 1, query, graph: { version: 1, nodes,
    edges: [{ from: 'source', to: 'selected', input: 'query', output: 'query' }, { from: 'selected', to: 'result', input: 'query', output: 'query' }],
    notes: '', layout: { source: { x: 24, y: 36 }, selected: { x: 304, y: 36 }, result: { x: 584, y: 36 } } } };
  return { document, name: plainName(`${who} · selected ${format}`), run: { consistency: 'current' }, autoRun: true,
    notice: `${ids.length.toLocaleString('en-US')} ${ids.length === 1 ? 'ID' : 'IDs'} from Library. Library's conditions are not applied, so every selected video stays in the study.`
      + (excluded ? ` ${excluded.toLocaleString('en-US')} selected ${excluded === 1 ? 'video of another format is' : 'videos of other formats are'} not included: a study has one format.` : '') };
}
/** The hand-off as it may be written to history.state: without the fixed-list reference. */
export function withoutReference(handoff: WorkshopHandoff): WorkshopHandoff {
  const copy = structuredClone(handoff);
  delete copy.frozen;
  return copy;
}
/** A hand-off read back from history.state. A reference there means it was not written by this page: refused. */
export function parseHandoff(value: unknown): WorkshopHandoff | null {
  const h = record(value);
  if (!h || !keys(h, ['version', 'kind', 'origin', 'query'], ['ids', 'other_formats_excluded']) || h.version !== 1) return null;
  const origin = parseOrigin(h.origin);
  if (!origin) return null;
  const query = studyQuery(h.query, origin);
  if ('refused' in query) return null;
  if (h.other_formats_excluded !== undefined && (!Number.isSafeInteger(h.other_formats_excluded) || Number(h.other_formats_excluded) < 1)) return null;
  if (h.kind === 'selection') return h.ids === undefined ? { version: 1, kind: 'selection', origin, query } : null;
  if (h.kind !== 'selected_ids' || !Array.isArray(h.ids) || !h.ids.length || h.ids.length > MAX_SELECTED_IDS
    || h.ids.some(id => typeof id !== 'string' || !VIDEO_ID.test(id)) || new Set(h.ids).size !== h.ids.length) return null;
  return { version: 1, kind: 'selected_ids', origin, query, ids: [...h.ids as string[]],
    ...(h.other_formats_excluded !== undefined ? { other_formats_excluded: h.other_formats_excluded as number } : {}) };
}

// ---- Fixed list lifetime. Nothing replaces a list silently. ----
export const EXPIRY_CHOICES = [{ id: 'fix_new_list', label: 'Fix a new list' }, { id: 'use_current_reads', label: 'Use current reads' }] as const;
export type ExpiryChoice = typeof EXPIRY_CHOICES[number]['id'];
export type FixedListDecision =
  | { action: 'reuse'; run: { consistency: 'frozen'; frozen_ref: string }; notice: string }
  | { action: 'new_list'; run: { consistency: 'frozen' }; notice: string }
  | { action: 'choose'; notice: string };
const clock = (iso: string, seconds = true) => iso.slice(11, seconds ? 19 : 16);
/** Before a fixed-list run: reuse Library's list while it lives; once it has expired, the user chooses. */
export function fixedListDecision(reference: FixedListReference | undefined, now: number): FixedListDecision {
  if (!reference) return { action: 'new_list', run: { consistency: 'frozen' }, notice: 'Opened again: Run fixes a new list.' };
  if (Date.parse(reference.valid_before) <= now) return { action: 'choose', notice: `Library's fixed list expired at ${clock(reference.valid_before, false)} UTC.` };
  return { action: 'reuse', run: { consistency: 'frozen', frozen_ref: reference.ref },
    notice: `Fixed list from Library · ${reference.total_count.toLocaleString('en-US')} ${reference.total_count === 1 ? 'video' : 'videos'} · fixed at ${clock(reference.evaluated_at)} UTC · until ${clock(reference.valid_before)} UTC` };
}
/** A run refused because the list is gone (restart, eviction, hidden channel) or access changed: drop it and ask. */
export function fixedListRefusal(code: string | undefined): { action: 'choose'; notice: string } | null {
  return code === 'cursor_expired' || code === 'cursor_context_changed'
    ? { action: 'choose', notice: 'The fixed list is no longer available: data or access changed.' } : null;
}
// ---- Tab history: the study document mirrored in history.state.workshop. ----
// canvas: the canvas this entry shows, unsaved edits included (the study document stays for older entries).
// seq: which copy is newer, this one or the tab's draft of the same entry (canvasDrafts); it grows with every write.
// damaged: read only, never written — the mirror is there but cannot be read (then the tab's draft may still be).
// base_revision: the revision of the Saved canvas this tab's edits started from; saving sends it.
export type WorkshopHistory = { version: 1; entry: string; document?: ResearchDocument; canvas?: CanvasGraph; name?: string; saved_id?: string;
  handoff?: WorkshopHandoff; seq?: number; base_revision?: number; damaged?: true };
export const newWorkshopEntry = (): string => newSavedWorkId();
/** The mirror of this history entry; a damaged or older mirror keeps only its entry id and says it is damaged. */
export function readWorkshopHistory(state: unknown): WorkshopHistory | null {
  const w = record(record(state)?.workshop);
  if (!w || w.version !== 1 || typeof w.entry !== 'string' || !ENTRY.test(w.entry)) return null;
  const entry = w.entry;
  try {
    if (!keys(w, ['version', 'entry'], ['document', 'canvas', 'name', 'saved_id', 'handoff', 'seq', 'base_revision'])) throw new Error();
    const mirror: WorkshopHistory = { version: 1, entry };
    if (w.seq !== undefined) { if (!Number.isSafeInteger(w.seq) || Number(w.seq) < 0) throw new Error(); mirror.seq = w.seq as number; }
    if (w.base_revision !== undefined) { if (!Number.isSafeInteger(w.base_revision) || Number(w.base_revision) < 1) throw new Error(); mirror.base_revision = w.base_revision as number; }
    if (w.document !== undefined) mirror.document = parseStudyDocument(w.document);
    // Without the save budget: an unsaved canvas over it opens (and says so on Save) instead of being lost.
    if (w.canvas !== undefined) mirror.canvas = parseCanvasDraft({ version: 4, graph: w.canvas }).graph;
    if (w.name !== undefined) { if (typeof w.name !== 'string' || [...w.name].length > 120) throw new Error(); mirror.name = w.name; }
    if (w.saved_id !== undefined) mirror.saved_id = savedId(w.saved_id);
    if (w.handoff !== undefined) { const handoff = parseHandoff(w.handoff); if (!handoff) throw new Error(); mirror.handoff = handoff; }
    return mirror;
  } catch { return { version: 1, entry, damaged: true }; }
}
/** history.state with this entry's mirror; other keys of the state are kept, the reference never written. */
export function writeWorkshopHistory(state: unknown, mirror: WorkshopHistory): Record<string, unknown> {
  const { handoff, damaged: _damaged, ...rest } = mirror;
  return { ...(record(state) ?? {}), workshop: { ...structuredClone(rest), ...(handoff ? { handoff: withoutReference(handoff) } : {}) } };
}

// ---- Opening /research?saved=<id> . ----
export type SavedStudyPlan =
  | { action: 'leave'; url: URL }
  | { action: 'refuse'; notice: string }
  | { action: 'open'; document: ResearchDocument; canvas?: CanvasGraph; name: string; saved?: SavedView; fromQuery: boolean; autoRun: boolean; sourceMissing: boolean; notice: string };
/** v3 opens as saved; a v2 query opens as a first study (one run); v1 and Library v2 views open in Library. */
export function savedStudyPlan(view: SavedView, origin: string): SavedStudyPlan {
  const target = savedOpenTarget(view.conditions), missing = !view.channel_available;
  // A missing source is stated once, by the Source block the study opens on (sourceMissing).
  // A canvas (v4) opens as saved; its graph names every source. The study document is not used by the canvas.
  if (target === 'canvas' && view.conditions.version === 4) return { action: 'open', document: newStudy(), canvas: structuredClone(view.conditions.graph),
    name: view.name, saved: view, fromQuery: false, autoRun: false, sourceMissing: missing, notice: '' };
  if (target === 'study' && view.conditions.version === 3) return { action: 'open', document: openResearch(view.conditions, view.channel_id),
    name: view.name, saved: view, fromQuery: false, autoRun: false, sourceMissing: missing, notice: '' };
  let fallback = '';
  if (target === 'library') {
    if (view.conditions.version === 1 && missing) return { action: 'refuse', notice: 'This saved view’s channel is unavailable. The saved view is kept.' };
    const url = savedViewUrl(view, origin);
    if (url.pathname.replace(/\/$/, '') !== RESEARCH_WORKSHOP_PATH) return { action: 'leave', url };
    // Library cannot show this view (yet): it opens here with the same query instead of looping back.
    fallback = 'This view was saved in Library. It opens here as a study with the same conditions.';
  }
  if (view.conditions.version !== 2) return { action: 'refuse', notice: 'This saved item cannot be opened here.' };
  const grid = (view.conditions as { presentation?: { view?: unknown } }).presentation?.view === 'grid';
  return { action: 'open', document: studyFromSavedQuery(view.conditions, view.channel_id, grid ? 'gallery' : 'table'), name: view.name,
    fromQuery: true, autoRun: !missing, sourceMissing: missing, notice: fallback };
}

// ---- The study a workshop history entry shows. ----
export type WorkshopStudy = { entry: string; document: ResearchDocument; canvas?: CanvasGraph; name?: string; saved?: SavedView; fromQuery: boolean; autoRun: boolean;
  notice: string; sourceMissing: boolean; handoff?: WorkshopHandoff;
  // The number of the copy it opened from (history.state or the tab's draft); later writes go on from it.
  seq?: number;
  // The Saved revision this tab's edits started from (when it edits a saved canvas).
  baseRevision?: number;
  // How the next graph run reads rows. Library's reference stays in this tab's memory only.
  run: RunOptions };
export function workshopMirror(study: Pick<WorkshopStudy, 'entry' | 'document' | 'name' | 'saved' | 'handoff' | 'seq' | 'baseRevision'> & { canvas?: CanvasGraph }): WorkshopHistory {
  return { version: 1, entry: study.entry, document: study.document, ...(study.canvas ? { canvas: study.canvas } : {}), ...(study.name !== undefined ? { name: study.name } : {}),
    ...(study.saved ? { saved_id: study.saved.id } : {}), ...(study.handoff ? { handoff: study.handoff } : {}), ...(study.seq !== undefined ? { seq: study.seq } : {}),
    ...(study.baseRevision !== undefined ? { base_revision: study.baseRevision } : {}) };
}
// ---- What a saved canvas keeps: its graph and its name. One notion for "Unsaved changes", the tab's copies, the
// revision the edits started from and a conflict (including name changes). ----
/** The name as Save stores it: trimmed, and "Untitled canvas" when empty (savedWork.savedName trims the same way). */
export const savedCanvasName = (name: string): string => name.trim() || 'Untitled canvas';
/** Whether this graph and name are exactly what the Saved canvas holds. An unknown name (undefined) is the saved one. */
export function sameSavedContent(saved: SavedView | undefined, canvas: CanvasGraph | undefined, name: string | undefined): boolean {
  return saved?.conditions.version === 4 && canvas !== undefined && canonicalSaved(canvas) === canonicalSaved(saved.conditions.graph)
    && (name === undefined || savedCanvasName(name) === saved.name);
}
/** What the tab keeps of an entry: a canvas whose graph and name equal its Saved version is not kept, so it reopens as
 *  saved (newer saves elsewhere included); a changed one — a new name alone too — is kept with the revision its edits
 *  started from. */
export function tabMirror(study: Parameters<typeof workshopMirror>[0]): WorkshopHistory {
  return workshopMirror({ ...study, canvas: sameSavedContent(study.saved, study.canvas, study.name) ? undefined : study.canvas });
}
/** A saved canvas opened with this tab's unsaved edits (graph or name): they conflict when it was saved elsewhere after
 *  they began with other content. The edits keep their start revision; only when both hold exactly the same content does
 *  the tab move on to the newest revision. */
export function savedConflict(saved: SavedView | undefined, baseRevision: number | undefined, canvas: CanvasGraph, name?: string): { conflict: boolean; base: number | undefined } {
  if (saved?.conditions.version !== 4) return { conflict: false, base: undefined };
  const same = sameSavedContent(saved, canvas, name);
  return { conflict: !same && baseRevision !== undefined && saved.revision > baseRevision, base: same || baseRevision === undefined ? saved.revision : baseRevision };
}
// A Library selection reopened from this tab's history: its fixed list stayed in memory only.
export const REOPENED_SELECTION_NOTE = 'Opened again: Run fixes a new list.';
// Neither of the tab's copies of this entry's unsaved canvas could be read: said, never replaced silently.
export const DAMAGED_CANVAS_NOTE = "This page's unsaved canvas could not be reopened: it is damaged or from another version.";
// A fresh entry took back the unsaved canvas this tab left at this address (an ended session, a reload after it).
export const RESTORED_CANVAS_NOTE = 'Restored your unsaved canvas.';
/** This entry's study: its history mirror (reload, Back, Forward), else a Library hand-off, else ?saved=, else a new study.
 *  Returns null when cancelled, or the Library address a Saved item opens at. */
export async function resolveWorkshopStudy({ state, search, origin, signal, loadSavedView, handoffFor, readDraft, resume }: {
  state: unknown; search: string; origin: string; signal: AbortSignal;
  loadSavedView: (id: string, signal: AbortSignal) => Promise<SavedView>; handoffFor: (entry: string) => WorkshopHandoff | undefined;
  // This tab's draft of an entry (canvasDrafts.readDraft); without it only history.state is read.
  readDraft?: (entry: string) => CanvasDraft | null;
  // The entry this tab left with unsaved work at this address (canvasDrafts.takeResume): used by a fresh entry only.
  resume?: string | null;
}): Promise<{ study: WorkshopStudy } | { leave: URL } | null> {
  const address = readWorkshopAddress(search);
  // A fresh entry (sign-in came back here, or a reload after the session ended lost history.state) takes back the draft
  // of the entry this tab left at this address, under that entry's id.
  const resumed = !readWorkshopHistory(state) && resume ? readDraft?.(resume) ?? null : null;
  const found = resumed ? { version: 1 as const, entry: resumed.entry, seq: 0 } : readWorkshopHistory(state);
  // The tab's draft wins when it is newer (the page was left before history.state was written) or when history.state's
  // copy cannot be read. The study document and Library's hand-off stay history.state's.
  const draft = found ? readDraft?.(found.entry) ?? null : null;
  const fromDraft = Boolean(found && draft && (found.damaged || draft.seq > (found.seq ?? 0)));
  const mirror: WorkshopHistory | null = fromDraft && found && draft ? { version: 1, entry: found.entry, document: found.document ?? newStudy(), canvas: draft.canvas,
    ...(draft.name !== undefined ? { name: draft.name } : {}), ...(draft.saved_id ? { saved_id: draft.saved_id } : {}), ...(found.handoff ? { handoff: found.handoff } : {}), seq: draft.seq,
    ...(draft.base_revision !== undefined ? { base_revision: draft.base_revision } : {}) } : found;
  const base: WorkshopStudy = { entry: mirror?.entry ?? newWorkshopEntry(), document: newStudy(), fromQuery: false, autoRun: false, notice: '',
    sourceMissing: false, run: { consistency: 'current' } };
  let study = base;
  if (mirror?.document) {
    study = { ...base, document: mirror.document, canvas: mirror.canvas, name: mirror.name, handoff: mirror.handoff, seq: mirror.seq };
    // A Library selection keeps reading a fixed list; its reference did not survive the reload.
    if (mirror.handoff?.kind === 'selection') { study.run = { consistency: 'frozen' }; study.notice = REOPENED_SELECTION_NOTE; }
    if (mirror.saved_id) {
      try {
        const view = await loadSavedView(mirror.saved_id, signal);
        if (view.conditions.version === 3) study.saved = view;
        // A saved canvas reopens with this entry's unsaved edits, or as its newest saved version; saving updates the same
        // item against the revision the edits started from, so a newer save elsewhere is never overwritten.
        // Without unsaved changes (no canvas kept) the name is the newest saved one too: an older name kept in the tab
        // would otherwise look like this tab's rename and replace another tab's.
        if (view.conditions.version === 4) { study.saved = view; study.canvas = mirror.canvas ?? structuredClone(view.conditions.graph);
          study.name = mirror.canvas ? mirror.name ?? view.name : view.name;
          study.baseRevision = mirror.canvas ? mirror.base_revision ?? view.revision : view.revision; }
      }
      catch { if (signal.aborted) return null; study.notice = 'This saved item was deleted or is unavailable. Your study is kept; saving creates a new item.'; }
    }
  } else if (mirror?.handoff) {
    // Memory first: only it keeps Library's fixed-list reference.
    const handoff = handoffFor(base.entry) ?? mirror.handoff, opened = studyFromHandoff(handoff);
    // The workshop states the fixed list (Library's, a new one, or the choice once it expired) before it runs.
    study = { ...base, document: opened.document, name: opened.name, autoRun: true, handoff, run: opened.run, notice: opened.notice };
  } else if (address.error) study = { ...base, notice: address.error };
  else if (address.saved) {
    try {
      const plan = savedStudyPlan(await loadSavedView(address.saved, signal), origin);
      if (signal.aborted) return null;
      if (plan.action === 'leave') return { leave: plan.url };
      study = plan.action === 'refuse' ? { ...base, notice: plan.notice } : { ...base, document: plan.document, canvas: plan.canvas, name: plan.name, saved: plan.saved,
        fromQuery: plan.fromQuery, autoRun: plan.autoRun, sourceMissing: plan.sourceMissing, notice: plan.notice,
        ...(plan.saved?.conditions.version === 4 ? { baseRevision: plan.saved.revision } : {}) };
    } catch { if (signal.aborted) return null; study = { ...base, notice: 'This saved item was deleted or is unavailable.' }; }
  }
  if (found?.damaged && !fromDraft) study = { ...study, notice: [DAMAGED_CANVAS_NOTE, study.notice].filter(Boolean).join(' ') };
  if (resumed && fromDraft) study = { ...study, notice: [RESTORED_CANVAS_NOTE, study.notice].filter(Boolean).join(' ') };
  return signal.aborted ? null : { study };
}
