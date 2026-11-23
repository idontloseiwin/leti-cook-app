// The research workshop's model, pure functions only: the block registry, Undo/Redo of
// study edits, and what a result block says and how it pages. Requests only to /research/queries/execute.
import { ExplorerApiError, requestJson } from './api';
import { NODE_KINDS, OPERATION_OF_BLOCK, nodePath, parseBlockPage, type BlockPage, type GraphResult, type NodeKind, type ResearchDocument } from './researchGraph';
import { FORMAT_TURNED_OFF_TEXT, researchError, type ResearchFilters, type ResearchFrozenPage } from './researchQuery';
import type { FixedListReference } from './workshopHandoff';

// ---- Block registry ----
// The research workshop's block registry: every block type the browser draws, the server
// operation it runs, its default parameters and how its result is shown. The server derives its calculation
// blocks from its operation registry (graph.OPERATION_TYPES); tests/fixtures/research_operation_blocks.json
// pins both sides. A block whose operation the server does not grant is never offered.

export type BlockGrain = 'video' | 'channel';
export type BlockSpec = {
  type: NodeKind;
  label: string;
  // source: the study's root query; query: changes the rows it passes on; rows/operation: a result block.
  role: 'source' | 'query' | 'rows' | 'operation';
  operation?: string;
  grains: readonly BlockGrain[];
  // How the result is shown in the browser; null: a query block has no result of its own.
  display: 'rows' | 'operation' | null;
  // Explicit YouTube acquisition: never run by Run research, offered only when the server
  // declares it, in its own palette group. No such block exists yet.
  acquisition: false;
  // The server refuses the whole study when this block's scope is not one channel.
  needsChannelScope?: true;
  defaults: (timeZone: string) => Record<string, unknown>;
};
const none = () => ({});
export const WORKSHOP_BLOCKS: Readonly<Record<NodeKind, BlockSpec>> = {
  source: { type: 'source', label: 'Source', role: 'source', grains: ['video', 'channel'], display: null, acquisition: false, defaults: none },
  filter: { type: 'filter', label: 'Conditions', role: 'query', grains: ['video', 'channel'], display: null, acquisition: false, defaults: () => ({ where: null }) },
  selection: { type: 'selection', label: 'Selected IDs', role: 'query', grains: ['video', 'channel'], display: null, acquisition: false,
    defaults: () => ({ mode: 'include', ids: [] }) },
  table: { type: 'table', label: 'Table', role: 'rows', grains: ['video', 'channel'], display: 'rows', acquisition: false, defaults: none },
  gallery: { type: 'gallery', label: 'Gallery', role: 'rows', grains: ['video', 'channel'], display: 'rows', acquisition: false, defaults: none },
  summary: { type: 'summary', label: 'Fingerprint', role: 'operation', operation: OPERATION_OF_BLOCK.summary, grains: ['video'], display: 'operation',
    acquisition: false, defaults: none },
  calendar: { type: 'calendar', label: 'Publishing calendar', role: 'operation', operation: OPERATION_OF_BLOCK.calendar, grains: ['video'], display: 'operation',
    // The calendar counts in the study's zone (Source › Dates and years, `time.timezone`):
    // a new block carries no zone of its own; a block saved with one keeps it until removed.
    acquisition: false, defaults: () => ({ year: new Date().getUTCFullYear() }) },
  history: { type: 'history', label: 'Channel history', role: 'operation', operation: OPERATION_OF_BLOCK.history, grains: ['channel'], display: 'operation',
    acquisition: false, needsChannelScope: true, defaults: () => ({ metric: 'views', hours: 696, bucket_hours: 24 }) },
};
export const blockLabel = (type: NodeKind): string => WORKSHOP_BLOCKS[type].label;
export const isOutputBlock = (type: NodeKind): boolean => WORKSHOP_BLOCKS[type].display !== null;
/** Blocks the "Add block" list offers for this row kind: query and result blocks, calculations only when granted. */
export function offeredBlocks(grain: BlockGrain, operations: readonly string[]): NodeKind[] {
  return NODE_KINDS.filter(type => {
    const spec = WORKSHOP_BLOCKS[type];
    return spec.role !== 'source' && !spec.acquisition && spec.grains.includes(grain) && (!spec.operation || operations.includes(spec.operation));
  });
}
// ---- Undo / Redo ----
// Undo / Redo of study edits: up to 50 steps, in this tab's memory only. Pure functions.
// One step per edit; a run of edits with the same key (dragging one block, typing notes, nudging
// a block with the arrow keys) is one step.
export const HISTORY_LIMIT = 50;
export type EditHistory<T> = { past: T[]; present: T; future: T[]; key?: string };

