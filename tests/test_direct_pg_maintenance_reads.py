"""Maintenance catalog capability is finite and cannot lend admission to SQL."""
from pathlib import Path
import os
import sys
from types import MappingProxyType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'application'))
import hot_runtime
from db.base.postgres import DatabaseError, PostgresConfig, PostgresDatabase
from test_direct_pg_transactions import Connection, Pool, Result, DSN

class MaintenanceReadTests(unittest.TestCase):
    def setUp(self):
        self.trace=[]
        self.connection=Connection(self.trace)
        self.pool=Pool(self.connection,self.trace)
        self.gates=[]
        self.database=PostgresDatabase(PostgresConfig(DSN),admission=self.gates.append,
                                        pool_factory=lambda config:self.pool).open()
        self.addCleanup(self.database.close)
        stub=SimpleNamespace(MAINTENANCE_CAPABILITY_QUERIES=MappingProxyType({
            'schema_release':'SELECT 25 AS current_version','inventory_switch':'SELECT true AS schema_ready', 'hot_runtime':'SELECT 17 AS version', 'user_work_restore':'SELECT true AS ready', 'video_failure':'SELECT true AS ready','evidence_retention':'SELECT true AS ready','inventory_receipts':'SELECT true AS ready','operational_retention':'SELECT true AS ready','deleted_channels':'SELECT true AS ready','collection_completion':'SELECT true AS ready'}))
        imported=patch.dict(sys.modules,{'db.readiness.capabilities':stub})
        imported.start();self.addCleanup(imported.stop)

    def test_only_ten_owned_names_receive_fixed_read_only_permission(self):
        for name in ('schema_release','inventory_switch','hot_runtime','user_work_restore','video_failure','evidence_retention','inventory_receipts','operational_retention','deleted_channels','collection_completion'):
            self.connection.results.append(Result(rows=({'schema_ready':True},)))
            self.assertEqual(self.database.read_maintenance_capability(name),{'schema_ready':True})
        self.assertEqual(self.gates,['get_hot_runtime_contract']*30)
        self.assertEqual(sum(s=='SET TRANSACTION READ ONLY' for s,p in self.connection.setup),10)
        self.assertTrue(all('LIMIT 2' in query for query,params in self.connection.queries))
        self.assertTrue(all(params==() for query,params in self.connection.queries))

    def test_unknown_name_and_caller_sql_are_refused_before_pool(self):
        for name in (None,[],{},'get_hot_runtime_contract','hot_runtime; DELETE',''):
            with self.assertRaises(ValueError):self.database.read_maintenance_capability(name)
        with self.assertRaises(TypeError):
            self.database.read_maintenance_capability('hot_runtime',statement='SELECT 1')
        self.assertEqual(self.pool.borrowed,0)
        self.assertEqual(self.gates,[])

    def test_missing_or_multiple_rows_roll_back(self):
        for rows in ((),({'ready':True},{'ready':False})):
            self.connection.results.append(Result(rows=rows))
            with self.assertRaises(DatabaseError):self.database.read_maintenance_capability('hot_runtime')
        self.assertEqual(self.connection.rollbacks,2)
        self.assertEqual(self.connection.commits,0)

    def test_real_gate_allows_only_catalog_read_during_maintenance(self):
        self.database._admission=__import__('db.base.postgres',fromlist=['_runtime_admission'])._runtime_admission
        env={'LIC_DEPLOYMENT_PROFILE':'self-hosted','LIC_DATABASE_TRANSPORT':'postgres',
             'LIC_DATABASE_ID':'91f5d9f3-9230-4ee6-b170-155d4c3c1719',
             'LIC_HOT_RUNTIME_SECRET':'internal_runtime_secret_0123456789'}
        controller=Mock()
        controller.require_ready.side_effect=hot_runtime.HotRuntimeUnavailable('hot_runtime_not_ready')
        controller.require_collection_ready.side_effect=hot_runtime.HotRuntimeUnavailable('hot_runtime_not_ready')
        with patch.dict(os.environ,env,clear=True),patch.object(hot_runtime,'_local_controller',controller):
            with self.assertRaises(hot_runtime.HotDatabaseAdmissionDenied):
                self.database.read_maintenance_capability('hot_runtime')
            with hot_runtime.maintenance_access():
                self.connection.results.append(Result(rows=({'schema_ready':True},)))
                self.database.read_maintenance_capability('hot_runtime')
                self.connection.results.append(Result(rows=({'ready':True},)))
                self.database.read_maintenance_capability('user_work_restore')
                self.connection.results.append(Result(rows=({'ready':True},)))
                self.database.read_maintenance_capability('video_failure')
                self.connection.results.append(Result(rows=({'ready':True},)))
                self.database.read_maintenance_capability('evidence_retention')
                for name in ('schema_release','inventory_receipts','operational_retention','deleted_channels','collection_completion'):
                    self.connection.results.append(Result(rows=({'ready':True},)))
                    self.database.read_maintenance_capability(name)
                with self.assertRaises(hot_runtime.HotDatabaseAdmissionDenied):
                    self.database.read_row('get_hot_runtime_contract','SELECT 1')
        self.assertEqual(self.pool.borrowed,9)
        self.assertEqual(len(self.connection.queries),9)

if __name__=='__main__':unittest.main()
