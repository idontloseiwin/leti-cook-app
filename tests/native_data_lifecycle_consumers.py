"""Small PG17 source-pair / cleanup / startup / copy acceptance group.

Invoked only by the disposable native runner through run(Fixture). No network,
provider, container management, schema mutation or implicit standalone entry.
Synthetic ages are explicit; all existing integrity triggers remain enabled.
"""
from datetime import timedelta
import json
import time
from psycopg.types.json import Jsonb

from db.hot.source_clocks import SOURCE_CLOCKS
from db.hot.update_copy_sources import UPDATE_COPY_SOURCE_ROWS, _source_facts, _UNPROVED
from db.retention.inventory_chain_retention import SOURCE_FREE_JOB_SQL
from db.videos.video_queue_ensure import ensure_video_collection_state
from schema25_regressions.fixtures import CHANNEL


VIDEO = 'lifecopy001'
JOB = 'dca10000-0000-4000-8000-000000000001'


def _window(f):
    return f.database.read_update_copy_window()


def _source_diagnostics(f):
    """One aggregate row per registry source, never IDs or stored API values."""
    reports=' UNION ALL '.join(
        "SELECT '"+name+"'::text AS source,facts.* FROM ("+_source_facts(name,rows)+") facts"
        for name,rows in UPDATE_COPY_SOURCE_ROWS.items())
    query="""WITH clock AS MATERIALIZED(SELECT clock_timestamp() AS at), reports AS (
    """+reports+""") SELECT source,oldest_source_at,inclusive,invalid_source_clock,
     coalesce(isfinite(oldest_source_at) AND (oldest_source_at<clock.at-interval '696 hours'
      OR (inclusive AND oldest_source_at=clock.at-interval '696 hours')),false) AS expired
     FROM reports CROSS JOIN clock ORDER BY source"""
    return dict(sources=f.rows(query),unproved_copy=f.scalar('SELECT ('+_UNPROVED+') AS unproved'))


def _startup(f, cutoff):
    return f.database.read_startup_expiry(cutoff=cutoff, strict_entry=True)['expired']


def _channel(f):
    f.channel()
    # Synthetic profile acquisition. No source timestamp is inferred from a
    # local insertion during this fixture's copy-admission checks.
    f.rows("UPDATE public.channels SET channel_metadata_status='ok',channel_metadata_checked_at=clock_timestamp() WHERE channel_id=%s", (CHANNEL,))


def _inventory(f, *, kind='stream'):
    _channel(f)
    f.rows("""INSERT INTO public.video_inventory(video_id,channel_id,video_type,published_at,published_acquired_at)
     VALUES(%s,%s,%s,clock_timestamp()-interval '2 years',clock_timestamp()-interval '2 hours')""", (VIDEO,CHANNEL,kind))
    prefix = {'stream':'UULV','long':'UULF'}[kind]
    f.rows("""INSERT INTO public.video_inventory_memberships(video_id,channel_id,video_type,playlist_id,
     source_published_at,last_seen_at,last_present_at)
     SELECT video_id,channel_id,video_type,%s,published_at,published_acquired_at,published_acquired_at
     FROM public.video_inventory WHERE video_id=%s""", (prefix+CHANNEL[2:],VIDEO))


def _pair_shapes(f):
    for name,value in (('inventory_publication','published_at'),('queue_publication','typed_source_published_at'),
                       ('queue_stream_end','typed_stream_actual_end_at')):
        spec=SOURCE_CLOCKS[name]
        rows=UPDATE_COPY_SOURCE_ROWS[name].replace('public.'+spec.table+' source',
            'jsonb_populate_record(NULL::public.'+spec.table+',%(probe_row)s::jsonb) source')
        query='WITH clock AS MATERIALIZED(SELECT clock_timestamp() AS at) '+_source_facts(name,rows)
        for stamp in (None,'infinity','-infinity',(f.now+timedelta(days=1)).isoformat()):
            result=f.rows(query,{'probe_row':Jsonb({value:f.now.isoformat(),spec.expression:stamp})})[0]
            f.check(result['invalid_source_clock'] is True,'pair_invalid_clock_'+name)
        empty=f.rows(query,{'probe_row':Jsonb({value:None,spec.expression:None})})[0]
        f.check(empty['oldest_source_at'] is None and empty['invalid_source_clock'] is False,'empty_pair_'+name)


