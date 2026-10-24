import { useSourceExpiry } from '../sourceLifetimeReact';
import { sourceExpired } from '../sourceLifetime';
import { Fragment, useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { HiddenVideoIdentities } from './HiddenVideoIdentities';
import {
  decideVideoType, loadVideoTypeCase, loadVideoTypeCases, newVideoTypeRequestId,
  VIDEO_REVIEW_LABELS, VIDEO_TYPE_REASON_LIMIT, VideoTypeReviewError, videoTypeReason, videoTypeReviewLinks,
  type VideoReviewFilter, type VideoReviewType, type VideoTypeCase, type VideoTypeCaseDetail,
  type VideoTypeCasesPage, type VideoTypeCommand,
} from '../videoTypeReviews';

function errorText(error: unknown): string {
  return error instanceof Error ? error.message.slice(0, 360) : 'Video type reviews are unavailable.';
}
function dateText(value: string): string { return new Date(value).toLocaleString(); }
function typeText(value: VideoReviewType | null): string { return value ? VIDEO_REVIEW_LABELS[value] : 'Unknown'; }

const REVIEW_LABELS = {
  needs_review: 'Needs review', awaiting_refresh: 'Awaiting automatic recheck',
  channel_conflict: 'Channel identity check', confirmed: 'Type confirmed', history: 'Decision history',
};
function reviewLabel(row: VideoTypeCase): string {
  return sourceExpired(row) ? 'Awaiting automatic recheck' : REVIEW_LABELS[row.review_status] ?? 'Awaiting automatic recheck';
}

/** Keep an empty manual-work form out of ordinary health; real exceptions retain an indicator. */
export function VideoTypeReviewDisclosure({ readVersion }: { readVersion?: string }) {
  const [expanded, setExpanded] = useState(false);
  const [summary, setSummary] = useState<VideoTypeCasesPage>();
  const [unavailable, setUnavailable] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [hasOpened, setHasOpened] = useState(false);
  useSourceExpiry(summary, () => setSummary(undefined), () => setRefresh(value => value + 1));
  useEffect(() => {
    if (expanded) return;
    const controller = new AbortController();
    setUnavailable(false);
    void loadVideoTypeCases('open', undefined, controller.signal).then(page => {
      if (!controller.signal.aborted) setSummary(page);
    }).catch(() => { if (!controller.signal.aborted) { setSummary(undefined); setUnavailable(true); } });
    return () => controller.abort();
  }, [readVersion, expanded, refresh]);
  const attention = summary?.cases.some(row => !sourceExpired(row)
    && (row.review_status === 'needs_review' || row.review_status === 'channel_conflict'));
  return <section className="health-section" aria-label="Type checks and decision history">
    <button type="button" className="health-refresh" aria-expanded={expanded} aria-controls="video-type-checks-content"
      onClick={() => { setHasOpened(true); setExpanded(value => !value); }}>
      {attention ? 'Type checks need attention' : unavailable ? 'Type checks unavailable · open to retry' : 'Type checks and decision history'}
    </button>
    {/* Keep pending decisions mounted across collapse so their request ID remains retryable. */}
    {(expanded || hasOpened) && <div id="video-type-checks-content" hidden={!expanded}><VideoTypeReviewPanel /></div>}
  </section>;
}

export function VideoTypeReviewPanel() {
  const [filter, setFilter] = useState<VideoReviewFilter>('open');
  const [cursors, setCursors] = useState<(string | undefined)[]>([undefined]);
  const [list, setList] = useState<{ page?: VideoTypeCasesPage; reviewChanged: boolean }>({ reviewChanged: false });
  const { page, reviewChanged } = list;
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string>();
  const [refresh, setRefresh] = useState(0);
  const [selection, setSelection] = useState<{ row: VideoTypeCase; mode: 'edit' | 'history' }>();
  const [, repaint] = useState(0);
  useSourceExpiry(page, () => repaint(value => value + 1), () => { if (!selection && !loading) setRefresh(value => value + 1); });
  const generation = useRef(0);
  const cursor = cursors[cursors.length - 1];
  useEffect(() => {
    const ticket = ++generation.current, controller = new AbortController();
    setLoading(true); setError(undefined); setList({ reviewChanged: false });
    void loadVideoTypeCases(filter, cursor, controller.signal).then((result) => {
      if (ticket === generation.current && !controller.signal.aborted) setList({ page: result, reviewChanged: false });
    }).catch((error) => {
      if (ticket === generation.current && !controller.signal.aborted) setError(errorText(error));
    }).finally(() => {
      if (ticket === generation.current && !controller.signal.aborted) setLoading(false);
    });
    return () => { controller.abort(); generation.current += 1; };
  }, [filter, cursor, refresh]);
  const updateCase = useCallback((row: VideoTypeCase) => {
    setList((current) => {
      if (!current.page) return current;
      const previous = current.page.cases.find((item) => item.case_id === row.case_id);
      if (!previous) return current;
      return {
        page: { ...current.page, cases: current.page.cases.map((item) => item.case_id === row.case_id ? row : item) },
        reviewChanged: current.reviewChanged || previous.revision !== row.revision || previous.state !== row.state || previous.review_status !== row.review_status,
      };
    });
  }, []);
  const closeReview = () => {
    setSelection(undefined);
    // Keep confirmation/history mounted until the operator closes it, then
    // reload the bounded list so a resolved case no longer stays under Open.
    if (reviewChanged) {
      setList((current) => ({ ...current, reviewChanged: false }));
      setCursors([undefined]);
      setRefresh((value) => value + 1);
    }
  };
  const changeFilter = (next: VideoReviewFilter) => { setFilter(next); setCursors([undefined]); };

  return <section id="video-type-reviews" className="panel video-type-reviews" aria-labelledby="video-type-reviews-heading">
    <div className="video-review-heading">
      <div><p className="eyebrow">Type checks</p><h2 id="video-type-reviews-heading">Type checks and decision history</h2></div>
      <button type="button" className="icon-button" disabled={loading || Boolean(selection)} onClick={() => setRefresh((value) => value + 1)}>Refresh reviews</button>
    </div>
    <p className="video-review-intro">Only current contradictions that automatic checks cannot settle need a manual decision. Missing or expired evidence waits for automatic rechecking. Saved decisions and their history remain available.</p>
    <div className="video-review-toolbar">
      <div className="video-review-filters" role="group" aria-label="Review state">
        {(['open', 'awaiting', 'resolved', 'all'] as const).map((value) => <button type="button" key={value} aria-pressed={filter === value}
          disabled={Boolean(selection)} onClick={() => changeFilter(value)}>{value === 'open' ? 'Needs attention' : value === 'awaiting' ? 'Awaiting recheck' : value === 'resolved' ? 'Saved decisions' : 'All history'}</button>)}
      </div>
      <span className="video-review-meta">Page {cursors.length}{page ? ` · ${page.cases.length} shown` : ''}</span>
    </div>
    {error && <div className="video-review-error" role="alert">{error} <button type="button" onClick={() => setRefresh((value) => value + 1)}>Retry loading</button></div>}
    {loading && <p role="status" className="video-review-empty">Loading video type reviews…</p>}
    {!loading && page?.cases.length === 0 && <p className="video-review-empty">{filter === 'open' ? 'No type checks need attention.' : filter === 'awaiting' ? 'No retained type checks are awaiting recheck.' : filter === 'resolved' ? 'No saved type decisions.' : 'No type-check history.'}</p>}
    {page && page.cases.length > 0 && <div className="video-review-table-wrap">
      <table className="video-review-table">
        <caption className="video-review-sr-only">Video type conflicts and recorded operator decisions</caption>
        <thead><tr><th scope="col">Video / channel</th><th scope="col">Observed / current type</th><th scope="col">Operator decision</th><th scope="col">State</th><th scope="col">Actions</th></tr></thead>
        <tbody>{page.cases.map((row) => {
          const links = videoTypeReviewLinks(row), expanded = selection?.row.case_id === row.case_id;
          return <Fragment key={row.case_id}>
            <tr>
              <th scope="row">{links.video ? <a href={links.video} target="_blank" rel="noopener noreferrer" aria-label={`Open video ${row.video_id} on YouTube`}>{row.video_id} ↗</a> : <span>Video unavailable</span>}
                {links.channel && <a className="video-review-channel" href={links.channel} target="_blank" rel="noopener noreferrer" aria-label={`Open channel ${row.channel_id} on YouTube`}>{row.channel_id} ↗</a>}</th>
              <td><span>{sourceExpired(row) ? 'Current evidence is being refreshed.' : row.observed_types.map(typeText).join(', ') || 'No present membership'}</span><small>Current: {sourceExpired(row) ? 'Unavailable' : typeText(row.canonical_type)}</small></td>
              <td>{row.decision ? <><strong>{typeText(row.decision.video_type)}</strong><p className="video-review-reason">{row.decision.reason}</p><small>{dateText(row.decision.decided_at)}</small></> : <span className="video-review-meta">No decision yet</span>}</td>
              <td><span className={`video-review-state is-${row.state}`}>{reviewLabel(row)}</span><small>Revision {row.revision}</small>
                {row.source_detached_at && <small className="video-review-blocked">Source unavailable · history retained</small>}
                {sourceExpired(row) ? <small className="video-review-blocked">Current evidence unavailable · awaiting automatic recheck</small>
                  : row.blocked_reason === 'channel_conflict' && <small className="video-review-blocked">Channel identity conflict</small>}</td>
              <td><div className="video-review-row-actions"><button type="button" disabled={Boolean(selection) || !row.can_decide || sourceExpired(row)} aria-expanded={expanded} onClick={() => setSelection({ row, mode: 'edit' })}>{row.decision ? 'Edit decision' : 'Review'}</button>
                <button type="button" disabled={Boolean(selection)} onClick={() => setSelection({ row, mode: 'history' })}>History</button></div></td>
            </tr>
            {expanded && <tr className="video-review-expanded"><td colSpan={5}><VideoTypeReviewDetails key={row.case_id} initialCase={selection.row}
              initialMode={selection.mode} onCase={updateCase} onClose={closeReview} /></td></tr>}
          </Fragment>;
        })}</tbody>
      </table>
    </div>}
    <div className="video-review-pagination">
      <button type="button" disabled={loading || Boolean(selection) || cursors.length === 1} onClick={() => setCursors((current) => current.slice(0, -1))}>Previous page</button>
      <span className="video-review-meta">{selection ? 'Close the current review to change the list.' : 'Reviews load on demand.'}</span>
      <button type="button" disabled={loading || Boolean(selection) || !page?.next_cursor} onClick={() => page?.next_cursor && setCursors((current) => [...current, page.next_cursor!])}>Next page</button>
    </div>
    <HiddenVideoIdentities />
  </section>;
}

export function VideoTypeReviewDetails({ initialCase, initialMode, onCase, onClose }: {
  initialCase: VideoTypeCase; initialMode: 'edit' | 'history'; onCase: (row: VideoTypeCase) => void; onClose: () => void;
}) {
  const caseId = initialCase.case_id;
  const [detail, setDetail] = useState<VideoTypeCaseDetail>();
  const [mode, setMode] = useState(initialMode);
  const [loading, setLoading] = useState(true);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [readError, setReadError] = useState<string>();
  const [type, setType] = useState<VideoReviewType | ''>('');
  const [reason, setReason] = useState('');
  const [pending, setPending] = useState<VideoTypeCommand>();
  const [saving, setSaving] = useState(false);
  const [writeError, setWriteError] = useState<string>();
  const [needsReload, setNeedsReload] = useState(false);
  const [notice, setNotice] = useState<string>();
  const draftInitialized = useRef(false), mounted = useRef(true), reading = useRef(0), writing = useRef(false);
  const [, repaint] = useState(0);
  useSourceExpiry(detail?.case ?? initialCase, () => repaint(value => value + 1), () => { if (!loading && !saving && !historyLoading) void readDetail(); });

  const readDetail = async (signal?: AbortSignal) => {
    const ticket = ++reading.current;
    setLoading(true); setReadError(undefined);
    try {
      const result = await loadVideoTypeCase(caseId, undefined, signal);
      if (!mounted.current || ticket !== reading.current || signal?.aborted) return;
      setDetail(result); onCase(result.case);
      if (!draftInitialized.current) {
        setType(result.case.decision?.video_type ?? ''); setReason(result.case.decision?.reason ?? ''); draftInitialized.current = true;
      }
      if (needsReload) { setNeedsReload(false); setPending(undefined); setWriteError(undefined); setNotice('Latest revision loaded. Your draft is preserved; review it before saving.'); }
    } catch (error) {
      if (mounted.current && ticket === reading.current && !signal?.aborted) setReadError(errorText(error));
    } finally {
      if (mounted.current && ticket === reading.current && !signal?.aborted) setLoading(false);
    }
  };
  useEffect(() => {
    mounted.current = true;
    const controller = new AbortController();
    void readDetail(controller.signal);
    return () => { mounted.current = false; controller.abort(); reading.current += 1; };
  }, [caseId]);

  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (writing.current || loading || historyLoading || needsReload || !detail || (!pending && (!detail.case.can_decide || sourceExpired(detail.case)))) return;
    let command: VideoTypeCommand;
    try {
      if (!pending && !type) throw new VideoTypeReviewError('Choose Long, Short, or Stream.');
      command = pending ?? { request_id: newVideoTypeRequestId(), expected_revision: detail.case.revision, video_type: type as VideoReviewType, reason: videoTypeReason(reason) };
    } catch (error) { setWriteError(errorText(error)); return; }
    writing.current = true; setSaving(true); setPending(command); setWriteError(undefined); setNotice(undefined);
    try {
      const result = await decideVideoType(caseId, command);
      if (!mounted.current) return;
      setDetail((current) => current ? { ...current, case: result.case } : current); onCase(result.case);
      setPending(undefined); setNeedsReload(false);
      setType(result.case.decision?.video_type ?? ''); setReason(result.case.decision?.reason ?? '');
      setNotice(result.case.decision && result.case.decision.revision > result.mutation.applied_revision
        ? 'This request was recorded. A newer operator decision is shown.' : `Decision saved at revision ${result.mutation.applied_revision}.`);
      setMode('history');
      await readDetail();
    } catch (error) {
      if (!mounted.current) return;
      const status = error instanceof VideoTypeReviewError ? error.status : undefined;
      const code = error instanceof VideoTypeReviewError ? error.code : undefined;
      const uncertain = status === undefined || status === 408 || status >= 500;
      const busy = status === 409 && code === 'video_type_review_busy';
      if (uncertain || busy) {
        setWriteError(`${errorText(error)} ${busy ? 'The video is busy.' : 'The outcome is not confirmed.'} Retry sends the same decision request.`);
      } else {
        setPending(undefined); setNeedsReload(status === 409 || status === 404);
        setWriteError(`${errorText(error)}${status === 409 ? ' Your draft is preserved. Reload the current case before saving again.' : ''}`);
      }
    } finally { writing.current = false; if (mounted.current) setSaving(false); }
  };
  const moreHistory = async () => {
    if (!detail?.next_before_revision || historyLoading || loading || saving) return;
    const ticket = reading.current, expected = detail.case.revision;
    setHistoryLoading(true); setReadError(undefined);
    try {
      const result = await loadVideoTypeCase(caseId, detail.next_before_revision);
      if (!mounted.current || ticket !== reading.current) return;
      if (result.case.revision !== expected) { setReadError('The case changed while reading history. Reload to see its current history.'); return; }
      setDetail((current) => current ? { ...current, history: [...current.history, ...result.history], next_before_revision: result.next_before_revision } : current);
    } catch (error) { if (mounted.current && ticket === reading.current) setReadError(errorText(error)); }
    finally { if (mounted.current) setHistoryLoading(false); }
  };
  const current = detail?.case ?? initialCase, links = videoTypeReviewLinks(current);
  const frozenDraft = saving || Boolean(pending);
  const prefix = `video-review-${caseId}`;
  return <div className="video-review-details" aria-label={`Review ${caseId}`}>
    <div className="video-review-heading"><div><h3>Review {current.video_id ?? 'unavailable video'}</h3><p className="video-review-meta">Opened {dateText(current.first_seen_at)} · Last review check {dateText(current.last_seen_at)}</p></div>
      <button type="button" disabled={saving} onClick={onClose}>Close review</button></div>
    <div className="video-review-detail-actions">
      {links.video && <a href={links.video} target="_blank" rel="noopener noreferrer">Open video on YouTube ↗</a>}
      {links.channel && <a href={links.channel} target="_blank" rel="noopener noreferrer">Open channel on YouTube ↗</a>}
      <button type="button" disabled={loading || historyLoading || saving} onClick={() => void readDetail()}>Reload current case</button>
    </div>
    {current.source_detached_at && <p className="video-review-meta">Source data is unavailable. Your decision and its history are preserved.</p>}
    {readError && <p className="video-review-error" role="alert">{readError}</p>}
    {loading && <p role="status">Loading current case and history…</p>}
    {notice && <p className="video-review-notice" role="status">{notice}</p>}
    {writeError && <p className="video-review-error" role="alert">{writeError}</p>}
    {detail && <>
      <div className="video-review-filters" role="group" aria-label="Review detail">
        <button type="button" aria-pressed={mode === 'edit'} disabled={current.source_detached_at !== null && !pending} onClick={() => setMode('edit')}>{current.decision ? 'Edit decision' : 'Record decision'}</button>
        <button type="button" aria-pressed={mode === 'history'} onClick={() => setMode('history')}>Decision history</button>
      </div>
      {mode === 'edit' ? <form className="video-review-form" onSubmit={(event) => void save(event)}>
        {(!current.can_decide || sourceExpired(current)) && <p className="video-review-blocked" role="status">{sourceExpired(current) ? 'Current evidence unavailable. Reload to check this case.' : current.blocked_reason === 'channel_conflict' ? 'Channel identity also conflicts. A type decision cannot resolve this case.' : current.review_status === 'awaiting_refresh' ? 'Awaiting automatic recheck. No manual decision is required.' : 'Current decision eligibility is unavailable. Reload to check this case.'}</p>}
        <div className="video-review-form-fields">
          <label htmlFor={`${prefix}-type`}>Operator-selected type<select id={`${prefix}-type`} aria-label="Operator-selected type" required value={type} disabled={frozenDraft || !current.can_decide || sourceExpired(current)}
            onChange={(event) => { setType(event.target.value as VideoReviewType | ''); setWriteError(undefined); }}>
            <option value="">Choose type</option>{(Object.keys(VIDEO_REVIEW_LABELS) as VideoReviewType[]).map((value) => <option key={value} value={value}>{VIDEO_REVIEW_LABELS[value]}</option>)}
          </select></label>
          <label htmlFor={`${prefix}-reason`}>Reason for this manual decision<textarea id={`${prefix}-reason`} aria-label="Reason for this manual decision" required rows={3} value={reason}
            readOnly={frozenDraft} disabled={(!current.can_decide || sourceExpired(current)) && !pending} aria-describedby={`${prefix}-reason-hint`}
            onChange={(event) => { setReason(event.target.value); setWriteError(undefined); }} /></label>
        </div>
        <div className="video-review-save-row"><small id={`${prefix}-reason-hint`}>{[...reason.trim()].length} / {VIDEO_TYPE_REASON_LIMIT} characters · One-line operator annotation, separate from observed membership.</small>
          <button type="submit" disabled={saving || loading || historyLoading || needsReload || (!pending && (!current.can_decide || sourceExpired(current) || !type || !reason.trim() || [...reason.trim()].length > VIDEO_TYPE_REASON_LIMIT))}>
            {saving ? 'Saving…' : pending ? 'Retry same decision' : 'Save decision'}
          </button></div>
      </form> : <div className="video-review-history">
        <p className="video-review-meta">Recorded operator decisions, newest first. Current case revision: {current.revision}.</p>
        {detail.history.length === 0 ? <p>No decisions have been recorded.</p> : <ol>{detail.history.map((entry) => <li key={entry.revision}>
          <div><strong>Revision {entry.revision}: {typeText(entry.previous_type)} → {typeText(entry.video_type)}</strong><time dateTime={entry.decided_at}>{dateText(entry.decided_at)}</time></div>
          <p>{entry.reason}</p>
        </li>)}</ol>}
        {detail.next_before_revision !== null && <button type="button" disabled={historyLoading || loading || saving} onClick={() => void moreHistory()}>{historyLoading ? 'Loading older decisions…' : 'Load older decisions'}</button>}
      </div>}
    </>}
  </div>;
}
