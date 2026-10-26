import { useEffect, useRef, useState } from 'react';
import { sourceExpired } from '../sourceLifetime';
import { useSourceExpiry } from '../sourceLifetimeReact';
import { loadResearchCapabilities, loadResearchPage, researchError, type ResearchCell, type ResearchField, type ResearchFilters, type ResearchPage, type ResearchType } from './researchQuery';
import './researchCatalogue.css';

const typeNames = { long: 'Long-form', short: 'Shorts', stream: 'Streams' };
const labels: Record<ResearchField, string> = { 'video.id': 'Video ID', 'video.channel_id': 'Channel', 'video.type': 'Type', 'video.title': 'Title', 'video.published_at': 'Published (UTC)', 'video.duration_seconds': 'Duration', 'video.views': 'Views' };
function display(cell: ResearchCell, field: ResearchField): string {
  if (cell.state !== 'known' || cell.value === null) return { unknown: 'Unknown', expired: 'Expired', unavailable: 'Unavailable', known: 'Unknown' }[cell.state];
  if (field === 'video.title' && cell.value === '') return 'Untitled video';
  if (field === 'video.views') return BigInt(cell.value).toLocaleString('en-US');
  if (field === 'video.duration_seconds') { const n = BigInt(cell.value); return `${n / 60n}:${String(n % 60n).padStart(2, '0')}`; }
  if (field === 'video.published_at') return cell.value.slice(0, 19).replace('T', ' ');
  return cell.value;
}
export function ResearchCatalogue({ folders, foldersLoaded, apiAvailable }: { folders: { id: string; name: string }[]; foldersLoaded: boolean; apiAvailable: boolean }) {
  const [filters, setFilters] = useState<ResearchFilters>({ folder: '', type: 'long', title: '', minimumViews: '', sort: 'video.views', direction: 'desc' });
  const [types, setTypes] = useState<ResearchType[]>([]);
  const [result, setResult] = useState<ResearchPage | null>(null);
  const [message, setMessage] = useState(''), [loading, setLoading] = useState(false), [page, setPage] = useState(0);
  const pending = useRef<AbortController | null>(null), generation = useRef(0);
  const invalidate = () => { generation.current++; pending.current?.abort(); setResult(null); setPage(0); setLoading(false); };
  useEffect(() => {
    if (!apiAvailable) {
      generation.current++; pending.current?.abort(); setResult(null); setLoading(false); setPage(0);
    }
  }, [apiAvailable]);
  useEffect(() => {
    const controller = new AbortController();
    loadResearchCapabilities(controller.signal).then(setTypes).catch(error => { if (!controller.signal.aborted) setMessage(researchError(error)); });
    return () => { controller.abort(); generation.current++; pending.current?.abort(); };
  }, []);
  useEffect(() => {
    if (filters.folder && !folders.some(folder => folder.id === filters.folder) && foldersLoaded) {
      generation.current++; pending.current?.abort(); setResult(null); setLoading(false); setPage(0);
      setFilters(current => ({ ...current, folder: '' })); setMessage('The selected folder is no longer available.');
    }
  }, [folders, foldersLoaded, filters.folder]);
  useSourceExpiry(result, () => { invalidate(); setMessage('This page has expired. Refresh to load current data.'); });
  const change = (values: Partial<ResearchFilters>) => { invalidate(); setMessage(''); setFilters(current => ({ ...current, ...values })); };
  const run = async (after?: string) => {
    const number = after ? page + 1 : 1;
    invalidate(); const id = generation.current, controller = new AbortController(); pending.current = controller;
    setLoading(true); setMessage('');
    const timeout = window.setTimeout(() => controller.abort(), 15_000);
    try {
      const answer = await loadResearchPage(filters, controller.signal, after, () => {
        if (id === generation.current && !controller.signal.aborted) setMessage('Preparing the catalogue in the background…');
      });
      if (id !== generation.current || controller.signal.aborted) return;
      setMessage(''); setResult(answer); setPage(number);
    } catch (error) {
      if (id === generation.current) setMessage(controller.signal.aborted ? 'The request timed out. Refresh to try again.' : researchError(error));
    } finally { window.clearTimeout(timeout); if (id === generation.current) setLoading(false); }
  };
  const current = apiAvailable && result && !sourceExpired(result) ? result : null;
  const canQuery = Boolean(apiAvailable && filters.folder && folders.some(folder => folder.id === filters.folder) && types.includes(filters.type));
  return <section className="research-catalogue" aria-labelledby="research-title">
    <header><p className="research-eyebrow">Research catalogue · Preview</p><h1 id="research-title">Explore a folder</h1><p>Browse stored videos from the channels in one folder. Choose one format at a time.</p></header>
    <form onSubmit={event => { event.preventDefault(); if (canQuery) void run(); }} className="research-controls">
      <label>Folder<select value={filters.folder} onChange={event => change({ folder: event.target.value })}><option value="">{foldersLoaded ? 'Choose a folder' : 'Loading folders…'}</option>{folders.map(folder => <option key={folder.id} value={folder.id}>{folder.name}</option>)}</select></label>
      <label>Format<select value={filters.type} onChange={event => change({ type: event.target.value as ResearchType })}>{(Object.keys(typeNames) as ResearchType[]).map(type => <option key={type} value={type} disabled={!types.includes(type)}>{typeNames[type]}</option>)}</select></label>
      <label>Title contains<input value={filters.title} maxLength={1000} onChange={event => change({ title: event.target.value })} placeholder="Search titles" /></label>
      <label>Minimum views<input value={filters.minimumViews} inputMode="numeric" pattern="(0|[1-9][0-9]{0,19})" maxLength={20} onChange={event => change({ minimumViews: event.target.value })} placeholder="Any" /></label>
      <label>Sort by<select value={filters.sort} onChange={event => change({ sort: event.target.value as ResearchField })}>{Object.entries(labels).map(([id, label]) => <option key={id} value={id}>{label}</option>)}</select></label>
      <label>Order<select value={filters.direction} onChange={event => change({ direction: event.target.value as 'asc' | 'desc' })}><option value="desc">Descending</option><option value="asc">Ascending</option></select></label>
      <button type="submit" disabled={!canQuery || loading}>{loading ? 'Loading…' : 'Refresh'}</button>
    </form>
    <p className="research-note">{filters.type === 'stream' ? 'Completed streams only. ' : ''}Pages use current data. Updates can move videos between pages; refresh to start again. Unknown values stay unknown and sort last.</p>
    {!apiAvailable && <p role="status">Stored data is being checked or is temporarily unavailable.</p>}
    {!folders.length && foldersLoaded && <p>No folders yet. Create a folder and assign channels in Channel library.</p>}
    {message && <p role="status" className="research-message">{message}</p>}
    {current && <>
      <div className="research-summary"><span>{current.coverage.status === 'complete' ? 'Complete inventory' : 'Partial inventory'} · {current.coverage.channels_complete} of {current.coverage.channels_in_scope} channels complete</span><span>Page {page} · {current.rows.length} videos</span></div>
      {Object.keys(current.coverage.reasons).length > 0 && <details className="research-coverage"><summary>Coverage details</summary><ul>{Object.entries(current.coverage.reasons).map(([key, number]) => <li key={key}>{({ inventory_incomplete: 'Channels with incomplete inventory', source_rows_unavailable: 'Unavailable or excluded source rows', required_fields_missing: 'Rows missing required fields' } as Record<string, string>)[key] ?? 'Other source limitation'}: {number}</li>)}</ul></details>}
      <div className="research-table-wrap"><table><caption>Stored {typeNames[filters.type]} videos · values reported by YouTube</caption><thead><tr><th>Video</th><th>Channel</th><th>Published (UTC)</th><th>Duration</th><th>Views</th></tr></thead><tbody>{current.rows.map(row => <tr key={row.id} data-video-id={row.id}>
        {(['video.title', 'video.channel_id', 'video.published_at', 'video.duration_seconds', 'video.views'] as ResearchField[]).map(field => {
          const cell = row.cells[field], text = display(cell, field), provenance = current.provenance[cell.provenance_ref];
          return <td key={field} title={`${provenance.origin === 'youtube_reported' ? 'YouTube' : 'Normalized from YouTube'} · ${provenance.method} · checked ${cell.observed_at ?? 'unknown'}`}>
            {field === 'video.title' ? <><a href={`https://www.youtube.com/watch?v=${row.id}`} target="_blank" rel="noopener noreferrer">{text}</a><small>{row.id}</small></> : field === 'video.channel_id' ? <a href={`https://www.youtube.com/channel/${cell.value}`} target="_blank" rel="noopener noreferrer">{text}</a> : text}</td>;
        })}</tr>)}</tbody></table></div>
      {!current.rows.length && <p>No available videos match this selection.</p>}
      <footer><small>Type follows recorded YouTube playlist membership and the application's conflict resolution. Checked {current.context.evaluated_at.replace('T', ' ').replace('Z', ' UTC')}.</small><button disabled={!current.page.has_more || loading} onClick={() => void run(current.page.next_cursor ?? undefined)}>Next page →</button></footer>
    </>}
  </section>;
}
