"""Enqueue/estimate protocols under existing direct grants.

Python eligibility and initialization share the owner transaction; intrinsic
job/target source-scope guards remain authoritative. The estimator reads one
native snapshot, retaining PostgreSQL JSONB/count/clock semantics and computing
only the bounded three-format arithmetic in Python.
"""
from ..channel.channel_read_policy import (CHANNEL_CONTROL_FACTS_SQL, channel_mode_row)
from contextlib import contextmanager
from psycopg.types.json import Jsonb
from ..channel.channel_control import assert_channel_scope
from ..base.operation_contracts import get_operation
from ..channel.channel_workflow_updates import apply_channel_stage
from ..base.postgres import DatabaseServerError,Operation
from ..base.repository import DatabaseRepository
from .operator_job_insert_eligibility import assert_operator_job_insert_eligible

_TYPES=('long','short','stream')
_RESTART_EXPRESSION="""coalesce((SELECT job.status='error' AND job.last_error_code='source_scope_expired'
 AND job.lease_token IS NULL AND job.lease_expires_at IS NULL AND isfinite(job.requested_at)
 AND job.requested_at<clock_timestamp()-interval '696 hours'
 FROM public.operator_jobs job WHERE job.channel_id=channel.channel_id AND job.job_type='channel_initial_long_inventory'
 ORDER BY job.requested_at DESC,job.id DESC LIMIT 1),false)"""
_VISIBLE="NOT coalesce((SELECT control.is_hidden FROM public.channel_collection_state control WHERE control.channel_id=channel.channel_id),true)"
_ESTIMATE="""WITH channel AS MATERIALIZED(SELECT channel.channel_id,channel.initial_inventory_estimate AS estimate,
 channel.initial_long_inventory_status,channel.video_count,
 coalesce(%(as_of)s::timestamptz,statement_timestamp()) AS at,
 """+_RESTART_EXPRESSION+""" AS restart FROM public.channels channel
 WHERE channel.channel_id=%(channel_id)s::text AND """+_VISIBLE+"""), formats AS MATERIALIZED(
 SELECT channel.*,kind,ordinal,estimate->'statuses'->>kind AS status,estimate->'counts'->kind AS value
 FROM channel CROSS JOIN unnest(ARRAY['long','short','stream']) WITH ORDINALITY k(kind,ordinal)),
 numbers AS MATERIALIZED(SELECT *,CASE WHEN status='ok' AND jsonb_typeof(estimate)='object'
 AND jsonb_typeof(value)='number' AND value::text ~ '^[0-9]{1,10}$' THEN value::text::bigint END AS number FROM formats),
 clocks AS MATERIALIZED(SELECT *,CASE WHEN number<=2147483647 THEN
 CASE WHEN estimate ? 'checked_at_by_type' THEN CASE WHEN jsonb_typeof(estimate->'checked_at_by_type')='object'
 THEN estimate->'checked_at_by_type'->>kind END ELSE estimate->>'checked_at' END
 WHEN status='playlist_not_found' THEN estimate->'checked_at_by_type'->>kind END AS clock_text FROM numbers),
 diagnosed AS MATERIALIZED(SELECT *,(pg_input_error_info(clock_text,'timestamptz')).sql_error_code AS error_code FROM clocks),
 checked AS MATERIALIZED(SELECT *,CASE WHEN error_code IS NULL THEN clock_text::timestamptz END AS clock FROM diagnosed),
 facts AS(SELECT ordinal,kind,status,CASE WHEN number<=2147483647 AND clock IS NOT NULL AND isfinite(clock)
 AND clock<=at AND clock>=at-interval '696 hours' THEN number::integer END AS count,
 coalesce(status='playlist_not_found' AND clock IS NOT NULL AND isfinite(clock)
 AND clock<=at AND clock>=at-interval '696 hours',false) AS absent,
 CASE WHEN error_code IN('22007','22008') OR (status='ok' AND error_code='22003') THEN NULL ELSE error_code END AS error
 FROM checked)
 SELECT channel.channel_id,channel.restart,channel.video_count,
 channel.restart OR channel.initial_long_inventory_status IN('manual_required','error','pending') AS enqueue_allowed,
 (SELECT jsonb_agg(to_jsonb(facts) ORDER BY ordinal) FROM facts) AS facts FROM channel"""
