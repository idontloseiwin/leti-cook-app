import { useEffect, useState } from 'react';
import { sourceExpired } from '../sourceLifetime';
import { useSourceExpiry } from '../sourceLifetimeReact';
import { loadChannelPublications } from './api';
import { formatTimestampLocal } from './localTime';
import { publicationStatusLabel, publicationTypeLabel, type ChannelPublicationsPage } from './publicationStatus';
import './ChannelPublications.css';

export function ChannelPublications({ channelId, initialComplete }: { channelId: string; initialComplete: boolean }) {
  const [page, setPage] = useState<ChannelPublicationsPage>();
  const [cursors, setCursors] = useState<(string | undefined)[]>([undefined]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const cursor = cursors.at(-1);
  useSourceExpiry(page, () => setPage(undefined), () => setRefresh(value => value + 1));
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError(false); setPage(undefined);
    loadChannelPublications(channelId, cursor, controller.signal)
      .then(value => { if (!controller.signal.aborted) setPage(value); })
      .catch(() => { if (!controller.signal.aborted) setError(true); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [channelId, cursor, refresh, initialComplete]);
  if (!loading && !error && page?.videos.length === 0 && cursors.length === 1) return null;
  const rows = page?.videos.filter(row => !sourceExpired(row)) ?? [];
  return <section className="explorer-publications" aria-label="Publication status">
    <div className="explorer-publications-heading">
      <h2>Upcoming and live videos</h2>
      <button type="button" className="explorer-secondary-button" disabled={loading}
        onClick={() => { setCursors([undefined]); setRefresh(value => value + 1); }}>Refresh list</button>
    </div>
    <p className="explorer-publications-note">Last reported by YouTube. Scheduled times can change; reaching a time does not confirm that a video has started or finished.</p>
    {loading && <p role="status">Loading publication status…</p>}
    {error && <p role="status">Publication status is unavailable. Refresh the list to try again.</p>}
    {!loading && !error && rows.length === 0 && <p>No current observations on this page.</p>}
    {rows.length > 0 && <ul>{rows.map(row => <li key={row.video_id}>
      <div className="explorer-publications-video">
        <a href={`https://www.youtube.com/watch?v=${encodeURIComponent(row.video_id)}`} target="_blank" rel="noopener noreferrer">{row.title || row.video_id}</a>
        <small>{publicationTypeLabel(row.video_type)} · {publicationStatusLabel(row.publication_status)}</small>
      </div>
      <div className="explorer-publications-time">
        <span>{row.scheduled_start_at ? <>Scheduled: <time dateTime={row.scheduled_start_at}>{formatTimestampLocal(row.scheduled_start_at)}</time></> : 'Scheduled time not reported'}</span>
        <small>Checked <time dateTime={row.checked_at}>{formatTimestampLocal(row.checked_at)}</time></small>
      </div>
    </li>)}</ul>}
    <p className="explorer-publications-note">Shown separately from the catalogue and analytics until publication is confirmed and its data is ready. Times are in your time zone.</p>
    {(cursors.length > 1 || page?.next_cursor) && <div className="explorer-publications-pagination">
      <button type="button" className="explorer-secondary-button" disabled={loading || cursors.length === 1}
        onClick={() => setCursors(value => value.slice(0, -1))}>Previous</button>
      <button type="button" className="explorer-secondary-button" disabled={loading || !page?.next_cursor}
        onClick={() => { if (page?.next_cursor) setCursors(value => [...value, page.next_cursor!]); }}>Next</button>
    </div>}
  </section>;
}
