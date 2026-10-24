"""Private two-connection fixtures; real PG17 driver/admission, synthetic local gate."""
from contextlib import contextmanager
from pathlib import Path
from datetime import timedelta
import hashlib,json,os,re,time
from unittest.mock import patch
import psycopg
from psycopg.rows import dict_row
from psycopg.conninfo import conninfo_to_dict
from db.base.postgres import PostgresDatabase,PostgresConfig,DatabaseError
from db.readiness import capabilities
from collection_schedule import CollectionSchedules
import hot_runtime
from .manifest import ROOT,load_fixture,require
CHANNEL='UCprobeprobeprobeprobe01'
_DEFAULT_CADENCE=object()

class Gate:
    ready=True
    def require_ready(self):
        if not self.ready:raise hot_runtime.HotRuntimeUnavailable()

    require_collection_ready = require_ready
    require_database_ready = require_ready

@contextmanager
def admission():
    # Same real ContextVar/admission function as production; no HOT readiness
    # claim is inferred from this synthetic owner. Actual guard facts run below.
    gate=Gate()
    env={'LIC_DEPLOYMENT_PROFILE':'self-hosted','LIC_DATABASE_TRANSPORT':'postgres',
         'LIC_DATABASE_ID':'10000000-0000-4000-8000-000000000001',
         'LIC_HOT_RUNTIME_SECRET':'schema_ci_synthetic_private_admission_000100'}
    with patch.dict(os.environ,env):
        hot_runtime.install_local_runtime(gate)
        try:yield gate
        finally:hot_runtime.remove_local_runtime(gate)

def dsns():
    require(os.getenv('LIC_SCHEMA_CI_OWNED')=='schema25_disposable','ci_owned_required')
    result=[]
    for key,user in (('LIC_SCHEMA_CI_ADMIN_DSN','postgres'),('LIC_SCHEMA_CI_DSN','authenticator')):
        raw=os.environ.get(key,'');value=conninfo_to_dict(raw)
        require(set(value)<=set(('host','port','dbname','user','password','sslmode','connect_timeout')),'dsn_options')
        require(value.get('host')=='127.0.0.1' and value.get('port')=='5432' and value.get('dbname')=='leti-cook'
                and value.get('user')==user and len(value.get('password',''))>=32,'dsn_identity')
        result.append(raw)
    return tuple(result)

