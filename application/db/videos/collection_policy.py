"""Bound fixed process rules at native fact positions; never read config files.

The retained cadence table is checked only for an explicit legacy-authority
conflict. It is not a runtime fallback or a projection of the schedule file.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime
from psycopg.types.json import Jsonb

from collection_policy import CadenceRule, CadenceSnapshot, PolicyError, RecheckSchedules, clock
from collection_schedule import CollectionSchedules
from collection_schedule_model import InventorySchedule, InventorySchedules, UnavailableSchedule, UnavailableSchedules
from ..base.postgres import DatabaseError

MAX_CADENCE_RULES = 30
_FIELDS = tuple(CadenceRule.__dataclass_fields__)
# Fixed types retain integer native age/ranking operations. Only the 30 reviewed
# records can enter this relation; source clocks still belong to its statement.
_SQL = """
    SELECT video_type, collection_kind, availability_class, min_age_seconds,
           max_age_seconds, cadence_seconds, policy_version, enabled, age_bucket,
           jitter_window_seconds, statement_timestamp() AS captured_at,
           true AS clocks_present
    FROM jsonb_to_recordset(%s::jsonb) AS policy(
        video_type text, collection_kind text, availability_class text,
        min_age_seconds bigint, max_age_seconds bigint, cadence_seconds integer,
        policy_version integer, enabled boolean, age_bucket text,
        jitter_window_seconds integer)
    ORDER BY policy_version DESC, min_age_seconds DESC,
             video_type, collection_kind, availability_class
"""
_NAMED_SQL = _SQL.replace('%s', '%(cadence)s')
# Exact historical schema25 seed values; deliberately independent of a custom
# process snapshot. This is migration compatibility evidence, not live policy.
LEGACY_COMPATIBILITY_SQL = """
WITH expected(video_type,collection_kind,availability_class,min_age_seconds,max_age_seconds,cadence_seconds,policy_version,enabled,age_bucket,jitter_window_seconds) AS (VALUES
('long','counters','public',0,86400,3600,1,true,NULL,0),
('long','counters','public',86400,604800,7200,1,true,NULL,0),
('long','counters','public',604800,2419200,21600,1,true,NULL,0),
('long','counters','public',2419200,7776000,43200,1,true,NULL,0),
('long','counters','public',7776000,31536000,86400,1,true,NULL,0),
('long','counters','public',31536000,NULL,604800,1,true,NULL,0),
('long','counters','unlisted',0,NULL,2419200,1,true,NULL,0),
('long','metadata','public',0,604800,21600,1,true,NULL,0),
('long','metadata','public',604800,2419200,86400,1,true,NULL,0),
('long','metadata','public',2419200,31536000,604800,1,true,NULL,0),
('long','metadata','public',31536000,NULL,2419200,1,true,NULL,0),
('long','metadata','unlisted',0,NULL,2419200,1,true,NULL,0),
('short','metadata','all',0,7776000,604800,1,true,'age_0_90d',21600),
('short','metadata','all',7776000,NULL,2419200,1,true,'age_91d_plus',21600),
('stream','metadata','all',0,7776000,604800,1,true,'age_0_90d',21600),
('stream','metadata','all',7776000,NULL,2419200,1,true,'age_91d_plus',21600))
SELECT ((SELECT count(*) FROM public.video_collection_cadence)=16
 AND NOT EXISTS(SELECT 1 FROM public.video_collection_cadence WHERE created_at IS NULL OR updated_at IS NULL)
 AND NOT EXISTS((SELECT video_type,collection_kind,availability_class,min_age_seconds,max_age_seconds,cadence_seconds,policy_version,enabled,age_bucket,jitter_window_seconds FROM public.video_collection_cadence EXCEPT SELECT * FROM expected)
 UNION ALL (SELECT * FROM expected EXCEPT SELECT video_type,collection_kind,availability_class,min_age_seconds,max_age_seconds,cadence_seconds,policy_version,enabled,age_bucket,jitter_window_seconds FROM public.video_collection_cadence))) AS legacy_cadence_defaults
