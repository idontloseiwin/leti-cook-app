"""Bounded user-work snapshots; business limits in Python, projection in PostgreSQL.

All sections share one read-only REPEATABLE READ snapshot. Native jsonb encoding
keeps timestamp, UUID and byte-size semantics. No rows are transferred before
capacity/size checks; import retains its independent, locked empty-state check.
"""
from __future__ import annotations

import json

from ..base.postgres import DatabaseServerError, Operation
from ..base.sql_values import application_value
from .user_work_reconnect import reconnect_status

_MAX_BYTES = 16 * 1024 * 1024
# The three export statements use only native projections/aggregates; the
# reviewed closure never changes timeout settings. The narrow channel join is
# an experimental planning reduction pending native parity/performance proof:
# export needs visibility, not the control view's job-derived collection mode.
_EXPORT_OPERATION = Operation("user_work_export", read_only=True,
    repeatable_read=True, total_timeout_ms=60_000, cache_statement_timeout=True)
_STATUS_OPERATION = Operation("user_work_restore_status", read_only=True,
    repeatable_read=True, total_timeout_ms=60_000)

_EXPORT_CAPACITY = """
SELECT
 (SELECT count(*) FROM (SELECT 1 FROM readable.channel_user_work LIMIT 10001) x) AS channels,
 (SELECT count(*) FROM (SELECT 1 FROM public.user_channel_restore_items LIMIT 10001) x) AS pending,
 (SELECT count(*) FROM (SELECT 1 FROM public.saved_objects WHERE deleted_at IS NULL LIMIT 1001) x) AS objects,
 coalesce((SELECT sum(jsonb_array_length(saved_views)) FROM public.user_channel_restore_items),0) AS pending_views,
 (SELECT count(*) FROM (SELECT 1 FROM public.folders LIMIT 1001) x) AS folders,
 (SELECT count(*) FROM (SELECT 1 FROM public.channel_labels LIMIT 1002) x) AS labels,
 (SELECT count(*) FROM (SELECT 1 FROM public.channel_label_assignments LIMIT 100001) x) AS assignments,
 coalesce((SELECT sum(cardinality(label_keys)) FROM public.user_channel_restore_items),0) AS pending_labels,
 (SELECT count(*) FROM (SELECT 1 FROM public.saved_collection_members LIMIT 100001) x) AS members,
 (SELECT count(*) FROM (SELECT 1 FROM readable.video_type_user_work c WHERE
  c.decision_type IS NOT NULL OR EXISTS(SELECT 1 FROM public.video_type_review_history h WHERE h.case_id=c.case_id)
  LIMIT 1001) x) AS reviews,
 (SELECT count(*) FROM (SELECT 1 FROM public.video_type_review_history LIMIT 10001) x) AS history
"""
_STATUS_CAPACITY = """
SELECT
 (SELECT count(*) FROM (SELECT 1 FROM readable.channel_user_work LIMIT 10001) x) AS channels,
 (SELECT count(*) FROM (SELECT 1 FROM public.user_channel_restore_items LIMIT 10001) x) AS pending,
 (SELECT count(*) FROM (SELECT 1 FROM public.folders LIMIT 1001) x) AS folders
"""

