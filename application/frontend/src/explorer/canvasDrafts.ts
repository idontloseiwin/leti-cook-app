// This tab's second copy of unsaved canvas work. history.state keeps the canvas of each
// history entry; this copy lives in the tab's sessionStorage under the entry's id, so it can still be written once
// the address has moved on (Back, another page), when the page is left (reload, closing, the end of a session), and
// read when the entry's history copy is missing, older or damaged. It holds what history.state holds: the canvas,
// its name and the Saved item it edits. Never results, cursors or Library's list reference. It ends with the tab.
import { parseCanvasDraft } from './researchGraph';
import { savedId } from './savedWork';
import type { CanvasGraph } from './canvas/canvasModel';
import { writeWorkshopHistory, type WorkshopHistory } from './workshopHandoff';

export type CanvasDraft = { version: 1; entry: string; seq: number; at: number; canvas: CanvasGraph; name?: string; saved_id?: string; base_revision?: number };
export type DraftStore = Pick<Storage, 'getItem' | 'setItem' | 'removeItem' | 'key' | 'length'>;
export const DRAFT_PREFIX = 'leti-cook.workshop.draft.v1.';
// The entry this tab left with unsaved work, and at which address (an ended session, a reload after it).
export const RESUME_KEY = 'leti-cook.workshop.resume.v1';
const RESUME_MS = 12 * 3600 * 1000; // a password session lives at most 12 hours
// A tab keeps the drafts of its last 20 canvas entries (each at most a few tens of KB of a ~5 MB store).
const KEEP = 20;
const ENTRY = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

/** This tab's sessionStorage, or null where the browser refuses it (then history.state is the only copy). */
export function tabStore(): DraftStore | null {
  try { return window.sessionStorage; } catch { return null; }
}
const record = (value: unknown): Record<string, unknown> | null => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null;
const allowed = new Set(['version', 'entry', 'seq', 'at', 'canvas', 'name', 'saved_id', 'base_revision']);

/** The draft of one history entry, or null when there is none or it cannot be read. */
export function readDraft(entry: string, store: DraftStore | null = tabStore()): CanvasDraft | null {
  try {
    const raw = store?.getItem(DRAFT_PREFIX + entry);
    const d = raw ? record(JSON.parse(raw)) : null;
    if (!d || d.version !== 1 || d.entry !== entry || !ENTRY.test(entry) || !Object.keys(d).every(key => allowed.has(key))
      || !Number.isSafeInteger(d.seq) || Number(d.seq) < 1 || !Number.isSafeInteger(d.at)) return null;
    const draft: CanvasDraft = { version: 1, entry, seq: d.seq as number, at: d.at as number, canvas: parseCanvasDraft({ version: 4, graph: d.canvas }).graph };
    if (d.name !== undefined) { if (typeof d.name !== 'string' || [...d.name].length > 120) return null; draft.name = d.name; }
    if (d.saved_id !== undefined) draft.saved_id = savedId(d.saved_id);
    if (d.base_revision !== undefined) { if (!Number.isSafeInteger(d.base_revision) || Number(d.base_revision) < 1) return null; draft.base_revision = d.base_revision as number; }
    return draft;
  } catch { return null; }
}
function drafts(store: DraftStore): { key: string; at: number }[] {
  const found: { key: string; at: number }[] = [];
  for (let i = 0; i < store.length; i++) {
    const key = store.key(i);
    if (!key?.startsWith(DRAFT_PREFIX)) continue;
    let at = 0;
    try { at = Number(record(JSON.parse(store.getItem(key) ?? ''))?.at) || 0; } catch { /* an unreadable draft goes first */ }
    found.push({ key, at });
  }
  return found.sort((a, b) => b.at - a.at);
}
function prune(store: DraftStore, keep: number, except?: string) {
  for (const { key } of drafts(store).filter(d => d.key !== except).slice(Math.max(0, keep - (except ? 1 : 0)))) store.removeItem(key);
}
/** Keep this entry's draft; on a full store the oldest drafts of other entries make room, one by one. False when
 *  it could not be kept (then history.state is its only copy). */
