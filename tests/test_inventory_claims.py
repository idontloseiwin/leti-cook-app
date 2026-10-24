"""Offline transaction/ordering contracts for prepared playlist lease claims."""
from pathlib import Path
from datetime import datetime,timezone
from dataclasses import replace
from uuid import UUID
import sys
import unittest
from unittest.mock import patch
import psycopg
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.inventory import inventory_claims as claims
from collection_schedule import CollectionSchedules
from db.base.postgres import DatabaseError,DatabaseServerError,Operation,PostgresConfig,PostgresDatabase,_reviewed_statement
from test_direct_pg_transactions import Connection,Pool,Result,ServerFailure,DSN
from test_video_scheduled_cursor import Cursor as StreamingCursor

CHANNEL='UC0000000000000000000001';JOB='f8900000-0000-4000-8000-000000000001';TOKEN='f8900000-0000-4000-8000-000000000002'
SCAN='f8900000-0000-4000-8000-000000000003'
def one(**value):return Result((value,))
def write():return Result(has_columns=False,rowcount=1)
def args(**changes):
    return dict(claim_limit=3,channel_ids=None,video_types=['long','short','stream'],worker=' worker ',run_id=' run ',lease_seconds=900,
        scopes=None,scopes_text=None,only_due=True,continuations=None,continuations_text=None,include_due=True,
        initial_job_id=None,initial_job_lease_token=None,limit_invalid=False,channels_invalid=False,types_invalid=False,
        owner_invalid=False,lease_invalid=False,**changes)
def source(kind='long',channel=CHANNEL):return dict(source='{"video_type":"'+kind+'"}',epoch=17,channel_id=channel,video_type=kind)


class ClaimConnection(Connection):
    def __init__(self,events):super().__init__(events);self.streams=[]
    def cursor(self,**kwargs):
        if 'name' not in kwargs:return super().cursor()
        response=self.results.popleft();cursor=StreamingCursor(response.rows);execute=cursor.execute
        def record(sql,params):self.queries.append((sql,params));execute(sql,params)
        cursor.execute=record;cursor.error=response.fetch_error;self.streams.append(cursor);return cursor


