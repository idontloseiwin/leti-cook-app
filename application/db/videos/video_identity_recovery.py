"""Refresh retained IDs through the existing queue without inferring a type.

Metadata stays hidden until typed playlist evidence or a saved manual decision
restores the canonical type. An exact receipt makes completion replay safe;
provider omissions are observations, not guesses about deletion or privacy.
"""
from psycopg.types.json import Jsonb

from ..base.postgres import DatabaseServerError, Operation
from ..base.operation_contracts import get_operation
from ..channel.channel_control import assert_channel_scopes
from ..channel.channel_read_policy import CHANNEL_CONTROL_SQL
from ..inventory.inventory_review_events import after_inventory_source_write
from .collection_policy import require_cadence_snapshot
from .video_counter_completion import _clock_check
from .video_source_invalidation import source_clock_invalidation
from .video_availability_schedule import missing_schedule, observation_clock
from datetime import timedelta
from contextlib import contextmanager
from collection_policy import counters_due_at
from .video_initial_policy import VIDEO_HANDOFF_TYPED_SQL, VIDEO_INITIAL_CHUNK_TYPED_SQL


ERRORS = get_operation('complete_video_identity_refresh').error_codes
ELIGIBLE_SQL = """SELECT EXISTS(SELECT 1 FROM public.video_inventory i
 JOIN (""" + CHANNEL_CONTROL_SQL + """) control USING(channel_id)
 WHERE i.video_id=%s::text AND i.channel_id=%s::text
 AND (control.collection_mode IN ('active','metadata_only') OR ("""+VIDEO_HANDOFF_TYPED_SQL+""") OR ("""+VIDEO_INITIAL_CHUNK_TYPED_SQL+"""))
 AND NOT control.is_hidden AND control.owner_permission='accepted') AS eligible"""

_DEFINITION = """video_id text,channel_id text,video_type text,lease_token uuid,
 collection_attempt_id uuid,recorded_at timestamptz,missing boolean,title text,
 published_at timestamptz,duration text,tags text[],category_id text,
 youtube_topic_categories text[],default_audio_language text,default_language text,
 caption_available boolean,stream_actual_end_at timestamptz,counterless_profile boolean,availability_status text,
 privacy_status text,upload_status text"""
_INPUT = """SELECT input.*,md5(raw.payload::text) AS fingerprint
 FROM jsonb_array_elements(%s::jsonb) raw(payload)
 CROSS JOIN LATERAL jsonb_to_record(raw.payload) input(""" + _DEFINITION + ")"
_VALID = "WITH input AS (" + _INPUT + """) SELECT NOT EXISTS(SELECT 1 FROM input
 WHERE video_id IS NULL OR btrim(video_id)='' OR channel_id IS NULL OR btrim(channel_id)=''
 OR video_type IS NULL OR video_type NOT IN ('unknown','long','short','stream') OR lease_token IS NULL OR collection_attempt_id IS NULL
 OR (counterless_profile IS TRUE AND (missing IS NOT FALSE OR video_type='unknown'))
 OR recorded_at IS NULL OR missing IS NULL OR (NOT missing AND (title IS NULL
 OR length(title)>1000 OR published_at IS NULL OR NOT isfinite(published_at)
 OR duration IS NULL OR availability_status NOT IN ('public','unlisted','private')
 OR availability_status IS NULL OR privacy_status IS DISTINCT FROM availability_status
 OR upload_status IS NULL OR length(upload_status)>64 OR stream_actual_end_at>recorded_at
 OR cardinality(tags)>500 OR octet_length(array_to_string(tags,''))>25600
 OR cardinality(youtube_topic_categories)>50 OR length(default_audio_language)>64
 OR length(default_language)>64)))
 AND NOT EXISTS(SELECT video_id FROM input GROUP BY video_id HAVING count(*)>1) AS valid"""
_ROWS = "SELECT input.*,receipt.video_id IS NOT NULL AS replayed,receipt.payload_fingerprint IS DISTINCT FROM input.fingerprint OR receipt.receipt_kind IS DISTINCT FROM 'identity_refresh' AS mismatch FROM (" + _INPUT + """) input
 LEFT JOIN public.typed_video_collection_receipts receipt USING(video_id,collection_attempt_id)
 ORDER BY input.video_id"""
_LOCK = "SELECT * FROM public.video_collection_state WHERE video_id=%s::text FOR UPDATE NOWAIT"
_INVENTORY_LOCK = """SELECT skeleton_since,video_type AS inventory_type,channel_id AS inventory_channel,
 to_jsonb(i)::text AS inventory_before FROM public.video_inventory i WHERE video_id=%s::text FOR UPDATE NOWAIT"""
