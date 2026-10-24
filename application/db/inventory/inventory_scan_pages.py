"""Page/finish protocol with no internal retries.

The receipt's advisory key precedes playlist/scan locks. Fresh pages take
sorted video advisory locks before canonical INSERT and membership
observations. Only full coverage marks older unseen membership absent. Python
queue/review effects run after their source statement; native integrity guards
remain authoritative.
"""
from __future__ import annotations

from ..videos.typed_video_read_policy import (TYPED_CANDIDATE_INPUT_VIDEO_ID_SQL)
from ..channel.channel_read_policy import (CHANNEL_CONTROL_FACTS_SQL, channel_eligible_row)
import re
from psycopg.types.json import Jsonb

from collection_policy import inventory_source_policy, PolicyError
from ..videos.collection_policy import require_cadence_snapshot
from ..channel.channel_control import assert_channel_scope
from .inventory_scan_steps import _fail, _hot, _operation, _owned, _replay, _value, _TYPES
from ..videos.video_queue_initialization import ensure_video_collection_state_in_transaction
from .page_effect_batches import after_page_inventory_inserts
from .membership_page_effects import observe_page_memberships

_VIDEO = re.compile(r"[A-Za-z0-9_-]{11}\Z")
_STATUSES = frozenset({"continuation", "complete", "empty", "boundary"})
_INPUT = """
    SELECT to_jsonb(input) AS value,source_checked_at>now()+interval '5 minutes' AS future
    FROM (SELECT %s::text AS channel_id,%s::text AS video_type,%s::uuid AS lease_token,
        %s::uuid AS scan_id,%s::uuid AS transition_id,%s::text AS expected_page_token,
        %s::text AS next_page_token,%s::text AS page_status,%s::jsonb AS items,
        %s::timestamptz AS source_checked_at) input
"""
_ITEM_FACTS = """
    SELECT jsonb_typeof(item) AS kind,item->>'video_id' AS video_id,
        item ? 'published_at' AS has_published_at,jsonb_typeof(item->'published_at') AS published_kind
    FROM jsonb_array_elements(%s::jsonb) input(item)
"""
_HASH = """
    SELECT md5(jsonb_build_array(btrim(%s::text),%s::text,%s::uuid,%s::uuid,%s::text,
        %s::text,%s::text,%s::jsonb,extract(epoch FROM %s::timestamptz))::text) AS value
"""
_ELIGIBLE = ('SELECT (SELECT policy_facts FROM (' + CHANNEL_CONTROL_FACTS_SQL + ') policy WHERE channel_id=%s) AS policy_facts')
_SOURCE_AFTER_BOUNDARY = "SELECT %s::timestamptz<%s::timestamptz AS before_boundary"
_TOKEN_CYCLE = """
    SELECT EXISTS(SELECT 1 FROM public.typed_inventory_receipts WHERE receipt_scope='scan_transition'
        AND transition_kind='page' AND scan_id=%s::uuid
        AND next_page_token_hash=sha256(convert_to(%s::text,'UTF8'))) AS present
"""
_VIDEO_LOCKS = """
    SELECT pg_advisory_xact_lock(hashtextextended(item->>'video_id',962106))
    FROM jsonb_array_elements(%s::jsonb) input(item) ORDER BY item->>'video_id'
"""
_NEW_MEMBERSHIPS = """
    SELECT count(*)::integer AS count FROM (SELECT DISTINCT btrim(item->>'video_id') AS video_id
        FROM jsonb_array_elements(%s::jsonb) input(item)) item
    WHERE NOT EXISTS(SELECT 1 FROM public.video_inventory_memberships membership
        WHERE membership.video_id=item.video_id AND membership.channel_id=%s AND membership.video_type=%s
          AND membership.membership_status='present')
"""
_INSERT_CANONICAL = """
    INSERT INTO public.video_inventory(video_id,channel_id,discovered_at,updated_at,video_type,published_at,published_acquired_at)
    SELECT btrim(item->>'video_id'),%(channel_id)s,%(source_checked_at)s::timestamptz,
        %(source_checked_at)s::timestamptz,%(video_type)s,
        CASE WHEN nullif(item->>'published_at','') IS NULL THEN NULL ELSE (item->>'published_at')::timestamptz END,
        CASE WHEN nullif(item->>'published_at','') IS NOT NULL THEN %(source_checked_at)s::timestamptz END
    FROM jsonb_array_elements(%(items)s::jsonb) input(item)
    ON CONFLICT(video_id) DO NOTHING RETURNING video_id
"""
_INSERT_CANONICAL_EFFECT = _INSERT_CANONICAL + ",to_jsonb(video_inventory)::text AS effect_new"
_AFTER_INSERT = """
    SELECT (SELECT coalesce(array_agg(id ORDER BY id),ARRAY[]::text[]) FROM unnest(%s::text[]) inserted(id)) AS inserted_ids,
        (SELECT coalesce(array_agg(item->>'video_id' ORDER BY item->>'video_id'),ARRAY[]::text[])
          FROM jsonb_array_elements(%s::jsonb) input(item)) AS observed_ids,
        (SELECT count(*)::integer FROM (SELECT DISTINCT btrim(item->>'video_id') AS video_id
          FROM jsonb_array_elements(%s::jsonb) input(item)) item JOIN public.video_inventory inventory USING(video_id)
          WHERE inventory.channel_id IS DISTINCT FROM %s OR inventory.video_type IS DISTINCT FROM %s) AS conflicts
"""
_UPSERT_MEMBERSHIPS = """
    INSERT INTO public.video_inventory_memberships AS membership(video_id,channel_id,video_type,playlist_id,
        source_published_at,membership_status,evidence_source,first_seen_at,last_seen_at,last_present_at,
        absent_since,first_seen_scan_id,last_seen_scan_id,absence_confirmed_by_scan_id,updated_at)
    SELECT btrim(input.item->>'video_id'),%(channel_id)s,%(video_type)s,%(playlist_id)s,
        CASE WHEN nullif(input.item->>'published_at','') IS NULL THEN NULL ELSE (input.item->>'published_at')::timestamptz END,
        'present','typed_playlist_scan',%(source_checked_at)s::timestamptz,%(source_checked_at)s::timestamptz,
        %(source_checked_at)s::timestamptz,NULL,%(scan_id)s::uuid,%(scan_id)s::uuid,NULL,now()
    FROM jsonb_array_elements(%(items)s::jsonb) input(item)
    JOIN public.video_inventory inventory ON inventory.video_id=btrim(input.item->>'video_id')
    ON CONFLICT(video_id,channel_id,video_type) DO UPDATE SET playlist_id=excluded.playlist_id,
        source_published_at=CASE WHEN membership.last_present_at IS NULL OR excluded.last_present_at>=membership.last_present_at
            THEN excluded.source_published_at ELSE membership.source_published_at END,
        membership_status='present',evidence_source='typed_playlist_scan',
        first_seen_at=least(membership.first_seen_at,excluded.first_seen_at),
        last_seen_at=greatest(membership.last_seen_at,excluded.last_seen_at),
        last_present_at=greatest(membership.last_present_at,excluded.last_present_at),absent_since=NULL,
        first_seen_scan_id=coalesce(membership.first_seen_scan_id,excluded.first_seen_scan_id),
        last_seen_scan_id=excluded.last_seen_scan_id,absence_confirmed_by_scan_id=NULL,updated_at=now()
    WHERE membership.last_present_at IS NULL OR excluded.last_present_at>=membership.last_present_at
"""
_PROGRESS = """
    SELECT (SELECT count(*)::integer FROM public.video_inventory_memberships
        WHERE channel_id=%s AND video_type=%s AND last_seen_scan_id=%s::uuid) AS items_seen,
        coalesce(%s::integer,0)+%s::integer AS total_new
"""
_SCAN_UPDATE = """
    UPDATE public.channel_video_inventory_scans SET status=CASE WHEN %(terminal)s THEN 'completed' ELSE 'running' END,
        coverage=CASE WHEN %(complete_coverage)s THEN 'complete' ELSE 'partial' END,
        resume_page_token=CASE WHEN %(terminal)s THEN NULL ELSE %(next_page_token)s END,
        pages_completed=pages_completed+1,items_seen=%(items_seen)s,new_memberships_found=%(total_new)s,
        source_checked_at=%(source_checked_at)s::timestamptz,
        completed_at=CASE WHEN %(terminal)s THEN %(source_checked_at)s::timestamptz END,error_code=NULL,error_at=NULL,
        last_transition_id=%(transition_id)s::uuid,last_transition_kind='page',updated_at=now()
    WHERE scan_id=%(scan_id)s::uuid RETURNING pages_completed
"""
_ABSENT = """
    UPDATE public.video_inventory_memberships SET membership_status='absent',absent_since=%(source_checked_at)s::timestamptz,
        absence_confirmed_by_scan_id=%(scan_id)s::uuid,updated_at=now()
    WHERE channel_id=%(channel_id)s AND video_type=%(video_type)s AND membership_status='present'
      AND last_seen_scan_id IS DISTINCT FROM %(scan_id)s::uuid
      AND (last_present_at IS NULL OR last_present_at<=%(scan_boundary_at)s::timestamptz)
"""
_PENDING_CHANGE = """
    SELECT coalesce(isfinite(channel_metadata_checked_at)
        AND channel_metadata_checked_at BETWEEN now()-interval '696 hours' AND now()+interval '5 minutes'
        AND isfinite(channel_video_count_changed_at)
        AND channel_video_count_changed_at BETWEEN now()-interval '696 hours' AND now()+interval '5 minutes'
        AND channel_video_count_changed_at<=channel_metadata_checked_at
        AND (%s::timestamptz IS NULL OR channel_video_count_changed_at>%s::timestamptz),false) AS pending
    FROM public.channels WHERE channel_id=%s
"""
_RELEASE = """
    UPDATE public.channel_playlist_state SET inventory_status=CASE WHEN %(complete_coverage)s OR last_completed_scan_id IS NOT NULL
        OR product_exposure_ready OR (inventory_state_origin='legacy_long_backfill' AND inventory_status='completed') THEN 'completed'
        WHEN NOT %(terminal)s THEN 'running' ELSE 'pending' END,
        expected_item_count=CASE WHEN %(complete_coverage)s THEN %(items_seen)s ELSE expected_item_count END,
        last_completed_scan_id=CASE WHEN %(complete_coverage)s THEN %(scan_id)s::uuid ELSE last_completed_scan_id END,
        inventory_completed_at=CASE WHEN %(complete_coverage)s THEN %(source_checked_at)s::timestamptz ELSE inventory_completed_at END,
        inventory_last_error_code=NULL,inventory_last_error_at=NULL,
        inventory_error_count=CASE WHEN %(terminal)s THEN 0 ELSE inventory_error_count END,
        inventory_state_origin=CASE WHEN %(complete_coverage)s THEN 'typed_native' ELSE inventory_state_origin END,
        inventory_updated_at=now(),status='ready',next_due_at=%(next_due_at)s::timestamptz,
        priority=CASE WHEN %(terminal)s THEN 0 ELSE 100 END,no_new_streak=%(no_new_streak)s,
        active_scan_id=CASE WHEN %(terminal)s THEN NULL ELSE %(scan_id)s::uuid END,
        resume_page_token=CASE WHEN %(terminal)s THEN NULL ELSE %(next_page_token)s END,
        resume_started_at=CASE WHEN %(terminal)s THEN NULL ELSE coalesce(resume_started_at,%(started_at)s::timestamptz) END,
        scan_boundary_at=CASE WHEN %(terminal)s THEN NULL ELSE %(scan_boundary_at)s::timestamptz END,
        source_next_after=%(next_due_at)s::timestamptz,
        source_checked_at=CASE WHEN %(video_type)s='long' AND %(scan_kind)s='full_reconciliation' AND NOT %(terminal)s
            THEN source_checked_at ELSE %(source_checked_at)s::timestamptz END,
        last_result=CASE WHEN NOT %(terminal)s THEN 'catchup_pending' WHEN %(total_new)s>0 THEN 'new_videos_found' ELSE 'no_new_videos' END,
        discovery_started_at=NULL,discovery_error=NULL,discovery_error_count=0,
        lease_channel_epoch=NULL,lease_token=NULL,lease_owner=NULL,lease_run_id=NULL,lease_expires_at=NULL,
        attempt_count=0,last_error_code=NULL,last_completion_attempt_id=%(transition_id)s::uuid,updated_at=now()
    WHERE channel_id=%(channel_id)s AND video_type=%(video_type)s AND status='leased'
        AND lease_token=%(lease_token)s::uuid AND active_scan_id=%(scan_id)s::uuid
    RETURNING to_jsonb(next_due_at)#>>'{}' AS next_due_at
"""
_CANONICAL = """
    SELECT coalesce(array_agg(inventory.video_id ORDER BY inventory.video_id),ARRAY[]::text[]) AS ids
    FROM jsonb_array_elements(%s::jsonb) input(item) JOIN public.video_inventory inventory ON inventory.video_id=item->>'video_id'
    WHERE inventory.channel_id=%s AND inventory.video_type=%s
"""
_TYPED_QUEUE = ("""
    SELECT coalesce(array_agg(candidate.video_id ORDER BY candidate.video_id),ARRAY[]::text[]) AS ids
    FROM unnest(%s::text[]) input(video_id) CROSS JOIN LATERAL
        (SELECT c.video_id FROM """ + "(" + TYPED_CANDIDATE_INPUT_VIDEO_ID_SQL + ")" + """ c WHERE c.video_id=input.video_id
          AND c.channel_id=%s AND c.video_type=%s OFFSET 0) candidate
""")
_RECEIPT = """
    INSERT INTO public.typed_inventory_receipts(receipt_scope,transition_id,scan_id,transition_kind,result,
        source_checked_at,next_page_token_hash,created_at)
    VALUES ('scan_transition',%s::uuid,%s::uuid,'page',%s::jsonb,%s::timestamptz,
        CASE WHEN %s::text='continuation' THEN sha256(convert_to(%s::text,'UTF8')) END,now())
"""


