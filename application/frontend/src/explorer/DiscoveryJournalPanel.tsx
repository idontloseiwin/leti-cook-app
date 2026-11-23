import { useEffect, useState } from 'react';
import { requestJson } from './api';

type Entry = { id: string; ts: string; operation: string; status: string; subject_ref?: string; channel_ref?: string;
  read_id?: string; work_ref?: string; operation_id?: string; request_attempt_id?: string; reason?: string;
  writer_id?: string; dropped_total?: number; sink_errors_total?: number };
type Snapshot = { status: string; events: Entry[]; metrics: { dropped?: number; sink_errors?: number } };
const stages = new Set(['signal', 'claim', 'wait', 'recovery', 'provider', 'writer', 'projection', 'api', 'browser', 'worker']);
const statuses = new Set(['accepted', 'started', 'received', 'confirmed', 'waiting', 'unknown', 'recovered', 'rejected', 'failed', 'rendered', 'stopped', 'prepared', 'delivered']);
export function parseDiscoveryJournal(value: unknown): Snapshot {
  if (!value || typeof value !== 'object') throw new Error('Journal unavailable');
  const root = value as Record<string, unknown>;
  if (!['ok', 'off', 'unavailable'].includes(String(root.status)) || !Array.isArray(root.events) || root.events.length > 200) throw new Error('Journal unavailable');
  const events: Entry[] = root.events.map((raw: unknown) => {
    if (!raw || typeof raw !== 'object') throw new Error('Journal unavailable');
    const item = raw as Record<string, unknown>;
    if (typeof item.id !== 'string' || item.id.length > 96 || typeof item.ts !== 'string' || !Number.isFinite(Date.parse(item.ts))
      || !stages.has(String(item.operation)) || !statuses.has(String(item.status))) throw new Error('Journal unavailable');
    const event: Entry = { id: item.id, ts: item.ts, operation: String(item.operation), status: String(item.status) };
    for (const key of ['subject_ref', 'channel_ref', 'read_id', 'work_ref', 'operation_id', 'request_attempt_id', 'writer_id'] as const) {
      if (item[key] !== undefined && (typeof item[key] !== 'string' || !/^[a-f0-9]{32}$/.test(item[key] as string))) throw new Error('Journal unavailable');
      if (typeof item[key] === 'string') event[key] = item[key];
    }
    for (const key of ['dropped_total', 'sink_errors_total'] as const) {
      const value = item[key];
      if (value !== undefined && (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0)) throw new Error('Journal unavailable');
      if (typeof value === 'number') event[key] = value;
    }
    if (typeof item.reason === 'string' && /^[a-z][a-z0-9_]{0,63}$/.test(item.reason)) event.reason = item.reason;
    return event;
  });
  const metrics: Snapshot['metrics'] = {};
  if (root.metrics && typeof root.metrics === 'object') for (const key of ['dropped', 'sink_errors'] as const) {
    const value = (root.metrics as Record<string, unknown>)[key];
    if (typeof value === 'number' && Number.isSafeInteger(value) && value >= 0) metrics[key] = value;
  }
  return { status: String(root.status), events, metrics };
}

export function DiscoveryJournalPanel() {
  // Closed until asked for: the journal is read and drawn only while it is open.
  const [open, setOpen] = useState(false);
  const [snapshot, setSnapshot] = useState<Snapshot>();
  const [failed, setFailed] = useState(false);
  const [revision, setRevision] = useState(0);
  const [subject, setSubject] = useState('');
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    requestJson('/diagnostics/discovery-journal', controller.signal).then(parseDiscoveryJournal).then(value => {
      if (!controller.signal.aborted) { setSnapshot(value); setFailed(false); }
    }).catch(() => { if (!controller.signal.aborted) setFailed(true); });
    return () => controller.abort();
  }, [open, revision]);
  const events = (snapshot?.events ?? []).filter(event => !subject || event.subject_ref === subject || event.channel_ref === subject).slice().reverse();
  const loss = new Map<string, {dropped: number; errors: number}>();
  for (const event of snapshot?.events ?? []) if (event.writer_id) {
    const previous = loss.get(event.writer_id);
    loss.set(event.writer_id, {dropped: Math.max(previous?.dropped ?? 0, event.dropped_total ?? 0), errors: Math.max(previous?.errors ?? 0, event.sink_errors_total ?? 0)});
  }
  const retainedLoss = [...loss.values()].reduce((sum, item) => ({dropped: sum.dropped + item.dropped, errors: sum.errors + item.errors}), {dropped: 0, errors: 0});
  return <section className="telemetry-v2-journal" aria-labelledby="discovery-journal-title">
    <details onToggle={event => setOpen(event.currentTarget.open)}>
    <summary><h2 id="discovery-journal-title">Discovery support journal</h2><span>{open ? 'Hide' : 'Show recent discovery boundaries'}</span></summary>
    {open && <>
    <p>Recent boundary observations with private references. Browser entries are client reports. Missing entries, dropped observations and unfinished work remain unknown; this journal does not establish collection health or visibility.</p>
    <button type="button" onClick={() => setRevision(value => value + 1)}>Refresh journal</button>
    {subject && <button type="button" onClick={() => setSubject('')}>Show all references</button>}
    <p role="status">{failed || snapshot?.status === 'unavailable' ? 'Journal unavailable; previously read observations are kept.' : snapshot?.status === 'off' ? 'Support journal is off.' : snapshot ? `${snapshot.events.length} retained observations` : 'Reading journal…'}</p>
    {snapshot && <p>Current process: {snapshot.metrics.dropped ?? 0} dropped observations; {snapshot.metrics.sink_errors ?? 0} write errors. Other processes report cumulative loss in their event envelopes.</p>}
    {snapshot && <p>Retained producer reports: {retainedLoss.dropped} dropped observations; {retainedLoss.errors} write errors. Rotation and missing final reports can hide further loss.</p>}
    <table><caption>Recent discovery boundaries</caption><thead><tr><th>Time</th><th>Boundary</th><th>Observation</th><th>Reference</th><th>Reason</th><th>Correlation</th></tr></thead><tbody>
      {events.map(event => <tr key={event.id}><td>{event.ts}</td><td>{event.operation}</td><td>{event.status}</td>
        <td>{[event.subject_ref, event.channel_ref].filter((value): value is string => Boolean(value)).map(value => <button key={value} type="button" onClick={() => setSubject(value)} title={value}>{value.slice(0, 12)}</button>)}</td><td>{event.reason ?? '—'}</td>
        <td><details><summary>IDs</summary><dl>{(['work_ref', 'operation_id', 'request_attempt_id', 'read_id'] as const).filter(key => event[key]).map(key => <div key={key}><dt>{key}</dt><dd>{event[key]}</dd></div>)}</dl></details></td></tr>)}
    </tbody></table>
    </>}
    </details>
  </section>;
}
