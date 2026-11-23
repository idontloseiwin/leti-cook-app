// Cards of a canvas result: thumbnails and the familiar video facts, pages of the block's own query, and a
// selection that survives paging. "Continue with selected" hands the exact IDs to the canvas (a Selected
// items node on the same input), never all matching rows.
import { useEffect, useRef, useState } from 'react';
import { ExplorerApiError } from '../api';
import { CellValue, SourceDetails, VideoImage } from '../ResearchRows';
import { blockFilters, type BlockPage, type NodeResult, type ResearchDocument } from '../researchGraph';
import { channelSource, researchLabels, type ResearchField, type ResearchPage } from '../researchQuery';
import { blockError, blockTimeLabel, isFrozenPage, loadBlockPage, samePageSession } from '../workshopModel';

const MAX_PICKED = 1000;
const label = (field: string) => (researchLabels as Record<string, string>)[field] ?? field;

export function CanvasCards({ id, study, result, onContinue }: {
  // A refusal (the picked items do not fit into the canvas) is shown next to the button.
  id: string; study: ResearchDocument; result: NodeResult; onContinue: (ids: string[]) => string | null;
}) {
  const channels = study.query.source.id === channelSource;
  const first = result.state === 'ready' && result.output === 'page' ? result.value as BlockPage : null;
  const [view, setView] = useState(() => first ? { page: first, cursors: [undefined] as (string | undefined)[], start: 0 } : null);
  const [picked, setPicked] = useState<string[]>([]), [loading, setLoading] = useState(false), [message, setMessage] = useState('');
  const pending = useRef<AbortController | null>(null);
  useEffect(() => () => pending.current?.abort(), []);
  if (result.state === 'error') return <p role="status">{blockError(result.code, result.path, result.limit)}</p>;
  if (!first || !view) return null;
  const page = view.page, query = result.query, rows = page.rows, fixed = isFrozenPage(page) ? page : null;
  const facts = page.columns.filter(f => !f.endsWith('_url') && f !== (channels ? 'channel.title' : 'video.title') && f !== 'video.id' && f !== 'channel.id');
  async function go(after: string | undefined, cursors: (string | undefined)[], start: number) {
    if (!query) return;
    pending.current?.abort(); const controller = new AbortController(); pending.current = controller;
    setLoading(true); setMessage('');
    try {
      const next = await loadBlockPage(query, blockFilters(study, id), after, controller.signal, () => setMessage('Stored data is being prepared in the background…'));
      if (pending.current !== controller) return;
      if (!samePageSession(page, next)) throw new Error('This page belongs to another read. Run the canvas again.');
      setView({ page: next, cursors, start: isFrozenPage(next) ? next.page.start_index : start }); // a fixed list numbers its own rows
    } catch (error) {
      if (pending.current !== controller) return;
      setMessage(error instanceof ExplorerApiError ? blockError(error.code) : error instanceof Error && error.message ? error.message : 'The page could not be loaded.');
    } finally { if (pending.current === controller) setLoading(false); }
  }
  const toggle = (row: string, on: boolean) => setPicked(ids => on ? (ids.length >= MAX_PICKED || ids.includes(row) ? ids : [...ids, row]) : ids.filter(x => x !== row));
  return <div className="canvas-cards">
    <p className="research-note">{fixed ? "Library's fixed list · " : ''}{blockTimeLabel(page.context)} · {rows.length ? `rows ${view.start + 1}–${view.start + rows.length}` : 'no rows'}{fixed ? ` of ${fixed.page.total_count.toLocaleString('en-US')}` : page.page.has_more ? ' · more available' : ''}</p>
    {!rows.length && <p role="status">0 {channels ? 'channels' : 'videos'} match this branch.</p>}
    <div className="canvas-card-grid">{rows.map(row => <article key={row.id} className={picked.includes(row.id) ? 'picked' : undefined} data-video-id={row.id}>
      <label className="canvas-card-pick"><input type="checkbox" checked={picked.includes(row.id)} onChange={e => toggle(row.id, e.target.checked)}
        aria-label={'Select ' + row.id} />Select</label>
      <a className="canvas-card-image" href={(channels ? 'https://www.youtube.com/channel/' : 'https://www.youtube.com/watch?v=') + row.id} target="_blank" rel="noopener noreferrer">
        <VideoImage row={row} field={channels ? 'channel.avatar_url' : 'video.thumbnail_url'} /></a>
      <h4><CellValue row={row} field={channels ? 'channel.title' : 'video.title'} /></h4>
      <dl>{facts.map(field => <div key={field}><dt>{label(field)}</dt><dd><CellValue row={row} field={field as ResearchField} /></dd></div>)}</dl>
      <SourceDetails row={row} page={page as ResearchPage} />
    </article>)}</div>
    {message && <p role="status">{message}</p>}
    <footer className="research-actions">
      {/* A fixed list goes back with its own cursor; a current read re-reads the previous page. */}
      <button type="button" disabled={(fixed ? !fixed.page.previous_cursor : view.cursors.length < 2) || loading}
        onClick={() => void go(fixed ? fixed.page.previous_cursor ?? undefined : view.cursors[view.cursors.length - 2], view.cursors.slice(0, -1), Math.max(0, view.start - rows.length))}>← Previous</button>
      <button type="button" disabled={!page.page.has_more || loading} onClick={() => void go(page.page.next_cursor ?? undefined, [...view.cursors, page.page.next_cursor ?? undefined], view.start + rows.length)}>Next →</button>
      <span className="canvas-picked">{picked.length ? `${picked.length} selected` : 'Select cards to continue with them'}</span>
      <button type="button" disabled={!picked.length} onClick={() => setMessage(onContinue(picked) ?? '')}>Continue with selected</button>
      {picked.length > 0 && <button type="button" onClick={() => setPicked([])}>Clear selection</button>}
    </footer>
  </div>;
}
