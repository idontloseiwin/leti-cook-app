import type { TelemetryEvent } from '../types';
import { eventIdentity, parseTelemetryEvent, TELEMETRY_EVENT_BYTES, TELEMETRY_VIEW_LIMIT } from './telemetryWorkspace';

export type TelemetryConnection = 'connecting' | 'connected' | 'reconnecting' | 'unavailable';
export type TelemetryJournalStatus = 'unknown' | 'ok' | 'unavailable';
type StreamLike = Pick<EventSource, 'onopen' | 'onmessage' | 'onerror' | 'close' | 'readyState' | 'addEventListener' | 'removeEventListener'>;
/** Source health is a separate named control message, never a diagnostic event. */
export function parseTelemetryJournalStatus(data: unknown): Exclude<TelemetryJournalStatus, 'unknown'> | null {
  if (typeof data !== 'string' || data.length > 64) return null;
  try {
    const value = JSON.parse(data);
    if (!value || typeof value !== 'object' || Array.isArray(value) || Object.keys(value).length !== 1) return null;
    return value.status === 'ok' || value.status === 'unavailable' ? value.status : null;
  } catch { return null; }
}
export type TelemetryStreamOptions = {
  lastEventId?: string;
  createSource?: (url: string) => StreamLike;
  onConnection: (state: TelemetryConnection) => void;
  onJournalStatus: (state: TelemetryJournalStatus) => void;
  onEvents: (events: TelemetryEvent[]) => void;
  onRejected: () => void;
  onCursor: (id: string) => void;
};

/** Native reconnect retains Last-Event-ID. A new connection after pause uses the same explicit cursor. */
export function connectTelemetryStream(options: TelemetryStreamOptions): () => void {
  let alive = true;
  let source: StreamLike;
  let flushTimer: ReturnType<typeof setTimeout> | undefined;
  let pending: TelemetryEvent[] = [];
  const seen = new Set<string>();
  const seenOrder: string[] = [];
  const onStatus: EventListener = (message) => {
    if (!alive) return;
    const status = parseTelemetryJournalStatus((message as MessageEvent).data);
    if (status) options.onJournalStatus(status);
    else options.onRejected();
  };
  const stop = () => {
    alive = false;
    if (flushTimer !== undefined) clearTimeout(flushTimer);
    pending = [];
    if (source) {
      source.onopen = source.onmessage = source.onerror = null;
      source.removeEventListener('telemetry-status', onStatus);
      source.close();
    }
  };
  options.onJournalStatus('unknown');
  options.onConnection('connecting');
  try {
    const url = options.lastEventId ? `/telemetry/stream?lastEventId=${encodeURIComponent(options.lastEventId)}` : '/telemetry/stream';
    source = options.createSource ? options.createSource(url) : new EventSource(url);
  } catch {
    options.onConnection('unavailable');
    return stop;
  }
  source.addEventListener('telemetry-status', onStatus);
  source.onopen = () => {
    if (alive) { options.onJournalStatus('unknown'); options.onConnection('connected'); }
  };
  source.onerror = () => {
    if (alive) {
      options.onJournalStatus('unknown');
      options.onConnection(source.readyState === 2 ? 'unavailable' : 'reconnecting');
    }
  };
  source.onmessage = (message) => {
    if (!alive) return;
    let event: TelemetryEvent | null = null;
    try {
      if (typeof message.data === 'string' && message.data.length <= TELEMETRY_EVENT_BYTES) {
        event = parseTelemetryEvent(JSON.parse(message.data));
        if (message.lastEventId && event?.id !== message.lastEventId) event = null;
      }
    } catch { /* Invalid data never reaches the render/mapping layer. */ }
    if (!event) { options.onRejected(); return; }
    const id = eventIdentity(event);
    if (seen.has(id)) return;
    seen.add(id);
    seenOrder.push(id);
    if (seenOrder.length > 1200) seen.delete(seenOrder.shift()!);
    pending.push(event);
    if (pending.length > TELEMETRY_VIEW_LIMIT) pending.shift();
    if (flushTimer === undefined) {
      flushTimer = setTimeout(() => {
        flushTimer = undefined;
        if (!alive) return;
        const batch = pending;
        pending = [];
        options.onEvents(batch);
        if (batch.length) options.onCursor(eventIdentity(batch[batch.length - 1]));
      }, 100);
    }
  };
  return stop;
}
