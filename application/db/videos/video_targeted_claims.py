"""Discovery's addressed adapter to the existing automatic video lease owner.

No job, replacement queue or source inference. Only supplied IDs can be leased;
the canonical channel/type/initial/availability policy still admits each row.
Healthy event targets may verify metadata before its normal due time. Protected
recovery and error backoffs are never shortened by an incoming notification.
"""
from uuid import UUID
from psycopg.types.json import Jsonb

from ..base.postgres import DatabaseError, Operation
from ..channel.channel_control import assert_channel_scopes
from .video_initial_policy import VIDEO_INITIAL_PENDING_STATE_SQL
from . import video_scheduled_claims as scheduled

_LOCK = """SELECT state.video_id,state.channel_id,state.video_type,
 state.availability_first_missing_at,state.availability_schedule_type,state.availability_resume_observed_at,
 state.last_error_type,now() AS observed_at
 FROM public.video_collection_state state WHERE state.video_id=ANY(%(ids)s::text[])
 AND state.paused_reason IS NULL
 AND (state.video_type IS NULL OR state.video_type=ANY(%(formats)s::text[]))
 AND (state.lease_token IS NULL OR state.lease_expires_at<=now())
 AND state.last_claim_run_id IS DISTINCT FROM %(run_id)s::uuid
 AND NOT """ + VIDEO_INITIAL_PENDING_STATE_SQL + """
 AND (%(verify)s::boolean OR state.discovery_hydration_requested_at IS NOT NULL)
 AND (state.next_due_at<=now() OR (%(verify)s::boolean
   AND state.availability_first_missing_at IS NULL AND state.availability_resume_observed_at IS NULL
   AND state.last_error_type IS NULL))
 ORDER BY state.video_id FOR UPDATE OF state SKIP LOCKED"""
_PROMOTE = """UPDATE public.video_collection_state state SET metadata_due_at=now(),
 next_due_at=least(state.next_due_at,now()),updated_at=now()
 WHERE state.video_id=ANY(%s::text[]) RETURNING state.video_id"""
_STATUS = """SELECT input.video_id,state.channel_id,state.video_type,state.next_due_at,
 state.paused_reason,state.lease_token,state.lease_expires_at,
 state.availability_first_missing_at,state.availability_resume_observed_at,state.last_error_type,
 state.discovery_hydration_requested_at,state.last_claim_run_id,
 """ + VIDEO_INITIAL_PENDING_STATE_SQL + """ AS initial_pending,
 (SELECT initial_job_id FROM public.channel_collection_state c WHERE c.channel_id=state.channel_id) AS initial_job_id,
 now() AS observed_at FROM unnest(%s::text[]) input(video_id)
 LEFT JOIN public.video_collection_state state USING(video_id) ORDER BY input.video_id"""
_TAIL = """SELECT state.video_id,state.discovery_hydration_requested_at AS requested_at
 FROM public.video_collection_state state
 WHERE state.discovery_hydration_requested_at IS NOT NULL AND state.paused_reason IS NULL
 AND state.next_due_at<=now() AND (state.lease_token IS NULL OR state.lease_expires_at<=now())
 AND (state.video_type IS NULL OR state.video_type=ANY(%(formats)s::text[]))
 AND NOT """ + VIDEO_INITIAL_PENDING_STATE_SQL + """
 AND (%(after_at)s::timestamptz IS NULL OR (state.discovery_hydration_requested_at,state.video_id)>
 (%(after_at)s::timestamptz,%(after_id)s::text))
 ORDER BY state.discovery_hydration_requested_at,state.video_id LIMIT %(limit)s::integer"""


def _ids(values):
    if (not isinstance(values, (list, tuple)) or len(values)>50
            or any(type(value) is not str or not value or len(value)>128 or value!=value.strip() for value in values)
            or len(set(values))!=len(values)):
        raise ValueError('targeted_video_ids_invalid')
    return list(values)


