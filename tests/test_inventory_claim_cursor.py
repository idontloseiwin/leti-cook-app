"""Inventory claims preserve bounded native lock acquisition and cursor ownership."""
from pathlib import Path
import sys, unittest
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'application'))
from db.base.postgres import Transaction, DatabaseError, DatabaseServerError
from db.inventory.inventory_claims import _CANDIDATES
from test_direct_pg_transactions import Connection, ServerFailure
from test_video_scheduled_cursor import Cursor

class InventoryCursorTests(unittest.TestCase):
    def setUp(self):
        self.rows = [dict(source='{}', epoch=1, channel_id=str(i), video_type='long') for i in range(80)]
        self.cursor = Cursor(self.rows)
        self.connection = Connection([])
        self.connection.cursor = Mock(return_value=self.cursor)
        self.gate = Mock()
        self.tx = Transaction(self.connection, self.gate, total_timeout_ms=8000)
        self.args = dict(claim_limit=80, channel_ids=None, video_types=['long','short','stream'],
            scopes_text=None, only_due=True, continuations_text=None, include_due=True,
            initial_job_id=None, initial_job_lease_token=None)
    def stream(self, **changes):
        return self.tx.inventory_claim_candidates({**self.args, **changes})
    def test_first_ten_then_fifty_and_early_close(self):
        with self.stream() as rows:
            self.assertEqual([next(rows)['channel_id'] for _ in range(11)], [str(i) for i in range(11)])
        self.assertEqual(self.cursor.sizes, [10,50]); self.assertEqual(len(self.cursor.rows),20)
        self.assertTrue(self.cursor.closed); self.assertFalse(self.tx._failed)
        self.assertFalse(self.tx._scheduled_cursor_active)
    def test_complete_stream_and_fixed_query(self):
        with self.stream(unused='ignored') as rows: self.assertEqual(list(rows),self.rows)
        self.assertEqual(self.cursor.sizes,[10,50,50,50])
        self.assertEqual(self.cursor.calls,[(_CANDIDATES,self.args)])
        options=self.connection.cursor.call_args.kwargs
        self.assertEqual(options['name'],'lic_inventory_claim_candidates')
        self.assertIs(options['withhold'],False); self.assertIs(options['scrollable'],False)
    def test_invalid_bounds_and_modes_never_dispatch(self):
        for change in ({'claim_limit':0},{'claim_limit':501},{'claim_limit':True},
                {'channel_ids':[]},{'channel_ids':['x']*1001},{'channel_ids':[123]},
                {'video_types':[]},{'video_types':['other']},{'video_types':['long']*4},
                {'only_due':None},{'include_due':1}):
            self.tx=Transaction(self.connection,self.gate)
            with self.assertRaises(DatabaseError):
                with self.stream(**change): pass
            self.assertTrue(self.tx._failed)
        self.connection.cursor.assert_not_called()
    def test_readonly_rpc_and_missing_parameter_rejected(self):
        for options,args in (({'read_only':True},self.args),
                ({'rpc_name':'cleanup_hot_operational_copies'},self.args),
                ({},{k:v for k,v in self.args.items() if k!='scopes_text'})):
            self.tx=Transaction(self.connection,self.gate,**options)
            with self.assertRaises(DatabaseError):
                with self.tx.inventory_claim_candidates(args): pass
            self.assertTrue(self.tx._failed)
        self.connection.cursor.assert_not_called()
    def test_nested_video_cursor_cannot_take_ownership(self):
        with self.assertRaises(DatabaseError):
            with self.stream():
                with self.assertRaises(DatabaseError):
                    with self.tx.video_scheduled_candidates(['one'],'run'): pass
                self.assertTrue(self.tx._scheduled_cursor_active)
        self.assertFalse(self.tx._scheduled_cursor_active)
        self.assertEqual(self.connection.cursor.call_count,1)
    def test_cursor_cannot_escape_context(self):
        with self.stream() as rows: next(rows)
        with self.assertRaises(DatabaseError): next(rows)
        self.assertTrue(self.tx._failed)
    def test_fetch_failure_sanitized_and_transaction_poisoned(self):
        self.cursor.error=ServerFailure('55P03')
        with self.assertRaises(DatabaseError):
            with self.stream() as rows:
                with self.assertRaises(DatabaseServerError) as caught: next(rows)
                self.assertEqual(caught.exception.code,'55P03')
                self.assertNotIn('private',str(caught.exception))
        self.assertTrue(self.tx._failed); self.assertTrue(self.cursor.closed)
    def test_deadline_and_admission_cover_each_fetch_and_buffer(self):
        self.tx._check_budget=Mock()
        with self.stream() as rows:
            for _ in range(11): next(rows)
        self.assertEqual(sum(c.kwargs.get('configure',False) for c in self.tx._check_budget.call_args_list),3)
        self.assertEqual(self.gate.call_count,6)
        self.tx=Transaction(self.connection,self.gate,total_timeout_ms=8000)
        with self.assertRaises(DatabaseServerError):
            with self.stream() as rows:
                next(rows); self.tx._deadline=0; next(rows)
        self.assertTrue(self.tx._failed)
    def test_driver_overreturn_fails_closed(self):
        with self.assertRaises(DatabaseError):
            with self.stream(claim_limit=1) as rows: next(rows)
        self.assertTrue(self.tx._failed); self.assertTrue(self.cursor.closed)
    def test_native_null_and_multidimensional_arrays_preserved(self):
        channels=[['channel',None]]; types=[['long','short']]
        with self.stream(channel_ids=channels,video_types=types) as rows:
            channels[0].append('later'); types[0].append('stream'); next(rows)
        self.assertEqual(self.cursor.calls[0][1]['channel_ids'],[['channel',None]])
        self.assertEqual(self.cursor.calls[0][1]['video_types'],[['long','short']])
    def test_nested_array_cardinality_limit_and_cycle_rejected(self):
        cycle=[]; cycle.append(cycle)
        for channels in ([['x']*501,['x']*501], cycle):
            self.tx=Transaction(self.connection,self.gate)
            with self.assertRaises(DatabaseError):
                with self.stream(channel_ids=channels): pass
            self.assertTrue(self.tx._failed)
        self.connection.cursor.assert_not_called()
    def test_bound_arrays_are_copied_before_yield(self):
        channels=['channel']; types=['long']
        with self.stream(channel_ids=channels,video_types=types) as rows:
            channels.append('later'); types.append('short'); next(rows)
        self.assertEqual(self.cursor.calls[0][1]['channel_ids'],['channel'])
        self.assertEqual(self.cursor.calls[0][1]['video_types'],['long'])

if __name__=='__main__': unittest.main()