export function startHistory<T>(present: T): EditHistory<T> {
  return { past: [], present, future: [] };
}
/** The next state as a new step, or merged into the last step when it continues the same edit (same key). */
export function recordEdit<T>(history: EditHistory<T>, next: T, key?: string): EditHistory<T> {
  if (next === history.present) return history;
  if (key !== undefined && key === history.key) return { ...history, present: next, future: [] };
  const past = [...history.past, history.present].slice(-HISTORY_LIMIT);
  return { past, present: next, future: [], key };
}
export function undoEdit<T>(history: EditHistory<T>): EditHistory<T> {
  if (!history.past.length) return history;
  return { past: history.past.slice(0, -1), present: history.past[history.past.length - 1], future: [history.present, ...history.future] };
}
export function redoEdit<T>(history: EditHistory<T>): EditHistory<T> {
  if (!history.future.length) return history;
  return { past: [...history.past, history.present].slice(-HISTORY_LIMIT), present: history.future[0], future: history.future.slice(1) };
}
/** Ctrl+Z / Ctrl+Shift+Z / Ctrl+Y (Cmd on macOS) outside text fields: a field keeps its own undo. */
export function shortcutAction(event: { key: string; ctrlKey: boolean; metaKey: boolean; shiftKey: boolean; altKey: boolean;
  target: { tagName?: string; isContentEditable?: boolean } | null }): 'undo' | 'redo' | null {
  if (!(event.ctrlKey || event.metaKey) || event.altKey) return null;
  const tag = event.target?.tagName?.toUpperCase();
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || event.target?.isContentEditable) return null;
  const key = event.key.toLowerCase();
  if (key === 'z') return event.shiftKey ? 'redo' : 'undo';
  if (key === 'y' && !event.shiftKey) return 'redo';
  return null;
}

// ---- Result blocks: errors, read times, fixed lists, paging ----
// What a workshop result block says and how it pages: readable errors, read times,
// fixed-list lines, next/previous pages of one block. No storage; requests only to /research/queries/execute.