# The public estimate reads admitted fields; enqueue retains the locked raw snapshot.
_ESTIMATE_READ = (_ESTIMATE
 .replace(_RESTART_EXPRESSION, "coalesce((SELECT restart_initial_scope FROM readable.channel_control WHERE channel_id=channel.channel_id),false)")
 .replace("FROM public.channels channel", "FROM readable.channels channel")
 .replace("FROM public.channel_collection_state control", "FROM readable.channel_control control")
 .replace("channel.video_count,\n", "channel.video_count,channel.source_expires_at,\n"))

_STATS_INPUT="""SELECT %s::text AS channel_id,%s::integer AS max_items,%s::integer AS max_quota_units,
 %s::integer AS max_target_attempts,%s::bigint AS expected_epoch"""
_INITIAL_INPUT="""SELECT %s::text AS channel_id,%s::integer AS expected_items,%s::integer AS expected_pages,
 %s::integer AS expected_quota_units,%s::integer AS max_items,%s::integer AS max_pages,%s::integer AS max_quota_units,
 %s::integer AS max_attempts,%s::boolean AS confirmed,%s::bigint AS expected_epoch,
 to_jsonb(statement_timestamp())#>>'{}' AS as_of"""
_CHANNEL="SELECT channel.channel_id FROM public.channels channel WHERE channel.channel_id=%s::text AND "+_VISIBLE+" FOR UPDATE"
_RESTART="SELECT "+_RESTART_EXPRESSION+" AS restart FROM public.channels channel WHERE channel_id=%s::text"
_MODE=('SELECT (SELECT policy_facts FROM (' + CHANNEL_CONTROL_FACTS_SQL + ') policy WHERE channel_id=%s::text) AS policy_facts')
_STALE="""UPDATE public.operator_jobs stale SET status='suspended',finished_at=coalesce(stale.finished_at,now()),
 lease_token=NULL,lease_channel_epoch=NULL,lease_owner=NULL,lease_expires_at=NULL,
 last_error_code='channel_sync_not_ready',last_error_summary='Channel control generation changed.'
 WHERE stale.channel_id=%(channel_id)s::text AND stale.status IN('queued','running')
 AND stale.accepted_epoch IS DISTINCT FROM %(expected_epoch)s::bigint"""
_EXISTING="SELECT to_jsonb(job) AS job FROM public.operator_jobs job WHERE channel_id=%s::text AND status IN('queued','running') LIMIT 1"
_INSERT_STATS="""INSERT INTO public.operator_jobs(job_type,channel_id,status,phase,target_count,max_items,max_quota_units,
 quota_units_estimated,max_target_attempts,finished_at) VALUES('channel_current_stats_backfill',%(channel_id)s::text,
 'completed','video_targets',0,coalesce(%(max_items)s::integer,1),%(max_quota_units)s::integer,0,
 %(max_target_attempts)s::integer,now()) RETURNING id::text AS id"""
_TARGETS="""INSERT INTO public.operator_job_targets(job_id,video_id)
 SELECT %s::uuid,inventory.video_id FROM public.video_inventory inventory
 WHERE inventory.channel_id=%s::text AND inventory.video_type='long' ON CONFLICT(job_id,video_id) DO NOTHING"""
_COUNTS="SELECT %s::integer AS target_count,2*((%s::integer+49)/50) AS quota"
_SETTLE_STATS="""UPDATE public.operator_jobs SET target_count=%(target_count)s::integer,
 max_items=coalesce(%(max_items)s::integer,greatest(%(target_count)s::integer,1)),quota_units_estimated=%(quota)s::integer,
 status=CASE WHEN %(target_count)s::integer=0 THEN 'completed' ELSE 'queued' END,
 finished_at=CASE WHEN %(target_count)s::integer=0 THEN now() ELSE NULL END WHERE id=%(id)s::uuid"""
