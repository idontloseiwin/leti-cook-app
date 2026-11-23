"""Missing-video receipt/incident transaction tests; no network or database."""
from pathlib import Path
import json,sys,unittest
import psycopg
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.videos import video_missing_completion as helper, incident_statement_effects as effects, video_queue_initialization as initialization
from collection_schedule import CollectionSchedules
from db.videos.collection_policy import cadence_parameter
from db.base.postgres import DatabaseError,DatabaseServerError,Operation,PostgresConfig,PostgresDatabase,_reviewed_statement
from test_direct_pg_transactions import Connection,Pool,Result,ServerFailure,DSN
from test_collection_policy import rows as cadence_rows,NOW
from test_video_stream_completion import payload,CHANNEL

def one(**values):return Result((values,))
def write(count=1):return Result(has_columns=False,rowcount=count)


def incident_results(ids, *, kind, closed, operation='update'):
    """Native OLD/NEW receipts and facts for the real borrowed effect writer."""
    if not ids:return [Result()]
    receipts=[]
    for video_id in ids:
        old=json.dumps(dict(video_id=video_id,closed_at=None))
        new=json.dumps(dict(video_id=video_id,closed_at=NOW.isoformat() if closed else None))
        receipts.append(dict(new_text=new) if operation=='insert' else dict(old_text=old,new_text=new))
    values=[Result(receipts),Result([dict(ordinality=index,operation=operation,
        old_video_id=None if operation=='insert' else video_id,new_video_id=video_id,
        old_closed=False,new_closed=closed,withdrawal=False,state_type=kind)
        for index,video_id in enumerate(ids,1)])]
    return values


