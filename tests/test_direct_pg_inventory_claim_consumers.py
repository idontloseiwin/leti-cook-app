"""Four public inventory claim routes preserve admission and one dispatch."""
from pathlib import Path
from unittest.mock import Mock,patch
import sys,unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'application'))
from db.inventory import inventory_claims as helper
from db.inventory.typed_inventory import TypedInventoryRepository,TypedInventoryClaim
from db.hot.hot_collection_control import uncertain_collection_ack
from db.base.postgres import DatabaseUnavailable,DatabaseServerError
from hot_runtime import HotDatabaseAdmissionDenied
CHANNEL='UC'+'a'*22
ID='f8900000-0000-4000-8000-000000000001'
TOKEN='f8900000-0000-4000-8000-000000000002'
PROOF=dict(channel_id=CHANNEL,video_type='short',scan_id=ID,control_epoch=7,last_transition_id=TOKEN)
SCOPES=[dict(channel_id=CHANNEL,control_epoch=7)]
NAMES=('claim_due_typed_inventory','claim_inventory_pass','claim_typed_inventory_channels','claim_initial_inventory_source')


def row():
    return dict(channel_id=CHANNEL,video_type='short',playlist_id='UUSH'+CHANNEL[2:],lease_token=TOKEN,
        lease_channel_epoch=7,lease_expires_at='2026-10-06T12:00:00.123450+00:00',no_new_streak=2,
        active_scan_id=ID,resume_page_token='next',scan_kind='full_reconciliation',
        scan_boundary_at='2026-10-05T12:00:00.123450+00:00')


