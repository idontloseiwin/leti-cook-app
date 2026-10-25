"""Manual, bounded synthetic PG17 measurements; never an installation benchmark.

The parent runner owns a new loopback cluster, its time/disk budget and cleanup.
No DSN argument, provider calls, schema changes or runtime flags are accepted.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from functools import cmp_to_key
from pathlib import Path
import argparse
import concurrent.futures
import json
import heapq
import math
import os
import platform
import statistics
import sys
import threading
import time
import tracemalloc
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'application'), str(ROOT/'tests')]
import psycopg
from db.base.postgres import PostgresConfig, PostgresDatabase
from db.reads.research_query import ResearchQueryRepository
from db.reads.research_sql import compile_query
from research_query.service import QueryService
from research_query.validation import AccessContext, QueryRejected, canonical, validate
from research_query_pg import bootstrap
from research_query_oracle import compare, condition
from test_research_query_contract import query

SIZES = {'pilot': (10, 300, 300), 'S': (100, 10_000, 100_000), 'M': (1000, 250_000, 1_000_000)}
FOLDER = '77777777-7777-4777-8777-777777777777'
EMPTY = '88888888-8888-4888-8888-888888888888'
NOW = datetime(2026, 10, 25, tzinfo=timezone.utc)
TRACE = ContextVar('synthetic_measurement', default=None)

# Fixed before evaluating the candidate. These are local engineering targets,
# not an installation-wide SLA and not evidence until measurements pass them.
TARGETS = {'S_warm_p95_ms':750,'M_warm_p95_ms':2500,'no_query_timeout':True,
           'correctness_equal':True,'python_peak_bytes':2*1024*1024,
           'write_median_regression_ratio':1.25}


def generated_row(i, channels, now=NOW):
    # Half the rows belong to ten large channels; the rest span small channels.
    channel = i % 10 if i % 2 == 0 else (i * 17) % channels
    kind = ('long', 'short', 'stream')[i % 3]
    states = {}
    if i % 101 == 0: states['video.title'] = 'expired'
    if i % 103 == 0: states['video.views'] = 'expired'
    row = dict(id=f'z{i:010}', channel=channel, channel_id='UC'+f'{channel:022}', type=kind,
               title=None if i % 97 == 0 else ('Straße 測試 '+str(i) if i % 11 == 0 else 'Synthetic video '+str(i % 100)),
               views=None if i % 89 == 0 else str((i * 7919) % 10000),
               duration_seconds=str(i % 5000), states=states,
               published_at=(now-timedelta(days=2+i % 365)).isoformat(),
               # Long/stream overlap stays unresolved. A short/long overlap may
               # legitimately resolve by the existing duration policy.
               proof='unresolved' if i % 211 == 0 and kind != 'short' else 'resolved',
               availability='unknown' if i % 223 == 0 else 'public',
               hidden=channel % 47 == 46)
    if kind == 'stream': row['stream_completion'] = 'missing' if i % 227 == 0 else 'proven'
    return row


def oracle(q, channels, videos, now, offset=0):
    rows = (generated_row(i, channels, now) for i in range(videos))
    rows = (r for r in rows if not r['hidden'] and r['type'] == q['video_types'][0]
            and (q['scope']['kind'] != 'channel' or r['channel_id'] == q['scope']['id'])
            and r['proof'] == 'resolved' and r['availability'] == 'public'
            and (r['type'] != 'stream' or (r['stream_completion'] == 'proven' and not r['states']))
            and condition(q.get('where'), r) is True)
    rows = heapq.nsmallest(offset+q['page']['size'], rows,
                          key=cmp_to_key(lambda a,b: compare(a,b,q['order_by'])))
    return [r['id'] for r in rows[offset:offset+q['page']['size']]]


def summarize(values):
    if not values: return {'n': 0}
    ordered = sorted(values)
    return dict(n=len(values), median_ms=round(statistics.median(values),3),
                max_ms=round(max(values),3), min_ms=round(min(values),3),
                p95_ms=round(ordered[math.ceil(.95*len(ordered))-1],3) if len(ordered)>=20 else None)


@contextmanager
def observe(operation, phase='component', **kwargs):
    start = time.perf_counter()
    try: yield
    finally:
        sink=TRACE.get()
        if sink is not None:
            sink.append({'operation':operation, 'phase':phase, 'ms':(time.perf_counter()-start)*1000})


def populate(admin, size):
    channels, videos, observations=SIZES[size]
    now=admin.execute('SELECT clock_timestamp()').fetchone()[0]
    admin.execute('INSERT INTO public.folders(id,name) VALUES(%s,%s),(%s,%s)',
                  (FOLDER,'Synthetic benchmark',EMPTY,'Empty fixture'))
    with admin.transaction():
        with admin.cursor().copy('COPY public.channels(channel_id,user_work_id,folder_id,added_at) FROM STDIN') as cp:
            for i in range(channels): cp.write_row(('UC'+f'{i:022}',uuid.UUID(int=i+1),FOLDER,now))
        with admin.cursor().copy('COPY public.channel_collection_state(channel_id,user_work_id,owner_permission,is_hidden,control_epoch) FROM STDIN') as cp:
            for i in range(channels): cp.write_row(('UC'+f'{i:022}',uuid.UUID(int=i+1),'legacy_hold' if i%43==42 else 'accepted',i%47==46,1))
        for kind,prefix in [('long','UULF'),('short','UUSH'),('stream','UULV')]:
            records=[(uuid.uuid5(uuid.NAMESPACE_OID,kind+str(i)),'UC'+f'{i:022}',kind,prefix+f'{i:022}') for i in range(channels)]
            with admin.cursor().copy('COPY public.channel_video_inventory_scans(scan_id,channel_id,video_type,playlist_id,scan_kind,status,coverage,scan_boundary_at,started_at,source_checked_at,completed_at) FROM STDIN') as cp:
                for r in records: cp.write_row((*r,'initial_inventory','completed','complete',*[now-timedelta(hours=1)]*4))
            with admin.cursor().copy('COPY public.channel_playlist_state(channel_id,video_type,playlist_id,inventory_status,last_completed_scan_id,source_checked_at,inventory_completed_at) FROM STDIN') as cp:
                for i,r in enumerate(records): cp.write_row((*r[1:],'not_started' if i%41==40 else 'completed',None if i%41==40 else r[0],now-timedelta(hours=1),now-timedelta(hours=1)))
    # Bounded batches keep fixture generation memory independent of total size.
    for start in range(0,videos,5000):
        rows=[generated_row(i,channels,now) for i in range(start,min(start+5000,videos))]
        with admin.transaction():
            with admin.cursor().copy('COPY public.video_inventory(video_id,channel_id,video_type) FROM STDIN') as cp:
                for r in rows: cp.write_row((r['id'],r['channel_id'],r['type']))
            with admin.cursor().copy('COPY public.video_inventory_memberships(video_id,channel_id,video_type,playlist_id,source_published_at,first_seen_at,last_seen_at,last_present_at,evidence_source) FROM STDIN') as cp:
                for r in rows:
                    for kind in [r['type']]+(['stream' if r['type']=='long' else 'long'] if r['proof']=='unresolved' else []):
                        cp.write_row((r['id'],r['channel_id'],kind,{'long':'UULF','short':'UUSH','stream':'UULV'}[kind]+r['channel_id'][2:],r['published_at'],*[now-timedelta(hours=1)]*3,'legacy_inventory_backfill'))
            with admin.cursor().copy('COPY public.videos(video_id,channel_id,title,views,checked_at,metadata_checked_at,published_at,duration,availability_status,availability_checked_at,stream_actual_end_at) FROM STDIN') as cp:
                for r in rows:
                    counter=now-timedelta(hours=700 if r['states'].get('video.views')=='expired' else 1)
                    metadata=now-timedelta(hours=700 if r['states'].get('video.title')=='expired' else 1)
                    # Completed streams with stale families are intentionally not admitted.
                    end=now-timedelta(hours=701) if r.get('stream_completion')=='proven' else None
                    cp.write_row((r['id'],r['channel_id'],r['title'],r['views'],counter,metadata,r['published_at'],'PT'+r['duration_seconds']+'S',r['availability'],now-timedelta(hours=1),end))
    for start in range(0,observations,10000):
        with admin.cursor().copy('COPY public.video_snapshots(video_id,views,recorded_at) FROM STDIN') as cp:
            for i in range(start,min(start+10000,observations)):
                cp.write_row((f'z{i%videos:010}',i%10000,now-timedelta(hours=700 if i%20==0 else 1+i//videos)))
    admin.execute('ANALYZE')
    return now


def workloads():
    base=query(scope={'kind':'folder','id':FOLDER,'include_descendants':False},page={'size':25},
               select=['video.id','video.title','video.views','video.published_at','video.duration_seconds'])
    return {
        'folder_long':base,
        'folder_short':{**base,'video_types':['short']},
        'folder_stream':{**base,'video_types':['stream']},
        'channel_small':{**base,'scope':{'kind':'channel','id':'UC'+f'{17:022}'}},
        'title_selective':{**base,'where':{'field':'video.title','op':'contains_casefold_v1','value':'strasse'}},
        'title_one_char':{**base,'where':{'field':'video.title','op':'contains_casefold_v1','value':'s'}},
        'title_sort':{**base,'order_by':[{'field':'video.title','direction':'asc'}]},
        'or_not':{**base,'where':{'any':[{'field':'video.views','op':'eq','value':'0'},{'not':{'field':'video.title','op':'contains_casefold_v1','value':'synthetic'}}]}},
    }


def writes(connection, samples, count=20):
    # A synthetic update/delete load, NOT a claim to emulate the whole collector.
    with psycopg.connect(**connection) as writer:
        writer.execute("SET statement_timeout='2s'")
        for i in range(count):
            start=time.perf_counter()
            try:
                with writer.transaction():
                    writer.execute('UPDATE public.videos SET views=coalesce(views,0)+1 WHERE video_id=%s',(f'z{i+10:010}',))
                    deleted=writer.execute("DELETE FROM public.video_snapshots WHERE id IN (SELECT id FROM public.video_snapshots WHERE recorded_at<clock_timestamp()-interval '696 hours' ORDER BY id LIMIT 20)").rowcount
                samples.append({'ms':(time.perf_counter()-start)*1000,'deleted':deleted,'error':None})
            except psycopg.errors.QueryCanceled:
                samples.append({'ms':(time.perf_counter()-start)*1000,'error':'statement_timeout'})
            time.sleep(.05)


def http_measurements(database, connection, output, q, check_budget):
    # Real loopback socket, owner middleware, HTTP/router and response lifetime.
    # Only HOT availability and fixture folder organization are supplied locally.
    output.mkdir(exist_ok=True)
    import httpx
    import socket
    import uvicorn
    from research_query_preview import create_preview_app
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    origin=f'http://127.0.0.1:{port}'
    app=create_preview_app(database,output/'owner.sqlite3',origin,[FOLDER])
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='error',access_log=False))
    events=[]
    def event(*args,**kwargs):
        if kwargs.get('duration_ms') is not None:
            events.append({'operation':args[3],'phase':kwargs.get('phase'),'ms':kwargs['duration_ms']})
    answer={'samples':[],'trace_events':events}
    with patch.dict(os.environ,LIC_RESEARCH_QUERY_PREVIEW='1'),patch('hot_runtime.require_hot_runtime_ready',lambda **kw:None),patch('telemetry.is_enabled',lambda:True),patch('telemetry.emit_event',event):
        thread=threading.Thread(target=server.run,daemon=True);thread.start()
        try:
            stop=time.monotonic()+10
            while not server.started:
                if time.monotonic()>stop: raise RuntimeError('fixture_http_start_timeout')
                time.sleep(.02)
            with httpx.Client(base_url=origin,headers={'Origin':origin},timeout=12) as client:
                for _ in range(5):
                    check_budget();begin=time.perf_counter()
                    response=client.post('/research/queries/execute',json=q)
                    received=time.perf_counter(); decoded=response.json();parsed=time.perf_counter()
                    answer['samples'].append({'http_ms':(received-begin)*1000,'json_decode_ms':(parsed-received)*1000,
                        'status':response.status_code,'bytes':len(response.content),'rows':len(decoded.get('rows',[]))})
        finally:
            server.should_exit=True;thread.join(timeout=15)
            if thread.is_alive(): raise RuntimeError('fixture_http_stop_timeout')
    return answer


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--size',choices=SIZES,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--seconds',type=int,default=240)
    args=parser.parse_args()
    if os.environ.get('LIC_RESEARCH_QUERY_DISPOSABLE')!='1': raise RuntimeError('disposable_cluster_required')
    if not 10<=args.seconds<=1100: raise RuntimeError('bounded_run_required')
    args.output.mkdir(exist_ok=False)
    started=time.monotonic(); deadline=started+args.seconds
    password=os.environ['LIC_QUERY_TEST_PASSWORD']; port=int(os.environ['LIC_QUERY_TEST_PORT'])
    connection=dict(host='127.0.0.1',port=port,dbname='leti-cook',user='postgres',password=password,autocommit=True)
    report={'size':args.size,'input_counts':SIZES[args.size],'samples':[], 'workloads':{}, 'plans':{},
            'condition':'freshly loaded and ANALYZE; OS cache not evicted; first call separate from warm',
            'limits':{'seconds':args.seconds,'page':25,'pool':4,'statement_ms':5000},
            'runtime':{'python':platform.python_version(),'platform':platform.platform(),'cpu_count':os.cpu_count()},
            'provider_calls':0,'production_accessed':False,'schema_changed':False}
    (args.output/'predeclared-targets.json').write_text(json.dumps(TARGETS,indent=2))
    def save():
        report['elapsed_seconds']=round(time.monotonic()-started,3)
        (args.output/'result.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    def check_budget():
        if time.monotonic()>deadline-10: raise TimeoutError('measurement_budget_exhausted')
    with psycopg.connect(**connection) as admin:
        version=int(admin.execute('SHOW server_version_num').fetchone()[0])
        if not 170000<=version<180000: raise RuntimeError('pg17_required')
        report['runtime']['postgres']=version
        bootstrap(admin)
        now=populate(admin,args.size)
        report['setup_seconds']=round(time.monotonic()-started,3)
        report['db_bytes']=admin.execute("SELECT pg_database_size(current_database())").fetchone()[0]
        report['settings']=dict(admin.execute("SELECT name,setting FROM pg_settings WHERE name IN ('shared_buffers','work_mem','jit','max_connections','temp_file_limit')").fetchall())
        report['indexes']=[list(r) for r in admin.execute("SELECT tablename,indexname FROM pg_indexes WHERE schemaname='public' AND tablename IN ('videos','video_inventory','video_inventory_memberships','channel_video_inventory_scans','video_snapshots') ORDER BY 1,2").fetchall()]
        dsn=f'host=127.0.0.1 port={port} dbname=leti-cook user=authenticator password={password}'
        db=PostgresDatabase(PostgresConfig(dsn,max_size=4),admission=lambda _:None).open()
        context=AccessContext('synthetic-measurement','owner-1',enabled=True)
        service=QueryService(ResearchQueryRepository(db),lambda:context)
        def sample(name,q):
            spans=[]; token=TRACE.set(spans); begin=time.perf_counter()
            result=None; error=None
            try: result=service.execute(canonical(q))
            except QueryRejected as e: error=str(e)
            finally: TRACE.reset(token)
            elapsed=(time.perf_counter()-begin)*1000
            record={'case':name,'ms':round(elapsed,3),'error':error,'spans':spans,
                    'bytes':len(canonical(result)) if result else 0,'rows':len(result['rows']) if result else 0}
            with lock: report['samples'].append(record)
            return result,record
        lock=threading.Lock()
        try:
            with patch('ui_read_trace.is_active',lambda:True),patch('ui_read_trace.scope',observe):
                for name,q in workloads().items():
                    check_budget()
                    result,first=sample(name,q)
                    case={'first_ms':first['ms'],'first_error':first['error']}
                    report['workloads'][name]=case
                    if result:
                        if args.size in SIZES:
                            # Independent model; streaming top-k keeps fixture memory bounded.
                            expected=oracle(q,*SIZES[args.size][:2],now)
                            actual=[r['id'] for r in result['rows']]
                            case['oracle_ids_equal']=actual==expected
                            if actual!=expected:
                                case['expected']=expected;case['actual']=actual
                                raise AssertionError('independent_oracle_mismatch:'+name)
                        case['coverage']=result['coverage']
                        warm=[]; rejected=[]
                        repeats=20 if name=='folder_long' else 3
                        if args.size=='pilot': repeats=1
                        for _ in range(repeats):
                            check_budget()
                            _,r=sample(name,q)
                            if r['error']:
                                rejected.append({'code':r['error'],'ms':r['ms']});break
                            warm.append(r['ms'])
                        case['warm']=summarize(warm)
                        case['warm_rejections']=rejected
                    save(); print(json.dumps({'size':args.size,'case':name,**case}),flush=True)
                check_budget()
                tracemalloc.start();cpu=time.process_time()
                _,r=sample('python_memory_probe',workloads()['folder_long'])
                report['python_memory_probe']={'peak_traced_bytes':tracemalloc.get_traced_memory()[1],
                    'python_cpu_ms':(time.process_time()-cpu)*1000,'error':r['error'],'note':'separate instrumented sample; excludes PostgreSQL and fixture generator memory'}
                tracemalloc.stop()
                # Sequential cursor traversal (bounded at eight pages), not OFFSET.
                q=deepcopy(workloads()['folder_long']); ids=[]; pages=0; deep=[]
                for _ in range(8 if args.size!='pilot' else 3):
                    check_budget(); result,r=sample('deep_page',q)
                    if not result: break
                    pages+=1;deep.append(r['ms']);ids.extend(v['id'] for v in result['rows'])
                    if not result['page']['has_more']: break
                    q['page']['after']=result['page']['next_cursor']
                report['deep_pages']={'pages':pages,'rows':len(ids),'duplicates':len(ids)-len(set(ids)),'latency':summarize(deep)}
                save()
                for workers in (1,4,8):
                    check_budget()
                    # Three waves only. Do not call their maximum a p95.
                    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                        for _ in range(3 if args.size!='pilot' else 1):
                            check_budget()
                            list(pool.map(lambda _:sample(f'concurrent_{workers}',workloads()['folder_long']),range(workers)))
                    save()
                check_budget()
                baseline=[];writes(connection,baseline)
                during=[]
                with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
                    writer=pool.submit(writes,connection,during)
                    readers=[pool.submit(sample,'with_writes',workloads()['folder_long']) for _ in range(4)]
                    for future in readers: future.result()
                    writer.result()
                report['synthetic_writes']={'idle':baseline,'with_four_readers':during,
                    'idle_summary':summarize([r['ms'] for r in baseline]),'concurrent_summary':summarize([r['ms'] for r in during]),
                    'note':'fixed small counter updates and expired snapshot deletes, not collector/retention RPC acceptance'}
                save()
            check_budget()
            report['http']=http_measurements(db,connection,args.output,workloads()['folder_long'],check_budget)
            save()
            # EXPLAIN executes under the same role, timeout and isolation; plans
            # include server-only SQL time, NOT the same sample as gateway spans.
            for name in ('folder_long','channel_small','title_selective'):
                check_budget()
                statement,params=compile_query(validate(canonical(workloads()[name]),context))
                try:
                    with admin.transaction():
                        admin.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
                        admin.execute('SET LOCAL ROLE lic_reader')
                        admin.execute('SET LOCAL row_security=on')
                        admin.execute("SET LOCAL statement_timeout='5s'")
                        plan=admin.execute('EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) '+statement,params).fetchone()[0]
                    (args.output/(name+'-plan.json')).write_text(json.dumps(plan,indent=2,default=str))
                    report['plans'][name]={'execution_ms':plan[0]['Execution Time'],'planning_ms':plan[0]['Planning Time']}
                except psycopg.errors.QueryCanceled: report['plans'][name]={'error':'statement_timeout_5s'}
                save()
        except TimeoutError:
            report['budget_exhausted']=True
        finally:
            db.close();save()
    print(json.dumps({'size':args.size,'elapsed':report['elapsed_seconds'],'db_bytes':report.get('db_bytes')}),flush=True)


if __name__=='__main__': main()