_EXPORT_ESTIMATE = """
SELECT coalesce(sum(n),0) AS total_bytes FROM (
SELECT coalesce(sum(octet_length(value::text)+2),0) n FROM (SELECT jsonb_build_object('id',f.id,'name',f.name,'created_at',f.created_at) AS value FROM public.folders f) projected
 UNION ALL
 SELECT coalesce(sum(octet_length(value::text)+2),0) n FROM (SELECT jsonb_build_object('label_key',l.label_key,'name',l.name,'label_type',l.label_type,'created_at',l.created_at) AS value FROM public.channel_labels l) projected
 UNION ALL
 SELECT coalesce(sum(octet_length(value::text)+2),0) n FROM (SELECT jsonb_build_object('channel_id',c.channel_id,'user_work_id',c.user_work_id,'user_input',c.user_input,'folder_id',c.folder_id,'parent_channel_id',c.parent_channel_id,'is_hidden',coalesce(control.is_hidden,true),'hidden_at',control.hidden_at,'added_at',c.added_at) AS value FROM readable.channel_user_work c LEFT JOIN readable.channel_control control ON control.channel_id=c.channel_id) projected
 UNION ALL
 SELECT coalesce(sum(octet_length(value::text)+2),0) n FROM (SELECT jsonb_build_object('channel_id',a.channel_id,'label_key',a.label_key) AS value FROM public.channel_label_assignments a) projected
 UNION ALL
 SELECT coalesce(sum(octet_length(value::text)+2),0) n FROM (SELECT jsonb_build_object('kind',o.kind,'id',o.id,'name',o.name,'channel_id',o.channel_id,'conditions',o.conditions,'revision',o.revision,'created_at',o.created_at,'updated_at',o.updated_at) AS value FROM public.saved_objects o WHERE o.deleted_at IS NULL) projected
 UNION ALL
 SELECT coalesce(sum(octet_length(value::text)+2),0) n FROM (SELECT jsonb_build_object('collection_id',m.collection_id,'video_id',m.video_id,'added_at',m.added_at,'channel_ref',coalesce(m.user_channel_ref,m.restore_owner_ref)) AS value FROM public.saved_collection_members m) projected
 UNION ALL
 SELECT coalesce(sum(octet_length(value::text)+2),0) n FROM (SELECT jsonb_build_object('id',p.id,'user_input',p.user_input,'folder_id',p.folder_id,'parent_ref',p.parent_ref,'is_hidden',p.is_hidden,'hidden_at',p.hidden_at,'added_at',p.added_at,'label_keys',p.label_keys,'saved_views',p.saved_views,'revision',p.revision) AS value FROM public.user_channel_restore_items p) projected
 UNION ALL
 SELECT coalesce(sum(octet_length((jsonb_build_object('case_id',c.case_id,'revision',c.revision,'state',c.state,'decision_type',c.decision_type,'decision_reason',c.decision_reason,'decision_at',c.decision_at,'decision_revision',c.decision_revision,'channel_ref',coalesce(c.user_channel_ref,c.restore_owner_ref)))::text)+32),0) FROM readable.video_type_user_work c WHERE (c.decision_type IS NOT NULL OR EXISTS(SELECT 1 FROM public.video_type_review_history h WHERE h.case_id=c.case_id))
 UNION ALL
 SELECT coalesce(sum(octet_length((jsonb_build_object('revision',h.revision,'video_type',h.video_type,'reason',h.reason,'decided_at',h.decided_at,'request_id',h.request_id,'expected_revision',h.expected_revision))::text)+2),0) FROM public.video_type_review_history h

) sizes
"""

