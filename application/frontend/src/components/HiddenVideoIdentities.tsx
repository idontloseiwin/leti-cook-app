import { useEffect, useState } from 'react';
import { loadHiddenVideoIdentities, videoTypeReviewLinks, type HiddenVideoIdentities as Page } from '../videoTypeReviews';

export function HiddenVideoIdentities({ channelId, countOnly = false }: { channelId?: string; countOnly?: boolean }) {
  const [page, setPage] = useState<Page>();
  const [cursors, setCursors] = useState<(string | undefined)[]>([undefined]);
  const [error, setError] = useState(false);
  const [loading, setLoading] = useState(true);
  const [refresh, setRefresh] = useState(0);
  const cursor = cursors.at(-1);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError(false); setPage(undefined);
    loadHiddenVideoIdentities({ channelId, countOnly, cursor, signal: controller.signal })
      .then((value) => { if (!controller.signal.aborted) setPage(value); })
      .catch(() => { if (!controller.signal.aborted) setError(true); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [channelId, countOnly, cursor, refresh]);
  if (countOnly && !loading && !error && page?.count === 0) return null;
  return <section className="video-review-empty" aria-label="Hidden videos awaiting type confirmation">
    <p>{page ? `${page.count.toLocaleString()} hidden videos · type unconfirmed` : loading ? 'Checking hidden videos…' : 'Hidden video count unavailable.'}</p>
    {error && <button type="button" onClick={() => setRefresh((value) => value + 1)}>Retry</button>}
    {countOnly ? <p className="video-review-meta">Listed under Health → Type checks and decision history. Awaiting automatic recheck; no manual decision is required.</p> : <>
      <p className="video-review-meta">These saved IDs await automatic rechecking and stay outside the catalogue until fresh evidence confirms their type. No manual decision is required.</p>
      <button type="button" disabled={loading} onClick={() => { setCursors([undefined]); setRefresh((value) => value + 1); }}>Refresh hidden videos</button>
      {page && page.videos.length > 0 && <ul>{page.videos.map((row) => {
        const links = videoTypeReviewLinks(row);
        return <li key={row.video_id}><a href={links.video!} target="_blank" rel="noopener noreferrer">{row.video_id}</a>
          {' · '}<a href={links.channel!} target="_blank" rel="noopener noreferrer">{row.channel_id}</a></li>;
      })}</ul>}
      <div className="video-review-pagination">
        <button type="button" disabled={loading || cursors.length === 1} onClick={() => setCursors((value) => value.slice(0, -1))}>Previous page</button>
        <button type="button" disabled={loading || !page?.next_cursor} onClick={() => page?.next_cursor && setCursors((value) => [...value, page.next_cursor!])}>Next page</button>
      </div>
    </>}
  </section>;
}
