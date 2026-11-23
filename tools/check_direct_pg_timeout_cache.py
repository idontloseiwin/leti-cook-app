#!/usr/bin/env python3
"""Opt-in timeout-cache proof on the guarded empty disposable PostgreSQL17 DB.

Read-only transactions and bounded setting reads only: no domain seed, DML,
DDL, delay or provider. One admin and one runtime connection. This is semantic
validation, not a latency measurement or authority to execute a server packet.
"""
import argparse,json,math,os,sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'tools'));sys.path.insert(0,str(ROOT/'application'))
import check_direct_pg_repositories as fixtures
from db.base import postgres as pg
safety=fixtures.safety
CASES={'contract':'Exact ceiling/integer reuse versus default-off configuration; both admission checks remain driver-owned.',
 'savepoints':'Real LOCAL rollback/release and native set_config inside rollback at a fixed driver clock.',
 'reuse':'Same pooled backend restores authenticator role and pre-transaction timeout after success and rollback.'}
TIMER="SELECT set_config('statement_timeout', %s, true)"
_SETTING="SELECT (extract(epoch FROM current_setting('statement_timeout')::interval)*1000)::integer AS value"
_SESSION="""SELECT pg_backend_pid() AS backend,session_user AS login,current_user AS role,
 (extract(epoch FROM current_setting('statement_timeout')::interval)*1000)::integer AS timeout"""
_NATIVE_CHANGE="SELECT pg_catalog.set_config('statement_timeout','777ms',true) AS value"


class RollbackFixture(Exception):pass


class CountConfiguration:
    """Observe only known client settings; never retain domain SQL/parameters."""
    def __init__(self,connection):self.connection=connection;self.values=[]
    def __getattr__(self,name):return getattr(self.connection,name)
    def execute(self,statement,parameters=()):
        result=self.connection.execute(statement,parameters)
        if statement==TIMER:self.values.append(int(parameters[0][:-2]))
        return result


@contextmanager
def transaction(probe,*,enabled=True,ceiling=1000):
    now=[10.0]
    # Replace only the driver's reference, not Python's global time module:
    # pool acquisition, psycopg and fixture deadlines retain real monotonic time.
    with patch.object(pg,'time',SimpleNamespace(monotonic=lambda:now[0])):
        operation=pg.Operation('timeout_cache_native_probe',read_only=True,
            statement_timeout_ms=ceiling,total_timeout_ms=1000,cache_statement_timeout=enabled)
        with probe.database.transaction(operation) as tx:
            counter=CountConfiguration(tx._connection);tx._connection=counter
            yield tx,now,counter


def setting(tx):return tx.fetch_one(_SETTING)['value']


def raw_one(connection,query):
    # One fixed read observes the real LOCAL rollback before the driver can
    # reconfigure it. It never mutates configuration or borrows domain rights.
    with connection.cursor() as cursor:
        cursor.execute(query,());rows=cursor.fetchmany(2)
    fixtures.require(len(rows)==1,'timeout_cache_raw_shape')
    return rows[0]


def observe(probe,name,counter):
    fixtures.require(len(counter.values)<=8 and all(type(v) is int and 1<=v<=1000 for v in counter.values),'timeout_cache_observation_bound')
    probe.cache_observations.append(dict(scenario=name,configuration_calls=len(counter.values),configured_ms=list(counter.values)))


def contract(probe):
    for enabled in (False,True):
        with transaction(probe,enabled=enabled,ceiling=500) as (tx,now,counter):
            fixtures.require(setting(tx)==setting(tx)==500,'timeout_cache_ceiling')
        probe.check('timeout_cache_default_'+str(enabled).lower(),counter.values==[500]*(1 if enabled else 3))
        observe(probe,'enabled' if enabled else 'default_off',counter)
    expected=[]
    with transaction(probe) as (tx,now,counter):
        for value in (10.0,10.00025,10.125,10.12525,10.12625):
            now[0]=value;target=min(1000,max(1,math.ceil((11.0-value)*1000)))
            if not expected or expected[-1]!=target:expected.append(target)
            fixtures.require(setting(tx)==target,'timeout_cache_exact_remaining')
    probe.check('timeout_cache_exact_integer',counter.values==expected==[1000,875,874])
    observe(probe,'exact_integer',counter)


