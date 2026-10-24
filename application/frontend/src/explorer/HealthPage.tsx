import { type ReactNode } from 'react';
import { AlertTriangle, ChevronDown, RefreshCw, ShieldCheck } from 'lucide-react';
import { loadPipelineHealth } from '../api';
import { ProviderKeyBanner, providerKeyRefusal } from '../components/ProviderKeyBanner';
import { VideoTypeReviewDisclosure } from '../components/VideoTypeReviewPanel';
import { YouTubeAttribution } from '../components/YouTubeAttribution';
import { formatDateTime, formatNumber, formatRelative } from '../format';
import {
  healthAssessment, healthCount, healthNumber, healthQueueState,
  healthSnapshotStale, healthSourceError, parsePipelineHealth, type HealthTone,
} from '../pipelineHealthPresentation';
import { pipelineQuotaPresentation } from '../quotaPresentation';
import { plainServerText } from '../serverText';
import { collectionReasonText } from '../collectionExplanation';
import type { PipelineHealthQueue, PipelineHealthQuota, Primitive } from '../types';
import { useOperationalRead } from './useOperationalRead';
import './health.css';

const REFRESH_MS = 30_000;
const PAUSED_TEXT = 'Automatic collection is off in config/config.ini. No collection pass or channel loading job runs, and new channels wait, until it is turned on and manage.py restart is run. Signals below were recorded before the pause.';
const readHealth = async (signal: AbortSignal) => parsePipelineHealth(await loadPipelineHealth(signal));

