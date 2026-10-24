"""Exact full-job inventory dispatch; fake repository/provider, no I/O clients."""
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'application'))
from collection import initial
from collection.budgets import PageBudget
from collection.inventory import run_typed_inventory_batch
from collection.settings import CollectionSettings
from db.inventory.typed_inventory import TypedInventoryClaim, TypedInventoryRepository
from db.hot.hot_collection_control import dispatch_owned_rpc
from hot_runtime import HotDatabaseAdmissionDenied
from runtime.collection import bind_collection_callbacks
from typed_playlist import typed_playlist_id

CHANNEL = 'UC' + 'a' * 22
JOB = '10000000-0000-0000-0000-000000000001'
JOB_LEASE = '20000000-0000-0000-0000-000000000001'
PLAYLIST_LEASE = '30000000-0000-0000-0000-000000000001'
RUN = '40000000-0000-0000-0000-000000000001'
TRANSITION = '50000000-0000-0000-0000-000000000001'
VIDEO = 'vid00000001'
NOW = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)


def owned(kind='long', **changes):
    value = TypedInventoryClaim(CHANNEL, kind, typed_playlist_id(CHANNEL, kind),
        PLAYLIST_LEASE, 7, NOW.isoformat(), 0, None, None, 'initial_inventory')
    return replace(value, **changes)


def page():
    return {'items': [{'contentDetails': {'videoId': VIDEO,
                       'videoPublishedAt': NOW.isoformat()}}], 'nextPageToken': 'next'}


class Repository:
    def __init__(self, claim=None):
        self.database = object()
        self.claim = claim or owned()
        self.events = []
        self.claim_initial_source = Mock(return_value=[self.claim])
        self.claim_channels = Mock(side_effect=AssertionError('generic claim forbidden'))
        self.claim_pass = Mock(side_effect=AssertionError('pass claim forbidden'))
        self.prepare_runtime = Mock(side_effect=AssertionError('preparation forbidden'))
        self.fail_scan = Mock(side_effect=AssertionError('replacement failure forbidden'))
        self.receipt_changes = {}
        self.omit_observed = False
        self.defer_claims_after_daily_quota = Mock(return_value=dict(status='deferred',
            requested=1, released=1, rejected=0, active_scan_state_preserved=True))

    def begin_scan(self, claim, **kw):
        self.events.append('begin')
        return dict(status='running', scan_id=kw['scan_id'], resume_page_token=None,
                    scan_boundary_at=NOW.isoformat())

    def settle_page(self, claim, **kw):
        self.events.append('settle')
        self.settled_items = kw['items']
        ids = [item['video_id'] for item in kw['items']]
        value = dict(channel_id=claim.channel_id, video_type=claim.video_type,
            lease_token=claim.lease_token, scan_id=kw['scan_id'], transition_id=kw['transition_id'],
            status='running', coverage='partial', resume_page_token=kw['next_page_token'],
            page_items=len(ids), inserted=0, already_matching=0, conflicts=len(ids),
            canonical_video_ids=[], new_video_ids=[], observed_video_ids=ids, items_seen=len(ids))
        if self.omit_observed:
            value.pop('observed_video_ids')
        value.update(self.receipt_changes)
        return value