_UPSERT = """INSERT INTO public.videos AS v(video_id,channel_id,title,published_at,duration,tags,category_id,
 youtube_topic_categories,default_audio_language,default_language,caption_available,stream_actual_end_at,
 metadata_checked_at,availability_status,privacy_status,upload_status,availability_checked_at)
 SELECT video_id,channel_id,CASE WHEN missing THEN NULL ELSE title END,
 CASE WHEN missing THEN NULL ELSE published_at END,CASE WHEN missing THEN NULL ELSE duration END,
 CASE WHEN missing THEN NULL ELSE coalesce(tags,'{}') END,
 CASE WHEN missing THEN NULL ELSE category_id END,CASE WHEN missing THEN NULL ELSE youtube_topic_categories END,
 CASE WHEN missing THEN NULL ELSE default_audio_language END,CASE WHEN missing THEN NULL ELSE default_language END,
 CASE WHEN missing THEN NULL ELSE caption_available END,CASE WHEN missing THEN NULL ELSE stream_actual_end_at END,
 CASE WHEN missing THEN NULL ELSE recorded_at END,CASE WHEN missing THEN 'unknown_unavailable' ELSE availability_status END,
 CASE WHEN missing THEN NULL ELSE privacy_status END,CASE WHEN missing THEN NULL ELSE upload_status END,recorded_at
 FROM (""" + _INPUT + """) input
 ON CONFLICT(video_id) DO UPDATE SET """
# On an omission retain only still-valid existing metadata; availability has its
# own observation clock. The retention owner independently expires both groups.
_UPSERT += ','.join(
    f"{field}=CASE WHEN excluded.metadata_checked_at IS NULL THEN v.{field} ELSE excluded.{field} END"
    for field in ('title','published_at','duration','tags','category_id','youtube_topic_categories',
                  'default_audio_language','default_language','caption_available','stream_actual_end_at','metadata_checked_at')
) + ",availability_status=excluded.availability_status,privacy_status=excluded.privacy_status,upload_status=excluded.upload_status,availability_checked_at=excluded.availability_checked_at"
_UPSERT += ",publication_status=CASE WHEN excluded.metadata_checked_at IS NULL THEN v.publication_status END,scheduled_start_at=CASE WHEN excluded.metadata_checked_at IS NULL THEN v.scheduled_start_at END"
_RECEIPT = """INSERT INTO public.typed_video_collection_receipts
 (video_id,collection_attempt_id,channel_id,video_type,lease_token,receipt_kind,recorded_at,payload_fingerprint)
 SELECT video_id,collection_attempt_id,channel_id,NULL,lease_token,'identity_refresh',recorded_at,fingerprint
 FROM (""" + _INPUT + ") input"
_SETTLE = """UPDATE public.video_collection_state SET next_due_at=least(%(next_due_at)s::timestamptz,
 CASE WHEN availability_resume_observed_at>%(recorded_at)s::timestamptz THEN availability_resume_observed_at END),
 metadata_due_at=least(%(next_due_at)s::timestamptz,
 CASE WHEN availability_resume_observed_at>%(recorded_at)s::timestamptz THEN availability_resume_observed_at END),stats_due_at=NULL,
 metadata_source_checked_at=CASE WHEN %(missing)s THEN metadata_source_checked_at ELSE %(recorded_at)s::timestamptz END,
 typed_collection_status=CASE WHEN NOT %(missing)s AND video_type IN ('short','stream')
  AND typed_collection_status='unavailable' THEN 'pending' ELSE typed_collection_status END,
 typed_collection_checked_at=CASE WHEN NOT %(missing)s AND video_type IN ('short','stream')
  AND typed_collection_status='unavailable' THEN %(recorded_at)s::timestamptz ELSE typed_collection_checked_at END,
 hydration_needed=true,availability_first_missing_at=%(first_missing_at)s::timestamptz,
 availability_schedule_type=%(schedule_type)s::text,
 availability_resume_observed_at=CASE WHEN availability_resume_observed_at>%(recorded_at)s::timestamptz
 THEN availability_resume_observed_at END,
 paused_reason=CASE WHEN %(stopped)s AND NOT coalesce(availability_resume_observed_at>%(recorded_at)s::timestamptz,false)
 THEN 'availability_retry_expired' ELSE NULL END,
 lease_channel_epoch=NULL,lease_token=NULL,lease_owner=NULL,lease_started_at=NULL,lease_expires_at=NULL,
 consecutive_failures=0,last_error_type=CASE WHEN %(counterless_profile)s THEN 'youtube_view_count_absent' END,
 last_success_at=%(recorded_at)s::timestamptz,updated_at=now()
 WHERE video_id=%(video_id)s::text"""