_EXPORT_DOCUMENT = """
WITH document AS MATERIALIZED (SELECT jsonb_build_object(
'folders',(WITH projected AS MATERIALIZED (SELECT jsonb_build_object('id',f.id,'name',f.name,'created_at',f.created_at) AS value FROM public.folders f) SELECT coalesce(jsonb_agg(value ORDER BY value->>'id'),'[]'::jsonb) FROM projected),
 'labels',(WITH projected AS MATERIALIZED (SELECT jsonb_build_object('label_key',l.label_key,'name',l.name,'label_type',l.label_type,'created_at',l.created_at) AS value FROM public.channel_labels l) SELECT coalesce(jsonb_agg(value ORDER BY value->>'label_key'),'[]'::jsonb) FROM projected),
 'channels',(WITH projected AS MATERIALIZED (SELECT jsonb_build_object('channel_id',c.channel_id,'user_work_id',c.user_work_id,'user_input',c.user_input,'folder_id',c.folder_id,'parent_channel_id',c.parent_channel_id,'is_hidden',coalesce(control.is_hidden,true),'hidden_at',control.hidden_at,'added_at',c.added_at) AS value FROM readable.channel_user_work c LEFT JOIN readable.channel_control control ON control.channel_id=c.channel_id) SELECT coalesce(jsonb_agg(value ORDER BY value->>'user_work_id'),'[]'::jsonb) FROM projected),
 'assignments',(WITH projected AS MATERIALIZED (SELECT jsonb_build_object('channel_id',a.channel_id,'label_key',a.label_key) AS value FROM public.channel_label_assignments a) SELECT coalesce(jsonb_agg(value ORDER BY value->>'channel_id'),'[]'::jsonb) FROM projected),
 'saved_objects',(WITH projected AS MATERIALIZED (SELECT jsonb_build_object('kind',o.kind,'id',o.id,'name',o.name,'channel_id',o.channel_id,'conditions',o.conditions,'revision',o.revision,'created_at',o.created_at,'updated_at',o.updated_at) AS value FROM public.saved_objects o WHERE o.deleted_at IS NULL) SELECT coalesce(jsonb_agg(value ORDER BY value->>'id'),'[]'::jsonb) FROM projected),
 'members',(WITH projected AS MATERIALIZED (SELECT jsonb_build_object('collection_id',m.collection_id,'video_id',m.video_id,'added_at',m.added_at,'channel_ref',coalesce(m.user_channel_ref,m.restore_owner_ref)) AS value FROM public.saved_collection_members m) SELECT coalesce(jsonb_agg(value ORDER BY value->>'video_id'),'[]'::jsonb) FROM projected),
 'pending',(WITH projected AS MATERIALIZED (SELECT jsonb_build_object('id',p.id,'user_input',p.user_input,'folder_id',p.folder_id,'parent_ref',p.parent_ref,'is_hidden',p.is_hidden,'hidden_at',p.hidden_at,'added_at',p.added_at,'label_keys',p.label_keys,'saved_views',p.saved_views,'revision',p.revision) AS value FROM public.user_channel_restore_items p) SELECT coalesce(jsonb_agg(value ORDER BY value->>'id'),'[]'::jsonb) FROM projected),
 'reviews',(WITH projected AS MATERIALIZED (SELECT (jsonb_build_object('case_id',c.case_id,'revision',c.revision,'state',c.state,'decision_type',c.decision_type,'decision_reason',c.decision_reason,'decision_at',c.decision_at,'decision_revision',c.decision_revision,'channel_ref',coalesce(c.user_channel_ref,c.restore_owner_ref))) || jsonb_build_object('history',(SELECT coalesce(jsonb_agg(jsonb_build_object('revision',h.revision,'video_type',h.video_type,'reason',h.reason,'decided_at',h.decided_at,'request_id',h.request_id,'expected_revision',h.expected_revision) ORDER BY h.revision),'[]'::jsonb) FROM public.video_type_review_history h WHERE h.case_id=c.case_id)) AS value FROM readable.video_type_user_work c WHERE (c.decision_type IS NOT NULL OR EXISTS(SELECT 1 FROM public.video_type_review_history h WHERE h.case_id=c.case_id))) SELECT coalesce(jsonb_agg(value ORDER BY value->>'case_id'),'[]'::jsonb) FROM projected)

) AS value),
encoded as materialized (select value::text as payload from document),
sized as materialized (select payload,octet_length(payload) as total_bytes from encoded)
select total_bytes,
 case when total_bytes <= %(max_bytes)s::bigint then payload else null end as encoded_value
from sized
"""

