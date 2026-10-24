"""Offline proof-harness guards only; never connect or execute its native main."""
import ast
from datetime import datetime,timezone
import importlib.util
from pathlib import Path
import re
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
SOURCE=ROOT
sys.path[:0]=[str(SOURCE/'application'),str(SOURCE/'tests')]
spec=importlib.util.spec_from_file_location('schedule_native_probe',ROOT/'tools/check_collection_schedule_snapshot.py')
probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer

class Facts:
    now=datetime(2026,10,7,5,tzinfo=timezone.utc)
    peak_rows=0
    def __init__(self):self.calls=[];self.checks=0
    def channel(self):pass
    def rows(self,sql,args=()):self.calls.append((sql,args));return []
    def scalar(self,sql,args=()):self.calls.append((sql,args));return 0
    def observe(self):return 20
    def check(self,condition,label):
        if not condition:raise AssertionError(label)
        self.checks+=1

class NativeScheduleGuards(unittest.TestCase):
    def test_seed_bindings_fixed_small_and_current_schema_relations(self):
        schema=(SOURCE/'schema/schema.sql').read_text()
        tables=set(re.findall(r'CREATE TABLE (?:ONLY )?([\w.]+)',schema))
        for kind in ('long','short','stream'):
            f=Facts();probe.seed(f,kind)
            self.assertLessEqual(len(f.calls),9)
            inserts=0
            for sql,args in f.calls:
                PostgresQuery(Transformer()).convert(sql,args)
                for name in re.findall(r'(?:FROM|INTO|UPDATE|JOIN)\s+(public\.[a-z_]+)',sql,re.I):self.assertIn(name,tables)
                if sql.startswith('INSERT'):inserts+=1
            self.assertEqual(inserts,3 if kind=='long' else 6)
            self.assertEqual(f.peak_rows,20)

    def test_native_expected_expression_uses_real_independent_model_fields(self):
        for model in (probe.DEFAULT,probe.CUSTOM):
            for kind in ('short','stream'):
                f=Facts();probe.expected_typed(f,model,kind,f.now,f.now)
                sql,args=f.calls[-1];PostgresQuery(Transformer()).convert(sql,args)
                fields=getattr(model,kind+'_metadata')
                self.assertEqual(args[3:6],(fields.age_through_90d_seconds,fields.age_over_90d_seconds,fields.jitter_seconds))
                self.assertIn("interval '90 days'",sql);self.assertIn('md5',sql)

    def test_restart_closes_old_pool_and_forwards_same_snapshot_by_identity(self):
        events=[]
        class Database:
            def __init__(self,config,*,cadence_snapshot):self.config=config;self.cadence_snapshot=cadence_snapshot;events.append(('construct',cadence_snapshot))
            def open(self):events.append(('open',));return self
            def close(self):events.append(('close',))
            def transaction(self,operation):
                from contextlib import contextmanager
                @contextmanager
                def tx():yield SimpleNamespace(cadence_snapshot=self.cadence_snapshot)
                return tx()
        f=Facts();f.database=Database('fixed_config',cadence_snapshot=probe.DEFAULT.cadence_snapshot());f.snapshot=lambda:{'queue':'original'}
        events.clear()
        with patch.object(probe,'PostgresDatabase',Database):probe.switch(f,probe.CUSTOM)
        self.assertEqual([e[0] for e in events],['close','construct','open'])
        self.assertIs(f.database.cadence_snapshot,events[1][1]);self.assertEqual(f.checks,3)

    def test_execution_is_explicit_finite_and_contains_cleanup_finally(self):
        tree=ast.parse((ROOT/'tools/check_collection_schedule_snapshot.py').read_bytes())
        self.assertEqual(probe.GROUPS,('readiness','long_deadlines','typed_deadlines','closed_reconciliation','maintenance_orphan'))
        self.assertEqual(probe.MAX_ROWS,1000);self.assertEqual(probe.WORK_SECONDS,180)
        loops=[n for n in ast.walk(tree) if isinstance(n,(ast.For,ast.While))]
        self.assertFalse(any(isinstance(n,ast.While) for n in loops))
        text=(ROOT/'tools/check_collection_schedule_snapshot.py').read_text()
        self.assertIn("LIC_F11_MAX_ROWS",text);self.assertIn("--run-proof",text)
        main=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='main')
        self.assertTrue(any(isinstance(n,ast.Try) and any(isinstance(x,ast.Expr) and isinstance(x.value,ast.Call) and isinstance(x.value.func,ast.Attribute) and x.value.func.attr=='close' for x in n.finalbody) for n in ast.walk(main)))
        self.assertNotRegex(text,r'ALTER |GRANT |DISABLE TRIGGER|requests\.|subprocess\.|\.call\(')

    def test_orphan_maintenance_uses_actual_factory_and_proves_missing_model_rollback(self):
        tree=ast.parse((ROOT/'tools/check_collection_schedule_snapshot.py').read_bytes())
        fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='maintenance_orphan')
        events=[]
        class Fake(Facts):
            def __init__(self):
                super().__init__();self.model=None;self.deleted=False;self.cleanups=[]
                self.database=SimpleNamespace(config='fixed_config',close=lambda:events.append('old_pool_closed'))
            def reset(self):pass
            def snapshot(self):return {'deleted':self.deleted}
            def scalar(self,sql,args=()):
                self.calls.append((sql,args))
                return 3 if 'channel_playlist_state' in sql else 0
            def cleanup(self,target,**kwargs):
                self.cleanups.append((self.model,target,kwargs))
                from db.hot.hot_retention import RetentionError
                raise RetentionError('cleanup_outcome_unknown')
        f=Fake();snapshots=[];positive=[]
        def factory(config,*,cadence_snapshot):
            self.assertEqual(config,'fixed_config');self.assertEqual(events,['old_pool_closed'])
            events.append('factory_opened');snapshots.append(cadence_snapshot)
            def cleanup(target,**kwargs):
                positive.append((target,kwargs));f.deleted=True
                return SimpleNamespace(scanned=1,deleted=5,deferred=0,scrubbed=0)
            return SimpleNamespace(database=SimpleNamespace(cadence_snapshot=cadence_snapshot),cleanup_page=cleanup)
        with patch.object(probe,'switch',lambda f,model:setattr(f,'model',model)),patch('db.hot.hot_runtime.maintenance_repository',factory):
            probe.maintenance_orphan(f)
        self.assertEqual([(x[1],x[2]) for x in f.cleanups],[('deleted_channels',{'limit':1})])
        self.assertIsNone(f.cleanups[0][0]);self.assertEqual(positive,[('deleted_channels',{'limit':1})])
        self.assertIs(f.database.cadence_snapshot,snapshots[0]);self.assertEqual(snapshots[0],probe.CUSTOM.cadence_snapshot())
        self.assertEqual(events,['old_pool_closed','factory_opened'])
        self.assertEqual(sum(sql.startswith('INSERT') for sql,args in f.calls),1)
        for sql,args in f.calls:PostgresQuery(Transformer()).convert(sql,args)
        self.assertFalse(any(isinstance(n,ast.While) for n in ast.walk(fn)))
        self.assertTrue(f.deleted)

    def test_actual_readiness_branch_uses_real_blocked_admission_and_scoped_catalog(self):
        from db.base.postgres import _runtime_admission
        from db.videos.collection_policy import cadence_report
        events=[]
        class Fake(Facts):
            def __init__(self,gate):super().__init__();self.gate=gate;self.database=None
            def reset(self):self.gate.ready=True
            def snapshot(self):return {'fixture':'unchanged'}
        def switch(f,model):f.database=SimpleNamespace(cadence_snapshot=None if model is None else model.cadence_snapshot())
        class Video:
            def __init__(self,database):self.database=database
            def schema_status(self):return cadence_report(self.database)
            def typed_schema_status(self):return cadence_report(self.database)
            def preflight_typed_collection_ready(self):return {'status':'ok'}
        class Inventory(Video):
            def assert_ready(self):return {'status':'ok'}
            def assert_quota_defer_ready(self):return cadence_report(self.database)
        def contract(database):
            try:_runtime_admission('get_hot_runtime_contract')
            except probe.HotDatabaseAdmissionDenied:
                events.append('ordinary_denied');raise
            if not f.gate.ready:
                events.append('maintenance_legacy_checked')
                # Fixed-catalog admission does not allow a generic query.
                with self.assertRaises(probe.HotDatabaseAdmissionDenied):_runtime_admission(None)
                raise probe.DatabaseError('collection_schedule_legacy_cadence_conflict')
            return {'schema_ready':True}
        with probe.admission() as gate:
            f=Fake(gate)
            with patch.object(probe,'switch',switch),patch.object(probe,'VideoCollectionRepository',Video),patch.object(probe,'TypedInventoryRepository',Inventory),patch.object(probe.capabilities,'get_hot_runtime_contract',contract):
                probe.readiness(f)
            self.assertTrue(gate.ready)
            gate.ready=False
            with self.assertRaises(probe.HotDatabaseAdmissionDenied):_runtime_admission('get_hot_runtime_contract')
            gate.ready=True
        self.assertEqual(events,['ordinary_denied','maintenance_legacy_checked']*2)
        self.assertGreater(f.checks,20)

    def test_error_diagnostic_never_echoes_arbitrary_exception_text(self):
        result=probe.safe_failure(RuntimeError('private password payload'))
        self.assertNotIn('private',repr(result));self.assertNotIn('password',repr(result))
        self.assertIsNone(result['check_code']);self.assertIsNone(result['sqlstate'])
        result=probe.safe_failure(AssertionError('typed_custom_native_due'))
        self.assertEqual(result['check_code'],'typed_custom_native_due')

    def test_healthy_reconciliation_must_wrap_cursor_and_examine_original_row(self):
        text=(ROOT/'tools/check_collection_schedule_snapshot.py').read_text()
        self.assertIn("wrapped['inventory_examined_count']==0 and visited['inventory_examined_count']==1",text)
        self.assertIn("f.check(queue(f)==before,'healthy_reconcile_no_reschedule')",text)
        self.assertIn("cadence=Jsonb([])",text)
        self.assertIn("'database_typed_reconcile_capability_invalid'",text)

if __name__=='__main__':unittest.main(verbosity=2)