class ClaimsTests(unittest.TestCase):
    def setUp(self):
        self.events=[];self.connection=ClaimConnection(self.events);self.pool=Pool(self.connection,self.events)
        self.database=PostgresDatabase(PostgresConfig(DSN),admission=lambda name:None,pool_factory=lambda _:self.pool,
            cadence_snapshot=CollectionSchedules().cadence_snapshot()).open()
        self.addCleanup(self.database.close)
        patcher=patch.object(claims,'assert_channel_scopes');self.fence=patcher.start();self.addCleanup(patcher.stop)
        patcher=patch.object(claims,'assert_full_initial_scope');self.scope=patcher.start();self.addCleanup(patcher.stop)
    def queue(self,*items):self.connection.results.extend(items)
    def core(self,**changes):
        with self.database.transaction(Operation('inventory_claim_test')) as tx:
            return claims.claim_inventory_states(tx,3,None,['long','short','stream'],' worker ',' run ',**changes)
    def initial(self,**changes):return claims.claim_initial_inventory_source(self.database,JOB,TOKEN,'worker','run',**changes)
    def initial_header(self,invalid=False):
        self.queue(one(job_id=JOB,job_lease_token=TOKEN,worker='worker',run_id='run',lease_seconds=900),
            one(channel_id=CHANNEL),one(channel_id=CHANNEL),one(job='{"opaque":1e1000}'),
            one(epoch=17,video_type='long',invalid=invalid))
    def initial_source(self,own=None,wait=False,active=None,owned_invalid=False):
        self.queue(one(playlist='{}'),one(own_id=own,proof='{}'),one(wait=wait,active_scan_id=active,owned_invalid=owned_invalid))
    def test_empty_due_claim_still_finishes_scope_fence_and_single_transaction(self):
        self.queue(one(**args()),Result());self.assertEqual(claims.claim_due_typed_inventory(self.database,3,'w','r'),[])
        self.fence.assert_called_once();self.assertEqual(self.fence.call_args.args[1],[])
        self.assertEqual((self.pool.borrowed,self.connection.commits),(1,1))
        self.assertIn(claims._CANDIDATES,self.connection.queries[-1][0]);self.assertEqual(self.connection.streams[-1].sizes,[10])
        self.assertTrue(self.connection.streams[-1].closed)
    def test_all_selected_rows_use_captured_epoch_and_deduplicated_final_scopes(self):
        self.queue(one(**args()),Result((source('long'),source('short'))),one(token=TOKEN,expires='10000-01-01T00:00:00+00:00'),
            one(scan='{}',scan_kind='initial_inventory'),one(video_type='long'),one(token=SCAN,expires='10000-01-01T00:00:00+00:00'),
            one(scan='{}',scan_kind='initial_inventory'),one(video_type='short'))
        self.assertEqual(self.core(),[{'video_type':'long'},{'video_type':'short'}])
        writes=[params for sql,params in self.connection.queries if claims._CLAIM in sql]
        self.assertEqual([p['epoch'] for p in writes],[17,17]);self.assertEqual([p['video_type'] for p in writes],['long','short'])
        self.fence.assert_called_once();self.assertEqual(self.fence.call_args.args[1],[dict(channel_id=CHANNEL,control_epoch=17)])
    def test_native_table_result_preserves_timestamp_microsecond_scale_and_uuid_shape(self):
        stamp=datetime(2026,10,6,1,2,3,123450,tzinfo=timezone.utc)
        self.queue(one(**args()),Result((source(),)),one(token=TOKEN,expires=stamp.isoformat()),
            one(scan='{}',scan_kind='initial_inventory'),
            one(lease_token=UUID(TOKEN),lease_expires_at=stamp,scan_boundary_at=stamp,active_scan_id=UUID(SCAN)))
        self.assertEqual(self.core(),[dict(lease_token=TOKEN,lease_expires_at='2026-10-06T01:02:03.123450+00:00',
            scan_boundary_at='2026-10-06T01:02:03.123450+00:00',active_scan_id=SCAN)])
        self.assertNotIn('jsonb_build_object',claims._RESULT)
    def test_completed_history_is_read_only_after_no_running_active_scan(self):
        self.queue(one(**args()),Result((source(),)),one(token=TOKEN,expires='2026-10-06'),Result(),
            one(scan_kind='discovery'),one(scan_kind='discovery'))
        self.assertEqual(self.core(),[{'scan_kind':'discovery'}])
        statements=[sql for sql,params in self.connection.queries]
        self.assertIn(claims._ACTIVE_SCAN,statements[-3]);self.assertIn(claims._HISTORY,statements[-2])

    def test_full_scan_choice_binds_each_types_configured_interval(self):
        base=CollectionSchedules()
        settings=replace(base,long_inventory=replace(base.long_inventory,reconciliation_seconds=1234),
            short_inventory=replace(base.short_inventory,reconciliation_seconds=2345),
            stream_inventory=replace(base.stream_inventory,reconciliation_seconds=3456))
        for kind,seconds in (('long',1234),('short',2345),('stream',3456)):
            self.queue(one(**args()),Result((source(kind),)),one(token=TOKEN,expires='2026-10-06'),Result(),
                one(scan_kind='discovery'),one(scan_kind='discovery'))
            with patch.object(self.database,'_cadence_snapshot',settings.cadence_snapshot()):
                self.core()
            query,params=[item for item in self.connection.queries if claims._HISTORY in item[0]][-1]
            self.assertEqual(params['reconciliation_seconds'],seconds)
            self.assertIn('make_interval(secs=>%(reconciliation_seconds)s::integer)',query)
            self.assertNotIn("interval '21 days'",query)

    def test_missing_schedule_refuses_claim_before_playlist_mutation(self):
        self.queue(one(**args()))
        with patch.object(self.database,'_cadence_snapshot',None):
            with self.assertRaisesRegex(DatabaseError,'collection_schedule_snapshot_required'):
                self.core()
        self.assertFalse(any('UPDATE public.channel_playlist_state' in sql for sql,_ in self.connection.queries))
    def test_missing_update_row_is_skipped_like_retained_found_and_not_fenced(self):
        self.queue(one(**args()),Result((source(),)),Result());self.assertEqual(self.core(),[])
        self.assertEqual(self.fence.call_args.args[1],[])
    def test_explicit_scope_envelope_fences_even_when_no_candidate_matched(self):
        values=args();values.update(channel_ids=[CHANNEL],scopes=[dict(channel_id=CHANNEL,control_epoch=17)],scopes_text='[]',only_due=False)
        self.queue(one(**values),one(invalid=False),Result());self.core()
        self.assertEqual(self.fence.call_args.args[1],values['scopes'])
    def test_native_input_cast_failure_precedes_every_domain_gate(self):
        self.queue(Result(execute_error=ServerFailure('22P02')))
        with self.assertRaises(DatabaseError) as caught:self.core()
        self.assertEqual(caught.exception.code,'22P02');self.assertEqual(len(self.connection.queries),1);self.fence.assert_not_called()
    def test_core_domain_precedence_and_no_lock_on_invalid_inputs(self):
        ordered=[('limit','claim_limit'),('channels','channel_ids'),('types','video_types'),('owner','claim_owner'),('lease','claim_lease')]
        for index,(flag,token) in enumerate(ordered):
            values=args()
            for other,_ in ordered[index:]:values[other+'_invalid']=True
            self.queue(one(**values));before=len(self.connection.queries)
            with self.assertRaisesRegex(DatabaseError,'typed_inventory_'+token+'_invalid'):self.core()
            self.assertEqual(len(self.connection.queries),before+1)
        self.fence.assert_not_called()
    def test_pass_public_null_gate_precedes_core_limit_gate(self):
        values=args();values['limit_invalid']=True
        self.queue(one(**values))
        with self.assertRaisesRegex(DatabaseError,'inventory_continuations_invalid'):
            claims.claim_inventory_pass(self.database,None,0,'w','r')
    def test_scope_invalid_precedes_continuation_invalid(self):
        values=args();values.update(scopes_text='{}',include_due=None)
        self.queue(one(**values),one(invalid=True))
        with self.assertRaisesRegex(DatabaseError,'channel_collection_scope_invalid'):self.core()
    def test_continuations_are_validated_before_selection_and_no_addressed_cross_product(self):
        for changes in (dict(include_due=None),dict(include_due=False),dict(continuations_text='[]',only_due=False),
                        dict(continuations_text='[]',channel_ids=[CHANNEL]),dict(continuations_text='[]',scopes_text='[]')):
            values=args();values.update(changes);self.queue(one(**values))
            if values['scopes_text'] is not None:self.queue(one(invalid=False))
            with self.assertRaisesRegex(DatabaseError,'inventory_continuations_invalid'):self.core()
        values=args();values.update(continuations_text='[]',include_due=False)
        self.queue(one(**values),one(invalid=False),Result());self.assertEqual(self.core(),[])
    def test_bad_continuation_pair_is_domain_error(self):
        values=args();values['continuations_text']='[{}]';self.queue(one(**values),one(invalid=True))
        with self.assertRaisesRegex(DatabaseError,'inventory_continuations_invalid'):self.core()
    def test_addressed_wrapper_preserves_native_array_and_fixed_500_limit(self):
        self.queue(one(**args()),Result());claims.claim_typed_inventory_channels(self.database,[' '+CHANNEL],[None],'w','r')
        parameters=self.connection.queries[0][1]
        self.assertEqual(parameters[:6],(500,[' '+CHANNEL],[None],'w','r',900));self.assertIs(parameters[7],False)
    def test_native_cast_array_shapes_reach_cursor_without_flattening_or_null_loss(self):
        for ids,types in (([None],['long']),([None,CHANNEL],['long','long']),([[None,CHANNEL]],[['long','short','stream']])):
            values=args();values.update(channel_ids=ids,video_types=types,only_due=False)
            self.queue(one(**values),Result())
            self.assertEqual(claims.claim_typed_inventory_channels(self.database,ids,types,'w','r'),[])
            sent=self.connection.streams[-1].calls[0][1]
            self.assertEqual(sent['channel_ids'],ids);self.assertEqual(sent['video_types'],types)
    def test_final_fence_failure_rolls_back_all_claims(self):
        self.queue(one(**args()),Result((source(),)),one(token=TOKEN,expires='2026-10-06'),one(scan='{}',scan_kind='initial_inventory'),one())
        self.fence.side_effect=DatabaseServerError('channel_collection_scope_lost',code='P0001')
        with self.assertRaisesRegex(DatabaseError,'channel_collection_scope_lost'):self.core()
        self.assertEqual((self.connection.commits,self.connection.rollbacks),(0,1))
    def test_caught_borrowed_validation_cannot_commit_previous_write(self):
        values=args();values['owner_invalid']=True;self.queue(write(),one(**values))
        with self.assertRaisesRegex(DatabaseError,'database_transaction_aborted'):
            with self.database.transaction(Operation('claim_owner')) as tx:
                tx.execute('UPDATE public.operator_jobs SET updated_at=now() WHERE id=%s::uuid',(JOB,))
                try:claims.claim_inventory_states(tx,3,None,['long'],'w','r')
                except DatabaseServerError:pass
        self.assertEqual(self.connection.commits,0)
    def test_unknown_commit_never_retries_or_returns_acquired_leases(self):
        self.queue(one(**args()),Result((source(),)),one(token=TOKEN,expires='2026-10-06'),one(scan='{}',scan_kind='initial_inventory'),one())
        self.connection.commit_error=psycopg.OperationalError('private detail')
        with self.assertRaises(DatabaseError) as caught:claims.claim_due_typed_inventory(self.database,3,'w','r')
        self.assertTrue(caught.exception.outcome_unknown);self.assertEqual(self.pool.borrowed,1)
        self.assertEqual(sum(claims._CLAIM in sql for sql,params in self.connection.queries),1)
    def test_operation_keeps_total_60_seconds_and_no_artificial_lock_timeout(self):
        operations=[];original=self.database.transaction
        def record(operation):operations.append(operation);return original(operation)
        self.queue(one(**args()),Result())
        with patch.object(self.database,'transaction',side_effect=record):claims.claim_due_typed_inventory(self.database,3,'w','r')
        self.assertEqual((operations[0].statement_timeout_ms,operations[0].total_timeout_ms,operations[0].lock_timeout_ms),(60000,60000,0))
    def test_initial_parent_lock_and_native_clock_precede_source_scope_and_playlist(self):
        self.initial_header(invalid=True)
        with self.assertRaisesRegex(DatabaseError,'operator_job_lease_lost'):self.initial()
        self.scope.assert_not_called();queries=[sql for sql,params in self.connection.queries]
        self.assertIn(claims._CHANNEL,queries[2]);self.assertIn(claims._JOB,queries[3]);self.assertIn(claims._JOB_CHECK,queries[4])
        self.assertNotIn('clock_timestamp',claims._JOB)
    def test_initial_keeps_opaque_job_text_into_borrowed_scope(self):
        self.initial_header();self.initial_source(wait=True);self.assertEqual(self.initial(),[])
        self.assertEqual(self.scope.call_args.args[1],' {"opaque":1e1000}'.lstrip())
        self.assertEqual(self.fence.call_args.args[1],[dict(channel_id=CHANNEL,control_epoch=17)])
        self.assertFalse(any(claims._CLAIM in sql for sql,params in self.connection.queries))
    def test_missing_playlist_precedes_proof_uuid_cast(self):
        self.initial_header();self.queue(Result())
        with self.assertRaisesRegex(DatabaseError,'initial_inventory_discovery_state_missing'):self.initial()
        self.assertIn(claims._PLAYLIST,self.connection.queries[-1][0])
    def test_foreign_running_scan_is_abandoned_before_single_shared_lease_writer(self):
        self.initial_header();self.initial_source(active=SCAN);self.queue(one(scan='{}'),one(invalid=False),write(),write())
        with patch.object(claims,'claim_inventory_states',return_value=['sentinel']) as core:
            self.assertEqual(self.initial(),['sentinel'])
        queries=[sql for sql,params in self.connection.queries]
        self.assertIn(claims._ABANDON,queries[-2]);self.assertIn(claims._CLEAR,queries[-1])
        self.assertEqual(core.call_args.args[1:7],(1,[CHANNEL],['long'],'worker','run',900))
        self.assertEqual(core.call_args.args[-2:],(JOB,TOKEN));self.fence.assert_not_called()
    def test_foreign_missing_or_wrong_scan_never_abandons(self):
        for response in ((Result(),),(one(scan='{}'),one(invalid=True))):
            self.initial_header();self.initial_source(active=SCAN);self.queue(*response)
            with self.assertRaisesRegex(DatabaseError,'full_initial_foreign_scan_invalid'):self.initial()
        self.assertFalse(any(claims._ABANDON in sql for sql,params in self.connection.queries))
    def test_owned_identity_failure_precedes_scan_lock(self):
        self.initial_header();self.initial_source(own=SCAN,owned_invalid=True)
        with self.assertRaisesRegex(DatabaseError,'full_initial_owned_scan_unavailable'):self.initial()
        self.assertIn(claims._WAIT,self.connection.queries[-1][0])
    def test_owned_native_coverage_error_rolls_back_without_new_claim(self):
        self.initial_header();self.initial_source(own=SCAN,active=SCAN);self.queue(one(scan='{}'),Result(execute_error=ServerFailure('22003')))
        with self.assertRaises(DatabaseError) as caught:self.initial()
        self.assertEqual(caught.exception.code,'22003');self.assertEqual(self.connection.commits,0)
    def test_owned_success_delegates_without_abandon_or_duplicate_final_fence(self):
        self.initial_header();self.initial_source(own=SCAN,active=SCAN);self.queue(one(scan='{}'),one(invalid=False))
        with patch.object(claims,'claim_inventory_states',return_value=[]) as core:self.assertEqual(self.initial(),[])
        core.assert_called_once();self.fence.assert_not_called()
    def test_all_fixed_statements_bind_and_pass_driver_guards(self):
        values=args();values.update(source='{}',epoch=17,channel_id=CHANNEL,video_type='long',token=TOKEN,expires='2026-10-06',
            job_id=JOB,job_lease_token=TOKEN,job='{}',playlist='{}',proof='{}',scan='{}',own_id=SCAN,scan_kind='initial_inventory',chain_transition=SCAN,
            reconciliation_seconds=1814400)
        inputs={claims._INPUT:(3,None,['long'],'w','r',900,None,True,None,True,None,None),
            claims._INITIAL_INPUT:(JOB,TOKEN,'w','r',900),claims._CHANNEL_ID:(JOB,),claims._CHANNEL:(CHANNEL,),claims._JOB:(JOB,),claims._SCAN:(SCAN,),claims._ABANDON:(SCAN,)}
        for name,sql in vars(claims).items():
            if not name.startswith('_') or not isinstance(sql,str) or not sql.lstrip().startswith(('SELECT','WITH','UPDATE')):continue
            writable=sql.lstrip().startswith('UPDATE')
            returning='RETURNING' in sql
            _reviewed_statement(sql,read=not writable or returning,max_rows=1 if returning else 0 if writable else 500,returning=returning)
            PostgresQuery(Transformer()).convert(sql,inputs.get(sql,values))
    def test_native_predicates_keep_receipt_and_fairness_semantics(self):
        for clause in ("ORDER BY d.dispatch_ordinal,d.priority DESC,d.channel_id,d.video_type",'FOR UPDATE OF d SKIP LOCKED',
            "r.next_page_token_hash=sha256(convert_to(own_scan.resume_page_token,'UTF8'))",'own_scan.token_history_verified',
            "own_scan.last_transition_kind='page'","NOT(r.result ? 'source_expired')","j.status<>'completed'",
            'j.lease_expires_at>clock_timestamp()',"coalesce(d.source_next_after,'infinity'::timestamptz)<=now()"):
            self.assertIn(clause,claims._CANDIDATES)
        self.assertIn("nextval('operator_private.inventory_dispatch_seq')",claims._CLAIM)
        self.assertIn("completed.latest<=now()-make_interval(secs=>%(reconciliation_seconds)s::integer)",claims._HISTORY)
        text=(ROOT/'application/db/inventory/inventory_claims.py').read_text()
        for forbidden in ('database.call(', 'public.typed_inventory_channel_eligible(', 'public.typed_inventory_quota_claim_allowed(', 'datetime.'):
            self.assertNotIn(forbidden,text)


if __name__=='__main__':unittest.main()