_INSERT_INITIAL="""INSERT INTO public.operator_jobs(job_type,channel_id,status,phase,target_count,max_items,max_pages,
 max_quota_units,quota_units_estimated,max_target_attempts,initial_video_types,initial_scope_version,summary_json)
 VALUES('channel_initial_long_inventory',%(channel_id)s::text,'queued','inventory',0,NULL,NULL,NULL,
 coalesce(%(quota)s::integer,0),%(max_attempts)s::integer,ARRAY['long','short','stream'],1,%(summary)s::jsonb)
 RETURNING id::text AS id"""
_PENDING="""UPDATE public.channels SET initial_long_inventory_status='pending',initial_long_inventory_started_at=NULL,
 initial_28d_stats_status='pending' WHERE channel_id=%s::text"""
_PREPARE_INPUT="SELECT btrim(%s::text) AS channel_id,length(%s::text) AS length"
_PREPARE="""INSERT INTO public.channel_playlist_state(channel_id,video_type,playlist_id,inventory_status,
 product_exposure_ready,runtime_enabled,status,next_due_at)
 VALUES(%s::text,%s::text,%s::text,'pending',false,true,'ready',now()) ON CONFLICT(channel_id,video_type) DO NOTHING"""
_ACCEPT_JOB="""SELECT job_type,lease_token IS NOT NULL OR lease_owner IS NOT NULL OR lease_expires_at IS NOT NULL AS leased
 FROM public.operator_jobs WHERE id=%(id)s::uuid AND channel_id=%(channel_id)s::text FOR UPDATE"""
_ACCEPT_CONTROL="""SELECT is_hidden,control_epoch,owner_permission FROM public.channel_collection_state
 WHERE channel_id=%s::text FOR UPDATE"""
_ACCEPT="""UPDATE public.channel_collection_state SET owner_permission='accepted',control_epoch=
 CASE WHEN owner_permission='accepted' THEN control_epoch ELSE nextval('public.channel_collection_epoch_seq') END,
 initial_job_id=CASE WHEN %(job_type)s::text='channel_initial_long_inventory' THEN %(id)s::uuid ELSE initial_job_id END
 WHERE channel_id=%(channel_id)s::text RETURNING control_epoch"""
_ACCEPT_EPOCH="UPDATE public.operator_jobs SET accepted_epoch=%s::bigint,lease_channel_epoch=NULL WHERE id=%s::uuid"
_JOB="SELECT to_jsonb(job) AS job FROM public.operator_jobs job WHERE id=%s::uuid"


def _fail(code):raise DatabaseServerError(code,code='P0001')


@contextmanager
def _transaction(database,transaction,name):
    if transaction is not None:
        with transaction.borrow():yield transaction
    else:
        spec=get_operation(name)
        with database.transaction(Operation(name,read_only=spec.read_only,statement_timeout_ms=spec.statement_timeout_ms,
                lock_timeout_ms=spec.lock_timeout_ms,total_timeout_ms=spec.statement_timeout_ms,error_codes=spec.error_codes)) as tx:yield tx


