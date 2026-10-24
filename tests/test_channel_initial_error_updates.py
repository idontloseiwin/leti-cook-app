"""Offline guards for finite unwired initial-error UPDATE stages."""
from collections import deque,UserDict
from contextlib import contextmanager
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
from psycopg.types.json import Jsonb
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'application'))
from db.channel import channel_initial_error_updates as writer
from db.channel import channel_metadata_batch as metadata
from db.jobs import operator_job_enqueuing as enqueue
from db.jobs import operator_job_lifecycle as lifecycle
from db.jobs import operator_job_inventory_page_settlement as page
from db.jobs import operator_job_inventory_settlement as settlement
from db.jobs import operator_job_retry as retry
from db.base.postgres import DatabaseError,DatabaseServerError,DatabaseUnavailable,_reviewed_statement


def rendered(value):return value.as_string() if hasattr(value,'as_string') else value

def native_row(**changes):
    return dict(identity='channel',old_text='old-native',proposed_text='new-native',estimate_text=None,**changes)

class Transaction:
    def __init__(self,*answers):self.answers=deque(answers);self.calls=[];self.events=[]
    @contextmanager
    def borrow(self):
        self.events.append('borrow')
        try:yield self
        except BaseException:self.events.append('poison');raise
    def answer(self,kind,statement,parameters):
        self.calls.append((kind,rendered(statement),parameters))
        answer=self.answers.popleft()
        if isinstance(answer,BaseException):raise answer
        return answer
    def execute(self,s,p=()):return self.answer('execute',s,p)
    def fetch_one(self,s,p=()):return self.answer('one',s,p)
    def returning_one(self,s,p=()):return self.answer('returning',s,p)