_AUTOMATIC_HANDOFF = """UPDATE public.video_collection_state q SET
 next_due_at=clock_timestamp(),metadata_due_at=clock_timestamp(),hydration_needed=true
 WHERE q.video_id=ANY(%s::text[]) AND q.lease_token IS NULL
 AND q.availability_first_missing_at IS NULL AND q.paused_reason IS NULL
 AND EXISTS(SELECT 1 FROM public.video_type_automatic_candidates choice
 WHERE choice.video_id=q.video_id AND choice.channel_id=q.channel_id AND choice.automatic_type=q.video_type)"""
_RESTORE = """UPDATE public.video_inventory i SET video_type=d.video_type,skeleton_since=NULL,
 published_at=d.source_published_at,published_acquired_at=d.published_acquired_at
 FROM (WITH evidence AS MATERIALIZED(SELECT m.video_type,m.channel_id,m.source_published_at,m.last_present_at
 FROM public.video_inventory_memberships m WHERE m.video_id=%(video_id)s
 AND m.membership_status='present' AND isfinite(m.last_present_at) AND isfinite(m.last_seen_at)
 AND m.last_present_at BETWEEN clock_timestamp()-interval '696 hours' AND clock_timestamp()
 AND m.last_seen_at BETWEEN clock_timestamp()-interval '696 hours' AND clock_timestamp()),
 decision AS (SELECT coalesce((SELECT c.decision_type FROM public.video_type_review_cases c
 WHERE c.video_id=%(video_id)s AND c.channel_id=%(channel_id)s AND c.decision_type IS NOT NULL
 AND c.blocked_reason IS NULL),CASE WHEN count(DISTINCT video_type)=1 THEN min(video_type) END) AS video_type
 FROM evidence WHERE NOT EXISTS(SELECT 1 FROM evidence WHERE channel_id<>%(channel_id)s))
 SELECT decision.video_type,publication.source_published_at,publication.published_acquired_at
 FROM decision LEFT JOIN LATERAL (
 SELECT source_published_at,CASE WHEN source_published_at IS NOT NULL THEN last_present_at END AS published_acquired_at
 FROM evidence WHERE video_type=decision.video_type
 ORDER BY source_published_at ASC NULLS LAST,last_present_at ASC LIMIT 1) publication ON true
 WHERE NOT EXISTS(SELECT 1 FROM evidence WHERE channel_id<>%(channel_id)s)) d
 WHERE i.video_id=%(video_id)s AND i.channel_id=%(channel_id)s AND d.video_type IS NOT NULL
 RETURNING to_jsonb(i)::text AS new_text"""

_INCIDENT_LOCK = """SELECT incident.id,incident.last_seen_at>input.recorded_at AS regressed
 FROM public.video_availability_incidents incident JOIN ("""+_INPUT+""") input USING(video_id)
 WHERE NOT input.missing AND incident.restored_at IS NULL ORDER BY incident.video_id FOR UPDATE OF incident"""
_RESTORE_INCIDENTS = """UPDATE public.video_availability_incidents incident
 SET restored_at=input.recorded_at,restored_status=input.availability_status,
 source_restored='video_identity_refresh',updated_at=input.recorded_at
 FROM ("""+_INPUT+""") input WHERE incident.video_id=input.video_id
 AND NOT input.missing AND incident.restored_at IS NULL
 RETURNING incident.video_id,incident.channel_id,incident.latest_status AS from_status,
 incident.restored_status AS to_status,incident.restored_at::text AS observed_at"""
_RESTORED_EVENTS = """INSERT INTO public.video_availability_events
 (video_id,channel_id,event_type,observed_at,source,from_status,to_status)
 SELECT video_id,channel_id,'restored',observed_at,'video_identity_refresh',from_status,to_status
 FROM jsonb_to_recordset(%s::jsonb) restored(video_id text,channel_id text,
 from_status text,to_status text,observed_at timestamptz)"""


def _reject(code):
    raise DatabaseServerError(code, code='P0001')


@contextmanager
def _transaction(database,transaction):
    if transaction is not None:
        with transaction.borrow(): yield transaction
    else:
        with database.transaction(Operation('complete_video_identity_refresh',statement_timeout_ms=8000,
                lock_timeout_ms=250,total_timeout_ms=8000,error_codes=ERRORS)) as tx:
            yield tx


