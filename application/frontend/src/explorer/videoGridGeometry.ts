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