class UpdateTests(unittest.TestCase):
    def test_complete_native_physical_order_matches_schema(self):
        schema=(ROOT/'schema/schema.sql').read_text()
        body=re.search(r'CREATE TABLE public.channels \((.*?)\n\);',schema,re.S).group(1)
        columns=re.findall(r'^    (?!CONSTRAINT)(\w+) ',body,re.M)
        self.assertEqual(columns,[name for name,_ in writer.COLUMN_TYPES]);self.assertEqual(len(columns),39)

    def test_untargeted_generic_keeps_single_original_update(self):
        values={'title':'changed','initial_long_inventory_error_count':None}
        tx=Transaction(1)
        with patch.object(writer,'normalize_initial_error_fields',side_effect=AssertionError('effect')):
            self.assertEqual(writer.update_channel_fields(tx,'channel',values),1)
        self.assertEqual(len(tx.calls),1);kind,sql,params=tx.calls[0]
        self.assertEqual(kind,'execute');self.assertNotIn('FOR ',sql);self.assertNotIn('initial_long_inventory_error_at',sql)
        self.assertIsInstance(params[0],Jsonb);self.assertEqual(params[0].obj,values);self.assertEqual(params[1],'channel')

    def test_control_mapping_adapter_copies_to_plain_dict_before_json_binding(self):
        values=UserDict({'title':'mapped'});tx=Transaction(1)
        writer.update_channel_fields(tx,'channel',values)
        bound=tx.calls[0][2][0].obj
        self.assertIs(type(bound),dict);self.assertEqual(bound,dict(values))

    def test_generic_targets_lock_full_native_old_and_preserve_srf_cast_boundary(self):
        tx=Transaction(native_row(),1)
        values={'title':'x','initial_long_inventory_status':'completed'}
        with patch.object(writer,'normalize_initial_error_fields',return_value='normalized') as effect:
            self.assertEqual(writer.update_channel_fields(tx,'channel',values),1)
        query=tx.calls[0][1]
        self.assertIn('CROSS JOIN jsonb_populate_record(NULL::public.channels,%s::jsonb)',query)
        self.assertIn('FOR NO KEY UPDATE OF channel',query);self.assertNotIn('FOR UPDATE OF',query)
        self.assertIn('to_jsonb(locked.current)::text',query)
        effect.assert_called_once_with(tx,proposed_columns_text='new-native',old_columns_text='old-native',status_targeted=True)
        final=tx.calls[1][1]
        for name in (*values,*writer.ERROR_FIELDS):self.assertIn('"'+name+'"=proposed."'+name+'"',final)
        self.assertNotIn('"channel_metadata_status"=',final)
        self.assertEqual(tx.calls[1][2],('normalized','channel'))

    def test_no_match_skips_effect_and_final_dml(self):
        for invoke in (lambda tx:writer.update_channel_fields(tx,'absent',{writer._STATUS:'error'}),
                       lambda tx:writer.apply_channel_stage(tx,'page_finish',('absent',))):
            tx=Transaction(None)
            with patch.object(writer,'normalize_initial_error_fields',side_effect=AssertionError('effect')):
                self.assertEqual(invoke(tx),0)
            self.assertEqual(len(tx.calls),1)
        tx=Transaction(None)
        self.assertIsNone(writer.apply_channel_stage(tx,'record_error',('code','absent')))

    def test_estimate_sql_null_and_json_null_are_separate_native_text(self):
        for estimate in (None,'null','{"huge":'+'9'*5001+'}'):
            row=native_row();row['estimate_text']=estimate;tx=Transaction(row,1)
            with patch.object(writer,'normalize_initial_error_fields',return_value='normalized'):
                writer.update_channel_fields(tx,'channel',{writer._STATUS:'completed',writer._ESTIMATE:None})
            self.assertIn('"initial_inventory_estimate"=%s::jsonb',tx.calls[1][1])
            self.assertEqual(tx.calls[1][2],(estimate,'normalized','channel'))

    def test_no_effect_stages_execute_only_their_original_fields(self):
        names=('enqueue_pending','claim_running','page_absent','page_pending')
        self.assertEqual({name for name,spec in writer._STAGES.items() if spec.no_effect},set(names))
        for name in names:
            tx=Transaction(1)
            with patch.object(writer,'normalize_initial_error_fields',side_effect=AssertionError('effect')):
                self.assertEqual(writer.apply_channel_stage(tx,name,('channel',)),1)
            self.assertEqual(tx.calls,[('execute',rendered(writer._STAGES[name].original()),('channel',))])
        self.assertNotIn('initial_long_inventory_error_count',rendered(writer._STAGES['claim_running'].original()))
        self.assertIn('coalesce(channel.initial_long_inventory_started_at,now())',rendered(writer._STAGES['claim_running'].original()))

    def test_record_increment_and_clock_stay_native_before_effect(self):
        tx=Transaction(native_row(),{'value':{'error_count':8}})
        with patch.object(writer,'normalize_initial_error_fields',return_value='normalized'):
            result=writer.apply_channel_stage(tx,'record_error',('recorded_error','channel'))
        self.assertEqual(result,{'value':{'error_count':8}})
        self.assertIn('(channel.initial_long_inventory_error_count+1)::integer',tx.calls[0][1])
        self.assertIn('(now())::timestamptz',tx.calls[0][1]);self.assertNotIn('clock_timestamp',tx.calls[0][1])
        self.assertEqual(tx.calls[0][2],('recorded_error','channel'))
        self.assertEqual(tx.calls[1][0],'returning');self.assertIn('RETURNING jsonb_build_object',tx.calls[1][1])
        self.assertNotIn('+1',tx.calls[1][1])

    def test_retry_keeps_complete_operator_job_composite_and_native_predicate(self):
        for name in ('retry_finish','retry_pending'):
            query=rendered(writer._STAGES[name].proposal())
            self.assertIn('jsonb_populate_record(NULL::public.operator_jobs,%s::jsonb) j',query)
            self.assertIn('WHERE channel.channel_id=j.channel_id',query)
            self.assertIn('cardinality(j.initial_video_types)=3',query)
            self.assertNotIn('json.loads',query)
        query=rendered(writer._STAGES['retry_pending'].proposal())
        self.assertIn("j.inventory_video_type<>'long'",query);self.assertIn(" ? 'long'",query)

    def test_final_target_is_captured_locked_identity_even_when_input_job_is_text(self):
        row=native_row();row['identity']='native-channel';tx=Transaction(row,1)
        raw='{"channel_id":"native-channel"}'
        with patch.object(writer,'normalize_initial_error_fields',return_value='normalized'):
            writer.apply_channel_stage(tx,'retry_finish',(raw,))
        self.assertEqual(tx.calls[0][2],(raw,));self.assertEqual(tx.calls[1][2],('normalized','native-channel'))
        self.assertIn('WHERE channel.channel_id=%s',tx.calls[1][1])

    def test_each_dynamic_stage_writes_only_exact_original_targets_and_error_fields(self):
        for name,spec in writer._STAGES.items():
            if spec.no_effect:continue
            fields=tuple(n for n,_ in spec.expressions)
            final=rendered(writer._final_statement(fields,returning=spec.returning))
            targets=re.findall(r'"([a-z_0-9]+)"=',final)
            self.assertEqual(targets,list(dict.fromkeys((*fields,*writer.ERROR_FIELDS))),name)

    def test_native_failure_never_retries_or_reaches_final_update(self):
        failure=DatabaseServerError('database_operation_failed',code='22003');tx=Transaction(failure)
        with self.assertRaises(DatabaseServerError) as caught:writer.apply_channel_stage(tx,'record_error',('error','channel'))
        self.assertIs(caught.exception,failure);self.assertEqual(len(tx.calls),1);self.assertEqual(tx.events,['borrow','poison'])

    def test_effect_failure_and_unknown_ack_propagate_once(self):
        failure=DatabaseUnavailable('database_operation_failed')
        tx=Transaction(native_row())
        with patch.object(writer,'normalize_initial_error_fields',side_effect=failure):
            with self.assertRaises(DatabaseUnavailable):writer.apply_channel_stage(tx,'page_finish',('channel',))
        self.assertEqual(len(tx.calls),1)
        tx=Transaction(native_row(),failure)
        with patch.object(writer,'normalize_initial_error_fields',return_value='normalized'):
            with self.assertRaises(DatabaseUnavailable):writer.apply_channel_stage(tx,'page_finish',('channel',))
        self.assertEqual(len(tx.calls),2)

    def test_invalid_native_shape_never_writes(self):
        for row in ({},dict(identity=3,old_text='x',proposed_text='y',estimate_text=None),
                    dict(identity='c',old_text='x',proposed_text={},estimate_text=None)):
            tx=Transaction(row)
            with self.assertRaisesRegex(DatabaseError,'database_result_shape_invalid'):
                writer.apply_channel_stage(tx,'page_finish',('channel',))
            self.assertEqual(len(tx.calls),1)

    def test_fixed_proposals_and_final_statements_pass_driver_and_bindings(self):
        for name,spec in writer._STAGES.items():
            count=rendered(spec.original()).count('%s');parameters=('value',)*count
            statement=spec.original() if spec.no_effect else spec.proposal()
            text=_reviewed_statement(statement,read=not spec.no_effect,max_rows=1 if not spec.no_effect else None)
            query=PostgresQuery(Transformer());query.convert(text,parameters)
            self.assertEqual(len(query.params),count)
        query=PostgresQuery(Transformer());query.convert(_reviewed_statement(writer._final_statement((writer._STATUS,)),read=False,max_rows=None),('native','channel'))
        self.assertEqual(len(query.params),2)

    def test_original_stages_match_each_current_owner(self):
        originals={'enqueue_pending':enqueue._PENDING,'claim_running':lifecycle._START_INVENTORY,
          'page_absent':page._ABSENT_CHANNEL,'page_pending':page._CHANNEL_PENDING,'page_finish':page._CHANNEL_FINISH,
          'page_all':page._CHANNEL_ALL,'full_settlement':settlement._CHANNEL,
          'retry_finish':retry._CHANNEL_FINISH,'retry_pending':retry._CHANNEL_PENDING,'record_error':metadata._RECORD_ERROR}
        def canonical(text):
            text=rendered(text).replace('UPDATE public.channels channel','UPDATE public.channels')
            text=re.sub(r'%\([a-z_]+\)s','%s',text).replace('channel.','').replace('"','')
            return re.sub(r'\s+','',text).replace('%s::text','%s')
        for name,original in originals.items():
            self.assertEqual(canonical(writer._STAGES[name].original()),canonical(original),name)

if __name__=='__main__':unittest.main()