def _independent_copies(f):
    _inventory(f)
    # The real typed initializer requires a completed authoritative scan and
    # enabled playlist, not just a membership row. These are synthetic API
    # facts; the initializer below remains the real queue writer.
    scan='dca20000-0000-4000-8000-000000000001'
    playlist='UULV'+CHANNEL[2:]
    f.rows("""INSERT INTO public.channel_video_inventory_scans(scan_id,channel_id,video_type,playlist_id,
     scan_kind,status,coverage,completed_at,source_checked_at,pages_completed,items_seen)
     VALUES(%s,%s,'stream',%s,'full_reconciliation','completed','complete',now(),now(),1,1)""",(scan,CHANNEL,playlist))
    f.rows("""INSERT INTO public.channel_playlist_state(channel_id,video_type,playlist_id,runtime_enabled,status,
     next_due_at,inventory_status,last_completed_scan_id,inventory_completed_at,source_checked_at)
     VALUES(%s,'stream',%s,true,'ready',now(),'completed',%s,now(),now())
     ON CONFLICT(channel_id,video_type) DO UPDATE SET runtime_enabled=true,status='ready',next_due_at=now(),
     inventory_status='completed',last_completed_scan_id=excluded.last_completed_scan_id,
     inventory_completed_at=now(),source_checked_at=excluded.source_checked_at""",(CHANNEL,playlist,scan))
    f.rows("UPDATE public.typed_video_collection_runtime SET runtime_enabled=true WHERE video_type='stream'")
    changed=ensure_video_collection_state(f.database,[VIDEO],[{'channel_id':CHANNEL,'control_epoch':f.epoch()}])
    f.check(changed==1,'pending_queue_created_by_real_initializer')
    queue=f.rows('SELECT typed_source_published_at,typed_source_published_acquired_at,typed_collection_checked_at FROM public.video_collection_state WHERE video_id=%s',(VIDEO,))[0]
    f.check(queue['typed_source_published_at'] is not None and queue['typed_source_published_acquired_at'] is not None
            and queue['typed_collection_checked_at'] is None,'pending_copy_is_dated_before_hydration')
    f.check(_window(f)['invalid_source_clock'] is False,'pending_copy_admitted')
    f.rows("""UPDATE public.video_collection_state SET typed_stream_actual_end_at=clock_timestamp()-interval '1 year',
     typed_source_stream_end_acquired_at=clock_timestamp()-interval '1 hour' WHERE video_id=%s""",(VIDEO,))
    # A different, fresh membership must neither invalidate the old copy nor
    # silently renew it. Its own acquisition time remains the authority.
    f.rows("""UPDATE public.video_inventory_memberships SET source_published_at=clock_timestamp()-interval '3 years',
     last_present_at=clock_timestamp(),last_seen_at=clock_timestamp() WHERE video_id=%s""",(VIDEO,))
    f.check(_window(f)['invalid_source_clock'] is False,'witness_change_does_not_invalidate_own_clock')
    f.rows("UPDATE public.video_inventory SET published_acquired_at=clock_timestamp()-interval '30 days' WHERE video_id=%s",(VIDEO,))
    f.rows("UPDATE public.video_collection_state SET typed_source_published_acquired_at=clock_timestamp()-interval '30 days' WHERE video_id=%s",(VIDEO,))
    cutoff=f.scalar("SELECT clock_timestamp()-interval '697 hours'")
    f.check(_startup(f,cutoff) is True and _window(f)['expired'] is True,'old_copies_close_startup_and_copy')
    page=f.cleanup('inventory')
    f.check(page.scrubbed==2,'only_expired_publication_pairs_scrubbed')
    row=f.rows("""SELECT i.published_at,i.published_acquired_at,q.typed_source_published_at,
     q.typed_source_published_acquired_at,q.typed_stream_actual_end_at,q.typed_source_stream_end_acquired_at
     FROM public.video_inventory i JOIN public.video_collection_state q USING(video_id) WHERE i.video_id=%s""",(VIDEO,))[0]
    f.check(all(row[key] is None for key in ('published_at','published_acquired_at','typed_source_published_at','typed_source_published_acquired_at'))
            and row['typed_stream_actual_end_at'] is not None and row['typed_source_stream_end_acquired_at'] is not None,
            'pair_scrub_preserves_fresh_stream_end')
    window=_window(f)
    f.check(_startup(f,cutoff) is False and not window['expired'] and not window['invalid_source_clock'],
            'cleanup_reopens_startup_and_copy')


