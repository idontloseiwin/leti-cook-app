"""A supervised worker closes runs left behind by a killed worker at once. Source only: no database."""
import ast
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from pathlib import Path
import unittest
import sys
ROOT=Path(__file__).resolve().parents[1]
RUNS=ROOT/'application/collection/runs.py'
METADATA=ROOT/'application/collection/channel_metadata.py'
sys.path.insert(0,str(ROOT/'application'))
from channel_collection_scope import channel_scope

def cutoff_function():
    tree=ast.parse(RUNS.read_text(encoding='utf-8'))
    node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='stale_sync_run_cutoff')
    namespace={'timedelta':timedelta}
    exec(compile(ast.Module(body=[node],type_ignores=[]),str(RUNS),'exec'),namespace)
    return namespace['stale_sync_run_cutoff']

class StaleRunCutoffTests(unittest.TestCase):
    now=datetime(2026,9,25,7,0,tzinfo=timezone.utc)

    def test_supervised_worker_closes_every_earlier_run(self):
        self.assertEqual(cutoff_function()(self.now,environ={'LIC_COLLECTION_SUPERVISED':'1'},stale_after_hours=24),self.now.isoformat())

    def test_unsupervised_run_keeps_the_stale_window(self):
        self.assertEqual(cutoff_function()(self.now,environ={},stale_after_hours=24),(self.now-timedelta(hours=24)).isoformat())

    def test_start_uses_the_cutoff_and_the_supervisor_sets_the_flag(self):
        tree=ast.parse(RUNS.read_text(encoding='utf-8'))
        start=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='start_sync_run')
        cutoff=next(n for n in ast.walk(start) if isinstance(n,ast.Call) and getattr(n.func,'id',None)=='stale_sync_run_cutoff')
        self.assertEqual(ast.unparse(cutoff.args[0]),'now_dt')
        self.assertEqual({k.arg:ast.unparse(k.value) for k in cutoff.keywords},
                         {'environ':'context.supervised_environ()',
                          'stale_after_hours':'context.policy.sync_run_stale_after_hours'})
        supervisor=(ROOT/'application/self_hosted_collection.py').read_text(encoding='utf-8')
        self.assertIn("env['LIC_COLLECTION_SUPERVISED'] = '1'",supervisor)
        # One worker at a time: a new one is spawned only after poll() saw the previous exit.
        self.assertIn('code = self.process.poll()',supervisor)

class JobOwnedStatusTests(unittest.TestCase):
    """A running initial status that an operator job owns is left to the job's lease."""

    def recover(self, rows, owned):
        tree=ast.parse(METADATA.read_text(encoding='utf-8'))
        nodes=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in ('recover_stale_running_statuses','running_status_is_stale')]
        updates={}
        class Repository:
            def __init__(self,_):pass
            def load_running_status_page(self,**_):return rows
            def channels_with_active_initial_jobs(self,ids):return {i for i in ids if i in owned}
            def update_channel(self,channel_id,values,*,expected_epoch):
                assert expected_epoch == 7
                updates[channel_id]=values
        ns={'channel_scope':channel_scope,'datetime':datetime,'timezone':timezone,'timedelta':timedelta,'ChannelSyncRepository':Repository,'CollectionContext':SimpleNamespace,
            'parse_dt':lambda v:datetime.fromisoformat(v) if v else None,'RUNNING_STATUS_STALE_AFTER':timedelta(hours=6),
            'CHANNEL_METADATA_RUNNING_STALE_AFTER':timedelta(minutes=15),'CHANNEL_METADATA_STATUS_RUNNING':'running',
            'CHANNEL_METADATA_STATUS_ERROR':'error','CHANNEL_METADATA_REFRESH_INTERVAL':timedelta(hours=24),
            'STALE_RUNNING_RECOVERY_CODE':'stale_running_recovered','source_log_message':lambda *a,**k:''}
        exec(compile(ast.Module(body=nodes,type_ignores=[]),str(METADATA),'exec'),ns)
        context=SimpleNamespace(database=object(),sources=SimpleNamespace(parse_dt=ns['parse_dt']))
        return ns['recover_stale_running_statuses'](now=datetime(2026,9,25,12,tzinfo=timezone.utc),context=context),updates

    def test_job_owned_initial_statuses_are_left_to_the_job(self):
        old='2026-09-25T00:00:00+00:00'
        rows=[{'channel_id':'UCjob','collection_control_epoch':7,'initial_long_inventory_status':'running','initial_long_inventory_started_at':old,
               'initial_28d_stats_status':'running','initial_28d_stats_started_at':old},
              {'channel_id':'UCfree','collection_control_epoch':7,'initial_long_inventory_status':'running','initial_long_inventory_started_at':old}]
        summary,updates=self.recover(rows,{'UCjob'})
        self.assertNotIn('UCjob',updates)
        self.assertEqual(updates['UCfree']['initial_long_inventory_status'],'error')
        self.assertEqual(summary['initial_long_inventory'],1)

    def test_inventory_recovery_leaves_scan_and_receipt_to_lease_owner(self):
        old = '2026-09-25T00:00:00+00:00'
        row = {'channel_id': 'UCinventory', 'collection_control_epoch': 7,
               'channel_discovery_status': 'running', 'channel_discovery_started_at': old,
               'channel_discovery_error_count': 2, 'channel_discovery_resume_page_token': 'cursor-A'}
        summary, updates = self.recover([row], set())
        self.assertEqual(updates, {})
        self.assertEqual(summary['channel_discovery'], 0)
        self.assertEqual(row['channel_discovery_resume_page_token'], 'cursor-A')

    def test_metadata_recovery_does_not_rewrite_concurrent_inventory(self):
        old = '2026-09-25T00:00:00+00:00'
        row = {'channel_id': 'UCmixed', 'collection_control_epoch': 7,
               'channel_discovery_status': 'running', 'channel_discovery_started_at': old,
               'channel_metadata_status': 'running', 'channel_metadata_started_at': old}
        summary, updates = self.recover([row], set())
        self.assertEqual(updates['UCmixed']['channel_metadata_status'], 'error')
        self.assertFalse(any(key.startswith('channel_discovery_') for key in updates['UCmixed']))
        self.assertEqual(summary['channel_discovery'], 0)
        self.assertEqual(summary['channel_metadata'], 1)

    def test_recovered_run_keeps_its_marker(self):
        from db.runs.sync_run_recovery import _RECOVERY_CANDIDATES
        query=_RECOVERY_CANDIDATES.as_string()
        self.assertIn("'stale_run_recovered',true",query)
        self.assertIn('FOR NO KEY UPDATE OF run',query)
        self.assertIn(' stale_run_recovered".split())',(ROOT/'application/operational_copy_policy.py').read_text(encoding='utf-8'))

if __name__=='__main__':unittest.main()
