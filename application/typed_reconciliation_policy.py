"""Pure typed reconciliation schedule instructions from native locked facts.

No database, clocks or datetime parsing. Native projection retains calendar and
extended/BC/infinite timestamp semantics; only finite branch/jitter policy lives
here. The fixed SQL reader uses the same pause and ranking vocabulary.
"""
from collection_policy import PolicyError, typed_jittered_seconds

LEGACY_CHANNEL_PAUSES = ('channel_hidden','channel_no_long_playlist','channel_inventory_not_ready',
    'channel_inventory_manual_required','channel_inventory_error')


def reconcile_schedule(previous, facts):
    video_id = previous['video_id']
    now = facts['transaction_at']
    next_due = previous['next_due_at']
    result = dict(base_at=now, seconds=0, cap_at=None, old_cap_at=None)
    if previous['paused_reason'] not in LEGACY_CHANNEL_PAUSES:
        result['base_at'] = now if previous['video_type'] == 'long' else (next_due or now)
        return result
    if previous['consecutive_failures'] > 0 and next_due is not None:
        result['base_at'] = next_due
        return result
    result['old_cap_at'] = next_due or now
    if previous['typed_collection_checked_at'] is not None:
        if facts['typed_input_invalid'] is True:
            raise PolicyError('typed_video_collection_due_input_invalid')
        if facts['typed_clock_invalid'] is True:
            result['base_at'] = facts['fallback_at']
        else:
            cadence = facts['cadence']
            if cadence is None:
                raise PolicyError('typed_video_collection_cadence_missing')
            result['base_at'] = previous['typed_collection_checked_at']
            result['seconds'] = typed_jittered_seconds(video_id,cadence['cadence_seconds'],cadence['jitter_window_seconds'])
    if previous['typed_collection_status'] == 'hydrated':
        if facts['hot_as_of_invalid'] is True:
            raise PolicyError('hot_video_refresh_as_of_invalid')
        result['cap_at'] = now if facts['hot_availability_invalid'] is True else facts['hot_cap_at']
    return result