def savepoints(probe):
    for scenario in ('rollback','release','native_guc'):
        with transaction(probe) as (tx,now,counter):
            fixtures.require(setting(tx)==1000,'timeout_cache_initial_setting')
            try:
                with tx.savepoint():
                    now[0]=10.25
                    if scenario=='native_guc':
                        tx.fetch_one(_NATIVE_CHANGE)
                        fixtures.require(raw_one(tx._connection,_SETTING)['value']==777,'timeout_cache_native_setting')
                    else:fixtures.require(setting(tx)==750,'timeout_cache_inner_setting')
                    if scenario!='release':raise RollbackFixture()
            except RollbackFixture:pass
            restored=750 if scenario=='release' else 1000
            fixtures.require(raw_one(tx._connection,_SETTING)['value']==restored,'timeout_cache_real_local_restore')
            # No driver clock advance: a stale cached750 would skip this SET
            # after the server restored1000, so the returned value detects it.
            fixtures.require(setting(tx)==750,'timeout_cache_reconfigured_after_rollback')
        probe.check('timeout_cache_savepoint_'+scenario,counter.values==([1000,750] if scenario=='release' else [1000,750,750]))
        observe(probe,scenario,counter)


def session(probe):
    with probe.database._pool.connection(timeout=probe.database.config.acquire_timeout) as connection:
        return raw_one(connection,_SESSION)


def reuse(probe):
    before=session(probe)
    fixtures.require(before['login']==before['role']=='authenticator','timeout_cache_pooled_role')
    for rollback in (False,True):
        try:
            with transaction(probe,ceiling=500) as (tx,now,counter):
                fixtures.require(setting(tx)==500,'timeout_cache_reuse_setting')
                if rollback:raise RollbackFixture()
        except RollbackFixture:pass
        fixtures.require(session(probe)==before,'timeout_cache_no_setting_or_role_leak')
    probe.check('timeout_cache_same_backend_no_leak',True)


RUNNERS={name:globals()[name] for name in CASES}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--case',choices=sorted(CASES),action='append',default=[])
    parser.add_argument('--list-cases',action='store_true');parser.add_argument('--report',type=Path);args=parser.parse_args(argv)
    if args.list_cases:print(json.dumps({'cases':CASES,'seed_row_limit':0,'phase_accepted':False}));return 0
    if not args.case or len(args.case)!=len(set(args.case)):parser.error('Select distinct --case names explicitly.')
    if args.report and args.report.exists():parser.error('Report must be a new file.')
    report={'status':'failed','cases':[],'phase_accepted':False,'youtube_requests':0,'seed_row_limit':0,'fixture_cleanup_incomplete':False,
      'limitations':['Semantic cache proof only; no performance measurement or changed thresholds.',
       'Driver clock alone is controlled; pool, network and case deadlines remain real.',
       'Only fixed read-only SQL and transaction-local timeout settings; native mutation is777ms within the1000ms ceiling.',
       'Pooled timeout/role equality is checked on the same backend; backend identity is not included in output.']}
    admin=None;dirty=False;metrics={'client_execute_calls':0,'client_execute_seconds':0.0}
    try:
        dsns=safety.validate_configuration(os.environ.get('LIC_DIRECT_PG_PROBE_ADMIN_DSN',''),os.environ.get('LIC_DIRECT_PG_PROBE_DSN',''))
        admin=safety.connect(dsns[0],metrics);snapshots,counts=safety.preflight(admin)
        with fixtures.self_hosted_profile(),fixtures.open_database(dsns[1]) as database:
            fixtures.require(database.read_row('timeout_cache_identity','SELECT session_user AS login,current_user AS role')=={'login':'authenticator','role':'service_role'},'timeout_cache_runtime_identity')
            for case in args.case:
                probe=fixtures.Probe(admin,database,snapshots,counts);probe.cache_observations=[]
                item={'case_id':case,'status':'failed'};report['cases'].append(item);dirty=True
                try:RUNNERS[case](probe);probe.observe();item['status']='passed'
                except BaseException as error:item.update(fixtures.safe_failure(error));raise
                finally:
                    try:probe.reset();dirty=False
                    except BaseException as error:item.update(status='failed',cleanup_failure=fixtures.safe_failure(error));raise
                    finally:item.update(probe.report(),cache_observations=probe.cache_observations)
        report['status']='passed'
    except BaseException as error:report.update(fixtures.safe_failure(error),fixture_cleanup_incomplete=dirty)
    finally:
        if admin is not None:admin.connection.close()
    encoded=json.dumps(report,indent=2)
    if args.report:
        with args.report.open('x',encoding='utf-8') as handle:handle.write(encoded+'\n')
    print(encoded);return 0 if report['status']=='passed' else 1


if __name__=='__main__':raise SystemExit(main())
