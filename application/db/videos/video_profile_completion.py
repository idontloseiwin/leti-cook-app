"""Long profile wrapper, borrowing base metadata completion atomically.

The native ownership bitmap freezes input ordinals before incident lock waits.
Only a <=1,000-row restoration payload crosses into Python; oversized requests
retain native pre-base clock/stale/cast ordering and fail at the base count cap.
The bitmap is linear in the already supplied input, not another row payload.
The public video repository owns dispatch and exact receipt-backed replay.
"""
from contextlib import contextmanager
from .incident_statement_effects import after_incident_statement
from psycopg.types.json import Jsonb
from ..channel.channel_control import assert_channel_scopes
from ..base.operation_contracts import get_operation
from ..base.postgres import DatabaseServerError,Operation
from . import video_metadata_completion as metadata

_PROFILE_ARRAY="""SELECT NOT EXISTS(SELECT 1 FROM jsonb_array_elements(%s::jsonb) raw(value)
 WHERE NOT(raw.value ? 'youtube_topic_categories')
 OR jsonb_typeof(raw.value->'youtube_topic_categories')<>'array') AS valid"""
_IDENTITIES="""SELECT input.*,raw.ordinality FROM jsonb_array_elements(%s::jsonb) WITH ORDINALITY raw(value,ordinality)
 CROSS JOIN LATERAL jsonb_to_record(raw.value) input(video_id text,channel_id text,lease_token uuid,collection_attempt_id uuid)"""
_ELIGIBLE=""" FROM input JOIN public.video_collection_state state ON state.video_id=input.video_id
 AND state.channel_id=input.channel_id AND state.lease_token=input.lease_token AND state.video_type='long'
 JOIN public.video_inventory inventory ON inventory.video_id=state.video_id AND inventory.channel_id=state.channel_id
 AND inventory.video_type='long' WHERE NOT EXISTS(SELECT 1 FROM public.video_snapshots snapshot
 WHERE snapshot.video_id=input.video_id AND snapshot.collection_attempt_id=input.collection_attempt_id)"""
_LOCK='WITH input AS MATERIALIZED('+_IDENTITIES+') SELECT count(*) AS count FROM (SELECT state.video_id'+_ELIGIBLE+' ORDER BY state.video_id FOR UPDATE OF state,inventory) locked'
# The original temporary table freezes membership before incident locks. A fixed
# ordinal mask keeps that snapshot even when a new receipt arrives during a wait.
_OWNED='WITH input AS MATERIALIZED('+_IDENTITIES+'), owned AS MATERIALIZED(SELECT input.ordinality'+_ELIGIBLE+''')
 SELECT coalesce(string_agg(CASE WHEN owned.ordinality IS NULL THEN '0' ELSE '1' END,'' ORDER BY input.ordinality),'') AS mask,
 count(owned.ordinality)::integer AS count FROM input LEFT JOIN owned USING(ordinality)'''
_RESTORE_DEFINITION='''video_id text,channel_id text,recorded_at timestamptz,metadata_observed_at timestamptz,availability_checked_at timestamptz,
 availability_restored_at timestamptz,availability_source text,availability_status text'''
_RESTORE_INPUT='''SELECT input.video_id,input.channel_id,input.recorded_at,
 coalesce(input.availability_checked_at,input.metadata_observed_at,input.recorded_at) AS observed_at,
 coalesce(input.availability_restored_at,input.metadata_observed_at,input.recorded_at) AS restored_at,
 coalesce(nullif(input.availability_source,''),'video_stats_sync') AS source,
 coalesce(nullif(input.availability_status,''),'public') AS restored_status
 FROM jsonb_array_elements(%(rows)s::jsonb) WITH ORDINALITY raw(value,ordinality)
 CROSS JOIN LATERAL jsonb_to_record(raw.value) input('''+_RESTORE_DEFINITION+''')
 WHERE get_bit(%(mask)s::bit varying,(raw.ordinality-1)::integer)=1 '''
_INCIDENT_LOCK='WITH owned AS MATERIALIZED('+_RESTORE_INPUT+''') SELECT count(*) AS count FROM (
 SELECT incident.id FROM public.video_availability_incidents incident JOIN owned USING(video_id)
 WHERE incident.restored_at IS NULL ORDER BY incident.video_id FOR UPDATE OF incident) locked'''
_RESTORED_CLOCKS='''WITH checks AS MATERIALIZED(SELECT coalesce(input.availability_restored_at,input.metadata_observed_at,input.recorded_at) AS at,
 clock_timestamp() AS wall FROM jsonb_to_recordset(%s::jsonb) input(recorded_at timestamptz,metadata_observed_at timestamptz,availability_restored_at timestamptz))
 SELECT coalesce(bool_and(at IS NOT NULL AND isfinite(at) AND at>=wall-interval '696 hours' AND at<=wall),true) AS valid FROM checks'''
_CAPTURE='WITH owned AS MATERIALIZED('+_RESTORE_INPUT+'''), captured AS MATERIALIZED(
 SELECT owned.*,incident.id AS incident_id,incident.latest_status,incident.last_seen_at
 FROM owned JOIN public.video_availability_incidents incident ON incident.video_id=owned.video_id AND incident.restored_at IS NULL)
 SELECT count(*)::integer AS count,
 coalesce(bool_or(last_seen_at>least(recorded_at,observed_at,restored_at)),false) AS stale,
 CASE WHEN jsonb_array_length(%(rows)s::jsonb)<=1000
 THEN coalesce(jsonb_agg(to_jsonb(captured)),'[]'::jsonb)::text END AS rows FROM captured'''
