"""Prepared effect protocol guards; native trigger parity is a separate gate."""
from collections import deque
from contextlib import contextmanager
import inspect
import json
from pathlib import Path
import sys
import unittest
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'application'))
from db.channel import channel_initial_error_fields as effect
from db.base.postgres import DatabaseError,DatabaseServerError,_reviewed_statement

OLD='{"channel_id":"old-native-row"}'
NEW='{"channel_id":"new-native-row"}'

def facts(**changes):
    result=dict(proposed_text=NEW,completed=False,errored=False,was_error=False,previous_count=8)
    result.update(changes);return result

class Transaction:
    def __init__(self,*answers):self.answers=deque(answers);self.calls=[];self.events=[]
    @contextmanager
    def borrow(self):
        self.events.append('borrow')
        try:yield self
        except BaseException:self.events.append('poison');raise
    def fetch_one(self,statement,parameters=()):
        self.calls.append((statement,parameters))
        answer=self.answers.popleft()
        if isinstance(answer,BaseException):raise answer
        return answer

def update(tx,**kwargs):
    return effect.normalize_initial_error_fields(tx,proposed_columns_text=NEW,old_columns_text=OLD,status_targeted=True,**kwargs)

class InitialErrorEffectTests(unittest.TestCase):
    def test_untargeted_update_is_exact_text_identity_and_opens_nothing(self):
        raw='{"initial_long_inventory_status":"completed","private":{"huge":'+'9'*5001+'}}'
        tx=Transaction()
        self.assertIs(effect.normalize_initial_error_fields(tx,proposed_columns_text=raw,old_columns_text=OLD),raw)
        self.assertEqual(tx.calls,[]);self.assertEqual(tx.events,[])
    def test_same_value_completed_update_still_clears(self):
        tx=Transaction(facts(completed=True),{'proposed_text':'cleared-native-row'})
        self.assertEqual(update(tx),'cleared-native-row')
        self.assertEqual(tx.calls,[(effect._FACTS,(NEW,OLD)),(effect._COMPLETED,(NEW,))])
        self.assertEqual(tx.events,['borrow'])
    def test_error_transition_uses_native_previous_int32_without_incrementing_python(self):
        tx=Transaction(facts(errored=True,previous_count=2147483647),{'proposed_text':'native-result'})
        self.assertEqual(update(tx),'native-result')
        self.assertEqual(tx.calls[1],(effect._ERROR,(NEW,2147483647)))
        self.assertIn('%s::integer+1',effect._ERROR)
    def test_native_int32_overflow_propagates_and_never_retries(self):
        error=DatabaseServerError('database_operation_failed',code='22003')
        tx=Transaction(facts(errored=True,previous_count=2147483647),error)
        with self.assertRaises(DatabaseServerError) as caught:update(tx)
        self.assertIs(caught.exception,error)
        self.assertEqual(len(tx.calls),2);self.assertEqual(tx.events,['borrow','poison'])
    def test_repeat_error_preserves_explicit_record_error_increment_and_nulls(self):
        tx=Transaction(facts(errored=True,was_error=True))
        self.assertEqual(update(tx),NEW)
        self.assertEqual(len(tx.calls),1)
    def test_insert_runs_without_status_target_and_has_previous_zero(self):
        tx=Transaction(facts(errored=True,previous_count=123),{'proposed_text':'insert-result'})
        self.assertEqual(effect.normalize_initial_error_fields(tx,proposed_columns_text=NEW,inserted=True),'insert-result')
        self.assertEqual(tx.calls,[(effect._FACTS,(NEW,None)),(effect._ERROR,(NEW,0))])
    def test_insert_completed_and_other_status_dispatch(self):
        tx=Transaction(facts(completed=True),{'proposed_text':'done'})
        self.assertEqual(effect.normalize_initial_error_fields(tx,proposed_columns_text=NEW,inserted=True),'done')
        for values in (facts(),facts(was_error=True)):
            tx=Transaction(values);self.assertEqual(update(tx),NEW);self.assertEqual(len(tx.calls),1)
    def test_empty_string_code_and_supplied_time_are_left_to_native_coalesce(self):
        self.assertIn("coalesce((proposed).initial_long_inventory_error_code,'initial_inventory_failed')",effect._ERROR)
        self.assertIn('coalesce((proposed).initial_long_inventory_error_at,now())',effect._ERROR)
        self.assertNotIn('clock_timestamp',effect._ERROR)
        self.assertNotIn('nullif',effect._ERROR)
    def test_only_three_fields_are_patched_and_full_native_text_remains_opaque(self):
        raw='{"initial_inventory_estimate":{"count":'+'8'*5001+'},"initial_long_inventory_error_count":2}'
        tx=Transaction(facts(errored=True,proposed_text=raw),{'proposed_text':raw})
        self.assertEqual(update(tx),raw)
        self.assertEqual(tx.calls[1][1],(raw,8))
        for field in effect.ERROR_FIELDS:
            self.assertIn("'"+field+"'",effect._ERROR)
            self.assertIn("'"+field+"'",effect._COMPLETED)
        self.assertNotIn('json.loads',inspect.getsource(effect))
    def test_malformed_native_owner_input_rejected_before_borrow(self):
        for kwargs in (dict(proposed_columns_text={}),dict(proposed_columns_text=NEW),
                       dict(proposed_columns_text=NEW,inserted=True,old_columns_text=OLD),
                       dict(proposed_columns_text=NEW,old_columns_text=OLD,status_targeted=1)):
            tx=Transaction()
            with self.assertRaisesRegex(ValueError,'channel_initial_error_native_row_invalid'):
                effect.normalize_initial_error_fields(tx,**kwargs)
            self.assertEqual(tx.events,[])
    def test_malformed_database_facts_or_patch_reject_without_write(self):
        for answer in ({},facts(previous_count=True),facts(previous_count=2147483648),facts(completed=None),facts(proposed_text={})):
            tx=Transaction(answer)
            with self.assertRaisesRegex(DatabaseError,'database_result_shape_invalid'):update(tx)
            self.assertEqual(tx.events,['borrow','poison'])
        tx=Transaction(facts(completed=True),{'proposed_text':None})
        with self.assertRaisesRegex(DatabaseError,'database_result_shape_invalid'):update(tx)
    def test_fixed_statement_bindings_pass_readonly_driver_boundary(self):
        for statement,params in ((effect._FACTS,(NEW,OLD)),(effect._COMPLETED,(NEW,)),(effect._ERROR,(NEW,8))):
            _reviewed_statement(statement,read=True,max_rows=1)
            query=PostgresQuery(Transformer());query.convert(statement,params)
            self.assertEqual(len(query.params),len(params))
        source=inspect.getsource(effect)
        for token in ('INSERT INTO','UPDATE public.','DELETE FROM','database.transaction','transaction.execute','FOR UPDATE','retry('):
            self.assertNotIn(token,source)

if __name__=='__main__':unittest.main()