export function writeDraft(draft: CanvasDraft, store: DraftStore | null = tabStore()): boolean {
  if (!store) return false;
  const key = DRAFT_PREFIX + draft.entry, value = JSON.stringify(draft);
  const put = () => { try { store.setItem(key, value); return true; } catch { return false; } };
  try {
    let kept = put();
    for (const { key: old } of kept ? [] : drafts(store).filter(d => d.key !== key).reverse()) { store.removeItem(old); if ((kept = put())) break; }
    if (kept) prune(store, KEEP, key);
    return kept;
  } catch { return false; }
}
/** This entry has no unsaved changes any more (it equals its saved canvas). */
export function removeDraft(entry: string, store: DraftStore | null = tabStore()) {
  try { store?.removeItem(DRAFT_PREFIX + entry); } catch { /* nothing to remove */ }
}
/** Every canvas draft of this tab and its resume mark (an explicit sign-out). */
export function clearDrafts(store: DraftStore | null = tabStore()) {
  try { if (store) { for (const { key } of drafts(store)) store.removeItem(key); store.removeItem(RESUME_KEY); } } catch { /* nothing more to clear */ }
}
/** This tab leaves the page with unsaved work in this entry: a new entry at the same address may take it back. */
export function markResume(entry: string, path: string, now: number, store: DraftStore | null = tabStore()) {
  try { store?.setItem(RESUME_KEY, JSON.stringify({ version: 1, entry, path, at: now })); } catch { /* the history copy stays */ }
}
/** The mark, taken once: the entry to restore at this address, or null (another address, older than a session, damaged). */
export function takeResume(path: string, now: number, store: DraftStore | null = tabStore()): string | null {
  try {
    const raw = store?.getItem(RESUME_KEY); store?.removeItem(RESUME_KEY);
    const m = raw ? record(JSON.parse(raw)) : null;
    return m && m.version === 1 && typeof m.entry === 'string' && ENTRY.test(m.entry) && m.path === path && Number.isSafeInteger(m.at)
      && now - (m.at as number) >= 0 && now - (m.at as number) <= RESUME_MS ? m.entry : null;
  } catch { return null; }
}

type HistoryLike = Pick<History, 'state' | 'replaceState'>;
const entryOf = (state: unknown) => record(record(state)?.workshop)?.entry;
/** The two copies of one history entry's canvas: written 500 ms after the last edit, and at once when the page is left.
 *  history.state is written only while that entry is still the current one; the tab's draft is written always. */
export class CanvasCopies {
  seq = 0;
  private pending: (() => WorkshopHistory) | null = null;
  private timer: ReturnType<typeof setTimeout> | undefined;
  constructor(private env: { history: HistoryLike; href: () => string; store: () => DraftStore | null; now: () => number;
    delay?: number; schedule?: (run: () => void, ms: number) => ReturnType<typeof setTimeout>; cancel?: (timer: ReturnType<typeof setTimeout> | undefined) => void }) {}
  private cancel() { (this.env.cancel ?? clearTimeout)(this.timer); this.timer = undefined; }
  /** An edit: both copies after the pause; `mirror` is read when they are written, so the latest edit is kept. */
  edited(mirror: () => WorkshopHistory) {
    this.pending = mirror; this.cancel();
    this.timer = (this.env.schedule ?? setTimeout)(() => this.flush(true), this.env.delay ?? 500);
  }
  /** The page is left: what is waiting is written now. toHistory: the entry is still the current one (reload, closing). */
  flush(toHistory: boolean) {
    const mirror = this.pending; this.pending = null; this.cancel();
    if (mirror) this.write(mirror(), toHistory);
  }
  /** Both copies now, under the next number: the newer copy wins when the entry opens again. */
  write(mirror: WorkshopHistory, toHistory: boolean): WorkshopHistory {
    const seq = ++this.seq, copy: WorkshopHistory = { ...mirror, seq };
    try {
      if (toHistory && entryOf(this.env.history.state) === copy.entry) this.env.history.replaceState(writeWorkshopHistory(this.env.history.state, copy), '', this.env.href());
    } catch { /* the tab's draft below still keeps it */ }
    if (copy.canvas) writeDraft({ version: 1, entry: copy.entry, seq, at: this.env.now(), canvas: copy.canvas,
      ...(copy.name !== undefined ? { name: copy.name } : {}), ...(copy.saved_id ? { saved_id: copy.saved_id } : {}),
      ...(copy.base_revision !== undefined ? { base_revision: copy.base_revision } : {}) }, this.env.store());
    else removeDraft(copy.entry, this.env.store());
    return copy;
  }
  /** Another entry opens: nothing of the previous one is waiting any more (flush it first); numbering goes on from `seq`. */
  reset(seq = 0) { this.pending = null; this.cancel(); this.seq = seq; }
}