def _formats(values):
    from collection_formats import checked_formats, collection_formats
    return checked_formats(collection_formats() if values is None else values)


def _deferred(row, formats, verify, run_id):
    """No lease disappearance or queue state is represented as hydration success."""
    now=row['observed_at']
    reason='admission_changed_or_locked'
    if row['channel_id'] is None: reason='queue_missing'
    elif row['video_type'] is not None and row['video_type'] not in formats: reason='format_turned_off'
    elif row['initial_pending']: reason='initial_owner'
    elif row['paused_reason']: reason=row['paused_reason']
    elif row['lease_token'] is not None and row['lease_expires_at'] is not None and row['lease_expires_at']>now: reason='video_owner_busy'
    elif str(row['last_claim_run_id'])==run_id: reason='already_attempted_in_run'
    elif not verify and row['discovery_hydration_requested_at'] is None: reason='no_initial_hydration_demand'
    elif row['next_due_at'] is not None and row['next_due_at']>now and (not verify or row['last_error_type']
            or row['availability_first_missing_at'] is not None or row['availability_resume_observed_at'] is not None):
        reason='canonical_retry_wait'
    # A healthy normal cadence is not the discovery deadline. A concurrent
    # owner delays this verification only until its lease; an advisory lock or
    # same-pass claim is retried by the discovery policy, not days later.
    next_at=(row['lease_expires_at'] if reason=='video_owner_busy'
             else row['next_due_at'] if reason=='canonical_retry_wait' else None)
    return dict(video_id=row['video_id'],reason=reason,next_eligible_at=next_at,
                owner_job_id=str(row['initial_job_id']) if reason=='initial_owner' and row['initial_job_id'] else None)


def claim_targeted_batch(database, video_ids, *, worker_id, run_id, lease_seconds=300,
                         video_types=None, verify=True, transaction=None):
    ids,formats=_ids(video_ids),_formats(video_types)
    if (type(verify) is not bool or type(worker_id) is not str or not worker_id.strip()
            or type(run_id) is not str or str(UUID(run_id))!=run_id
            or type(lease_seconds) is not int or not 30<=lease_seconds<=3600):
        raise ValueError('targeted_video_claim_invalid')
    if not ids:return dict(rows=[],deferred=[],inspected_count=0,paused_count=0,scan_exhausted=True)
    args=dict(ids=ids,formats=list(formats),worker=worker_id.strip(),run_id=run_id,
              lease_seconds=lease_seconds,video_type=None,limit=len(ids),verify=verify)
    with scheduled._transaction(database,transaction,'claim_due_video_collection_batch') as tx:
        states=tx.fetch_all(_LOCK,args,max_rows=len(ids))
        if len({state['video_id'] for state in states})!=len(states) or any(state['video_id'] not in ids for state in states):
            raise DatabaseError('database_result_shape_invalid')
        eligible,facts,paused=scheduled._inspect_prefix(tx,states,args,typed=False,limit=len(ids))
        # Only admitted healthy rows are promoted. Identity/lifecycle requests
        # keep their modes, and existing availability/recovery clocks survive.
        promote=[state for state in eligible if verify and facts[state['video_id']]['collection_mode']=='counters'
                 and state['availability_first_missing_at'] is None and state['availability_resume_observed_at'] is None]
        if promote:
            scheduled._rows_by_id(tx.returning_all(_PROMOTE,([state['video_id'] for state in promote],),max_rows=len(promote)),promote)
            for state in promote:facts[state['video_id']]['collection_mode']='metadata'
        claimed=scheduled._grant(tx,eligible,args,typed=False,mode_facts=facts)
        claimed_ids={item['value']['video_id'] for item in claimed}
        pending=[identity for identity in ids if identity not in claimed_ids]
        statuses=tx.fetch_all(_STATUS,(pending,),max_rows=len(pending)) if pending else []
        if {row['video_id'] for row in statuses}!=set(pending):raise DatabaseError('database_result_shape_invalid')
        scopes=[dict(channel_id=item['value']['channel_id'],control_epoch=item['epoch']) for item in claimed]
        assert_channel_scopes(tx,list({(s['channel_id'],s['control_epoch']):s for s in scopes}.values()))
        return dict(rows=[item['value'] for item in claimed],
                    deferred=[_deferred(row,formats,verify,run_id) for row in statuses],
                    inspected_count=len(ids),paused_count=paused,scan_exhausted=True)


