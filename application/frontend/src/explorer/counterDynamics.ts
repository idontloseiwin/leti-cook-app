import type {
  ChannelExplorerCounterDynamicsMetric,
  ChannelExplorerCounterDynamicsPeriodKey,
  ChannelExplorerCounterDynamicsPeriodsPayload,
  ChannelExplorerCounterDynamicsStatus,
} from '../types';

/** Display preference only; both comparisons already belong to the same read. */
export function preferredCounterDynamicsPeriod(
  periods: ChannelExplorerCounterDynamicsPeriodsPayload['periods'] | undefined,
  metricName: 'subscribers' | 'views',
): ChannelExplorerCounterDynamicsPeriodKey {
  return periods?.['28d'].metrics[metricName].status === 'ready' ? '28d' : '7d';
}

export type CounterDynamicsPercentPresentation = {
  kind: 'positive' | 'negative' | 'zero' | 'unavailable';
  text: string;
  ariaLabel: string;
  unavailableReason: string | null;
};

const STATUS_REASON: Record<Exclude<ChannelExplorerCounterDynamicsStatus, 'ready' | 'partial'>, string> = {
  not_reported: 'The current counter is not reported.',
  not_enough_history: 'Not enough stored history for the selected comparison period.',
  boundary_too_distant: 'No stored observation is close enough to the selected period boundary.',
  stale_current: 'The current stored observation is stale.',
  zero_baseline: 'Percentage change is unavailable because the starting value is zero.',
  unavailable: 'The counter change is unavailable for the selected period.',
};

function unavailablePresentation(reason: string): CounterDynamicsPercentPresentation {
  return {
    kind: 'unavailable',
    text: '—',
    ariaLabel: `Percentage change unavailable. ${reason}`,
    unavailableReason: reason,
  };
}

function formatPercentMagnitude(value: number): string {
  const formatted = new Intl.NumberFormat('en-US', {
    maximumFractionDigits: 2,
    minimumFractionDigits: 0,
    useGrouping: false,
  }).format(Math.abs(value));
  if (formatted === '0' && value !== 0) return '<0.01';
  return formatted;
}

export function presentCounterDynamicsPercent(
  metric: ChannelExplorerCounterDynamicsMetric,
): CounterDynamicsPercentPresentation {
  if (metric.status !== 'ready' && metric.status !== 'partial') {
    const reason = metric.reason?.trim() || STATUS_REASON[metric.status];
    return unavailablePresentation(reason);
  }

  if (metric.percent_change === null) {
    return unavailablePresentation('The stored result has no percentage value.');
  }
  const percent = Number(metric.percent_change);
  if (!Number.isFinite(percent)) {
    return unavailablePresentation('The stored percentage value is invalid.');
  }

  if (percent === 0 || Object.is(percent, -0)) {
    return {
      kind: 'zero',
      text: '0%',
      ariaLabel: 'No percentage change.',
      unavailableReason: null,
    };
  }

  const magnitude = formatPercentMagnitude(percent);
  if (percent > 0) {
    return {
      kind: 'positive',
      text: `+${magnitude}%`,
      ariaLabel: magnitude === '<0.01'
        ? 'Up less than 0.01 percent.'
        : `Up ${magnitude} percent.`,
      unavailableReason: null,
    };
  }
  return {
    kind: 'negative',
    text: `-${magnitude}%`,
    ariaLabel: magnitude === '<0.01'
      ? 'Down less than 0.01 percent.'
      : `Down ${magnitude} percent.`,
    unavailableReason: null,
  };
}