const BLOCK_ERRORS: Record<string, string> = {
  catalogue_preparing: 'Stored data is being prepared in the background. Run again shortly.',
  query_busy: 'The server is busy with other reads or lists. Run again shortly.',
  incomplete_source: 'This source is incomplete. Turn off “Require complete data” to see the available rows.',
  source_unavailable: 'This source is no longer available. Choose another source.',
  grant_required: 'This calculation requires an accepted analytics use case and server permission.',
  policy_denied: 'Your access has ended, or this installation does not allow this request.',
  field_disabled: 'A field of this block is disabled. Remove it from the study or enable it in Collection fields.',
  dependency_disabled: 'Data needed by this block is disabled.',
  format_turned_off: FORMAT_TURNED_OFF_TEXT,
  unsupported_field: 'A field of this block is not supported by this server.',
  unsupported_operator: 'A condition of this block is not supported by this server.',
  invalid_query: 'Check the conditions and values of this block, then run again.',
  cursor_expired: 'This list expired. Run research to read it again.',
  cursor_context_changed: 'Data or access changed. Run research again.',
  backend_unavailable: 'Stored data is temporarily unavailable. Run again shortly.',
};
/** A block's error in words; a raw code is never the message. */
export function blockError(code: string | undefined, path?: string | null, limit?: string): string {
  // One refusal code for more than 50,000 rows and for the server's memory limit: only a named bound gets its words.
  if (code === 'query_budget_exceeded') return path !== 'consistency' ? 'This block reached its limits. Narrow the selection and run again.'
    : limit === 'rows' ? 'Too many rows to fix (more than 50,000). Use current reads.'
      : limit === 'memory' ? 'Too much data to fix as one list (over the server’s memory limit). Use current reads.'
        : 'Too large to fix as one list. Use current reads.';
  return (code && BLOCK_ERRORS[code]) || 'This block could not run. No result is shown.';
}
const clock = (iso: string) => iso.slice(11, 19);
/** "Fixed list · fixed at 10:00:12 UTC · until 10:10:12 UTC" or "Current read at 10:03:20 UTC · valid until 10:05:20". */
export function blockTimeLabel(context: { evaluated_at: string; valid_before: string; consistency?: string }): string {
  return context.consistency === 'frozen'
    ? `Fixed list · fixed at ${clock(context.evaluated_at)} UTC · until ${clock(context.valid_before)} UTC`
    : `Current read at ${clock(context.evaluated_at)} UTC · valid until ${clock(context.valid_before)}`;
}
/** A calculation in a fixed-list study reads current data (until operations accept a fixed population). */
export function calculationNote(evaluatedAt: string): string {
  return `Calculated from current data at ${clock(evaluatedAt)} UTC with the same conditions, not from the fixed list.`;
}
export function fixedListLine(fixed: { evaluated_at: string; valid_before: string; total_count: number }, fromLibrary: boolean, channels: boolean): string {
  const n = fixed.total_count, noun = channels ? (n === 1 ? 'channel' : 'channels') : (n === 1 ? 'video' : 'videos');
  return `${fromLibrary ? 'Fixed list from Library' : 'Fixed list'} · ${n.toLocaleString('en-US')} ${noun} · fixed at ${clock(fixed.evaluated_at)} UTC · until ${clock(fixed.valid_before)} UTC`;
}
export const isFrozenPage = (page: BlockPage): page is ResearchFrozenPage => page.context.consistency === 'frozen';
/** The reference of the list a run fixed or reused: kept in this tab's memory for the next run. */
export function fixedReferenceOf(result: GraphResult): FixedListReference | undefined {
  const fixed = result.run?.fixed;
  if (!fixed) return undefined;
  for (const node of Object.values(result.nodes)) {
    const page = node.state === 'ready' && node.consistency === 'frozen' ? node.value as BlockPage : undefined;
    if (page && isFrozenPage(page)) return { ref: page.context.frozen_ref, evaluated_at: fixed.evaluated_at, valid_before: fixed.valid_before, total_count: fixed.total_count };
  }
  return undefined;
}
/** Where a block's page sits: "1–50 of 1,234" for a fixed list; "Page 2 · 25 videos" for current pages. */
export function pagePosition(page: BlockPage, pageNumber: number, channels: boolean): string {
  if (isFrozenPage(page)) {
    const { start_index: start, total_count: total } = page.page;
    return page.rows.length ? `${(start + 1).toLocaleString('en-US')}–${(start + page.rows.length).toLocaleString('en-US')} of ${total.toLocaleString('en-US')}` : `0 of ${total.toLocaleString('en-US')}`;
  }
  const n = page.rows.length;
  return `Page ${pageNumber} · ${n} ${channels ? (n === 1 ? 'channel' : 'channels') : (n === 1 ? 'video' : 'videos')}`;
}
/** Selected IDs (include) on the block's input path: "N selected IDs · rows found: M" once M is known. */
export function selectionNote(document: ResearchDocument, id: string, page: BlockPage, offset: number): string {
  const selected = nodePath(document.graph, id).filter(n => n.type === 'selection' && n.parameters.mode === 'include');
  if (selected.length !== 1) return '';
  const count = (selected[0].parameters.ids as string[]).length, ids = `${count.toLocaleString('en-US')} selected ${count === 1 ? 'ID' : 'IDs'}`;
  return page.page.has_more ? ids : `${ids} · rows found: ${(offset + page.rows.length).toLocaleString('en-US')}`;
}
/** A table fed by 2–4 selected IDs is a comparison: shown side by side. */
export function sideBySide(document: ResearchDocument, id: string): boolean {
  const selected = nodePath(document.graph, id).filter(n => n.type === 'selection');
  return selected.length === 1 && selected[0].parameters.mode === 'include' && (selected[0].parameters.ids as string[]).length >= 2
    && (selected[0].parameters.ids as string[]).length <= 4;
}
/** Next pages belong to the first page's read: the same query and, for a fixed list, the same session. */
export function samePageSession(first: BlockPage, next: BlockPage): boolean {
  if (first.query_fingerprint !== next.query_fingerprint || first.context.consistency !== next.context.consistency) return false;
  if (!isFrozenPage(first) || !isFrozenPage(next)) return true;
  return first.context.evaluated_at === next.context.evaluated_at && first.context.valid_before === next.context.valid_before
    && first.page.total_count === next.page.total_count;
}
function delay(signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    signal.throwIfAborted();
    const abort = () => { clearTimeout(timer); reject(signal.reason); };
    const timer = setTimeout(() => { signal.removeEventListener('abort', abort); resolve(); }, 2000);
    signal.addEventListener('abort', abort, { once: true });
  });
}
/** One page of a result block, read with the query the block ran (nodes[id].query) and page.after.
 *  A fixed list always pages with its own cursors: without one the server would fix a new list. */