def _estimate(tx,channel_id,as_of=None,*,read_only=False):
    captured=tx.fetch_one(_ESTIMATE_READ if read_only else _ESTIMATE,dict(channel_id=channel_id,as_of=as_of))
    if captured is None:_fail('operator_job_channel_not_visible')
    if read_only:DatabaseRepository(None).admit_source_deadlines([captured])
    counts={}
    statuses={}
    lower=upper=pages=0
    known=True
    for fact in captured['facts']:
        if fact['error'] is not None:raise DatabaseServerError('database_operation_failed',code=fact['error'])
        kind,n=fact['kind'],fact['count']
        counts[kind]=n
        statuses[kind]='ok' if n is not None else 'playlist_not_found' if fact['status']=='playlist_not_found' else 'error'
        if fact['absent']:pages+=1
        elif n is None:known=False
        else:lower=max(lower,n);upper+=n;pages+=max(1,(n+49)//50)
    if upper>2147483647 or pages>2147483647 or pages+((upper+49)//50)>2147483647:known=False
    if not known:upper=pages=hydration=quota=None
    else:hydration=(upper+49)//50;quota=pages+hydration
    restart=captured['restart']
    total=captured['video_count'] or 0
    return dict(channel_id=captured['channel_id'],scope='all',video_types=list(_TYPES),initial_scope_version=1,
        counts=counts,statuses=statuses,estimate_kind='upper_bound' if known else 'unknown',items_lower_bound=lower,
        items_upper_bound=upper,estimated_items=upper,estimated_pages=pages,estimated_hydration_units=hydration,
        estimated_quota_units=quota,enqueue_allowed=captured['enqueue_allowed'],
        auto_hydration_allowed=not restart and known and upper<=10000 and total<=10000,
        confirmation_required=restart or not known or upper>10000 or total>10000,quota_estimate_is_upper_bound=known)


def estimate_channel_initial_inventory_job(database,channel_id,*,transaction=None):
    with _transaction(database,transaction,'estimate_channel_initial_inventory_job') as tx:
        return _estimate(tx,channel_id,read_only=transaction is None)


def _accept(tx,values):
    # Channel owner is held by enqueue; child job precedes exclusive control.
    job=tx.fetch_one(_ACCEPT_JOB,values)
    if job is None or job['job_type'] not in ('channel_initial_long_inventory','channel_current_stats_backfill') or job['leased']:
        _fail('operator_job_lease_lost')
    control=tx.fetch_one(_ACCEPT_CONTROL,(values['channel_id'],))
    if control is None or control['is_hidden'] or control['control_epoch']!=values['expected_epoch']:_fail('channel_collection_scope_lost')
    if job['job_type']=='channel_current_stats_backfill':
        if control['owner_permission']!='accepted' or channel_mode_row(tx.fetch_one(_MODE,(values['channel_id'],)),field='mode')['mode']!='active':
            _fail('operator_job_channel_sync_not_ready')
    granted=tx.returning_one(_ACCEPT,{**values,'job_type':job['job_type']})['control_epoch']
    tx.execute(_ACCEPT_EPOCH,(granted,values['id']))
    return granted


def _existing(tx,args,kind):
    tx.execute(_STALE,args)
    existing=tx.fetch_one(_EXISTING,(args['channel_id'],))
    if existing is None:return None
    if existing['job']['job_type']!=kind:_fail('operator_job_channel_has_active_job')
    assert_channel_scope(tx,args['channel_id'],args['expected_epoch'])
    return dict(created=False,job=existing['job'])


def enqueue_channel_current_stats_backfill(database,channel_id,max_items,max_quota_units,max_target_attempts=3,expected_epoch=None,*,transaction=None):
    with _transaction(database,transaction,'enqueue_channel_current_stats_backfill') as tx:
        args=tx.fetch_one(_STATS_INPUT,(channel_id,max_items,max_quota_units,max_target_attempts,expected_epoch))
        if args['expected_epoch'] is None or args['expected_epoch']<=0:_fail('channel_collection_scope_invalid')
        if args['channel_id'] is None or not args['channel_id'].strip(' '):_fail('operator_job_channel_required')
        if args['max_items'] is not None and args['max_items']<=0:_fail('operator_job_max_items_required')
        if args['max_quota_units'] is None or args['max_quota_units']<=0:_fail('operator_job_max_quota_units_required')
        if args['max_target_attempts'] is None or not 1<=args['max_target_attempts']<=20:_fail('operator_job_max_target_attempts_invalid')
        if tx.fetch_one(_CHANNEL,(args['channel_id'],)) is None:_fail('operator_job_channel_not_visible')
        existing=_existing(tx,args,'channel_current_stats_backfill')
        if existing is not None:return existing
        assert_operator_job_insert_eligible(tx,args['channel_id'],'channel_current_stats_backfill')
        args.update(tx.returning_one(_INSERT_STATS,args))
        count=tx.execute(_TARGETS,(args['id'],args['channel_id']))
        args.update(tx.fetch_one(_COUNTS,(count,count)))
        # Retained formatted cap errors are intentionally private on the wire.
        if (args['max_items'] is not None and args['target_count']>args['max_items']) or args['quota']>args['max_quota_units']:
            _fail('database_operation_failed')
        tx.execute(_SETTLE_STATS,args)
        _accept(tx,args)
        return dict(created=True,job=tx.fetch_one(_JOB,(args['id'],))['job'])


def _prepare(tx,channel_id):
    args=tx.fetch_one(_PREPARE_INPUT,(channel_id,channel_id))
    identity=args['channel_id']
    if identity is None or not identity or args['length']>128:_fail('initial_typed_channel_id_invalid')
    for kind,prefix in zip(_TYPES,('UULF','UUSH','UULV')):
        tx.execute(_PREPARE,(identity,kind,prefix+identity[2:]))


def enqueue_channel_initial_inventory_job(database,channel_id,expected_items,expected_pages,expected_quota_units,
        max_items,max_pages,max_quota_units,max_attempts=3,confirmed=False,expected_epoch=None,*,transaction=None):
    with _transaction(database,transaction,'enqueue_channel_initial_inventory_job') as tx:
        args=tx.fetch_one(_INITIAL_INPUT,(channel_id,expected_items,expected_pages,expected_quota_units,
            max_items,max_pages,max_quota_units,max_attempts,confirmed,expected_epoch))
        if args['expected_epoch'] is None or args['expected_epoch']<=0:_fail('channel_collection_scope_invalid')
        if any(args[name] is not None for name in ('max_items','max_pages','max_quota_units')):_fail('full_initial_limits_must_be_null')
        if args['max_attempts'] is None or not 1<=args['max_attempts']<=20:_fail('operator_job_max_target_attempts_invalid')
        if tx.fetch_one(_CHANNEL,(args['channel_id'],)) is None:_fail('operator_job_channel_not_visible')
        restart=tx.fetch_one(_RESTART,(args['channel_id'],))['restart']
        if restart and not args['confirmed']:_fail('operator_job_confirmation_required')
        if not restart and channel_mode_row(tx.fetch_one(_MODE,(args['channel_id'],)),field='mode')['mode'] not in ('operator_hold','initial_inventory'):
            _fail('operator_job_initial_inventory_status_not_eligible')
        existing=_existing(tx,args,'channel_initial_long_inventory')
        if existing is not None:return existing
        estimate=_estimate(tx,args['channel_id'],args['as_of'])
        if any(args['expected_'+name]!=estimate['estimated_'+name] for name in ('items','pages','quota_units')):_fail('operator_job_estimate_changed')
        if estimate['confirmation_required'] and not args['confirmed']:_fail('operator_job_confirmation_required')
        summary={key:estimate[key] for key in ('estimate_kind','items_lower_bound','items_upper_bound','estimated_items',
            'estimated_pages','estimated_hydration_units','scope','video_types','estimated_quota_units','confirmation_required')}
        summary.update(full_inventory_sources={},confirmed=bool(args['confirmed']))
        args.update(quota=estimate['estimated_quota_units'],summary=Jsonb(summary))
        assert_operator_job_insert_eligible(tx,args['channel_id'],'channel_initial_long_inventory')
        args.update(tx.returning_one(_INSERT_INITIAL,args))
        apply_channel_stage(tx,'enqueue_pending',(args['channel_id'],))
        _prepare(tx,args['channel_id'])
        _accept(tx,args)
        return dict(created=True,job=tx.fetch_one(_JOB,(args['id'],))['job'])