_SCOPES='WITH owned AS MATERIALIZED('+_RESTORE_INPUT+''')
 SELECT CASE WHEN jsonb_array_length(%(rows)s::jsonb)<=1000 THEN coalesce(jsonb_agg(DISTINCT jsonb_build_object(
 'channel_id',state.channel_id,'control_epoch',state.lease_channel_epoch)),'[]'::jsonb) ELSE '[]'::jsonb END AS scopes
 FROM owned JOIN public.video_collection_state state USING(video_id)
 JOIN jsonb_to_recordset(%(rows)s::jsonb) input(video_id text,lease_token uuid)
 ON input.video_id=state.video_id AND input.lease_token=state.lease_token'''
_CAPTURED_DEFINITION='''video_id text,channel_id text,recorded_at timestamptz,observed_at timestamptz,restored_at timestamptz,
 source text,restored_status text,incident_id bigint,latest_status text,last_seen_at timestamptz'''
_RESTORE='''UPDATE public.video_availability_incidents incident SET channel_id=coalesce(incident.channel_id,restored.channel_id),
 restored_at=restored.restored_at,restored_status=restored.restored_status,source_restored=restored.source,updated_at=restored.restored_at
 FROM jsonb_to_recordset(%s::jsonb) restored('''+_CAPTURED_DEFINITION+''') WHERE incident.id=restored.incident_id'''
_EVENTS='''INSERT INTO public.video_availability_events(video_id,channel_id,event_type,observed_at,source,from_status,to_status)
 SELECT restored.video_id,restored.channel_id,'restored',restored.restored_at,restored.source,
 coalesce(nullif(restored.latest_status,''),'unknown_unavailable'),restored.restored_status
 FROM jsonb_to_recordset(%s::jsonb) restored('''+_CAPTURED_DEFINITION+''')'''

# The original owner already holds these incident row locks. Read the complete
# OLD tuple in the same UPDATE and carry actual NEW out of RETURNING.
_INCIDENT_EFFECT_UPDATE = _RESTORE.replace(' FROM ', ' FROM public.video_availability_incidents effect_previous, ',1)
_INCIDENT_EFFECT_UPDATE += ' AND effect_previous.id=incident.id RETURNING to_jsonb(effect_previous)::text AS old_text,to_jsonb(incident)::text AS new_text'


@contextmanager
def _transaction(database,transaction):
    if transaction is not None:
        with transaction.borrow():yield transaction
    else:
        spec=get_operation('complete_video_profile_metadata_collection')
        with database.transaction(Operation(spec.name,statement_timeout_ms=spec.statement_timeout_ms,
                lock_timeout_ms=spec.lock_timeout_ms,total_timeout_ms=spec.statement_timeout_ms,
                error_codes=spec.error_codes)) as tx:yield tx


def _clocks(tx,raw):
    metadata._clocks(tx,raw)
    if tx.fetch_one(_RESTORED_CLOCKS,(raw,))['valid'] is not True:
        raise DatabaseServerError('hot_source_clock_out_of_window',code='22023')


def complete_video_profile_metadata_collection(database,rows,*,transaction=None,_reconcile_type=True):
    """Restore captured Long incidents with the one base source/receipt writer."""
    with _transaction(database,transaction) as tx:
        if not isinstance(rows,list):metadata._reject()
        raw=Jsonb(rows)
        if tx.fetch_one(_PROFILE_ARRAY,(raw,))['valid'] is not True:metadata._reject()
        tx.fetch_one(_LOCK,(raw,))
        _clocks(tx,raw)
        owned=tx.fetch_one(_OWNED,(raw,))
        parameters=dict(rows=raw,mask=owned['mask'])
        tx.fetch_one(_INCIDENT_LOCK,parameters)
        _clocks(tx,raw)
        captured=tx.fetch_one(_CAPTURE,parameters)
        if captured['stale']:
            raise DatabaseServerError('typed_long_availability_restore_stale_observation',code='P0001')
        scopes=tx.fetch_one(_SCOPES,parameters)['scopes']
        completed=metadata.complete_video_metadata_collection(None,rows,transaction=tx)
        _clocks(tx,raw)
        if captured['count']==0:
            # Both repeated passes exist in the retained no-restoration branch.
            _clocks(tx,raw)
            _clocks(tx,raw)
        else:
            changed=tx.returning_all(_INCIDENT_EFFECT_UPDATE,(captured['rows'],),max_rows=1000)
            after_incident_statement(tx,[dict(operation='update',**row) for row in changed])
            tx.execute(_EVENTS,(captured['rows'],))
            _clocks(tx,raw)
        if _reconcile_type and owned['count']:
            from ..inventory.automatic_type_reconciliation import reconcile_automatic_video_types
            reconcile_automatic_video_types(tx,[row["video_id"] for row,flag in zip(rows,owned["mask"]) if flag=="1"])
        assert_channel_scopes(tx,scopes)
        return completed