export async function loadBlockPage(query: Record<string, unknown>, filters: ResearchFilters, after: string | undefined, signal: AbortSignal,
  onPreparing: () => void = () => {}): Promise<BlockPage> {
  if (query.consistency === 'frozen' && !after) throw new Error('A fixed list pages with its own cursors.');
  const page = query.page as { size: number };
  const body = JSON.stringify({ ...query, page: { size: page.size, ...(after ? { after } : {}) } });
  for (let attempt = 0; ; attempt++) {
    signal.throwIfAborted();
    try {
      return parseBlockPage(await requestJson('/research/queries/execute', signal, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body }), filters);
    } catch (error) {
      // Only an explicitly unexecuted read is retried, never an unknown result.
      if (!(error instanceof ExplorerApiError) || error.code !== 'catalogue_preparing' || attempt >= 2) throw error;
      onPreparing(); await delay(signal);
    }
  }
}
/** A refused run in words: the study's own limits, else the shared texts. */
export function studyError(error: unknown): string {
  const code = error instanceof ExplorerApiError ? error.code : undefined;
  const own: Record<string, string> = {
    query_budget_exceeded: 'The study reached its limits (30 seconds or 4 MiB of results). Remove blocks or narrow the source, then run again.',
    invalid_query: 'Check the study’s conditions and values, then run again.',
    invalid_research_definition: 'This study is not valid on this server. Check its blocks and connections.',
    cursor_context_changed: 'Data or access changed. Run research again.',
    field_disabled: 'A field of this study is disabled. Remove it or enable it in Collection fields.',
    catalogue_preparing: BLOCK_ERRORS.catalogue_preparing, query_busy: BLOCK_ERRORS.query_busy,
    backend_unavailable: BLOCK_ERRORS.backend_unavailable, hot_runtime_not_ready: BLOCK_ERRORS.backend_unavailable,
  };
  // The action a failed run has is Run research. A study or reply that fails its check is the workshop's own words.
  if (code && own[code]) return own[code];
  if (!(error instanceof ExplorerApiError) && !(error instanceof Error && error.name === 'ExplorerNetworkError')) {
    return 'The study could not run. No result is shown. Run research again.';
  }
  return researchError(error, 'Run research again.');
}
