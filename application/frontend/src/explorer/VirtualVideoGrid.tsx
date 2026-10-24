import {
  forwardRef, useCallback, useImperativeHandle, useLayoutEffect, useMemo, useRef, useState,
  type ReactNode,
} from 'react';
import type { ChannelExplorerVideo } from '../types';
import type { CatalogueDensity } from './cataloguePreferences';
import {
  buildVideoGridLayout, resolvedGridColumns, videoAnchorScrollTop, videoRowAtOffset,
  visibleVideoRows, type VideoGridAnchor,
} from './videoGridGeometry';

export type VideoGridFocusTarget = { videoId: string; trigger: 'thumbnail' | 'title' | 'selection' | 'youtube' | 'remove' };
export interface VirtualVideoGridHandle {
  captureAnchor: () => VideoGridAnchor | null;
  restore: (anchor: VideoGridAnchor | null, focus?: VideoGridFocusTarget) => void;
}

export interface VideoGridRestoreAnchor extends VideoGridAnchor { requestKey: string }

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
    const row = currentLayout.rows[videoRowAtOffset(currentLayout.rows, Math.max(0, -bounds.top))];
    const next = { videoId: currentIds[row.start], offsetPx: bounds.top + row.top };
    anchorRef.current = next;
    current.current.onAnchorChange?.(next);
    return next;
  }, []);

  const readViewport = useCallback(() => {
    const grid = gridRef.current;
    if (!grid || !grid.getClientRects().length) return;
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
      pendingFocus.current = undefined;
      focusedTarget.current = undefined;
      appliedRestoreKey.current = undefined;
      setFocusedVideoId(undefined);
      heights.current.clear();
    }
    if (restoreAnchor && appliedRestoreKey.current !== restoreAnchor.requestKey) {
      appliedRestoreKey.current = restoreAnchor.requestKey;
      pendingRestore.current = restoreAnchor;
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
      const anchor = pendingRestore.current;
      if (anchor) {
        const top = videoAnchorScrollTop(anchor, ids, layout, grid.getBoundingClientRect().top + window.scrollY);
        if (top !== null) {
          pendingRestore.current = null;
          if (Math.abs(window.scrollY - top) > 0.5) window.scrollTo({ top, behavior: 'instant' });
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
