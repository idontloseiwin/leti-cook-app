import type { VideoType } from '../types';

export interface VideoGridAnchor {
  videoId: string;
  /** The anchored row's top edge relative to the viewport. */
  offsetPx: number;
}

export interface VideoGridRow {
  key: string;
  start: number;
  end: number;
  top: number;
  height: number;
}

export interface VideoGridLayout {
  rows: VideoGridRow[];
  height: number;
  columns: number;
}

/**
 * The card's two forms: Long (16:9) for long videos and streams, Shorts (2:3) for the Videos tab's Shorts.
 * A grid that mixes types (a collection) has no type and keeps the Long form.
 */
export type VideoCardForm = 'long' | 'shorts';
export function videoCardForm(videoType?: VideoType): VideoCardForm {
  return videoType === 'short' ? 'shorts' : 'long';
}

/**
 * The thumbnail's height per width and the card's padding and fixed text block under it (videos.css).
 * Long: title 8 + 44 and two rows of 6 + 24 in Large, title 8 + 36 and the same rows in Compact.
 * Shorts: title 8 + 40, rows of 6 + 24, 6 + 24, 4 + 20 and 6 + 24 in Large; Compact as Long.
 */
const VIDEO_CARD_BOX = {
  long: { ratio: 9 / 16, large: { padding: 8, text: 112 }, compact: { padding: 6, text: 104 } },
  shorts: { ratio: 3 / 2, large: { padding: 8, text: 162 }, compact: { padding: 6, text: 104 } },
} as const;

/**
 * A row's height before it is measured: 2 × padding + the thumbnail (16:9, or 2:3 for Shorts) + the text block.
 * Rows are still measured; this only places them until then.
 */
export function estimatedVideoRowHeight(cardWidth: number, density: 'large' | 'compact', form: VideoCardForm = 'long'): number {
  const box = VIDEO_CARD_BOX[form];
  const { padding, text } = box[density];
  return 2 * padding + Math.max(0, cardWidth - 2 * padding) * box.ratio + text;
}

/** Read resolved CSS tracks, not a second JavaScript breakpoint table. */
export function resolvedGridColumns(value: string): number {
  const tracks = value.replace(/\[[^\]]*\]/g, '').trim().split(/\s+/);
  return Math.max(1, tracks.filter((track) => /^\d+(?:\.\d+)?px$/.test(track)).length);
}

export function buildVideoGridLayout(
  ids: readonly string[],
  columns: number,
  estimate: number,
  gap: number,
  measured: ReadonlyMap<string, number>,
): VideoGridLayout {
  const count = Math.max(1, Math.floor(columns));
  const rows: VideoGridRow[] = [];
  let top = 0;
  for (let start = 0; start < ids.length; start += count) {
    const end = Math.min(ids.length, start + count);
    const key = JSON.stringify(ids.slice(start, end));
    const height = Math.max(1, measured.get(key) ?? estimate);
    rows.push({ key, start, end, top, height });
    top += height + gap;
  }
  return { rows, height: Math.max(0, top - (rows.length ? gap : 0)), columns: count };
}

export function videoRowAtOffset(rows: readonly VideoGridRow[], offset: number): number {
  if (!rows.length) return -1;
  let low = 0;
  let high = rows.length - 1;
  while (low < high) {
    const middle = Math.floor((low + high) / 2);
    if (rows[middle].top + rows[middle].height <= offset) low = middle + 1;
    else high = middle;
  }
  return low;
}

/** Pinned keyboard/Inspector rows stay mounted without retaining the full list. */
export function visibleVideoRows(
  layout: VideoGridLayout,
  viewportTop: number,
  viewportHeight: number,
  pinnedItemIndices: readonly number[] = [],
  overscan = 3,
): number[] {
  if (!layout.rows.length) return [];
  const first = Math.max(0, videoRowAtOffset(layout.rows, viewportTop) - overscan);
  const last = Math.min(layout.rows.length - 1,
    videoRowAtOffset(layout.rows, viewportTop + viewportHeight) + overscan);
  const included = new Set<number>();
  for (let row = first; row <= last; row += 1) included.add(row);
  for (const item of pinnedItemIndices) {
    if (item < 0) continue;
    const row = Math.floor(item / layout.columns);
    // Neighbours permit normal Tab traversal even after a pointer scroll.
    for (let nearby = Math.max(0, row - 1); nearby <= Math.min(layout.rows.length - 1, row + 1); nearby += 1) {
      included.add(nearby);
    }
  }
  return [...included].sort((a, b) => a - b);
}

/**
 * The viewport offset at which a remembered anchor row is shown whole after a jump to a new
 * session (sort or filters changed, a session reopened), and at every later restore while that session
 * settles (pages arriving around it, rows measured). An offset that already shows the row whole is
 * kept; otherwise the row moves just enough: below the page's sticky bars (`topInset`) and above the window's
 * bottom edge, with `margin` to spare. A row taller than the room left starts below the sticky bars.
 */
export function visibleAnchorOffset(
  anchor: VideoGridAnchor,
  ids: readonly string[],
  layout: VideoGridLayout,
  viewportHeight: number,
  topInset = 0,
  margin = 16,
): number {
  const index = ids.indexOf(anchor.videoId);
  if (index < 0) return anchor.offsetPx;
  const row = layout.rows[Math.floor(index / layout.columns)];
  if (!row) return anchor.offsetPx;
  const highest = topInset + margin;
  return Math.max(highest, Math.min(anchor.offsetPx, viewportHeight - row.height - margin));
}

/**
 * The anchor the grid keeps for its next restore, with the grid's top edge `gridTop` px from the window's top.
 * Normally the first card of the row at the window's top edge. While a new session settles,
 * the remembered card of the jump (`settlingId`) instead: a previous page that arrives above it reflows every
 * row when its size is not a multiple of the columns (50 cards in 3 columns move each card two places), so a
 * top-row anchor let the remembered card drop one row, below the window.
 */
export function gridAnchorAt(
  ids: readonly string[],
  layout: VideoGridLayout,
  gridTop: number,
  settlingId?: string | null,
): VideoGridAnchor | null {
  if (!layout.rows.length) return null;
  const settling = settlingId ? ids.indexOf(settlingId) : -1;
  const row = settling >= 0
    ? layout.rows[Math.floor(settling / layout.columns)]
    : layout.rows[videoRowAtOffset(layout.rows, Math.max(0, -gridTop))];
  return { videoId: settling >= 0 ? settlingId! : ids[row.start], offsetPx: gridTop + row.top };
}

export function videoAnchorScrollTop(
  anchor: VideoGridAnchor,
  ids: readonly string[],
  layout: VideoGridLayout,
  gridDocumentTop: number,
): number | null {
  const index = ids.indexOf(anchor.videoId);
  if (index < 0) return null;
  const row = layout.rows[Math.floor(index / layout.columns)];
  return Math.max(0, gridDocumentTop + row.top - anchor.offsetPx);
}
