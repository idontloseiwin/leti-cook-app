"""Bounded exact cleanup of observed blockers through their existing writers.

The ordinary scan cursor is never used or changed. This entry owns one finite
maintenance transaction; per-candidate locks, savepoints and effects stay with
the existing cleanup owners. No generic SQL or new lease is accepted.
"""
from ..base.postgres import DatabaseServerError
from ..hot.hot_retention import CleanupPage, RetentionError, priority_references
from .current_source_retention import cleanup_hot_current_source
from .inventory_retention import cleanup_hot_inventory
from .evidence_retention import cleanup_hot_evidence
from .inventory_copies_retention import cleanup_hot_inventory_copies
from .operational_retention import cleanup_hot_operational_copies
from .deleted_channel_retention import cleanup_hot_deleted_channels
from .retention_queries import cleanup_deleted_saved_objects
from .category_reference_retention import cleanup_hot_category_reference
from ..readiness.deleted_channel_retention_capabilities import READINESS_SQL, validate_readiness

ERRORS = frozenset({'hot_priority_references_invalid'})
# Parents run after known child writers. Remaining child chunks/leases are still
# causes; they cannot be acknowledged merely because an external lock released.
_ORDER = ('video_counters', 'video_metadata', 'video_availability',
          'channel_profile', 'channel_country', 'operator_jobs', 'operator_job_targets',
          'playlist_state', 'inventory_receipts', 'inventory_scans',
          'video_snapshots', 'channel_snapshots', 'typed_video_collection_receipts',
          'availability_events', 'availability_incidents', 'review_evidence',
          'memberships', 'inventory', 'sync_runs', 'quota_attempts',
          'deleted_saved_objects', 'category_reference', 'deleted_channels')


def _owner(target):
    if target in ('memberships','inventory'):return cleanup_hot_inventory
    if target in ('video_counters','video_metadata','video_availability','channel_profile','channel_country'):return cleanup_hot_current_source
    if target in ('playlist_state','inventory_scans','inventory_receipts'):return cleanup_hot_inventory_copies
    if target in ('operator_jobs','operator_job_targets','sync_runs','quota_attempts'):return cleanup_hot_operational_copies
    if target=='deleted_channels':return cleanup_hot_deleted_channels
    if target=='deleted_saved_objects':return cleanup_deleted_saved_objects
    if target=='category_reference':return cleanup_hot_category_reference
    return cleanup_hot_evidence


def cleanup_hot_priority(transaction, *, references):
    try:
        requested = priority_references(references)
        if not requested:
            raise RetentionError('invalid_cleanup_result')
    except RetentionError:
        raise DatabaseServerError('hot_priority_references_invalid', code='P0001') from None
    tx = transaction
    with tx.borrow():
        stamp = tx.fetch_one("SELECT to_jsonb(clock_timestamp())#>>'{}' AS at")
        remaining = []
        deleted = scrubbed = 0
        for target in _ORDER:
            keys = [ref.key for ref in requested if ref.target == target]
            if not keys:
                continue
            # Preserve the catalog proof performed by the ordinary public entry.
            if target == 'deleted_channels':
                validate_readiness(tx.fetch_one(READINESS_SQL))
            owner = _owner(target)
            value = owner(tx, target=target, limit=len(keys), references=keys)
            page = CleanupPage.parse(target, value, len(keys))
            if (not page.priority_complete or not page.complete
                    or not set(page.priority_references) <= set(requested)):
                raise RetentionError('invalid_cleanup_result')
            remaining.extend(ref.wire() for ref in page.priority_references)
            deleted += page.deleted
            scrubbed += page.scrubbed
        return dict(as_of=stamp['at'], checked=[ref.wire() for ref in requested],
                    remaining=remaining, complete=True, deleted=deleted, scrubbed=scrubbed)
