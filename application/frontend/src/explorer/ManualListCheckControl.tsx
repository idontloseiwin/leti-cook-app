import { useEffect, useRef, useState } from 'react';
import type { ManualListCheck, VideoType } from '../types';
import { requestJson } from './api';
import { newSavedWorkId } from './savedWork';

type Props = {
  channelId: string; type: VideoType; coverage: string; check: ManualListCheck;
  onRead: () => void; onComplete: () => void;
};

const reasons: Record<string, string> = {
  discovery_off: 'Discovery is paused.', format_off: 'Collection is off for this format.',
  channel_on_hold: 'Collection for this channel is on hold.', source_unavailable: 'List source unavailable.',
  window_budget: 'Waiting for the next budget window.', daily_budget: 'Waiting for the daily allowance.',
  provider_quota: 'Waiting for YouTube quota.', reserve: 'Keeping the installation quota reserve.',
  full_budget_unavailable: 'The full-check budget is unavailable.', retry: 'Waiting to retry a page.',
};

export function ManualListCheckControl({ channelId, type, coverage, check, onRead, onComplete }: Props) {
  const [confirming, setConfirming] = useState(false);
  const [sending, setSending] = useState(false);
  const [notice, setNotice] = useState('');
  const command = useRef<{ request_id: string; previous: string | null; epoch: number } | null>(null);
  const { request } = check;
  const active = request?.state === 'queued' || request?.state === 'running';
  const callbacks = useRef({ onRead, onComplete });
  callbacks.current = { onRead, onComplete };
  const observing = useRef<string | null>(null);
  useEffect(() => {
    if (active && request) {
      observing.current = request.id;
      const timer = window.setInterval(() => { if (!document.hidden) callbacks.current.onRead(); }, 10000);
      return () => window.clearInterval(timer);
    }
    if (request && observing.current === request.id) {
      observing.current = null;
      callbacks.current.onComplete();
    }
  }, [active, request?.id]);
  const cost = coverage === 'absent' ? '1 General to check for the list. More pages if it has appeared.'
    : check.estimated_general === null ? 'From 1 General. List size unknown.'
    : 'About ' + check.estimated_general + ' General.';
  const submit = async () => {
    if (sending || check.epoch === null || check.unavailable || active) return;
    const payload = command.current ?? { request_id: newSavedWorkId(), previous: request?.id ?? null, epoch: check.epoch };
    command.current = payload;
    setSending(true);
    try {
      const result = await requestJson('/channels/' + encodeURIComponent(channelId) + '/explorer/list-check/' + type,
        undefined, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Saved-Work': '1' },
          body: JSON.stringify(payload) }) as { request: { id: string }; joined: boolean };
      observing.current = result.request.id;
      setNotice(result.joined ? 'Joined the existing check.' : 'Full list check requested.');
      setConfirming(false);
      command.current = null;
    } catch (error) {
      setNotice(error instanceof Error ? error.message : 'Request outcome unknown. Check status or retry.');
    } finally { setSending(false); callbacks.current.onRead(); }
  };
  return <div className="explorer-list-check">
    <span>{check.unavailable ? reasons[check.unavailable] ?? 'Check unavailable.' : cost + ' 0 Stats for the list check.'}</span>{' '}
    {!check.unavailable && !active && !confirming && <button type="button" onClick={() => {
      command.current = null; setConfirming(true); setNotice('');
    }}>Verify full list</button>}
    {confirming && !active && !check.unavailable && <span>
      {coverage === 'complete' ? 'Coverage is already confirmed. ' : ''}
      Retries can cost more. Fetching new video details is separate.{' '}
      <button type="button" disabled={sending} onClick={submit}>Confirm check</button>{' '}
      <button type="button" disabled={sending} onClick={() => setConfirming(false)}>Cancel</button>
    </span>}
    {request && <span role="status">{' '}{({queued:'Queued',running:'Checking',complete:'Full check complete',
      absent:'Public list absent',failed:'Check failed'})[request.state]}. {request.pages} pages.
      {' '}{request.general} General confirmed since request.
      {active && (check.quota_retry_at || request.wait) && ' ' + (check.quota_retry_at ? 'Waiting for YouTube quota.'
        : reasons[request.wait!] ?? 'Waiting for collector admission.')}
      {active && (check.quota_retry_at ?? request.retry_at) && ' Retry after ' + (check.quota_retry_at ?? request.retry_at) + '.'}
    </span>}
    {notice && <span role="status"> {notice}</span>}
  </div>;
}