def complete_video_identity_refresh(database, rows, *, transaction=None):
    if not isinstance(rows, list) or not 1 <= len(rows) <= 50:
        _reject('video_identity_refresh_invalid')
    with _transaction(database,transaction) as tx:
        snapshot = require_cadence_snapshot(tx)
        recheck_seconds = snapshot.rechecks.identity_seconds
        payload = Jsonb(rows)
        if tx.fetch_one(_VALID, (payload,))['valid'] is not True:
            _reject('video_identity_refresh_invalid')
        _clock_check(tx, payload)
        parsed = tx.fetch_all(_ROWS, (payload,), max_rows=50)
        if any(row['replayed'] and row['mismatch'] for row in parsed):
            _reject('video_identity_refresh_receipt_mismatch')
        pending = [row for row in parsed if not row['replayed']]
        # Match inventory writers' advisory -> queue -> inventory ordering.
        for row in pending:
            if tx.fetch_one('SELECT pg_try_advisory_xact_lock(hashtextextended(%s::text,962106)) AS ok',
                            (row['video_id'],))['ok'] is not True:
                raise DatabaseServerError('database_operation_failed', code='55P03')
        scopes = {}
        for row in pending:
            state = tx.fetch_one(_LOCK, (row['video_id'],))
            inventory = tx.fetch_one(_INVENTORY_LOCK, (row['video_id'],))
            state = {**state, **inventory} if state is not None and inventory is not None else None
            expected_type = None if row['video_type'] == 'unknown' else row['video_type']
            if (state is None or state['video_type'] != expected_type or state['inventory_type'] != expected_type
                    or state['channel_id'] != row['channel_id']
                    or state['inventory_channel'] != row['channel_id'] or state['lease_token'] != row['lease_token']
                    or not tx.fetch_one('SELECT %s::timestamptz>clock_timestamp() AS valid',
                                        (state['lease_expires_at'],))['valid']
                    or not tx.fetch_one(ELIGIBLE_SQL, (row['video_id'], row['channel_id']))['eligible']):
                _reject('video_identity_refresh_stale')
            clocks = tx.fetch_one('SELECT greatest(metadata_checked_at,availability_checked_at)>%s::timestamptz AS regressed FROM public.videos WHERE video_id=%s',
                                  (row['recorded_at'], row['video_id']))
            if clocks and clocks['regressed']:
                _reject('video_identity_refresh_clock_regression')
            row['inventory_before'] = state['inventory_before']
            row['first_missing_at'] = state.get('availability_first_missing_at')
            row['schedule_type'] = state.get('availability_schedule_type') or expected_type
            epoch = state['lease_channel_epoch']
            if row['channel_id'] in scopes and scopes[row['channel_id']] != epoch:
                _reject('video_identity_refresh_stale')
            scopes[row['channel_id']] = epoch
        # One <=50-row source write; no partial provider response commits.
        pending_ids = {row['video_id'] for row in pending}
        owned = Jsonb([row for row in rows if row['video_id'] in pending_ids])
        if pending:
            history=tx.fetch_all(_INCIDENT_LOCK,(owned,),max_rows=50)
            if any(item['regressed'] for item in history):
                _reject('video_identity_refresh_clock_regression')
            with source_clock_invalidation(tx, sorted(pending_ids)):
                if tx.execute(_UPSERT, (owned,)) != len(pending):
                    _reject('video_identity_refresh_stale')
            tx.execute(_RECEIPT, (owned,))
            for row in pending:
                schedule = (missing_schedule(snapshot,video_type=row['schedule_type'],
                    first_missing_at=row['first_missing_at'],observed_at=row['recorded_at'])
                    if row['missing'] else dict(first_missing_at=None,schedule_type=None,stopped=False,
                        next_due_at=(observation_clock(row['recorded_at'])+timedelta(seconds=recheck_seconds)).isoformat()))
                counterless=row.get('counterless_profile') is True
                if counterless:
                    observed=observation_clock(row['recorded_at'])
                    # This is a retry deadline, not a new counter acquisition.
                    retry=counters_due_at(snapshot,row['video_type'],row['availability_status'],
                        observation_clock(row['published_at']),observed,observed,db_now=observed)
                    schedule['next_due_at']=(retry or observed+timedelta(seconds=recheck_seconds)).isoformat()
                tx.execute(_SETTLE,dict(row,**schedule,counterless_profile=counterless))
                if not row['missing'] and row['video_type']=='unknown':
                    restored = tx.returning_one(_RESTORE, row)
                    if restored:
                        tx.execute('UPDATE public.video_collection_state SET next_due_at=clock_timestamp() WHERE video_id=%s',
                                   (row['video_id'],))
                        after_inventory_source_write(tx, operation='update', old_text=row['inventory_before'], **restored)
            restored_incidents=tx.returning_all(_RESTORE_INCIDENTS,(owned,),max_rows=50)
            if restored_incidents:
                tx.execute(_RESTORED_EVENTS,(Jsonb(restored_incidents),))
            from ..inventory.automatic_type_reconciliation import reconcile_automatic_video_types
            reconcile_automatic_video_types(tx,[row["video_id"] for row in pending if not row["missing"]])
            automatic_ready=[row["video_id"] for row in pending
                             if not row["missing"] and not row.get("counterless_profile")]
            if automatic_ready:tx.execute(_AUTOMATIC_HANDOFF,(automatic_ready,))
            _clock_check(tx,payload)
            assert_channel_scopes(tx, [dict(channel_id=channel_id, control_epoch=epoch)
                                       for channel_id, epoch in scopes.items()])
        return len(rows)