class InventoryClaimConsumerTests(unittest.TestCase):
    def setUp(self):
        self.db=Mock(spec=['call','transaction']);self.db.call.side_effect=AssertionError('RPC forbidden')
        self.repository=TypedInventoryRepository(self.db)
    def invoke(self,name):
        if name==NAMES[0]:return self.repository.claim_due(limit='3',worker_id='worker',run_id='run',lease_seconds='120')
        if name==NAMES[1]:return self.repository.claim_pass(continuations=[PROOF],limit=3,include_due=False,worker_id='worker',run_id='run',lease_seconds=120)
        if name==NAMES[2]:return self.repository.claim_channels([' '+CHANNEL+' ',CHANNEL],video_types=['short','short'],
            worker_id='worker',run_id='run',lease_seconds=120,expected_channel_scopes=SCOPES)
        return self.repository.claim_initial_source(job_id=ID,job_lease_token=TOKEN,worker_id='worker',run_id=ID,lease_seconds=120)
    def test_exact_four_dispatches_preserve_normalization_and_native_timestamp_scale(self):
        common=dict(worker_id='worker',run_id='run',lease_seconds=120)
        expected=(dict(common,limit=3),dict(common,continuations=[PROOF],limit=3,include_due=False),
            dict(common,channel_ids=[CHANNEL],video_types=['short'],expected_channel_scopes=SCOPES,only_due=False),
            dict(job_id=ID,job_lease_token=TOKEN,worker_id='worker',run_id=ID,lease_seconds=120))
        for name,arguments in zip(NAMES,expected):
            with self.subTest(name=name),patch.object(helper,name,return_value=[row()]) as action:
                result=self.invoke(name);action.assert_called_once_with(self.db,**arguments)
                self.assertEqual(result,[TypedInventoryClaim.from_row(row())])
                self.assertEqual(result[0].scan_boundary_at,'2026-10-05T12:00:00.123450+00:00')
        self.db.call.assert_not_called();self.db.transaction.assert_not_called()
    def test_opt_in_only_due_reaches_exact_channel_claim_without_changing_default(self):
        with patch.object(helper,NAMES[2],return_value=[]) as action:
            self.repository.claim_channels([CHANNEL],video_types=['long'],worker_id='worker',run_id='run',
                lease_seconds=120,expected_channel_scopes=SCOPES,only_due=True)
        action.assert_called_once_with(self.db,channel_ids=[CHANNEL],video_types=['long'],worker_id='worker',
            run_id='run',lease_seconds=120,expected_channel_scopes=SCOPES,only_due=True)
        self.db.call.assert_not_called();self.db.transaction.assert_not_called()
    def test_existing_rpc_telemetry_is_kept_for_each_named_direct_operation(self):
        for name in NAMES:
            with patch.object(helper,name,return_value=[]),patch('db.inventory.typed_inventory.emit_event') as emit:self.invoke(name)
            self.assertEqual([c.args[3] for c in emit.call_args_list],['rpc:'+name]*2)
            self.assertEqual([c.kwargs['status'] for c in emit.call_args_list],['started','success'])
            self.assertEqual(emit.call_args_list[-1].kwargs['count'],0)
    def test_unknown_ack_and_server_error_never_repeat_or_abort_any_claim(self):
        for name in NAMES:
            for error in (DatabaseUnavailable('database_connection_failed',code='08006',outcome_unknown=True),
                          DatabaseServerError('inventory_claim_limit_invalid',code='P0001')):
                with patch.object(helper,name,side_effect=error) as action,patch('db.hot.hot_collection_control.HotCollectionControlRepository.abort') as abort:
                    with self.assertRaises(type(error)) as caught:self.invoke(name)
                    self.assertIs(caught.exception,error);action.assert_called_once();abort.assert_not_called()
        self.db.call.assert_not_called()
    def test_definite_initial_denial_aborts_only_exact_owned_job_once(self):
        error=HotDatabaseAdmissionDenied()
        with patch.object(helper,NAMES[3],side_effect=error) as action,patch('db.hot.hot_collection_control.HotCollectionControlRepository.abort',return_value={}) as abort:
            with self.assertRaises(HotDatabaseAdmissionDenied):self.invoke(NAMES[3])
            action.assert_called_once();abort.assert_called_once_with(operators=[dict(job_id=ID,lease_token=TOKEN)])
        self.assertTrue(error.control_abort_attempted);self.db.call.assert_not_called()
    def test_unowned_claim_denial_has_no_abort_and_prior_ack_suppresses_initial_abort(self):
        for name in NAMES[:3]:
            with patch.object(helper,name,side_effect=HotDatabaseAdmissionDenied()) as action,patch('db.hot.hot_collection_control.HotCollectionControlRepository.abort') as abort:
                with self.assertRaises(HotDatabaseAdmissionDenied):self.invoke(name)
                action.assert_called_once();abort.assert_not_called()
        error=HotDatabaseAdmissionDenied()
        with uncertain_collection_ack(),patch.object(helper,NAMES[3],side_effect=error) as action,patch('db.hot.hot_collection_control.HotCollectionControlRepository.abort') as abort:
            with self.assertRaises(HotDatabaseAdmissionDenied):self.invoke(NAMES[3])
            action.assert_called_once();abort.assert_not_called()
        self.assertTrue(error.outcome_may_be_committed)
    def test_zero_and_invalid_inputs_still_precede_any_database_dispatch(self):
        self.assertEqual(self.repository.claim_due(limit=0,worker_id='w',run_id='r'),[])
        self.assertEqual(self.repository.claim_pass(continuations=[],limit=0,include_due=True,worker_id='w',run_id='r'),[])
        self.assertEqual(self.repository.claim_channels([],worker_id='w',run_id='r'),[])
        for callback in (lambda:self.repository.claim_due(limit=501,worker_id='w',run_id='r'),
            lambda:self.repository.claim_pass(continuations=[dict(PROOF,extra=1)],limit=1,include_due=True,worker_id='w',run_id='r'),
            lambda:self.repository.claim_channels([CHANNEL],video_types=['bad'],worker_id='w',run_id='r'),
            lambda:self.repository.claim_initial_source(job_id=ID,job_lease_token=TOKEN,worker_id=' ',run_id=ID)):
            with self.assertRaises(ValueError):callback()
        self.db.call.assert_not_called();self.db.transaction.assert_not_called()
    def test_invalid_result_or_initial_overbound_rejects_after_one_helper_dispatch(self):
        for name in NAMES:
            for result in (None,{},[dict(row(),lease_channel_epoch=None)]):
                with patch.object(helper,name,return_value=result) as action:
                    with self.assertRaises((TypeError,ValueError,RuntimeError)):self.invoke(name)
                    action.assert_called_once()
        with patch.object(helper,NAMES[3],return_value=[row(),row()]) as action:
            with self.assertRaisesRegex(RuntimeError,'exceeded its bound'):self.invoke(NAMES[3])
            action.assert_called_once()


if __name__=='__main__':unittest.main()
