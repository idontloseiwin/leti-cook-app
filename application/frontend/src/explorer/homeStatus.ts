import type { SystemSummary } from '../types';
import type { ExplorerHomeSummary } from './model';
import type { SummaryState } from './useVisibleSummary';
import { formatTimestamp } from './format';

// Home's System card and "Sync … ago" line. The server applies the collection
// rule Health uses (collection_state.py): a partial sync without errors is
// healthy, a YouTube quota or rate-limit stop is a warning, only a failure is
// an error. This module only presents that decision.

export type HomeSystemPresentation = {
  label: 'Checking…' | 'Healthy' | 'Warning' | 'Error' | 'Paused';
  tone: 'neutral' | 'healthy' | 'warning' | 'error';
  problem: boolean;
  reason?: string;
  detail?: string;
};

export function formatRelativeTime(value: string, now = Date.now()): string {
  const timestamp = new Date(value).getTime();
  if (!Number.isFinite(timestamp)) return 'at an invalid time';
  const elapsedMs = now - timestamp;
  if (elapsedMs < 0) return 'just now';
  const minutes = Math.floor(elapsedMs / 60_000);
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

const KNOWN_REASONS: Record<string, string> = {
  latest_run_failed: 'Latest sync failed',
  no_sync_runs: 'No sync runs recorded',
  system_warning: 'System needs attention',
  collection_waiting_for_quota: 'Waiting for YouTube quota',
  // One bucket ran out; the full notice on hover says what continues.
  collection_waiting_for_general_quota: 'Waiting for General quota',
  collection_waiting_for_stats_quota: 'Waiting for Batch statistics quota',
  collection_rate_limited: 'YouTube asked to slow down',
  collection_not_finishing: 'No sync finished recently',
  no_full_pass: 'No full pass in 24 h',
};

function shortReason(value?: string): string | undefined {
  const raw = value?.trim();
  if (!raw) return undefined;
  const humanized = KNOWN_REASONS[raw]
    ?? raw.replace(/_/g, ' ').replace(/\s+/g, ' ');
  const normalized = humanized.charAt(0).toUpperCase() + humanized.slice(1);
  if (!normalized) return undefined;
  return normalized.length > 92 ? `${normalized.slice(0, 89)}…` : normalized;
}

export function systemPresentation(state: SummaryState<SystemSummary>): HomeSystemPresentation {
  if (state.kind === 'loading') {
    return { label: 'Checking…', tone: 'neutral', problem: false };
  }
  if (state.kind === 'error') {
    return {
      label: 'Warning',
      tone: 'warning',
      problem: true,
      reason: 'System health unavailable',
      detail: state.message,
    };
  }
  const status = state.payload.status?.toLowerCase();
  const warning = state.payload.warnings?.[0];
  const warningDetail = warning?.message || warning?.code;
  if (status === 'ok' || (status === 'running' && !warningDetail)) {
    return { label: 'Healthy', tone: 'healthy', problem: false };
  }
  const detail = warningDetail
    || state.payload.status_reason
    || 'System status needs attention';
  // A collection signal has a short name; other warnings show their own text.
  const reason = (warning?.message && KNOWN_REASONS[warning.code ?? '']) || shortReason(detail);
  if (status === 'paused') {
    // Turned off on purpose: not a failure, but new channels wait until it is turned on.
    return { label: 'Paused', tone: 'neutral', problem: true, reason, detail };
  }
  if (status === 'error') {
    return { label: 'Error', tone: 'error', problem: true, reason, detail };
  }
  return { label: 'Warning', tone: 'warning', problem: true, reason, detail };
}

// "Sync … ago" is the latest sync without errors, complete or partial; the
// hover text adds the last full pass.
export function homeSyncPresentation(state: SummaryState<ExplorerHomeSummary>, now = Date.now()): {
  value: string; title?: string;
} {
  if (state.kind === 'loading') return { value: 'Loading…' };
  if (state.kind === 'error') return { value: 'Sync unavailable' };
  const syncAt = state.payload.last_sync_without_errors_at;
  const fullAt = state.payload.last_successful_sync_at;
  if (!syncAt) return { value: 'No successful sync' };
  return {
    value: `Sync ${formatRelativeTime(syncAt, now)}`,
    title: `${formatTimestamp(syncAt)} · Last full pass: ${formatTimestamp(fullAt, 'not recorded')}`,
  };
}