class FullInitialDispatchTests(unittest.TestCase):
    def setUp(self):
        guard = patch('socket.socket.connect', side_effect=AssertionError('network forbidden'))
        guard.start()
        self.addCleanup(guard.stop)

    def options(self, repository=None, **changes):
        repository = repository or Repository()
        def fetch(*args, **kwargs):
            repository.events.append('fetch')
            return page()
        def bind(*args):
            repository.events.append('bind')
            return True
        value = dict(repository=repository, fetch_page=Mock(side_effect=fetch), limit=1,
            worker_id='worker', run_id=RUN, explicit_channel_ids=[CHANNEL],
            expected_channel_scopes=[{'channel_id': CHANNEL, 'control_epoch': 7}],
            video_types=(repository.claim.video_type,), prepare=False,
            initial_job_id=JOB, initial_job_lease_token=JOB_LEASE,
            transition_id=TRANSITION, before_dispatch=Mock(side_effect=bind), now=lambda: NOW)
        value.update(changes)
        return value

    def collect_options(self, **changes):
        value = dict(database=object(), settings=CollectionSettings(True, True, True),
            worker_id='worker', fetch_page=Mock(return_value=page()),
            get_longform_playlist_id=lambda channel: typed_playlist_id(channel, 'long'),
            parse_dt=lambda value: NOW, playlist_not_found_error=LookupError,
            daily_quota_exceeded_error=RuntimeError, video_type='long', expected_epoch=7,
            initial_job_id=JOB, initial_job_lease_token=JOB_LEASE,
            transition_id=TRANSITION, before_dispatch=Mock(return_value=True))
        value.update(changes)
        return value

    def test_repository_exact_abi_and_zero_or_one_claim(self):
        repo = TypedInventoryRepository(object())
        repo._rpc = Mock(return_value=[asdict(owned())])
        result = repo.claim_initial_source(job_id=JOB, job_lease_token=JOB_LEASE,
            worker_id='worker', run_id=RUN)
        self.assertEqual(result, [owned()])
        repo._rpc.assert_called_once_with('claim_initial_inventory_source', {
            'p_job_id': JOB, 'p_job_lease_token': JOB_LEASE, 'p_worker_id': 'worker',
            'p_run_id': RUN, 'p_lease_seconds': 900})
        repo._rpc.return_value = []
        self.assertEqual(repo.claim_initial_source(job_id=JOB, job_lease_token=JOB_LEASE,
            worker_id='worker', run_id=RUN), [])
        repo._rpc.return_value = [asdict(owned()), asdict(owned())]
        with self.assertRaisesRegex(RuntimeError, 'exceeded its bound'):
            repo.claim_initial_source(job_id=JOB, job_lease_token=JOB_LEASE,
                worker_id='worker', run_id=RUN)

    def test_repository_invalid_identity_is_rejected_before_rpc(self):
        repo = TypedInventoryRepository(object())
        repo._rpc = Mock()
        valid = dict(job_id=JOB, job_lease_token=JOB_LEASE, worker_id='worker', run_id=RUN)
        for change in ({'job_id': None}, {'job_lease_token': 'bad'}, {'run_id': 'bad'},
                       {'worker_id': ''}, {'lease_seconds': True}, {'lease_seconds': 59}):
            with self.subTest(change=change), self.assertRaises((TypeError, ValueError)):
                repo.claim_initial_source(**{**valid, **change})
        repo._rpc.assert_not_called()

    def test_three_formats_keep_begin_bind_one_request_settle_and_unresolved_ids(self):
        for kind in ('long', 'short', 'stream'):
            with self.subTest(kind=kind):
                repo = Repository(owned(kind))
                budget = PageBudget(1)
                options = self.options(repo, page_budget=budget)
                result = run_typed_inventory_batch(**options)
                repo.claim_initial_source.assert_called_once_with(job_id=JOB,
                    job_lease_token=JOB_LEASE, worker_id='worker', run_id=RUN, lease_seconds=900)
                repo.claim_channels.assert_not_called(); repo.claim_pass.assert_not_called()
                self.assertEqual(repo.events, ['begin', 'bind', 'fetch', 'settle'])
                self.assertEqual(budget.attempts, 1)
                self.assertEqual(result['provider_attempts'], 1)
                self.assertEqual(options['fetch_page'].call_args.kwargs['max_results'], 50)
                self.assertEqual(options['before_dispatch'].call_args.args[2], TRANSITION)
                self.assertEqual(result['results'][0]['observed_video_ids'], [VIDEO])
                self.assertEqual(result['results'][0]['canonical_video_ids'], [])
                self.assertEqual(result['results'][0]['receipt']['observed_video_ids'], [VIDEO])

    def test_empty_job_claim_does_not_fall_back_or_spin(self):
        repo = Repository()
        repo.claim_initial_source.return_value = []
        options = self.options(repo)
        result = run_typed_inventory_batch(**options)
        self.assertEqual((result['claimed'], result['provider_attempts']), (0, 0))
        repo.claim_initial_source.assert_called_once()
        repo.claim_channels.assert_not_called(); repo.claim_pass.assert_not_called()
        self.assertEqual(repo.events, [])
        options['before_dispatch'].assert_not_called(); options['fetch_page'].assert_not_called()

    def test_foreign_claim_is_not_begun_adopted_or_released_even_when_stop_arrives(self):
        for change in ({'channel_id': 'UC' + 'b' * 22}, {'video_type': 'short'},
                       {'lease_channel_epoch': 8}, {'scan_kind': 'discovery'}):
            with self.subTest(change=change):
                repo = Repository()
                repo.claim_initial_source.return_value = [replace(owned(), **change)]
                options = self.options(repo)
                with patch('collection.inventory.stop_at_boundary', side_effect=[False, True]), \
                        patch('collection.inventory.release_unstarted_playlists') as release:
                    with self.assertRaisesRegex(RuntimeError, 'scope mismatch'):
                        run_typed_inventory_batch(**options)
                    release.assert_not_called()
                self.assertEqual(repo.events, [])
                options['before_dispatch'].assert_not_called(); options['fetch_page'].assert_not_called()

    def test_job_dispatch_requires_complete_planned_exact_scope_before_claim(self):
        cases = ({'initial_job_id': None}, {'initial_job_lease_token': None},
                 {'initial_job_id': 'bad'}, {'initial_job_lease_token': 'bad'},
                 {'run_id': 'bad'}, {'transition_id': 'bad'}, {'transition_id': None},
                 {'before_dispatch': None}, {'limit': 2}, {'explicit_channel_ids': None},
                 {'explicit_channel_ids': [CHANNEL, 'UC' + 'b' * 22]},
                 {'video_types': ('long', 'short')}, {'expected_channel_scopes': None},
                 {'expected_channel_scopes': [{'channel_id': CHANNEL, 'control_epoch': True}]})
        for index, change in enumerate(cases):
            with self.subTest(case=index):
                repo = Repository()
                with self.assertRaises((TypeError, ValueError)):
                    run_typed_inventory_batch(**self.options(repo, **change))
                repo.claim_initial_source.assert_not_called()
                repo.claim_channels.assert_not_called(); repo.claim_pass.assert_not_called()
                self.assertEqual(repo.events, [])

    def test_full_page_missing_or_wrong_observed_proof_retains_unknown_ack(self):
        for proof in (None, [VIDEO, VIDEO], ['other000001'], [VIDEO] * 51, ['bad']):
            with self.subTest(proof=proof):
                repo = Repository()
                if proof is None:
                    repo.omit_observed = True
                else:
                    repo.receipt_changes['observed_video_ids'] = proof
                options = self.options(repo)
                with patch('collection.inventory.release_unstarted_playlists') as release:
                    result = run_typed_inventory_batch(**options)
                self.assertTrue(result['settlement_unknown'])
                self.assertEqual(result['provider_attempts'], 1)
                repo.fail_scan.assert_not_called()
                self.assertTrue(all(not call.args[1] for call in release.call_args_list))

    def test_ordinary_addressed_path_accepts_legacy_receipt_without_observed_ids(self):
        repo = Repository()
        repo.omit_observed = True
        repo.claim_channels = Mock(return_value=[owned()])
        options = self.options(repo, initial_job_id=None, initial_job_lease_token=None,
                               before_dispatch=None, transition_id=None)
        result = run_typed_inventory_batch(**options)
        repo.claim_initial_source.assert_not_called()
        repo.claim_channels.assert_called_once()
        self.assertEqual(result['processed'], 1)
        self.assertNotIn('observed_video_ids', result['results'][0])
        self.assertFalse(result.get('settlement_unknown'))

    def test_present_observed_proof_is_validated_even_outside_full_scope(self):
        repo = Repository()
        repo.claim_channels = Mock(return_value=[owned()])
        repo.receipt_changes['observed_video_ids'] = ['bad']
        result = run_typed_inventory_batch(**self.options(repo, initial_job_id=None,
            initial_job_lease_token=None, before_dispatch=None, transition_id=None))
        self.assertTrue(result['settlement_unknown'])
        repo.fail_scan.assert_not_called()

    def test_replayed_binding_never_calls_provider(self):
        repo = Repository()
        options = self.options(repo, before_dispatch=Mock(return_value=False))
        result = run_typed_inventory_batch(**options)
        self.assertTrue(result['settlement_unknown'])
        self.assertEqual(result['provider_attempts'], 0)
        self.assertEqual(repo.events, ['begin'])
        options['fetch_page'].assert_not_called()

    def test_quota_error_retains_one_attempt_and_exact_playlist_deferral(self):
        class YouTubeDailyQuotaExceededError(RuntimeError):
            pass
        repo = Repository()
        result = run_typed_inventory_batch(**self.options(repo,
            fetch_page=Mock(side_effect=YouTubeDailyQuotaExceededError())))
        self.assertTrue(result['quota_exhausted'])
        self.assertEqual(result['provider_attempts'], 1)
        self.assertEqual(repo.defer_claims_after_daily_quota.call_args.args[0], [owned()])
        self.assertEqual(repo.defer_claims_after_daily_quota.call_args.kwargs['transition_id'], TRANSITION)
        repo.fail_scan.assert_not_called()
        repo.claim_initial_source.assert_called_once()

    def test_initial_reconciliation_zero_pages_creates_no_repository_or_claim(self):
        options = self.collect_options(max_pages=0)
        with patch.object(initial, 'TypedInventoryRepository') as repository:
            result = initial.collect_initial_inventory_chunk(CHANNEL, **options)
            repository.assert_not_called()
        self.assertEqual(result, {'pages': 0, 'items': 0, 'outcome': 'stored_inventory'})
        options['fetch_page'].assert_not_called(); options['before_dispatch'].assert_not_called()
        for change in ({'initial_job_lease_token': None}, {'initial_job_id': 'bad'},
                       {'video_type': None}, {'max_pages': 2}):
            with self.subTest(change=change), patch.object(initial, 'TypedInventoryRepository') as repository:
                with self.assertRaises((ValueError, TypeError)):
                    initial.collect_initial_inventory_chunk(CHANNEL, **self.collect_options(**change))
                repository.assert_not_called()

    def test_runtime_and_initial_forward_job_scope_to_actual_owner(self):
        repo = Repository(owned('stream'))
        provider = Mock(return_value=page())
        supplied = dict(database=object(), settings=CollectionSettings(True, True, True),
            fetch_full=Mock(), fetch_metadata=Mock(), fetch_counters=Mock(),
            build_full_payload=Mock(), build_counter_payload=Mock(), is_quota_exhausted=Mock(),
            worker_id_factory=lambda: 'worker', fetch_page=provider,
            get_longform_playlist_id=Mock(), parse_dt=Mock(), playlist_not_found_error=LookupError,
            daily_quota_exceeded_error=RuntimeError, copy_collection_stop=Mock())
        callbacks = bind_collection_callbacks(**supplied)
        with patch.object(initial, 'TypedInventoryRepository', return_value=repo):
            result = callbacks['run_initial_inventory_chunk'](CHANNEL, video_type='stream',
                expected_epoch=7, transition_id=TRANSITION, before_dispatch=Mock(return_value=True),
                initial_job_id=JOB, initial_job_lease_token=JOB_LEASE)
        kwargs = repo.claim_initial_source.call_args.kwargs
        self.assertEqual((kwargs['job_id'], kwargs['job_lease_token'], kwargs['worker_id']),
                         (JOB, JOB_LEASE, 'worker:typed-inventory'))
        self.assertEqual(result['observed_video_ids'], [VIDEO])
        self.assertEqual(result['pages'], 1)
        self.assertEqual(provider.call_count, 1)
        with self.assertRaisesRegex(ValueError, 'bound scope'):
            callbacks['run_initial_inventory_chunk'](CHANNEL, expected_epoch=7, database=object())

    def test_hot_denial_aborts_only_exact_already_owned_job_lease(self):
        error = HotDatabaseAdmissionDenied()
        with patch('db.hot.hot_collection_control.HotCollectionControlRepository') as control:
            with self.assertRaises(HotDatabaseAdmissionDenied):
                dispatch_owned_rpc(object(), 'claim_initial_inventory_source',
                    {'p_job_id': JOB, 'p_job_lease_token': JOB_LEASE}, Mock(side_effect=error))
            control.return_value.abort.assert_called_once_with(
                operators=[{'job_id': JOB, 'lease_token': JOB_LEASE}])
        self.assertTrue(error.control_abort_attempted)


if __name__ == '__main__':
    unittest.main()
