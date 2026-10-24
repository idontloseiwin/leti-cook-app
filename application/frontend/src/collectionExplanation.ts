/** Recorded run observations, never proof of current recovery or queue state. */
const stages = {
  run: 'Run',
  channel_metadata: 'Channel profiles',
  typed_inventory: 'Video discovery',
  channel_discovery: 'Channel discovery',
  video_collection: 'Video collection',
  regular_video_stats: 'Long-form collection',
  short_video_collection: 'Shorts collection',
  completed_stream_collection: 'Streams collection',
  new_video_stats: 'New videos',
  long_availability_loss: 'Availability checks',
} as const;

const codes = [
  'metadata_followup', 'counters_pending', 'metadata_deferred_unspecified',
  'source_counter_invalid', 'source_duplicate_items', 'selected_not_claimed',
  'settlement_unknown', 'infrastructure_error', 'quota_wait',
  'provider_rate_limited', 'batch_unavailable', 'scheduled_handoff',
  'recorded_errors', 'cause_not_recorded',
] as const;
type ReasonCode = typeof codes[number];
type ReasonStage = keyof typeof stages;
export type CollectionReason = { code: ReasonCode; count: number | null; stage: ReasonStage };
export type CollectionExplanation = {
  scope: 'recorded_run'; current_recovery: 'not_evaluated'; next_attempt: 'stored_queue';
  reasons: CollectionReason[];
};

const record = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === 'object' && !Array.isArray(value);

export function parseCollectionExplanation(value: unknown): CollectionExplanation | null {
  if (!record(value) || Object.keys(value).length > 8
      || value.scope !== 'recorded_run' || value.current_recovery !== 'not_evaluated'
      || value.next_attempt !== 'stored_queue' || !Array.isArray(value.reasons)
      || value.reasons.length > 128) return null;
  const reasons: CollectionReason[] = [];
  for (const reason of value.reasons) {
    if (!record(reason) || Object.keys(reason).some(key => !['code', 'count', 'stage'].includes(key))
        || typeof reason.code !== 'string' || !codes.includes(reason.code as ReasonCode)
        || typeof reason.stage !== 'string' || !Object.hasOwn(stages, reason.stage)
        || !(reason.count === null || (typeof reason.count === 'number'
          && Number.isSafeInteger(reason.count) && reason.count >= 0))) return null;
    reasons.push({ code: reason.code as ReasonCode, count: reason.count, stage: reason.stage as ReasonStage });
  }
  return { scope: 'recorded_run', current_recovery: 'not_evaluated', next_attempt: 'stored_queue', reasons };
}

export function collectionReasonText(reason: CollectionReason): string {
  const count = reason.count === null ? null : reason.count.toLocaleString('en');
  const videos = count === null ? 'Videos' : `${count} video${reason.count === 1 ? '' : 's'}`;
  const labels: Record<ReasonCode, string> = {
    metadata_followup: `${videos} required metadata and availability checks after a statistics response.`,
    counters_pending: `${videos} had profiles saved without a usable view count.`,
    metadata_deferred_unspecified: `${videos} needed further checks; the precise cause was not recorded.`,
    source_counter_invalid: `Invalid view-count observations were recorded${count === null ? '' : `: ${count}`}.`,
    source_duplicate_items: `Duplicate video entries were returned${count === null ? '' : `: ${count}`}.`,
    selected_not_claimed: `${videos} selected for this run were not started.`,
    settlement_unknown: 'A database write acknowledgement was uncertain; its result needs reconciliation.',
    infrastructure_error: 'An infrastructure error interrupted work.',
    quota_wait: 'YouTube quota stopped work in this run.',
    provider_rate_limited: 'YouTube rate-limited work in this run.',
    batch_unavailable: 'The statistics request could not complete.',
    scheduled_handoff: 'Work yielded at a scheduled handoff.',
    recorded_errors: `Errors were recorded${count === null ? '' : `: ${count}`}. This total does not identify their cause.`,
    cause_not_recorded: 'A more specific cause was not recorded in this summary.',
  };
  return `${stages[reason.stage]}: ${labels[reason.code]}`;
}