def settle_typed_inventory_page(database, *, channel_id, video_type, lease_token, scan_id, transition_id,
                                expected_page_token, next_page_token, page_status, items, source_checked_at):
    with database.transaction(_operation("settle_typed_inventory_page")) as tx:
        result = settle_page_in_transaction(tx, channel_id=channel_id, video_type=video_type, lease_token=lease_token,
            scan_id=scan_id, transition_id=transition_id, expected_page_token=expected_page_token,
            next_page_token=next_page_token, page_status=page_status, items=items, source_checked_at=source_checked_at)
    return result


def settle_page_in_transaction(transaction, **parameters):
    """Borrow the caller; failures cannot be caught to commit preceding writes."""
    with transaction.borrow():
        return _settle(transaction, **parameters)


def _validate(tx, values, future):
    if (values["channel_id"] is None or not values["channel_id"].strip(" ")
            or (values["video_type"] is not None and values["video_type"] not in _TYPES)
            or any(values[key] is None for key in ("lease_token", "scan_id", "transition_id", "source_checked_at"))
            or (values["page_status"] is not None and values["page_status"] not in _STATUSES) or future is True):
        _fail("typed_inventory_page_identity_invalid")
    expected, next_token, status = values["expected_page_token"], values["next_page_token"], values["page_status"]
    if expected is not None and not 1 <= len(expected) <= 2048: _fail("typed_inventory_expected_token_invalid")
    if status == "continuation" and (next_token is None or not 1 <= len(next_token) <= 2048 or next_token == expected):
        _fail("typed_inventory_continuation_invalid")
    if status is not None and status != "continuation" and next_token is not None: _fail("typed_inventory_terminal_token_invalid")
    items = values["items"]
    if not isinstance(items, list): _fail("typed_inventory_page_items_invalid")
    count = len(items)
    if count > 50: _fail("typed_inventory_page_too_large")
    if status == "continuation" and count == 0: _fail("typed_inventory_empty_page_with_continuation")
    if status == "empty" and count != 0: _fail("typed_inventory_empty_page_has_items")
    facts = tx.fetch_all(_ITEM_FACTS, (Jsonb(items),), max_rows=50)
    if any(row["kind"] != "object" or row["video_id"] is None or not _VIDEO.fullmatch(row["video_id"])
            or (row["has_published_at"] and row["published_kind"] not in {"string", "null"}) for row in facts):
        _fail("typed_inventory_page_item_invalid")
    if len({row["video_id"].strip(" ") for row in facts}) != count: _fail("typed_inventory_page_items_not_unique")
    return count


