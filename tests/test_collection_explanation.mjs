import assert from 'node:assert/strict';
import { parseCollectionExplanation, collectionReasonText } from '../application/frontend/src/collectionExplanation.ts';

const base = {
  scope: 'recorded_run', current_recovery: 'not_evaluated', next_attempt: 'stored_queue',
  reasons: [{code: 'metadata_followup', count: 7, stage: 'video_collection'}],
};
const parsed = parseCollectionExplanation(base);
assert.deepEqual(parsed, base);
assert.notEqual(parsed, base);
assert.notEqual(parsed.reasons, base.reasons);
assert.match(collectionReasonText(parsed.reasons[0]), /7 videos required metadata and availability checks/);
assert.doesNotMatch(collectionReasonText(parsed.reasons[0]), /metadata_followup|recovered|queued|deleted/);

for (const code of [
  'metadata_followup', 'counters_pending', 'metadata_deferred_unspecified',
  'source_counter_invalid', 'source_duplicate_items', 'selected_not_claimed',
  'settlement_unknown', 'infrastructure_error', 'quota_wait',
  'provider_rate_limited', 'batch_unavailable', 'scheduled_handoff',
  'recorded_errors', 'cause_not_recorded',
]) {
  for (const count of [null, 0, 1, 20_000]) {
    const value = parseCollectionExplanation({...base, reasons: [{code, count, stage: 'run'}]});
    assert.ok(value, code);
    const text = collectionReasonText(value.reasons[0]);
    assert.ok(text.startsWith('Run: '));
    assert.ok(!text.includes('undefined'));
    assert.ok(!text.includes(code));
  }
}
for (const stage of [
  'run', 'channel_metadata', 'typed_inventory', 'channel_discovery', 'video_collection',
  'regular_video_stats', 'short_video_collection', 'completed_stream_collection',
  'new_video_stats', 'long_availability_loss',
]) {
  const value = parseCollectionExplanation({...base, reasons: [{code: 'cause_not_recorded', count: null, stage}]});
  assert.ok(value);
  assert.ok(!collectionReasonText(value.reasons[0]).includes('_'));
}
for (const bad of [
  null, [], 'raw provider payload', {},
  {...base, scope: 'current_queue'}, {...base, current_recovery: 'recovered'},
  {...base, next_attempt: 'tomorrow'}, {...base, reasons: {}},
  {...base, reasons: Array(129).fill(base.reasons[0])},
  {...base, reasons: [{...base.reasons[0], code: 'secret'}]},
  {...base, reasons: [{...base.reasons[0], stage: 'secret'}]},
  {...base, reasons: [{...base.reasons[0], payload: 'secret'}]},
  {...base, reasons: [{code: 'metadata_followup', count: 1}]},
  {...base, reasons: [{code: 'metadata_followup', stage: 'run'}]},
  ...[-1, 1.5, '7', true, Infinity, NaN, Number.MAX_SAFE_INTEGER + 1].map(count =>
    ({...base, reasons: [{...base.reasons[0], count}]})),
]) assert.equal(parseCollectionExplanation(bad), null);

assert.deepEqual(parseCollectionExplanation({...base, status: 'partial', stages: {video_collection: {unresolved: 7}}}), base);
assert.match(collectionReasonText({code: 'settlement_unknown', count: null, stage: 'video_collection'}), /uncertain/);
assert.match(collectionReasonText({code: 'counters_pending', count: 1, stage: 'video_collection'}), /profile[s]? saved without a usable view count/);
console.log('Collection explanation parser and labels passed');
