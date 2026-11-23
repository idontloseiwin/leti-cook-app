"""Versioned operation semantics; definitions never grant execution rights.

Inputs are protected observations, not publication-time zeroes. The same method
and window apply to a whole query before filtering, sorting and pagination.
Whole-population operations are registered in operation_kinds, one module each;
the field-level operations below are computed as columns inside queries.
"""
from types import MappingProxyType

from . import growth
from .operation_kinds import OPERATION_KINDS
from .operation_kinds.kind import OperationDefinition

FIELD_LEVEL_OPERATIONS = (
    OperationDefinition('video.observed_change.v1','public.video.observations.v1',
        'video+observed_at+epoch','video','additional_analytics',
        ('observation.views_delta','observation.views_per_hour'),
        'Latest snapshot matching the current counter; last real point at or before its target boundary, within tolerance. Signed difference and average over actual elapsed seconds.'),
    OperationDefinition('video.observed_age.v1','public.video.observations.v1',
        'video+observed_at+epoch','video','source_observation',
        ('observation.views_at_age',),
        'Latest real point at or before publication (a Stream: its actual end) + target age, within tolerance. Actual age disclosed; no interpolation or publication-time zero.'),
    OperationDefinition(growth.OPERATION_ID,'public.video.observations.v1',
        'video+observed_at+epoch','video','additional_analytics', growth.FIELDS,
        'Library 28-day growth. Endpoint: snapshot at the current counter (counter, metadata and inventory '
        'proof observed within 672 h; Streams at or after their end). Start: latest snapshot at or before endpoint '
        '- 28 days, else the earliest after it. Ready: start at most 3 h late; partial: at least 24 h; started_today '
        'in the request time zone; else waiting_next_snapshot. Both points share one views definition epoch. '
        'Signed delta, whole-second interval, per-day mean half-even to 0.01.'),
)
# Columns calculated from the current stored row itself (no observations). Their
# policy gates the fields exactly as the Library's calculated filters were gated.
CALCULATED_FIELD_OPERATIONS = (
    OperationDefinition('video.static_metrics.v1','public.videos.current.v1','video','video','descriptive_analytics',
        ('video.title_length','video.tag_count','video.like_rate','video.comment_rate'),
        'Values calculated from the current stored row: title length in Unicode code points, number of stored tags, '
        'likes and comments per 100 views compared exactly; zero or unknown views give no rate.'),
)
OPERATIONS = MappingProxyType({x.id:x for x in (*(kind.definition for kind in OPERATION_KINDS.values()),
                                                *FIELD_LEVEL_OPERATIONS, *CALCULATED_FIELD_OPERATIONS)})
if len(OPERATIONS) != len(OPERATION_KINDS) + len(FIELD_LEVEL_OPERATIONS) + len(CALCULATED_FIELD_OPERATIONS):
    raise RuntimeError('duplicate_operation_id')
# Observation columns: their SQL joins snapshots and their cells carry evidence.
FIELD_OPERATIONS = MappingProxyType({field:op.id for op in FIELD_LEVEL_OPERATIONS for field in op.outputs})
# Every field that needs an allowed operation (grant_required otherwise).
FIELD_GRANTS = MappingProxyType({field:op.id for op in (*FIELD_LEVEL_OPERATIONS, *CALCULATED_FIELD_OPERATIONS)
                                 for field in op.outputs})