def _settle(tx, *, channel_id, video_type, lease_token, scan_id, transition_id,
            expected_page_token, next_page_token, page_status, items, source_checked_at):
    arguments = tx.fetch_one(_INPUT, (channel_id, video_type, lease_token, scan_id, transition_id,
        expected_page_token, next_page_token, page_status, Jsonb(items), source_checked_at))
    values = _value(arguments)
    _hot(tx, values["source_checked_at"])
    count = _validate(tx, values, arguments["future"])
    request_hash = tx.fetch_one(_HASH, (values["channel_id"], values["video_type"], values["lease_token"], values["scan_id"],
        values["expected_page_token"], values["next_page_token"], values["page_status"], Jsonb(values["items"]), values["source_checked_at"]))["value"]
    replay = _replay(tx, values, "page", request_hash)
    if replay is not None: return replay
    source, scan = _owned(tx, values, "page")
    identity = values["channel_id"].strip(" ")
    if channel_eligible_row(tx.fetch_one(_ELIGIBLE, (identity,)),field='valid')["valid"] is not True: _fail("typed_inventory_channel_not_eligible")
    if scan["status"] != "running" or scan["resume_page_token"] != values["expected_page_token"]:
        _fail("typed_inventory_page_checkpoint_mismatch")
    if not scan["token_history_verified"]: _fail("inventory_token_history_invalid")
    _hot(tx, scan["scan_boundary_at"])
    _hot(tx, scan["started_at"])
    if tx.fetch_one(_SOURCE_AFTER_BOUNDARY, (values["source_checked_at"], scan["scan_boundary_at"]))["before_boundary"] is True:
        _fail("typed_inventory_source_before_scan")
    status = values["page_status"]
    if status == "continuation" and tx.fetch_one(_TOKEN_CYCLE, (values["scan_id"], values["next_page_token"]))["present"]:
        _fail("inventory_token_cycle")
    if status == "boundary" and scan["scan_kind"] != "discovery": _fail("typed_inventory_boundary_requires_discovery")
    terminal = None if status is None else status != "continuation"
    full = scan["scan_kind"] in {"initial_inventory", "full_reconciliation"}
    complete = (None if status is None else status in {"complete", "empty"}) if full else False
    payload = Jsonb(values["items"])
    tx.fetch_all(_VIDEO_LOCKS, (payload,), max_rows=50)
    new_members = tx.fetch_one(_NEW_MEMBERSHIPS, (payload, identity, values["video_type"]))["count"]
    write = {**values, "channel_id": identity, "items": payload, "playlist_id": scan["playlist_id"],
        "scan_boundary_at": scan["scan_boundary_at"], "started_at": scan["started_at"], "scan_kind": scan["scan_kind"],
        "terminal": terminal, "complete_coverage": complete}
    inserted = tx.returning_all(_INSERT_CANONICAL_EFFECT, write, max_rows=50)
    after_page_inventory_inserts(tx,inserted)
    facts = tx.fetch_one(_AFTER_INSERT, ([row["video_id"] for row in inserted], payload, payload, identity, values["video_type"]))
    inserted_ids, conflicts = facts["inserted_ids"], facts["conflicts"]
    observe_page_memberships(tx,write)
    progress = tx.fetch_one(_PROGRESS, (identity, values["video_type"], values["scan_id"], scan["new_memberships_found"],
        len(inserted_ids) if values["video_type"] == "long" else new_members))
    write.update(progress)
    advanced = tx.returning_one(_SCAN_UPDATE, write)
    from .membership_absence_effects import mark_absent_memberships
    absent = mark_absent_memberships(tx, write) if complete else 0
    at = tx.fetch_one("SELECT clock_timestamp() AS at")["at"]
    try:
        policy = inventory_source_policy(values["video_type"], scan["scan_kind"], status, progress["total_new"],
            source["no_new_streak"], source["attempt_count"], source["discovery_error_count"], None, at,
            schedule=getattr(require_cadence_snapshot(tx).inventory_by_type, values["video_type"]))
    except PolicyError as error:
        _fail(error.code, error.sqlstate)
    pending = False
    if values["video_type"] == "long":
        row = tx.fetch_one(_PENDING_CHANGE, (source["source_checked_at"], source["source_checked_at"], identity))
        pending = row is not None and row["pending"] is True
    write.update(next_due_at=policy.next_due_at, no_new_streak=policy.no_new_streak)
    released = tx.returning_one(_RELEASE, write)
    if released is None: _fail("typed_inventory_page_lease_lost")
    canonical_ids = tx.fetch_one(_CANONICAL, (payload, identity, values["video_type"]))["ids"]
    if values["video_type"] == "long":
        canonical = set(canonical_ids)
        queue_ids = [identity for identity in inserted_ids if identity in canonical]
    else:
        queue_ids = tx.fetch_one(_TYPED_QUEUE, (canonical_ids, identity, values["video_type"]))["ids"]
    queued = ensure_video_collection_state_in_transaction(tx, queue_ids)
    result = {"scan_id": values["scan_id"], "channel_id": identity, "lease_token": values["lease_token"],
        "transition_id": values["transition_id"], "video_type": values["video_type"], "observed_video_ids": facts["observed_ids"],
        "canonical_video_ids": canonical_ids, "new_video_ids": inserted_ids, "page_items": count,
        "pending_channel_count_change": pending, "provider_attempts": 1,
        "status": "completed" if terminal else "running", "coverage": "complete" if complete else "partial",
        "scan_kind": scan["scan_kind"], "page_status": status, "pages_completed": advanced["pages_completed"],
        "items_seen": progress["items_seen"], "new_memberships_found": progress["total_new"], "inserted": len(inserted_ids),
        "already_matching": max(count-len(inserted_ids)-conflicts, 0), "conflicts": conflicts,
        "canonical_inserted": len(inserted_ids), "queue_inserted": queued, "canonical_conflicts": conflicts,
        "memberships_marked_absent": absent, "resume_page_token": None if terminal else values["next_page_token"],
        "next_due_at": released["next_due_at"], "replayed": False, "request_hash": request_hash}
    tx.execute(_RECEIPT, (values["transition_id"], values["scan_id"], Jsonb(result), values["source_checked_at"], status, values["next_page_token"]))
    _hot(tx, values["source_checked_at"])
    assert_channel_scope(tx, identity, source["lease_channel_epoch"])
    return result
