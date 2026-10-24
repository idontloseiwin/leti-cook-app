"""Focused guards for unwired direct quota writers; native parity is separate."""
from pathlib import Path
import sys,unittest
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.quota import quota_write_normalization as writer
from db.base.postgres import DatabaseError,DatabaseServerError,DatabaseUnavailable,_reviewed_statement
from test_operator_job_lifecycle import Database as BaseDatabase

class Database(BaseDatabase):
 def validate_operational_assignments(self,family,values,*,identity=None):
  self.assignment_validation=(family,dict(values),identity)


def row(**updates):
    return dict(columns_text='{}',phase='private-title',endpoint='videos',quota_bucket=None,error_type=None,**updates)
def text(statement):return statement.as_string() if hasattr(statement,'as_string') else statement

class QuotaWriterTests(unittest.TestCase):
    def test_start_canonicalizes_native_labels_without_reusing_python_input_types(self):
        tx=Database(row(),1)
        self.assertEqual(writer.start_quota_attempt(tx,{'project_id':'primary','phase':17}),1)
        self.assertEqual(tx.calls[0][2],('primary',17))
        self.assertEqual(tx.calls[-1][2],('other','videos','other',None,'{}'))
        self.assertEqual(tx.events,['borrow']);self.assertEqual(tx.operations,[])
        self.assertIn('%s::text AS "phase"',text(tx.calls[0][1]))
    def test_omitted_defaults_evaluated_once_and_never_in_final_insert(self):
        tx=Database(row(),1);writer.start_quota_attempt(tx,{'project_id':'primary'})
        proposal=text(tx.calls[0][1]);final=text(tx.calls[-1][1])
        self.assertIn('proposed AS MATERIALIZED',proposal)
        self.assertEqual(proposal.count('gen_random_uuid()'),1)
        self.assertEqual(proposal.count('now()'),2)
        self.assertNotIn('gen_random_uuid()',final);self.assertNotIn('now()',final)
    def test_explicit_null_is_not_default(self):
        values={name:None for name in ('request_attempt_id','quota_day','started_at','status')}
        tx=Database(row(),DatabaseServerError('database_operation_failed',code='23502'))
        with self.assertRaises(DatabaseServerError):writer.start_quota_attempt(tx,values)
        self.assertEqual(tx.calls[0][2],(None,)*4)
        self.assertNotIn('now()',text(tx.calls[0][1]));self.assertNotIn('gen_random_uuid()',text(tx.calls[0][1]))
        self.assertEqual(tx.events,['borrow','poison'])
    def test_finish_uses_matching_started_row_and_carries_inherited_labels(self):
        tx=Database(row(),1)
        self.assertEqual(writer.finish_quota_attempt(tx,'id',{'status':'success'}),1)
        proposal=text(tx.calls[0][1]);self.assertIn("ledger.status='started'",proposal)
        self.assertIn('FOR NO KEY UPDATE OF ledger',proposal);self.assertIn('locked.current',proposal)
        self.assertEqual(tx.calls[0][2],('success','id'))
        final=text(tx.calls[1][1]);self.assertIn('"status"=proposed."status"',final)
        for label in writer._LABELS:self.assertIn('"'+label+'"=safe."'+label+'"',final)
        self.assertEqual(tx.calls[1][2],('other','videos','other',None,'{}','id'))
    def test_finish_absent_or_terminal_has_no_write_or_normalization(self):
        tx=Database(None)
        self.assertEqual(writer.finish_quota_attempt(tx,'id',{'duration_ms':'bad'}),0)
        self.assertEqual(len(tx.calls),1)
    def test_supplied_canonical_labels_and_error_null_preserved(self):
        native=row();native.update(phase='initial_inventory',quota_bucket='general',error_type='HttpError')
        tx=Database(native,1);writer.finish_quota_attempt(tx,'id',{'error_type':'HttpError'})
        self.assertEqual(tx.calls[-1][2][:4],('initial_inventory','videos','general','HttpError'))
        self.assertEqual(text(tx.calls[-1][1]).count('"error_type"=safe."error_type"'),1)
    def test_bad_envelope_stops_before_database(self):
        for values in ({},{'bad-name':1},{f'f{x}':1 for x in range(257)}):
            for invoke in (lambda tx:writer.start_quota_attempt(tx,values),lambda tx:writer.finish_quota_attempt(tx,'id',values)):
                tx=Database()
                with self.assertRaises(ValueError):invoke(tx)
                self.assertEqual(tx.calls,[])
    def test_unknown_column_cannot_silently_be_ignored(self):
        for insert in (False,True):
            tx=Database(row())
            with self.assertRaises(DatabaseServerError) as caught:
                if insert:writer.start_quota_attempt(tx,{'unknown':3})
                else:writer.finish_quota_attempt(tx,'id',{'unknown':3})
            self.assertEqual(caught.exception.code,'42703');self.assertEqual(len(tx.calls),1)
            self.assertIn('ledger."unknown"',text(tx.calls[0][1]))
    def test_malformed_native_row_does_not_write(self):
        for native in (None,{},dict(columns_text='{}'),dict(row(),phase=3),dict(row(),columns_text=None)):
            tx=Database(native)
            with self.assertRaises(DatabaseError):writer.start_quota_attempt(tx,{'project_id':'primary'})
            self.assertEqual(len(tx.calls),1)
    def test_final_error_never_retries_or_acknowledges(self):
        for error in (DatabaseServerError('database_operation_failed',code='23503'),DatabaseUnavailable('database_connection_failed',outcome_unknown=True)):
            tx=Database(row(),error)
            with self.assertRaises(type(error)) as caught:writer.finish_quota_attempt(tx,'id',{'status':'success'})
            self.assertIs(caught.exception,error);self.assertEqual(len(tx.calls),2);self.assertEqual(tx.operations,[])
    def test_prepared_statements_pass_real_driver_guard_and_binding(self):
        for invoke in (lambda tx:writer.start_quota_attempt(tx,{'project_id':'primary'}),lambda tx:writer.finish_quota_attempt(tx,'id',{'status':'success'})):
            tx=Database(row(),1);invoke(tx)
            for mode,query,params in tx.calls:
                read=mode=='one';_reviewed_statement(query,read=read,max_rows=1 if read else 0)
                PostgresQuery(Transformer()).convert(query,params)

if __name__=='__main__':unittest.main()