class Fixture:
    def __init__(self,admin_dsn,runtime_dsn,gate,*,cadence_snapshot=_DEFAULT_CADENCE):
        self.admin=psycopg.connect(admin_dsn,autocommit=True,row_factory=dict_row,connect_timeout=5)
        self.database=PostgresDatabase(PostgresConfig(runtime_dsn,min_size=0,max_size=1,max_waiting=1,acquire_timeout=3,connect_timeout=5),
            cadence_snapshot=(CollectionSchedules().cadence_snapshot()
                if cadence_snapshot is _DEFAULT_CADENCE else cadence_snapshot))
        self.gate=gate;self.checks=0;self.budget=1000;self.deadline=time.monotonic()+420
        try:
            self.rows("SET statement_timeout='20s'; SET lock_timeout='250ms'; SET idle_in_transaction_session_timeout='30s'; SET timezone='UTC'")
            self.database.open()
            require(self.scalar('SELECT current_user')=='postgres','admin_role')
            require(170000<=int(self.scalar('SHOW server_version_num'))<180000,'pg17_required')
            require(self.database.read_row('schema_ci_identity','SELECT session_user AS login,current_user AS role')=={'login':'authenticator','role':'service_role'},'runtime_role')
            self.tables=[x['name'] for x in self.rows("SELECT n.nspname||'.'||c.relname AS name FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname IN('public','operator_private') AND c.relkind='r' ORDER BY 1")]
            require(1<=len(self.tables)<=100 and all(re.fullmatch('(public|operator_private)\\.[a-z_]+',x) for x in self.tables),'table_inventory')
            self.sequence_names=[x['name'] for x in self.rows("SELECT n.nspname||'.'||c.relname AS name FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname IN('public','operator_private') AND c.relkind='S' ORDER BY 1")]
            require(len(self.sequence_names)==6,'sequence_inventory')
            self.baseline=self.snapshot();self.sequence_baseline=self.sequences();self.catalog_baseline=self.catalog();self.now=self.scalar('SELECT clock_timestamp()')
            require(sum(len(json.loads(v)) for v in self.baseline.values())<100,'fresh_seed_bound')
            self.identity()
        except BaseException:
            self.database.close();self.admin.close();raise
    def rows(self,sql,args=()):
        with self.admin.cursor() as cursor:
            cursor.execute(sql,args or None)
            return cursor.fetchall() if cursor.description else []
    def scalar(self,sql,args=()):
        rows=self.rows(sql,args);require(len(rows)==1 and len(rows[0])==1,'scalar_shape');return next(iter(rows[0].values()))
    def check(self,condition,label):
        require(condition,label);self.checks+=1;require(time.monotonic()<self.deadline,'suite_deadline')
    def observe(self):
        total=sum(self.scalar('SELECT count(*) FROM '+name) for name in self.tables)
        self.check(total<=self.budget,'fixture_row_budget');return total
    def snapshot(self):
        return {n:self.scalar("SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),'[]'::jsonb)::text FROM "+n+' t') for n in self.tables}
    def sequences(self):return {n:self.rows('SELECT last_value,is_called FROM '+n)[0] for n in self.sequence_names}
    def catalog(self):
        return self.scalar("""SELECT md5(jsonb_build_array(
          (SELECT jsonb_agg(jsonb_build_array(n.nspname,p.proname,pg_get_functiondef(p.oid),p.proacl::text) ORDER BY p.oid) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname IN('public','operator_private')),
          (SELECT jsonb_agg(jsonb_build_array(tgrelid,tgname,tgenabled,pg_get_triggerdef(oid)) ORDER BY oid) FROM pg_trigger),
          (SELECT jsonb_agg(jsonb_build_array(oid,relname,relacl::text,relrowsecurity,relforcerowsecurity) ORDER BY oid) FROM pg_class WHERE relnamespace IN('public'::regnamespace,'operator_private'::regnamespace)),
          (SELECT jsonb_agg(jsonb_build_array(conrelid,conname,pg_get_constraintdef(oid)) ORDER BY conrelid,conname) FROM pg_constraint WHERE connamespace IN('public'::regnamespace,'operator_private'::regnamespace)),
          (SELECT jsonb_agg(jsonb_build_array(indexrelid,pg_get_indexdef(indexrelid)) ORDER BY indexrelid) FROM pg_index WHERE indrelid IN(SELECT oid FROM pg_class WHERE relnamespace IN('public'::regnamespace,'operator_private'::regnamespace))),
          (SELECT jsonb_agg(jsonb_build_array(attrelid,attnum,attname,attacl::text) ORDER BY attrelid,attnum) FROM pg_attribute WHERE attrelid IN(SELECT oid FROM pg_class WHERE relnamespace IN('public'::regnamespace,'operator_private'::regnamespace)) AND attnum>0 AND NOT attisdropped))::text)""")
    def identity(self):
        pins=json.loads((ROOT/'schema/function-body-pins.json').read_text())
        require(hashlib.sha256((ROOT/'schema/schema.sql').read_bytes()).hexdigest()==pins['schema_sha256'],'schema_digest')
        actual=self.rows("SELECT n.nspname||'.'||p.proname||'('||pg_get_function_identity_arguments(p.oid)||')' AS signature,encode(extensions.digest(convert_to(replace(replace(p.prosrc,chr(13)||chr(10),chr(10)),chr(13),chr(10)),'UTF8'),'sha256'),'hex') AS digest FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname IN('public','operator_private') ORDER BY signature")
        require(1<=len(pins['functions'])<=20 and len(actual)==len(pins['functions'])
            and {r['signature']:r['digest'] for r in actual}==pins['functions'],'intrinsic_bodies')
        capabilities.assert_typed_inventory_runtime_schema(self.database)
    def reset(self):
        self.gate.ready=True
        with self.admin.transaction():
            self.rows('TRUNCATE TABLE '+','.join(self.tables))
            for name,text in self.baseline.items():
                if text!='[]':self.rows('INSERT INTO '+name+' SELECT * FROM jsonb_populate_recordset(NULL::'+name+',%s::jsonb)',(text,))
        for name,value in self.sequence_baseline.items():self.rows('SELECT setval(%s::regclass,%s,%s)',(name,value['last_value'],value['is_called']))
        require(self.snapshot()==self.baseline and self.sequences()==self.sequence_baseline and self.catalog()==self.catalog_baseline,'fixture_restore_exact')
        self.now=self.scalar('SELECT clock_timestamp()')
    def close(self):
        try:self.reset()
        finally:self.database.close();self.admin.close()
    def channel(self,identity=CHANNEL,*,pending=False):
        from db.channel.channel_sync import ChannelSyncRepository
        result=ChannelSyncRepository(self.database).insert_channel({'channel_id':identity,'title':'Schema CI synthetic','user_input':'https://www.youtube.com/channel/'+identity,
            'initial_long_inventory_status':'pending' if pending else 'completed','initial_28d_stats_status':'pending' if pending else 'completed'},permission='accepted')
        self.check(isinstance(result,dict),'created_channel');return identity
    def epoch(self,channel=CHANNEL):return self.scalar('SELECT control_epoch FROM public.channel_collection_state WHERE channel_id=%s',(channel,))
    def update_channel(self,values,channel=CHANNEL):
        from db.channel.channel_sync import ChannelSyncRepository
        ChannelSyncRepository(self.database).update_channel(channel,values,expected_epoch=self.epoch(channel))
    def cleanup(self,target,**kwargs):
        from db.hot.hot_retention import HotRetentionRepository
        return HotRetentionRepository(self.database).cleanup_page(target,**kwargs)
    def seed_sql(self,name):
        sql=load_fixture(name,kind='seed')
        require(not re.search(r'public\.(?:create_channel_for_collection|cleanup_|get_long_availability|import_user_work)',sql),'legacy_seed_call')
        require(all(word not in sql.lower() for word in ('session_replication_role','create function','select public.','perform public.','do $$')),'raw_fixture_only')
        with self.admin.transaction():self.rows(sql)
        self.observe()


def fixture_json(f,name):
    """Evaluate a pinned current fixture expression using only SQL built-ins."""
    expr=load_fixture(name,kind='expression')
    require('public.' not in expr and ';' not in expr,'fixture_json_only')
    return f.scalar('SELECT '+expr)

def expect_error(f,action,exception,*,code=None):
    before=f.snapshot()
    try:action()
    except exception as error:
        if code is not None:f.check(getattr(error,'code',None)==code,'error_code')
    else:raise AssertionError('expected_error')
    f.check(f.snapshot()==before,'error_rows_unchanged')
