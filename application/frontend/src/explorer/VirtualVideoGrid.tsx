import {
  forwardRef, useCallback, useImperativeHandle, useLayoutEffect, useMemo, useRef, useState,
  type ReactNode,
} from 'react';
import type { ChannelExplorerVideo } from '../types';
import { reportDiscoveryRendered } from '../discoveryJournal';
import type { CatalogueDensity } from './cataloguePreferences';
import {
  buildVideoGridLayout, gridAnchorAt, resolvedGridColumns, videoAnchorScrollTop,
  visibleAnchorOffset, visibleVideoRows, type VideoGridAnchor,
} from './videoGridGeometry';

export type VideoGridFocusTarget = { videoId: string; trigger: 'thumbnail' | 'title' | 'selection' | 'youtube' | 'remove' };
export interface VirtualVideoGridHandle {
  captureAnchor: () => VideoGridAnchor | null;
  restore: (anchor: VideoGridAnchor | null, focus?: VideoGridFocusTarget) => void;
}

export interface VideoGridRestoreAnchor extends VideoGridAnchor { requestKey: string }

/**
 * The height the page's sticky bars cover at the top of the window now. Only a bar held at its `top`
 * (or being carried away above it) covers anything: a bar still in its place lower in the page, or one whose
 * container has scrolled away, does not (counting every bar's `top + height` kept cards low).
 */
function stickyTopInset(): number {
  let inset = 0;
  for (const node of document.querySelectorAll<HTMLElement>('.explorer-v2-topbar, .explorer-v2-page-tabs, .explorer-section-nav, .explorer-scope')) {
    const style = getComputedStyle(node);
    if (style.position !== 'sticky' && style.position !== 'fixed') continue;
    const top = Number.parseFloat(style.top);
    const rect = node.getBoundingClientRect();
    if (Number.isFinite(top) && rect.height > 0 && rect.top <= top + 1 && rect.bottom > 0) inset = Math.max(inset, rect.bottom);
  }
  return Math.min(inset, window.innerHeight / 2);
}