def initial_hydration_candidates(database, *, limit=50, after=None, video_types=None):
    """Bounded keyset over the durable marker, independent of ordinary stats EOF."""
    if type(limit) is not int or not 1<=limit<=50 or (after is not None and
            (not isinstance(after,(list,tuple)) or len(after)!=2 or type(after[1]) is not str)):
        raise ValueError('initial_hydration_candidates_invalid')
    args=dict(limit=limit,formats=list(_formats(video_types)),after_at=None if after is None else after[0],
              after_id=None if after is None else after[1])
    with database.transaction(Operation('common_video_candidates',read_only=True,disable_jit=True,
            statement_timeout_ms=5000,lock_timeout_ms=1000,total_timeout_ms=8000)) as tx:
        rows=tx.fetch_all(_TAIL,args,max_rows=limit)
    return dict(video_ids=[row['video_id'] for row in rows],
                next_after=(rows[-1]['requested_at'],rows[-1]['video_id']) if rows else after,
                scan_exhausted=len(rows)<limit)


def release_discovery_admission(database, claims):
    """Release only the original video leases, including after a lost fill acknowledgement.

    A discovery-lane budget does not postpone the ordinary queue's deadlines.
    Already settled/replaced tokens are untouched; locks prove no original
    token remains, including after a partial metadata continuation.
    """
    if not isinstance(claims, (list, tuple)) or len(claims)>50:
        raise ValueError('discovery_release_claims_invalid')
    rows=[]
    for claim in claims:
        if claim.video_type not in ('long','short','stream','unknown'):
            raise ValueError('discovery_release_claims_invalid')
        rows.append(dict(video_id=claim.video_id,video_type=claim.video_type,
                         lease_token=str(UUID(claim.lease_token))))
    _ids([row['video_id'] for row in rows])
    if not rows:return 0
    relation=''' FROM public.video_collection_state state
      JOIN jsonb_to_recordset(%s::jsonb) input(video_id text,video_type text,lease_token uuid)
      ON state.video_id=input.video_id AND state.video_type IS NOT DISTINCT FROM nullif(input.video_type,'unknown')
      AND state.lease_token=input.lease_token'''
    with database.transaction(Operation('discovery_video_admission_release',
            statement_timeout_ms=5000,lock_timeout_ms=1000,total_timeout_ms=8000)) as tx:
        owned=tx.fetch_all('SELECT state.video_id'+relation+' ORDER BY state.video_id FOR UPDATE OF state',
                           (Jsonb(rows),),max_rows=len(rows))
        count=tx.execute('''UPDATE public.video_collection_state state SET lease_token=NULL,lease_channel_epoch=NULL,
            lease_owner=NULL,lease_started_at=NULL,lease_expires_at=NULL,updated_at=clock_timestamp()
            FROM jsonb_to_recordset(%s::jsonb) input(video_id text,video_type text,lease_token uuid)
            WHERE state.video_id=input.video_id AND state.video_type IS NOT DISTINCT FROM nullif(input.video_type,'unknown')
              AND state.lease_token=input.lease_token''',(Jsonb(rows),))
        if count!=len(owned) or tx.fetch_all('SELECT state.video_id'+relation,(Jsonb(rows),),max_rows=len(rows)):
            raise DatabaseError('discovery_video_admission_release_unconfirmed')
    return count
