"""Signed cleanup priority through real initial page/parent dispatch owners."""
from dataclasses import asdict
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch
import sys, unittest
from pathlib import Path
sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'application'), str(Path(__file__).parent)]
import collection_shutdown as shutdown
import hot_runtime as hot
from operator_jobs import OperatorJobSettings, run_operator_jobs_dispatch
from db.base.postgres import DatabaseUnavailable
from test_operator_shared_dispatch import Jobs
from test_shared_inventory_engine import Repository, claim, page

CONFIG = hot.HotAdmissionConfig('91f5d9f3-9230-4ee6-b170-155d4c3c1719', 'internal_runtime_secret_0123456789')

class InitialPriorityHandoffTests(unittest.TestCase):
    def setUp(self):
        gate = patch.object(shutdown, '_requested', Event()); gate.start(); self.addCleanup(gate.stop)
        self.reason = 'ready'
        self.clock = 1800000000.0
        def proof():
            controller = SimpleNamespace(status=lambda: dict(status='ready', epoch='a'*32,
                api_read=dict(allowed=True, reason='ready'), user_work=dict(allowed=True, reason='ready'),
                collection=dict(allowed=self.reason == 'ready', reason=self.reason)))
            return hot.signed_status(controller, admission=CONFIG, now=self.clock)
        self.remote = hot.RemoteAdmission('http://127.0.0.1:8009/internal/hot-runtime', admission=CONFIG,
            fetch=proof, clock=lambda:self.clock, monotonic=lambda:self.clock)
        self.jobs = Jobs([0], phase='inventory')
        self.identity, self.job = next(iter(self.jobs.by_id.items()))
        self.repo = Repository()
        self.calls = []
        self.next_token = None
        native_settle = self.repo.settle_page
        def settle(owned, **values):
            self.remote.require_collection_ready()
            result = native_settle(owned, **values)
            self.next_token = result['resume_page_token']
            result['observed_video_ids'] = result['canonical_video_ids']
            return result
        self.repo.settle_page = settle
        self.jobs.claim_inventory_chain_source = lambda identity, token, kind, **kw: dict(
            claims=[asdict(claim(kind, channel_id=self.job['channel_id'], resume_page_token=self.next_token))])
        original_finish = self.jobs.finish_inventory_chains
        def finish(*args):
            self.remote.require_collection_ready()
            result = original_finish(*args)
            self.job.update(lease_token=None, lease_owner=None)
            return result
        self.jobs.finish_inventory_chains = finish
        def ack(identity, token, kind, **kw):
            self.remote.require_collection_ready()
            self.jobs.events.append(('ack', identity, kind))
            self.jobs.chain[identity, kind]['done'] = self.next_token is None
            return dict(ack={}, dispatch_allowed=False)
        self.jobs.acknowledge_inventory_chain = ack

    def run_worker(self, fetch):
        with hot.supervised_collection_handoff(shutdown.request_handoff):
            return run_operator_jobs_dispatch(self.jobs, inventory_repository=self.repo,
                video_service=object(), fetch_page=fetch, legacy_chunk=Mock(side_effect=AssertionError("unexpected legacy")),
                settings=OperatorJobSettings(None, 3, 50, 1200, initial_load_concurrency=1),
                concurrency=1, max_chunks=1, max_seconds=10,
                require_ready=self.remote.require_collection_ready,
                should_stop=shutdown.handoff_requested)

    def fetching(self, playlist, **values):
        self.calls.append(values.get('page_token'))
        self.reason = 'collection_priority'; self.clock += 2
        # Real signed source admission requests handoff in the provider context.
        self.remote.require_collection_ready()
        return page('confirmed-next')

    def test_priority_during_page_acks_then_yields_parent_and_resumes_exact_token(self):
        result = self.run_worker(self.fetching)
        self.assertFalse(result['settlement_unknown'])
        self.assertEqual(result['stop_reason'], 'scheduled_handoff')
        self.assertEqual(self.calls, [None])
        self.assertEqual(self.repo.events, ['begin', 'settle'])
        self.assertEqual([e[0] for e in self.jobs.events], ['claim', 'bind', 'ack', 'finish'])
        self.assertEqual((self.job['status'], self.job['phase'], self.job['lease_token']), ('queued', 'inventory', None))
        self.assertEqual(self.next_token, 'confirmed-next')
        shutdown._requested.clear(); self.reason = 'ready'; self.clock += 2
        def resumed(playlist, **values):
            self.calls.append(values.get('page_token')); return page()
        second = self.run_worker(resumed)
        self.assertFalse(second['settlement_unknown'])
        self.assertEqual(self.calls, [None, 'confirmed-next'])
        self.assertEqual(self.repo.events, ['begin', 'settle', 'begin', 'settle'])

    def test_priority_does_not_turn_unknown_page_ack_into_parent_success_or_retry(self):
        def uncertain(*a, **kw):
            self.remote.require_collection_ready()
            raise DatabaseUnavailable('database_connection_failed', outcome_unknown=True)
        self.repo.settle_page = uncertain
        result = self.run_worker(self.fetching)
        self.assertTrue(result['settlement_unknown'])
        self.assertEqual(self.calls, [None])
        self.assertEqual([e[0] for e in self.jobs.events], ['claim', 'bind'])
        self.assertEqual(self.repo.events, ['begin'])
        self.assertIsNotNone(self.job['lease_token'])

if __name__ == '__main__': unittest.main(verbosity=2)
