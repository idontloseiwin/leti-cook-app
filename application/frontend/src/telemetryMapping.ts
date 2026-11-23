import type { TelemetryEvent } from './types';

function textValue(value: unknown, fallback = ''): string {
    if (typeof value === 'string' && value.trim()) return value;
    if (typeof value === 'number' || typeof value === 'boolean') return String(value);
    return fallback;
}

export function normalizeTelemetryText(value: unknown): string {
    return textValue(value).toLowerCase().replace(/[\s.-]+/g, '_');
}

function operationText(event: TelemetryEvent): string {
    return textValue(event.operation || event.event || event.phase, 'telemetry.event');
}

export function eventIdentity(event: TelemetryEvent): string {
    return textValue(
        event.id,
        [event.ts || event.timestamp, event.kind, event.source, event.target, operationText(event), event.status]
            .map((value) => textValue(value))
            .join('|')
    );
}

export function eventTimestamp(event: TelemetryEvent): number {
    return Date.parse(textValue(event.ts || event.timestamp)) || 0;
}

export function mergeTelemetryEvents(baseEvents: TelemetryEvent[], streamEvents: TelemetryEvent[], maxEvents: number): TelemetryEvent[] {
    const lookup = new Map<string, TelemetryEvent>();
    [...baseEvents, ...streamEvents].forEach((event) => lookup.set(eventIdentity(event), event));
    return Array.from(lookup.values())
        .sort((left, right) => eventTimestamp(left) - eventTimestamp(right))
        .slice(-maxEvents);
}