class MissingCompletionTests(unittest.TestCase):
    def setUp(self):
        self.events=[];self.connection=Connection(self.events);self.pool=Pool(self.connection,self.events)
        self.database=PostgresDatabase(PostgresConfig(DSN),cadence_snapshot=CollectionSchedules().cadence_snapshot(),admission=lambda name:None,pool_factory=lambda _:self.pool).open()
        self.addCleanup(self.database.close)
    def queue(self,*values):self.connection.results.extend(values)
    def success(self,count=1,owned_count=None,pending_count=None,kind='stream',cadence=None):
        owned_count=count if owned_count is None else owned_count;pending_count=count if pending_count is None else pending_count
        rows=[payload(f'ms{i:09d}',video_type=kind,availability_status='private',availability_source='metadata') for i in range(count)]
        owned=rows[:owned_count];rules=cadence_rows() if cadence is None else cadence
        matches=[i for i,r in enumerate(rules) if r['video_type']==kind and r['min_age_seconds']==0]
        scopes=[dict(channel_id=CHANNEL,control_epoch=7)] if owned else []
        self.queue(one(count=count),one(valid=True),Result(tuple({'video_id':r['video_id']} for r in rows)),one(valid=True),
            one(mismatch=False),one(indices=list(range(1,pending_count+1))),one(stale=False),
            Result(tuple({'video_id':r['video_id']} for r in owned)),one(regression=False),one(mismatch=False),one(scopes=scopes),
            Result(),one(rows='[{"recorded_at":"10000-01-01T00:00:00+00:00"}]'),
            *incident_results([r['video_id'] for r in owned],kind=kind,closed=False),Result(),write(),write(),write(),
            one(sources=[dict(video_id=r['video_id'],video_type=kind,first_missing_at=None,observed_at=NOW.isoformat()) for r in owned]),
            write(owned_count),one(valid=True))
        if scopes:self.queue(one(channel_id=CHANNEL,control_epoch=7,is_hidden=False,owner_permission='accepted'))
        return rows
    def run_helper(self,rows):return helper.complete_typed_video_missing(self.database,rows)
    def test_complete_protocol_order_freezes_receipt_before_incident_effects(self):
        rows=self.success();self.assertEqual(self.run_helper(rows),1)
        statements=[sql for sql,params in self.connection.queries]
        expected=((2,helper._LOCK),(4,helper._MISMATCH),(6,helper._STALE),(7,helper._INSERT),(8,helper._REGRESSION),
            (9,helper._MISMATCH),(10,helper._SCOPES),(11,helper._INCIDENT_LOCK),(12,helper._HISTORY),
            (13,helper._INCIDENT_EFFECT_UPDATE),(14,effects._FACTS),(15,helper._INSERT_INCIDENT_EFFECT),(16,helper._DETECTED),(17,helper._CHANGED),
            (18,helper._VIDEO),(19,helper._POLICY),(20,helper._COMPLETE))
        for index,fragment in expected:self.assertIn(fragment,statements[index])
        self.assertIn('FOR SHARE',statements[-1]);self.assertEqual((len(statements),self.pool.borrowed,self.connection.commits),(23,1,1))
    def test_one_and_fifty_rows_share_constant_set_based_query_budget(self):
        for count in (1,50):
            start=len(self.connection.queries);rows=self.success(count);self.assertEqual(self.run_helper(rows),count)
            self.assertEqual(len(self.connection.queries)-start,23)
        self.assertIn(51,self.connection.fetch_sizes)
    def test_new_incident_receipt_does_not_clear_the_collection_lease(self):
        rows=self.success(kind='long')
        values=list(self.connection.results)
        values[13:16]=[Result(),*incident_results([rows[0]['video_id']],kind='long',closed=False,operation='insert')]
        self.connection.results.clear();self.connection.results.extend(values)
        self.assertEqual(self.run_helper(rows),1)
        sql=[q for q,_ in self.connection.queries]
        self.assertFalse(any("paused_reason='unavailable_inventory'" in q for q in sql))
        self.assertFalse(self.connection.results)

    def test_nonarray_empty_native_cast_and_batch_bound_order(self):
        for invalid in (None,{},'x'):
            with self.assertRaisesRegex(DatabaseServerError,'typed_video_missing_completion_requires_array'):self.run_helper(invalid)
        self.queue(one(count=0));self.assertEqual(self.run_helper([]),0)
        self.queue(Result(execute_error=ServerFailure('22P02')))
        with self.assertRaises(DatabaseError) as caught:self.run_helper([payload()]*51)
        self.assertEqual(caught.exception.code,'22P02')
        self.queue(one(count=51))
        with self.assertRaisesRegex(DatabaseError,'typed_video_missing_completion_input_invalid'):self.run_helper([payload()]*51)
        self.assertEqual(len(self.connection.queries),3)
    def test_domain_invalid_before_lock_and_long_source_is_required(self):
        self.queue(one(count=1),one(valid=False))
        with self.assertRaisesRegex(DatabaseError,'typed_video_missing_completion_input_invalid'):self.run_helper([payload()])
        self.assertEqual(len(self.connection.queries),2)
        self.assertIn("video_type IS NULL",helper._VALID);self.assertIn("availability_source='typed_video_collection_missing'",helper._VALID)
        self.assertIn("nullif(availability_status,'') IS NULL",helper._VALID)
    def test_queue_wait_error_precedes_source_clock_or_receipt_validation(self):
        self.queue(one(count=1),one(valid=True),Result(execute_error=ServerFailure('55P03')))
        with self.assertRaises(DatabaseError) as caught:self.run_helper([payload()])
        self.assertEqual(caught.exception.code,'55P03');self.assertEqual(len(self.connection.queries),3)
    def test_public_failure_order_and_atomic_rollback(self):
        vectors=((3,one(valid=False),'hot_source_clock_out_of_window'),(4,one(mismatch=True),'typed_video_missing_completion_receipt_mismatch'),
            (6,one(stale=True),'typed_video_missing_completion_stale_or_ineligible'),(8,one(regression=True),'typed_completion_observation_clock_regression'),
            (9,one(mismatch=True),'typed_video_missing_completion_receipt_mismatch'))
        for index,response,message in vectors:
            self.connection.results.clear();rows=self.success();self.connection.results[index]=response;start=len(self.connection.queries)
            with self.assertRaisesRegex(DatabaseError,message):self.run_helper(rows)
            self.assertEqual(len(self.connection.queries)-start,index+1)
        self.assertEqual(self.connection.commits,0)
    def test_exact_receipt_replay_skips_owned_effects_but_keeps_final_hot_clock(self):
        rows=self.success(owned_count=0,pending_count=0,cadence=[]);self.assertEqual(self.run_helper(rows),1)
        self.assertEqual(len(self.connection.queries),21)
        self.assertEqual(self.connection.queries[19][1]['owned_ids'],[])
        self.assertEqual(self.connection.queries[19][1]['schedules'].obj,[])
        self.assertIn('recorded_at',self.connection.queries[-1][0])
    def test_concurrent_receipt_loser_is_requested_count_without_owned_scope(self):
        rows=self.success(owned_count=0,pending_count=1,cadence=[]);self.assertEqual(self.run_helper(rows),1)
        self.assertEqual(self.connection.queries[7][1]['indices'],[1]);self.assertEqual(self.connection.queries[19][1]['owned_ids'],[])
    def test_incident_lock_error_after_receipt_and_scope_rolls_everything_back(self):
        rows=self.success();self.connection.results[11]=Result(execute_error=ServerFailure('55P03'))
        with self.assertRaises(DatabaseError) as caught:self.run_helper(rows)
        self.assertEqual(caught.exception.code,'55P03');self.assertEqual(len(self.connection.queries),12);self.assertEqual(self.connection.commits,0)
    def test_history_keeps_native_json_text_and_is_captured_after_incident_lock(self):
        rows=self.success();self.run_helper(rows)
        expected='[{"recorded_at":"10000-01-01T00:00:00+00:00"}]'
        for index in (13,15,16,17):self.assertEqual(self.connection.queries[index][1],(expected,))
        self.assertIn('ORDER BY incident.id DESC LIMIT 1',helper._HISTORY)
        self.assertNotIn('incident.last_seen_at<=',helper._HISTORY)
    def test_raw_fingerprint_includes_extra_fields_without_roundtrip(self):
        rows=self.success();rows[0]['opaque']={'count':9007199254740993};self.run_helper(rows)
        self.assertIs(self.connection.queries[7][1]['rows'].obj,rows);self.assertIn('md5(raw.payload::text)',helper._INPUT)
        self.assertIn('payload_fingerprint IS DISTINCT FROM',helper._MISMATCH)
    def test_all_types_share_episode_policy_without_renewing_metadata_clock(self):
        for kind in ('long','short','stream'):
            self.connection.queries.clear()
            rows=self.success(kind=kind)
            self.assertEqual(self.run_helper(rows),1)
            schedules=self.connection.queries[-3][1]['schedules'].obj
            self.assertEqual(schedules[0]['first_missing_at'],NOW.isoformat())
            self.assertEqual(schedules[0]['schedule_type'],kind)
            self.assertFalse(schedules[0]['stopped'])
        self.assertNotIn('metadata_source_checked_at=',helper._COMPLETE)
        self.assertNotIn('state.lease_token IS NULL',helper._OWNED_WHERE)
        self.assertIn('privacy_status=NULL,upload_status=NULL',helper._VIDEO)

    def test_policy_selects_only_owned_rows_for_all_types(self):
        self.assertIn(helper._OWNED_WHERE,helper._POLICY)
        self.assertIn(helper._OWNED_WHERE,helper._COMPLETE)
        self.assertNotIn("input.video_type<>'long'",helper._POLICY)

    def test_settlement_count_clock_and_scope_are_final_atomic_checks(self):
        for index,response,message in ((20,write(0),'typed_completion_owned_settlement_count_mismatch'),
            (21,one(valid=False),'hot_source_clock_out_of_window'),
            (22,one(channel_id=CHANNEL,control_epoch=8,is_hidden=False,owner_permission='accepted'),'channel_collection_scope_lost')):
            self.connection.results.clear();rows=self.success();self.connection.results[index]=response;start=len(self.connection.queries)
            with self.assertRaisesRegex(DatabaseError,message):self.run_helper(rows)
            self.assertEqual(len(self.connection.queries)-start,index+1)
        self.assertEqual(self.connection.commits,0)
    def test_borrowed_validation_poisons_parent_even_if_caught(self):
        with self.assertRaisesRegex(DatabaseError,'database_transaction_aborted'):
            with self.database.transaction(Operation('missing_parent')) as tx:
                try:helper.complete_typed_video_missing(None,None,transaction=tx)
                except DatabaseServerError:pass
        self.assertEqual(self.connection.commits,0)
    def test_unknown_commit_never_retries_or_returns_success(self):
        rows=self.success();self.connection.commit_error=psycopg.OperationalError('private detail')
        with self.assertRaises(DatabaseError) as caught:self.run_helper(rows)
        self.assertTrue(caught.exception.outcome_unknown);self.assertEqual(self.pool.borrowed,1)
    def test_fixed_sql_binds_and_no_retained_business_rpc_or_campaign_write(self):
        params=dict(rows=psycopg.types.json.Jsonb([]),indices=[],owned_ids=[],schedules=psycopg.types.json.Jsonb([]),cadence=cadence_parameter(self.database))
        history={helper._INCIDENT_EFFECT_UPDATE,helper._INSERT_INCIDENT_EFFECT,helper._DETECTED,helper._CHANGED}
        for name in ('_COUNT','_VALID','_LOCK','_MISMATCH','_PENDING','_STALE','_INSERT','_REGRESSION','_SCOPES','_INCIDENT_LOCK','_HISTORY',
                     '_INCIDENT_EFFECT_UPDATE','_INSERT_INCIDENT_EFFECT','_DETECTED','_CHANGED','_VIDEO','_POLICY','_COMPLETE'):
            sql=getattr(helper,name);write_statement=sql.startswith(('INSERT','UPDATE'));returning='RETURNING' in sql
            _reviewed_statement(sql,read=not write_statement or returning,max_rows=50 if not write_statement or returning else 0,returning=returning)
            PostgresQuery(Transformer()).convert(sql,('[]',) if sql in history else params)
        source=(ROOT/'application/db/videos/video_missing_completion.py').read_text()
        for forbidden in ('.call(', 'public.typed_video_collection_due_at(', 'public.typed_video_collection_identity_eligible(', 'UPDATE public.operator_jobs', 'UPDATE public.operator_job_targets'):
            self.assertNotIn(forbidden,source)


if __name__=='__main__':unittest.main()