function elapsed(value: Primitive, unit: 'seconds' | 'minutes' = 'seconds'): string {
  const number = healthNumber(value);
  if (number === null) return '—';
  const seconds = Math.round(number * (unit === 'minutes' ? 60 : 1));
  if (seconds < 60) return seconds + 's';
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return minutes + 'm' + (seconds % 60 ? ' ' + seconds % 60 + 's' : '');
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return hours + 'h' + (minutes % 60 ? ' ' + minutes % 60 + 'm' : '');
  return Math.floor(hours / 24) + 'd' + (hours % 24 ? ' ' + hours % 24 + 'h' : '');
}
function healthText(value: unknown): string {
  return plainServerText(value, 640) ?? 'A health signal was withheld: it looked like a URL, a token or a stack trace.';
}
function title(value: string): string { return value.replace(/_/g, ' ').replace(/\b\w/g, (letter) => letter.toUpperCase()); }
function countRows(counts?: Record<string, number>): Array<[string, number]> {
  return Object.entries(counts ?? {}).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
}
function Flag({ children, tone = 'unknown' }: { children: ReactNode; tone?: HealthTone }) {
  return <span className={'health-flag status-' + tone}><i aria-hidden="true" />{children}</span>;
}
function Metric({ label, value, note }: { label: string; value: ReactNode; note?: ReactNode }) {
  return <div className="health-metric"><span>{label}</span><strong>{value}</strong>{note && <small>{note}</small>}</div>;
}
function Values({ rows }: { rows: Array<[string, ReactNode]> }) {
  return <dl className="health-values">{rows.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>;
}
function Details({ label, children }: { label: string; children: ReactNode }) {
  return <details className="health-details"><summary>{label}<ChevronDown size={14} aria-hidden="true" /></summary><div>{children}</div></details>;
}

function QueueSummary({ name, note, kind, received, error, stale }: {
  name: string; note: string; kind: 'stats' | 'discovery' | 'metadata'; received?: PipelineHealthQueue; error?: string; stale: boolean;
}) {
  const queue = error ? undefined : received;
  const stats = kind === 'stats', discovery = kind === 'discovery';
  const leasedDiscovery = discovery && queue?.mode === 'leased_channel_discovery_queue';
  const status = healthQueueState(queue, kind, error, stale);
  const detailRows: Array<[string, ReactNode]> = stats ? [
    ['Hydration due', healthCount(queue?.hydration_due)], ['Metadata due', healthCount(queue?.metadata_due)],
    ['Counters due', healthCount(queue?.counter_due)], ['Queue entries with recorded failures', healthCount(queue?.failed)],
    ['Excluded failures', healthCount(queue?.excluded_failed)], ['Paused or excluded', healthCount(queue?.paused)], ['Excluded by channel state', healthCount(queue?.policy_paused)], ['Expired leases', healthCount(queue?.expired_leases)], ['Total videos', healthCount(queue?.total)],
  ] : [
    [leasedDiscovery ? 'Technical errors' : 'No schedule', healthCount(leasedDiscovery ? queue?.failed : queue?.null_schedule)],
    ...(leasedDiscovery ? [['Public playlist not found', healthCount(queue?.playlist_not_found)] as [string, ReactNode]] : []),
    ...(leasedDiscovery ? [['Total playlists', healthCount(queue?.total)], ['Distinct channels', healthCount(queue?.total_channels)], ['Excluded failures', healthCount(queue?.excluded_failed)], ['Owned by initial loading', healthCount(queue?.initial_owned)]] as Array<[string, ReactNode]> : []),
    [leasedDiscovery ? 'Expired leases' : 'Oldest running', leasedDiscovery ? healthCount(queue?.expired_leases) : elapsed(queue?.oldest_running_minutes, 'minutes')],
    ...(discovery ? [['Resume states', healthCount(queue?.active_resume_states)] as [string, ReactNode]] : []),
    ...countRows(discovery ? queue?.streak_buckets : queue?.status_counts).map(([label, count]): [string, ReactNode] => [title(label), healthCount(count)]),
    ...(discovery && !leasedDiscovery ? [['Oldest resume state', elapsed(queue?.oldest_resume_age_minutes, 'minutes')] as [string, ReactNode]] : []),
  ];
  return <article className="health-queue">
    <header><h3>{name}</h3><Flag tone={status.tone}>{status.label}</Flag></header>
    <p className="health-note">{note}</p>
    {error && <p className="health-source-error" role="status">This read is unavailable · {error}</p>}
    <div className="health-queue-metrics">
      <Metric label="Due now" value={healthCount(stats ? queue?.all_due : queue?.due)} />
      <Metric label="Leased" value={healthCount(stats ? queue?.leased : queue?.running)} />
      <Metric label="Oldest overdue" value={elapsed(queue?.oldest_overdue_minutes, 'minutes')} />
    </div>
    {queue?.by_type && <Details label="By video type"><Values rows={Object.entries(queue.by_type).map(([type, bucket]) => [
      ({ long: 'Long-form', short: 'Shorts', stream: 'Streams', unknown: 'Unresolved type' } as Record<string, string>)[type],
      `${healthCount(bucket?.due)} due · ${healthCount(bucket?.failed)} with failures`,
    ])} /></Details>}
    <Details label="Queue details"><Values rows={detailRows} /></Details>
  </article>;
}

function QuotaBucket({ name, label, quota, stale }: { name: string; label: string; quota: PipelineHealthQuota; stale: boolean }) {
  const { used, limit, remaining, usedPercent, status } = pipelineQuotaPresentation(quota, name);
  const tone: HealthTone = stale || status === 'unavailable' || usedPercent === null ? 'unknown' : usedPercent >= 90 ? 'critical' : status === 'partial' || usedPercent >= 70 ? 'warning' : 'ok';
  return <article className={'health-quota-bucket status-' + tone}>
    <div><span>{label}</span><strong>{used === null ? 'Unavailable' : formatNumber(used, false)}{used !== null && <small> / {limit === null ? '—' : formatNumber(limit, false)}</small>}</strong></div>
    <span className="health-quota-percent">{status === 'partial' ? 'Incomplete' : usedPercent === null ? 'Unavailable' : usedPercent.toFixed(1) + '%'}</span>
    {usedPercent !== null && <div className="health-quota-track" role="meter" aria-label={label + ' estimated quota used'} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.min(100, usedPercent)} aria-valuetext={usedPercent.toFixed(1) + '%'}><i style={{ width: Math.min(100, usedPercent) + '%' }} /></div>}
    <p>{remaining === null ? 'Estimated remaining unavailable' : healthCount(remaining) + ' estimated units remaining'}</p>
  </article>;
}

