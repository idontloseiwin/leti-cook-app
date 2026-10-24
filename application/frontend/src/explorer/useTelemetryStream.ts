import { useEffect, useRef, useState } from 'react';
import type { TelemetryEvent } from '../types';
import { appendTelemetryEvents, eventIdentity, type TelemetryRecentSnapshot } from './telemetryWorkspace';
import { connectTelemetryStream, type TelemetryConnection, type TelemetryJournalStatus } from './telemetryStream';

export function useTelemetryStream(recent: TelemetryRecentSnapshot | undefined, paused: boolean, revision: number) {
  const [events, setEvents] = useState<TelemetryEvent[]>([]);
  const [arrivals, setArrivals] = useState<TelemetryEvent[]>([]);
  const [connection, setConnection] = useState<TelemetryConnection>('connecting');
  const [journalStatus, setJournalStatus] = useState<TelemetryJournalStatus>('unknown');
  const [rejected, setRejected] = useState(0);
  const cursor = useRef('');
  const onEvents = (incoming: TelemetryEvent[]) => setEvents((current) => appendTelemetryEvents(current, incoming));
  useEffect(() => {
    if (paused || !recent) return;
    onEvents(recent.events);
    // Snapshot and live reads can complete in either order. Never replace a live cursor with older replay.
    if (!cursor.current && recent.events.length) cursor.current = eventIdentity(recent.events[recent.events.length - 1]);
  }, [recent, paused]);
  useEffect(() => {
    if (paused) return;
    return connectTelemetryStream({
      lastEventId: cursor.current,
      onConnection: setConnection,
      onJournalStatus: setJournalStatus,
      onEvents: (batch) => { onEvents(batch); setArrivals(batch); },
      onRejected: () => setRejected((value) => Math.min(value + 1, 1_000_000)),
      onCursor: (id) => { cursor.current = id; },
    });
  }, [paused, revision]);
  return { events, arrivals, connection, journalStatus, rejected };
}