/** Window-scrolling, measured rows. Received data may be any contiguous browse window. */
export const VirtualVideoGrid = forwardRef<VirtualVideoGridHandle, {
  videos: ChannelExplorerVideo[];
  density: CatalogueDensity;
  scopeKey: string;
  inspectedVideoId?: string;
  onAnchorChange?: (anchor: VideoGridAnchor) => void;
  restoreAnchor?: VideoGridRestoreAnchor;
  renderCard: (video: ChannelExplorerVideo) => ReactNode;
}>(function VirtualVideoGrid({
  videos, density, scopeKey, inspectedVideoId, onAnchorChange, restoreAnchor, renderCard,
}, ref) {
  const gridRef = useRef<HTMLDivElement>(null);
  const heights = useRef(new Map<string, number>());
  const anchorRef = useRef<VideoGridAnchor | null>(null);
  const pendingRestore = useRef<VideoGridAnchor | null>(null);
  // The remembered video of a new session (the restoreAnchor prop) stays the anchor of every restore, shown
  // whole, until the user moves the page; `ownScrollY` is where the grid itself last put the page.
  const settling = useRef<string | null>(null);
  const ownScrollY = useRef<number | null>(null);
  const pendingFocus = useRef<VideoGridFocusTarget | undefined>(undefined);
  const focusedTarget = useRef<VideoGridFocusTarget | undefined>(undefined);
  const appliedRestoreKey = useRef<string | undefined>(undefined);
  const previousScope = useRef(scopeKey);
  const previousIds = useRef<readonly string[]>([]);
  const [focusedVideoId, setFocusedVideoId] = useState<string | undefined>();
  const [revision, setRevision] = useState(0);
  const [geometry, setGeometry] = useState({ columns: 1, width: 0, gap: 15, estimate: 350, density });
  const [viewport, setViewport] = useState({ top: 0, height: 900 });
  const ids = useMemo(() => videos.map((video) => video.video_id || ''), [videos]);
  const layout = useMemo(() => buildVideoGridLayout(
    ids, geometry.columns, geometry.estimate, geometry.gap, heights.current,
  ), [ids, geometry, revision]);
  const current = useRef({ ids, layout, onAnchorChange });
  current.current = { ids, layout, onAnchorChange };

  const captureAnchor = useCallback((): VideoGridAnchor | null => {
    const grid = gridRef.current;
    if (!grid || !grid.getClientRects().length) return anchorRef.current;
    const bounds = grid.getBoundingClientRect();
    const { ids: currentIds, layout: currentLayout } = current.current;
    if (!currentLayout.rows.length || bounds.top >= window.innerHeight || bounds.bottom <= 0) return anchorRef.current;
    const next = gridAnchorAt(currentIds, currentLayout, bounds.top, settling.current);
    if (!next) return anchorRef.current;
    anchorRef.current = next;
    current.current.onAnchorChange?.(next);
    return next;
  }, []);

  const readViewport = useCallback(() => {
    const grid = gridRef.current;
    if (!grid || !grid.getClientRects().length) return;
    if (settling.current && ownScrollY.current !== null && !pendingRestore.current) {
      const y = window.scrollY;
      const own = ownScrollY.current;
      // The page shrank under the grid's own position (the browser clamps it): not a move by the user.
      const clamped = y < own && y >= document.documentElement.scrollHeight - window.innerHeight - 1;
      if (Math.abs(y - own) <= 1 || clamped) ownScrollY.current = y;
      else settling.current = null;
    }
    const top = -grid.getBoundingClientRect().top;
    const height = window.innerHeight;
    setViewport((previous) => previous.top === top && previous.height === height ? previous : { top, height });
    if (!pendingRestore.current) captureAnchor();
  }, [captureAnchor]);

  useImperativeHandle(ref, () => ({
    captureAnchor,
    restore(anchor, focus) {
      pendingRestore.current = anchor;
      if (focus) pendingFocus.current = focus;
      if (focus) setFocusedVideoId(focus.videoId);
      setRevision((value) => value + 1);
    },
  }), [captureAnchor]);

  // Events are throttled to a frame; no full hidden DOM catalogue is retained.
  useLayoutEffect(() => {
    let frame = 0;
    const update = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(readViewport);
    };
    window.addEventListener('scroll', update, { passive: true });
    window.addEventListener('resize', update);
    readViewport();
    return () => {
      cancelAnimationFrame(frame);
      window.removeEventListener('scroll', update);
      window.removeEventListener('resize', update);
    };
  }, [readViewport]);

  useLayoutEffect(() => {
    const grid = gridRef.current;
    if (!grid || !grid.getClientRects().length) return;
    if (previousScope.current !== scopeKey) {
      previousScope.current = scopeKey;
      anchorRef.current = null;
      pendingRestore.current = null;
      settling.current = null;
      ownScrollY.current = null;
      pendingFocus.current = undefined;
      focusedTarget.current = undefined;
      appliedRestoreKey.current = undefined;
      setFocusedVideoId(undefined);
      heights.current.clear();
    }
    if (restoreAnchor && appliedRestoreKey.current !== restoreAnchor.requestKey) {
      appliedRestoreKey.current = restoreAnchor.requestKey;
      pendingRestore.current = restoreAnchor;
      settling.current = restoreAnchor.videoId;
      ownScrollY.current = null;
    } else if (previousIds.current.length && previousIds.current[0] !== ids[0]) {
      // Prepend and a new sort both relocate an existing row; preserve its identity.
      pendingRestore.current ??= anchorRef.current;
      pendingFocus.current ??= focusedTarget.current;
      const validKeys = new Set(layout.rows.map((row) => row.key));
      for (const key of heights.current.keys()) if (!validKeys.has(key)) heights.current.delete(key);
    }
    const focused = focusedTarget.current;
    if (focused && previousIds.current !== ids) {
      const previousIndex = previousIds.current.indexOf(focused.videoId);
      const nextIndex = ids.indexOf(focused.videoId);
      if (previousIndex >= 0 && nextIndex >= 0) {
        const previousStart = Math.floor(previousIndex / geometry.columns) * geometry.columns;
        const previousKey = JSON.stringify(previousIds.current.slice(previousStart, previousStart + geometry.columns));
        const nextKey = layout.rows[Math.floor(nextIndex / layout.columns)]?.key;
        // Appending can fill the last partial row and replace its DOM subtree,
        // even though the first ID and column geometry have not changed.
        if (previousKey !== nextKey) {
          pendingFocus.current ??= focused;
          pendingRestore.current ??= anchorRef.current;
        }
      }
    }
    previousIds.current = ids;

    const measure = (): boolean => {
      if (!grid.getClientRects().length) return false;
      const style = getComputedStyle(grid);
      const width = grid.clientWidth;
      const columns = resolvedGridColumns(style.gridTemplateColumns);
      const gap = Number.parseFloat(style.columnGap) || 0;
      if (width !== geometry.width || columns !== geometry.columns || density !== geometry.density || gap !== geometry.gap) {
        pendingRestore.current ??= anchorRef.current;
        pendingFocus.current ??= focusedTarget.current;
        heights.current.clear();
        const cardWidth = (width - gap * (columns - 1)) / columns;
        setGeometry({ columns, width, gap, estimate: cardWidth * 9 / 16 + (density === 'compact' ? 116 : 132), density });
        return true;
      }
      let changed = false;
      for (const node of grid.querySelectorAll<HTMLElement>('[data-video-row]')) {
        const key = node.dataset.videoRow!;
        const height = node.getBoundingClientRect().height;
        if (height > 0 && Math.abs((heights.current.get(key) ?? -1) - height) > 0.5) {
          heights.current.set(key, height);
          changed = true;
        }
      }
      if (changed) {
        pendingRestore.current ??= anchorRef.current;
        setRevision((value) => value + 1);
      }
      return changed;
    };
    const changed = measure();
    if (!changed) {
      const pending = pendingRestore.current;
      if (pending) {
        // Every restore of the settling anchor shows its row whole (the jump, then pages and measures).
        const whole = settling.current !== null && pending.videoId === settling.current;
        const scrollTop = () => videoAnchorScrollTop(whole
          ? { ...pending, offsetPx: visibleAnchorOffset(pending, ids, layout, window.innerHeight, stickyTopInset()) } : pending,
        ids, layout, grid.getBoundingClientRect().top + window.scrollY);
        const top = scrollTop();
        if (top !== null) {
          pendingRestore.current = null;
          if (Math.abs(window.scrollY - top) > 0.5) window.scrollTo({ top, behavior: 'instant' });
          if (whole) {
            // A bar may stick only at the new position: place the row once more against what covers it there.
            const again = scrollTop();
            if (again !== null && Math.abs(window.scrollY - again) > 0.5) window.scrollTo({ top: again, behavior: 'instant' });
            ownScrollY.current = window.scrollY;
          }
        }
      }
      const focus = pendingFocus.current;
      if (focus) {
        const card = [...grid.querySelectorAll<HTMLElement>('[data-video-id]')]
          .find((node) => node.dataset.videoId === focus.videoId);
        const trigger = card?.querySelector<HTMLElement>(`[data-grid-focus="${focus.trigger}"]`);
        if (trigger) {
          trigger.focus({ preventScroll: true });
          pendingFocus.current = undefined;
        }
      }
      readViewport();
    }
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(() => {
      if (!measure()) readViewport();
    });
    observer?.observe(grid);
    grid.querySelectorAll<HTMLElement>('[data-video-row]').forEach((row) => observer?.observe(row));
    return () => observer?.disconnect();
  });

  const pinned = [focusedVideoId, inspectedVideoId, pendingFocus.current?.videoId]
    .filter((id): id is string => Boolean(id)).map((id) => ids.indexOf(id));
  const rows = visibleVideoRows(layout, viewport.top, viewport.height, pinned);
  useLayoutEffect(() => {
    reportDiscoveryRendered(rows.flatMap(index => videos.slice(layout.rows[index].start, layout.rows[index].end)));
  }, [videos, layout, rows]);
  return (
    <div
      ref={gridRef}
      className="explorer-v2-video-grid is-virtual"
      data-density={density}
      style={{ height: layout.height }}
      onFocusCapture={(event) => {
        const card = (event.target as HTMLElement).closest<HTMLElement>('[data-video-id]');
        const trigger = (event.target as HTMLElement).dataset.gridFocus as VideoGridFocusTarget['trigger'] | undefined;
        if (card?.dataset.videoId) {
          setFocusedVideoId(card.dataset.videoId);
          if (trigger) focusedTarget.current = { videoId: card.dataset.videoId, trigger };
        }
      }}
      onBlurCapture={(event) => {
        // A null target during row replacement is not a deliberate focus move.
        if (event.relatedTarget && !event.currentTarget.contains(event.relatedTarget as Node)) {
          setFocusedVideoId(undefined);
          focusedTarget.current = undefined;
        }
      }}
    >
      {rows.map((index) => {
        const row = layout.rows[index];
        return (
          <div key={row.key} className="explorer-v2-video-virtual-row" data-video-row={row.key} style={{ top: row.top }}>
            {videos.slice(row.start, row.end).map(renderCard)}
          </div>
        );
      })}
    </div>
  );
});