export function HealthPage() {
  const state = useOperationalRead(readHealth, { intervalMs: REFRESH_MS, errorMessage: 'Health could not be refreshed. Try again to recheck the last reading.' });
  const report = state.payload;
  const stale = healthSnapshotStale(report, state.error);
  const tone = healthAssessment(report, stale);
  const errors = {
    stats: healthSourceError(report, 'stats'), discovery: healthSourceError(report, 'discovery'),
    metadata: healthSourceError(report, 'metadata'), sync: healthSourceError(report, 'sync'), quota: healthSourceError(report, 'quota'),
  };
  const latest = errors.sync ? undefined : report?.latest_sync;
  const lastSuccess = errors.sync ? undefined : report?.last_successful_sync;
  const lastSuccessNote = errors.sync ?? (healthNumber(lastSuccess?.duration_seconds) === null
    ? undefined : elapsed(lastSuccess?.duration_seconds) + ' duration');
  const recent = errors.sync ? undefined : report?.recent_syncs;
  const stats = errors.stats ? undefined : report?.queues?.stats;
  const quota = errors.quota ? {} : report?.youtube_quota ?? {};
  const quotaStatus = pipelineQuotaPresentation(quota, 'general').status;
  const quotaAvailable = quotaStatus !== 'unavailable';
  const attention = [
    ...(report?.critical ?? []).map((message) => ({ tone: 'critical' as HealthTone, message: healthText(message) })),
    ...(report?.warnings ?? []).map((message) => ({ tone: 'warning' as HealthTone, message: healthText(message) })),
    ...(report?.collection_errors ?? []).map((error) => ({ tone: 'warning' as HealthTone, message: `${error.component ?? 'Collector'}: ${error.error_type ?? 'Read failed'}` })),
  ];
  // Turned off on purpose: failures recorded before the pause are history, not an outage now.
  const paused = !stale && report?.status === 'paused';
  const assessment = stale ? 'Stale reading' : !report ? state.loading ? 'Loading' : 'Unavailable' : paused ? 'Collection paused' : tone === 'ok' ? 'Healthy' : tone === 'critical' ? 'Critical' : tone === 'warning' ? 'Needs attention' : 'Unknown';
  const observed = !report?.generated_at ? 'No reading yet' : 'Updated ' + formatRelative(report.generated_at);
  const runTone: HealthTone = stale || errors.sync || !latest || healthNumber(latest.errors) === null ? 'unknown' : latest.status === 'completed' && healthNumber(latest.errors) === 0 ? 'ok' : 'warning';

  return <main className="health-workspace-v2">
    <header className="health-page-header">
      <div><p className="eyebrow">Operations</p><h1>System health</h1><p className="health-page-description">Collection, reliability and quota — from recorded pipeline data.</p></div>
      <div className="health-header-actions"><span className="health-updated" title={formatDateTime(report?.generated_at)}>{observed}</span>
        <button type="button" className="health-refresh" onClick={state.refresh} disabled={state.loading || state.refreshing}><RefreshCw size={15} className={state.loading || state.refreshing ? 'spin' : ''} /><span>{state.refreshing ? 'Refreshing' : 'Refresh'}</span></button>
      </div>
    </header>

    <ProviderKeyBanner refusal={stale ? undefined : providerKeyRefusal(report?.provider_key)} />
    <section className={'health-assessment status-' + tone} aria-label="Health assessment">
      <div className="health-assessment-status">{tone === 'ok' ? <ShieldCheck size={22} /> : <AlertTriangle size={22} />}<div><strong>{assessment}</strong>
        <span>{stale ? 'The last reading is not current health.' : !report ? 'Waiting for a valid pipeline reading.' : paused ? PAUSED_TEXT : tone === 'ok' ? 'No active critical or warning signals.' : 'Review the signals below and affected queues.'}</span></div></div>
    </section>
    {state.error && <p className="health-read-error" role="alert">{state.error}</p>}
    {!report && state.loading && <p className="health-note" role="status">Reading pipeline health…</p>}
    {stale && <p className="health-read-error" role="status">Showing the last available reading, not current health. Generated {formatDateTime(report?.generated_at)}. Refresh to recheck.</p>}
    {attention.length > 0 && <div className="health-attention" aria-label={stale ? 'Signals from last reading' : 'Signals needing attention'}>
      {attention.slice(0, 3).map((signal, index) => <p key={index} className={'status-' + (stale || paused ? 'unknown' : signal.tone)}><AlertTriangle size={14} />{signal.message}</p>)}
      {attention.length > 3 && <Details label={`${attention.length - 3} more signals`}>{attention.slice(3).map((signal, index) => <p key={index} className={'status-' + (stale || paused ? 'unknown' : signal.tone)}>{signal.message}</p>)}</Details>}
    </div>}

    {report && <>
      <section className="health-kpi-rail" aria-label="Pipeline summary">
        <Metric label="Last successful sync" value={lastSuccess?.finished_at ? formatRelative(lastSuccess.finished_at) : errors.sync ? 'Unavailable' : 'Not recorded'} note={lastSuccessNote} />
        <Metric label="Videos due" value={healthCount(stats?.all_due)} note={errors.stats ?? 'Long-form, Shorts and Streams'} />
        <Metric label="Recent sync cycles" value={healthCount(recent?.count)} note={errors.sync ?? healthCount(recent?.error_total) + ' errors · ' + healthCount(report.window_hours) + 'h window'} />
        <Metric label="Unfinished API attempts" value={quotaAvailable ? healthCount(quota.unfinished?.count) : 'Unavailable'} note={errors.quota ?? (quotaAvailable ? `${quotaStatus === 'partial' ? 'Incomplete · ' : ''}${healthCount(quota.unfinished?.stale_count)} stale` : 'Attempt data unavailable')} />
      </section>

      <section className="health-section" aria-labelledby="health-queues-heading">
        <div className="health-section-heading"><h2 id="health-queues-heading">Collection queues</h2><span>Stored work, not a live worker status</span></div>
        <div className="health-queue-grid">
          <QueueSummary name="Video collection" note="All formats · stored candidates after channel and pause checks" kind="stats" received={report.queues?.stats} error={errors.stats} stale={stale} />
          <QueueSummary name="Playlist discovery" note="All formats · due counts are playlists, not channels" kind="discovery" received={report.queues?.channel_discovery} error={errors.discovery} stale={stale} />
          <QueueSummary name="Channel metadata" note="Channel profile and source fields" kind="metadata" received={report.queues?.channel_metadata} error={errors.metadata} stale={stale} />
        </div>
      </section>

      <div className="health-secondary-grid">
        <section className="health-section health-run" aria-labelledby="health-run-heading">
          <div className="health-section-heading"><h2 id="health-run-heading">Latest recorded sync</h2><Flag tone={runTone}>{errors.sync ? 'Unavailable' : stale ? 'Last reading' : latest?.status ? title(latest.status) : 'Not recorded'}</Flag></div>
          {errors.sync && <p className="health-source-error" role="status">This read is unavailable · {errors.sync}</p>}
          <div className="health-run-metrics"><Metric label="Duration" value={elapsed(latest?.duration_seconds)} /><Metric label="Stats updated" value={healthCount(latest?.stats_updated)} /><Metric label="New videos" value={healthCount(latest?.new_videos)} /><Metric label="Errors" value={healthCount(latest?.errors)} /></div>
          <p className="health-run-time">Started {formatDateTime(latest?.started_at)}<span>Finished {formatDateTime(latest?.finished_at)}</span></p>
          {!!latest?.explanation?.reasons.length && <Details label="Recorded reasons"><ul>{latest.explanation.reasons.map((reason, index) => <li key={index}>{collectionReasonText(reason)}</li>)}</ul><p className="health-note">These describe this run. Current queues determine subsequent work; this record does not confirm recovery or the next attempt time. Counts can overlap.</p></Details>}
          <Details label="Run details"><Values rows={[
            ['Metadata processed', healthCount(latest?.metadata_processed)], ['Metadata updated', healthCount(latest?.metadata_updated)],
            ['Discovery processed', healthCount(latest?.discovery_processed)], ['Regular stats requested', healthCount(latest?.regular_stats_requested)],
            ['Unavailable marked', healthCount(latest?.unavailable_marked)], ['Missing', healthCount(latest?.missing)],
          ]} /></Details>
          <div className="health-reliability"><div className="health-section-heading"><h3>Recent history</h3><Flag tone="unknown">{errors.sync ? 'Unavailable' : stale ? 'Last reading' : healthCount(recent?.error_total) + ' recorded errors'}</Flag></div>
            <p className="health-note">{healthCount(recent?.count)} recorded cycles · {healthCount(report.window_hours)}h observation window</p>
            <p className="health-note">Past errors remain in history. A successful unrelated sync does not confirm that an affected item recovered.</p>
            <div className="health-reliability-counts">{countRows(recent?.status_counts).map(([label, count]) => <span key={label}>{title(label)} <strong>{healthCount(count)}</strong></span>)}</div>
            <Values rows={[["Median duration", elapsed(recent?.duration_seconds?.median)], ['Longest duration', elapsed(recent?.duration_seconds?.max)]]} />
          </div>
        </section>

        <section className="health-section health-quota" aria-labelledby="health-quota-heading">
          <div className="health-section-heading"><h2 id="health-quota-heading">YouTube quota</h2><span>{quota.quota_day ?? 'Date unavailable'} · Pacific</span></div>
          {errors.quota && <p className="health-source-error" role="status">This read is unavailable · {errors.quota}</p>}
          <QuotaBucket name="general" label="General Data API" quota={quota} stale={stale} />
          <QuotaBucket name="video_batch_get_stats" label="Batch video statistics" quota={quota} stale={stale} />
          <p className="health-note health-quota-note">Ledger estimate, not an authoritative Google Console balance. Reset in {elapsed(quota.minutes_until_reset, 'minutes')}.</p>
          {report.history && <p className="health-note">{healthCount(report.history.transport_errors)} recorded transport errors in this quota day{report.history.transport_read_status === 'partial' ? ' · incomplete read' : ''}. Current queue failures are shown above; per-item recovery is not established by this count.</p>}
          <Details label="Requests by endpoint"><Values rows={countRows(quotaAvailable ? quota.endpoint_attempt_counts : undefined).map(([label, count]) => [label, healthCount(count)])} />
            {(!quotaAvailable || !countRows(quota.endpoint_attempt_counts).length) && <p className="health-note">{!quotaAvailable ? 'Attempt data unavailable.' : quotaStatus === 'partial' ? 'No attempts in the incomplete read.' : 'No attempts in this quota day.'}</p>}
            <p className="health-note">{quotaAvailable ? healthCount(quota.attempts) : 'Unavailable'} attempts{quotaStatus === 'partial' ? ' · incomplete' : ''} · {quotaAvailable && quota.status_counts ? healthCount(quota.status_counts.success ?? 0) : '—'} successful · {quotaAvailable && quota.status_counts ? healthCount(quota.status_counts.http_error ?? 0) : '—'} HTTP errors</p>
          </Details>
        </section>
      </div>
    </>}

    <VideoTypeReviewDisclosure readVersion={report?.generated_at} />
    <footer className="health-page-footer"><span>Refreshes 30s after each read · Independent of telemetry</span><YouTubeAttribution label="Open YouTube" /></footer>
    {report && <Details label="Report details"><Values rows={[
      ['Generated', formatDateTime(report.generated_at)], ['Visible channels', healthCount(healthSourceError(report, 'channels') ? undefined : report.channels?.visible)],
      ['Batch stats', report.runtime?.batch_stats_enabled === true ? 'Enabled' : report.runtime?.batch_stats_enabled === false ? 'Disabled' : 'Unknown'],
    ]} /></Details>}
  </main>;
}