def _review_deadline(f):
    _inventory(f,kind='long')
    f.rows("""INSERT INTO public.video_type_review_cases(video_id,channel_id,state,observed_canonical_type,
     observed_types,observed_channels,evidence_expires_at,first_seen_at,last_seen_at)
     VALUES(%s,%s,'open','long',ARRAY['long','short'],ARRAY[%s],clock_timestamp()-interval '2 hours',
      clock_timestamp()-interval '60 days',clock_timestamp())""",(VIDEO,CHANNEL,CHANNEL))
    before=f.rows('SELECT first_seen_at,last_seen_at FROM public.video_type_review_cases WHERE video_id=%s',(VIDEO,))[0]
    cutoff=f.scalar("SELECT clock_timestamp()-interval '697 hours'")
    f.check(_startup(f,cutoff) is True and _window(f)['expired'] is True,'local_review_touch_does_not_renew_deadline')
    page=f.cleanup('review_evidence')
    after=f.rows("""SELECT first_seen_at,last_seen_at,evidence_expires_at,observed_canonical_type,
     cardinality(observed_types) AS types,cardinality(observed_channels) AS channels
     FROM public.video_type_review_cases WHERE video_id=%s""",(VIDEO,))[0]
    f.check(page.scrubbed==1 and after['evidence_expires_at'] is None and after['observed_canonical_type'] is None
            and after['types']==0 and after['channels']==0,'review_payload_and_deadline_scrubbed')
    f.check(all(before[k]==after[k] for k in before),'review_local_history_preserved')
    window=_window(f)
    f.check(_startup(f,cutoff) is False and not window['expired'] and not window['invalid_source_clock'],
            'review_cleanup_reopens_both_observers')


def _chain_markers(f):
    _inventory(f,kind='long')
    # The integrity trigger forbids inventing an already expired job. Create a
    # valid boundary fixture, then wait for its real deadline (under six seconds).
    with f.admin.transaction():
        f.rows("""INSERT INTO public.operator_jobs(id,job_type,channel_id,status,requested_at,phase,
         initial_scope_version,initial_video_types,summary_json)
         VALUES(%s,'channel_initial_long_inventory',%s,'completed',clock_timestamp()-interval '696 hours'+interval '5 seconds',
         'inventory',1,ARRAY['long','short','stream'],'{"inventory_chain_version":1,"full_inventory_sources":{}}'::jsonb)""",(JOB,CHANNEL))
        f.rows("""INSERT INTO public.operator_job_inventory_chains(job_id,channel_id,video_type)
         SELECT %s,%s,kind FROM unnest(ARRAY['long','short','stream']) kinds(kind)""",(JOB,CHANNEL))
        f.rows("INSERT INTO public.operator_job_targets(job_id,video_id,video_type) VALUES(%s,%s,'long')",(JOB,VIDEO))
        # Even an exact parent marker cannot hide a target row.
        f.rows("""UPDATE public.operator_jobs SET summary_json='{"source_scope_expired":true,"inventory_chain_version":1}'::jsonb,
         last_error_code='source_scope_expired' WHERE id=%s""",(JOB,))
    contract='SELECT ('+SOURCE_FREE_JOB_SQL+') AS source_free FROM public.operator_jobs source WHERE source.id=%s'
    f.check(f.scalar(contract,(JOB,)) is False,'marker_does_not_hide_targets')
    time.sleep(5.1)
    cutoff=f.scalar("SELECT clock_timestamp()-interval '696 hours'")
    f.check(_startup(f,cutoff) is True and _window(f)['expired'] is True,'retained_target_keeps_age_guard')
    f.cleanup('operator_jobs')
    f.cleanup('operator_job_targets')
    f.check(f.scalar(contract,(JOB,)) is True,'complete_parent_children_target_scrub_is_source_free')
    window=_window(f)
    f.check(_startup(f,cutoff) is False and not window['expired'] and not window['invalid_source_clock'],
            'source_free_chain_no_longer_holds_guards')
    f.check(f.scalar('SELECT count(*) FROM public.operator_job_inventory_chains WHERE job_id=%s',(JOB,))==3,
            'source_free_children_identity_preserved')


def run(f):
    failed=[]
    for case in (_pair_shapes,_independent_copies,_review_deadline,_chain_markers):
        before=f.checks
        try:
            case(f)
            f.observe()
        except Exception as error:
            failed.append(case.__name__)
            # Capture every source so a single failed gate is attributable,
            # then continue other independent cases on an exactly reset DB.
            try:
                print(json.dumps(dict(stage='consumer_case_failed',case=case.__name__,
                    error_type=type(error).__name__,error_code=str(error) if isinstance(error,AssertionError) else None,
                    diagnostics=_source_diagnostics(f)),default=str),flush=True)
            except Exception as diagnostic_error:
                print(json.dumps(dict(stage='consumer_diagnostics_failed',case=case.__name__,
                    error_type=type(diagnostic_error).__name__)),flush=True)
        else:
            print(json.dumps(dict(stage='consumer_case_passed',case=case.__name__,checks=f.checks-before)),flush=True)
        finally:
            f.reset()
    if failed:raise AssertionError('consumer_cases_failed:'+','.join(failed))