"""


def require_cadence_snapshot(owner) -> CadenceSnapshot:
    """Require an injected process snapshot, never substitute defaults."""
    snapshot = getattr(owner, 'cadence_snapshot', None)
    if type(snapshot) is not CadenceSnapshot:
        raise DatabaseError('collection_schedule_snapshot_required')
    rules = getattr(snapshot, 'rules', None)
    if type(rules) is not tuple or len(rules) != MAX_CADENCE_RULES:
        raise DatabaseError('collection_schedule_snapshot_required')
    if any(type(rule) is not CadenceRule for rule in rules):
        raise DatabaseError('collection_schedule_snapshot_invalid')
    try:
        rechecks = snapshot.rechecks
        if type(rechecks) is not RecheckSchedules:
            raise ValueError("invalid recheck snapshot")
        RecheckSchedules(rechecks.unavailable_long_max_seconds, rechecks.identity_seconds)
        unavailable = snapshot.unavailable_by_type
        if type(unavailable) is not UnavailableSchedules:
            raise ValueError("invalid unavailable snapshot")
        for kind in ('long', 'short', 'stream'):
            settings = getattr(unavailable, kind)
            if type(settings) is not UnavailableSchedule:
                raise ValueError("invalid unavailable type schedule")
            UnavailableSchedule(**asdict(settings))
        inventory = snapshot.inventory_by_type
        if type(inventory) is not InventorySchedules:
            raise ValueError("invalid inventory snapshot")
        for kind in ('long', 'short', 'stream'):
            settings = getattr(inventory, kind)
            if type(settings) is not InventorySchedule:
                raise ValueError("invalid inventory type schedule")
            InventorySchedule(**asdict(settings))
        # Validate even an incorrectly assembled internal snapshot through the
        # same finite pure contract; no transport or fallback is involved.
        for rule in rules:
            CadenceRule(**{field: getattr(rule, field) for field in _FIELDS})
    except (AttributeError, TypeError, ValueError, PolicyError):
        raise DatabaseError('collection_schedule_snapshot_invalid') from None
    if any((rule.video_type == 'long' or rule.collection_kind == 'counters')
           and rule.jitter_window_seconds != 0 for rule in rules):
        raise DatabaseError('collection_schedule_snapshot_invalid')
    expected = CollectionSchedules().cadence_snapshot().rules
    shape = lambda rule: tuple(getattr(rule, field) for field in _FIELDS
                              if field not in ('cadence_seconds', 'jitter_window_seconds'))
    if (snapshot.captured_at is not None
            or {shape(rule) for rule in snapshot.rules} != {shape(rule) for rule in expected}):
        raise DatabaseError('collection_schedule_snapshot_invalid')
    # CadenceRule validates each interval. The process snapshot additionally has a single
    # independent jitter per typed format, bounded by both of its intervals.
    for kind in ('short', 'stream'):
        rules = [rule for rule in snapshot.rules
                 if rule.video_type == kind and rule.collection_kind == 'metadata']
        if (len({rule.jitter_window_seconds for rule in rules}) != 1
                or rules[0].jitter_window_seconds > min(rule.cadence_seconds for rule in rules)):
            raise DatabaseError('collection_schedule_snapshot_invalid')
    return snapshot


def cadence_parameter(owner):
    """One bounded native JSON parameter, no SQL interpolation or I/O."""
    return Jsonb([asdict(rule) for rule in require_cadence_snapshot(owner).rules])


def cadence_policy_version(owner):
    # Revision1 describes the unchanged scheduling algorithm. File edits are
    # deliberately not a queue invalidation/rescheduling epoch.
    return max(rule.policy_version for rule in require_cadence_snapshot(owner).rules
               if rule.video_type == 'long' and rule.enabled)


def load_cadence_snapshot(database) -> CadenceSnapshot:
    return require_cadence_snapshot(database)


def load_cadence_snapshot_in_transaction(transaction) -> CadenceSnapshot:
    with transaction.borrow():
        return require_cadence_snapshot(transaction)


def validate_legacy_cadence_defaults(row):
    if (not isinstance(row, Mapping) or set(row) != {'legacy_cadence_defaults'}
            or type(row['legacy_cadence_defaults']) is not bool):
        raise DatabaseError('database_result_shape_invalid')
    if row['legacy_cadence_defaults'] is not True:
        raise DatabaseError('collection_schedule_legacy_cadence_conflict')
    return dict(row)


def assert_legacy_cadence_defaults(database):
    """Reject customized legacy DB authority; a custom file remains valid."""
    return validate_legacy_cadence_defaults(database.read_row(
        'collection_cadence_legacy_compatibility', LEGACY_COMPATIBILITY_SQL))


def cadence_report(owner):
    snapshot = require_cadence_snapshot(owner)
    metadata, jitter = {}, {}
    for kind in ('short', 'stream'):
        rules = {rule.age_bucket: rule for rule in snapshot.rules
                 if rule.video_type == kind and rule.collection_kind == 'metadata'}
        metadata[kind] = {'fresh': rules['age_0_90d'].cadence_seconds,
                          'old': rules['age_91d_plus'].cadence_seconds}
        jitter[kind] = rules['age_0_90d'].jitter_window_seconds
    return {'schedule_source': 'process_snapshot', 'metadata_cadence_seconds': metadata,
            'typed_jitter_seconds': jitter}


def _decode_cadence_rows(rows) -> CadenceSnapshot:
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_CADENCE_RULES:
        raise PolicyError("collection_cadence_invalid")
    rules, captured = [], None
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != set(_FIELDS) | {"captured_at", "clocks_present"}:
            raise PolicyError("collection_cadence_invalid")
        stamp = row["captured_at"]
        if isinstance(stamp, str):
            try:
                stamp = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            except ValueError:
                raise PolicyError("collection_cadence_invalid") from None
        stamp = clock(stamp)
        if not isinstance(stamp, datetime) or (captured is not None and stamp != captured):
            raise PolicyError("collection_cadence_invalid")
        if row["video_type"] in ("short", "stream") and row["clocks_present"] is not True:
            raise PolicyError("collection_cadence_invalid")
        captured = stamp
        rules.append(CadenceRule(**{field: row[field] for field in _FIELDS}))
    return CadenceSnapshot(tuple(rules), captured)