_STATUS_DOCUMENT = """
SELECT jsonb_build_object('can_restore',
  NOT EXISTS(SELECT 1 FROM readable.channel_user_work) AND NOT EXISTS(SELECT 1 FROM public.folders)
  AND NOT EXISTS(SELECT 1 FROM public.channel_labels WHERE label_key<>'monetized' OR name<>'Monetized' OR label_type<>'preset')
  AND NOT EXISTS(SELECT 1 FROM public.channel_label_assignments) AND NOT EXISTS(SELECT 1 FROM public.saved_objects WHERE deleted_at IS NULL)
  AND NOT EXISTS(SELECT 1 FROM public.saved_collection_members)
  AND NOT EXISTS(SELECT 1 FROM public.saved_collection_commands c WHERE NOT EXISTS(SELECT 1 FROM public.saved_objects o
    WHERE o.kind=c.collection_kind AND o.id=c.collection_id AND o.deleted_at IS NOT NULL))
  AND NOT EXISTS(SELECT 1 FROM readable.video_type_user_work) AND NOT EXISTS(SELECT 1 FROM public.video_type_review_history)
  AND NOT EXISTS(SELECT 1 FROM public.user_channel_restore_items),
  'pending',(SELECT coalesce(jsonb_agg(value ORDER BY blocked,id),'[]'::jsonb) FROM (
   SELECT p.id,(p.parent_ref IS NOT NULL AND NOT EXISTS(SELECT 1 FROM readable.channel_user_work parent WHERE parent.user_work_id=p.parent_ref)) blocked,
    jsonb_build_object('id',p.id,'user_input',p.user_input,'parent_ref',p.parent_ref,'is_hidden',p.is_hidden,'revision',p.revision,'reconnect_state',p.reconnect_state,
      'reconnect_not_before',p.reconnect_not_before,'reconnect_error_code',p.reconnect_error_code) value
   FROM public.user_channel_restore_items p ORDER BY blocked,p.id OFFSET %(offset)s::integer LIMIT 100) page),
  'pending_more',(SELECT count(*)>100 FROM(SELECT 1 FROM public.user_channel_restore_items OFFSET %(offset)s::integer LIMIT 101)p),
  'channel_mappings',(SELECT coalesce(jsonb_agg(jsonb_build_object('id',user_work_id,'channel_id',channel_id) ORDER BY user_work_id),'[]'::jsonb) FROM readable.channel_user_work),
  'user_channel_refs',(SELECT coalesce(jsonb_agg(id ORDER BY id),'[]'::jsonb) FROM(SELECT user_work_id id FROM readable.channel_user_work UNION ALL SELECT id FROM public.user_channel_restore_items) refs),
  'folder_ids',(SELECT coalesce(jsonb_agg(id ORDER BY id),'[]'::jsonb) FROM public.folders)

) AS value
"""


def _limit():
    raise DatabaseServerError("user_work_export_limit", code="P0001")


def _one(tx, statement, parameters=()):
    row = tx.fetch_one(statement, parameters)
    if not isinstance(row, dict):
        raise RuntimeError("user_work_response_invalid")
    return row


def _capacity(row, *, export):
    if row["channels"] + row["pending"] > 10000 or row["folders"] > 1000:
        _limit()
    if export and (
        row["objects"] + row["pending_views"] > 1000
        or row["labels"] > 1001
        or row["assignments"] + row["pending_labels"] > 100000
        or row["members"] > 100000
        or row["reviews"] > 1000
        or row["history"] > 10000
    ):
        _limit()


def _document(row):
    value = application_value(row["value"])
    if not isinstance(value, dict):
        raise RuntimeError("user_work_response_invalid")
    return value


def _export_document(row):
    # Native JSONB text supplies the byte bound and the wire payload once.
    encoded = row["encoded_value"]
    if not isinstance(encoded, str):
        raise RuntimeError("user_work_response_invalid")
    try:
        value = json.loads(encoded)
    except ValueError:
        raise RuntimeError("user_work_response_invalid") from None
    return _document({"value": value})


def export_snapshot(database):
    with database.transaction(_EXPORT_OPERATION) as tx:
        _capacity(_one(tx, _EXPORT_CAPACITY), export=True)
        if _one(tx, _EXPORT_ESTIMATE)["total_bytes"] + 1024 > _MAX_BYTES:
            _limit()
        row = _one(tx, _EXPORT_DOCUMENT, {"max_bytes": _MAX_BYTES})
        if row["total_bytes"] > _MAX_BYTES:
            _limit()
        result = _export_document(row)
    return result


def restore_status(database, offset=0):
    with database.transaction(_STATUS_OPERATION) as tx:
        # Preserve PostgreSQL argument casting before domain validation.
        offset = _one(tx, "SELECT %s::integer AS offset", (offset,))["offset"]
        if offset is None or not 0 <= offset <= 10000:
            raise DatabaseServerError("user_work_offset_invalid", code="P0001")
        _capacity(_one(tx, _STATUS_CAPACITY), export=False)
        result = _document(_one(tx, _STATUS_DOCUMENT, {"offset": offset}))
        result["reconnect"] = reconnect_status(tx)
    return result
