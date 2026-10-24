"""Finite direct hydration/priority protocols; no native database access."""
from pathlib import Path
import sys,unittest
from unittest.mock import patch
import psycopg
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
from psycopg.types.json import Jsonb
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.inventory import inventory_priority as priority,inventory_runtime_queries as reads
from db.base.postgres import DatabaseError,DatabaseServerError,Operation,PostgresConfig,PostgresDatabase,_reviewed_statement
from test_direct_pg_transactions import Connection,Pool,Result,ServerFailure,DSN
CHANNEL='UC0000000000000000000001';SCOPES=[dict(channel_id=CHANNEL,control_epoch=17)]
def one(**values):return Result((values,))

class RuntimeProtocolTests(unittest.TestCase):
 def setUp(self):
  self.events=[];self.connection=Connection(self.events);self.pool=Pool(self.connection,self.events)
  self.database=PostgresDatabase(PostgresConfig(DSN),admission=lambda name:None,pool_factory=lambda config:self.pool).open()
  self.addCleanup(self.database.close);p=patch.object(priority,'assert_channel_scopes');self.fence=p.start();self.addCleanup(p.stop)
 def queue(self,*values):self.connection.results.extend(values)
 def ready(self,scopes=False,rows=None):
  self.queue(one(count_invalid=False,id_invalid=False,has_scopes=scopes))
  if scopes:self.queue(one(invalid=False),one(value=SCOPES))
  self.queue(Result(tuple(SCOPES if rows is None else rows)))
 def test_priority_orders_validation_child_write_and_final_scope_in_one_transaction(self):
  self.ready(scopes=True)
  result=priority.prioritize_typed_inventory_channels(self.database,[CHANNEL],SCOPES)
  self.assertEqual(result,1);self.assertEqual((self.pool.borrowed,self.connection.commits),(1,1))
  for (sql,params),expected in zip(self.connection.queries,(priority._INPUT,priority._SCOPES,priority._SCOPES_VALUE,priority._UPDATE)):
   self.assertIn(expected,sql)
  self.fence.assert_called_once();self.assertEqual(self.fence.call_args.args[1],SCOPES)
 def test_priority_argument_failures_precede_scope_validation_and_dml(self):
  for facts,code in ((dict(count_invalid=True,id_invalid=True,has_scopes=True),'typed_inventory_priority_channel_ids_invalid'),
     (dict(count_invalid=False,id_invalid=True,has_scopes=True),'typed_inventory_priority_channel_id_invalid')):
   self.queue(one(**facts));before=len(self.connection.queries)
   with self.assertRaisesRegex(DatabaseError,code):priority.prioritize_typed_inventory_channels(self.database,[None],SCOPES)
   self.assertEqual(len(self.connection.queries)-before,1)
  self.assertEqual(self.connection.commits,0);self.fence.assert_not_called()
 def test_priority_native_cast_error_is_not_replaced_by_domain_error(self):
  self.queue(Result(execute_error=ServerFailure('22P02')))
  with self.assertRaises(DatabaseError) as error:priority.prioritize_typed_inventory_channels(self.database,'bad-array')
  self.assertEqual(error.exception.code,'22P02');self.assertEqual(len(self.connection.queries),1)
 def test_invalid_scope_is_rejected_without_lock_or_dml(self):
  self.queue(one(count_invalid=False,id_invalid=False,has_scopes=True),one(invalid=True))
  with self.assertRaisesRegex(DatabaseError,'channel_collection_scope_invalid'):
   priority.prioritize_typed_inventory_channels(self.database,[CHANNEL],{'wrong':True})
  self.assertEqual(len(self.connection.queries),2);self.assertEqual(self.connection.commits,0);self.fence.assert_not_called()
 def test_provided_scope_is_fenced_even_when_no_playlist_changes(self):
  self.ready(scopes=True,rows=[])
  self.assertEqual(priority.prioritize_typed_inventory_channels(self.database,[CHANNEL],SCOPES),0)
  self.assertEqual(self.fence.call_args.args[1],SCOPES)
 def test_generated_scope_is_deduplicated_without_truncating_count(self):
  self.ready(rows=SCOPES*3)
  self.assertEqual(priority.prioritize_typed_inventory_channels(self.database,[CHANNEL]),3)
  self.assertEqual(self.fence.call_args.args[1],SCOPES)
 def test_valid_500_channels_and_1500_rows_keep_constant_query_count(self):
  counts=[]
  for count in (1,500):
   rows=[dict(channel_id=str(i),control_epoch=i+1) for i in range(count) for _ in range(3)]
   self.ready(rows=rows);before=len(self.connection.queries)
   self.assertEqual(priority.prioritize_typed_inventory_channels(self.database,[str(i) for i in range(count)]),count*3)
   counts.append(len(self.connection.queries)-before)
  self.assertEqual(counts,[2,2])
 def test_returning_overflow_fails_closed_and_rolls_back(self):
  self.ready(rows=SCOPES*1501)
  with self.assertRaises(DatabaseError):priority.prioritize_typed_inventory_channels(self.database,[CHANNEL])
  self.assertEqual((self.connection.commits,self.connection.rollbacks),(0,1));self.fence.assert_not_called()
 def test_final_scope_loss_rolls_back_all_child_changes(self):
  self.ready();self.fence.side_effect=DatabaseServerError('channel_collection_scope_lost',code='P0001')
  with self.assertRaisesRegex(DatabaseError,'channel_collection_scope_lost'):
   priority.prioritize_typed_inventory_channels(self.database,[CHANNEL])
  self.assertEqual((self.connection.commits,self.connection.rollbacks),(0,1))
 def test_borrowed_rejection_poison_cannot_be_caught_and_committed(self):
  self.queue(one(count_invalid=True,id_invalid=False,has_scopes=False))
  with self.assertRaisesRegex(DatabaseError,'database_transaction_aborted'):
   with self.database.transaction(Operation('priority_parent')) as tx:
    try:priority.prioritize_typed_inventory_channels(None,[],transaction=tx)
    except DatabaseError:pass
  self.assertEqual(self.pool.borrowed,1);self.assertEqual(self.connection.commits,0)
 def test_committed_unknown_ack_has_no_automatic_retry(self):
  self.ready();self.connection.commit_error=psycopg.OperationalError('private detail')
  with self.assertRaises(DatabaseError) as error:priority.prioritize_typed_inventory_channels(self.database,[CHANNEL])
  self.assertTrue(error.exception.outcome_unknown);self.assertEqual(self.pool.borrowed,1);self.fence.assert_called_once()
 def test_hydration_one_readonly_snapshot_preserves_native_json_and_big_counts(self):
  value=dict(schema_version=1,status='ok',channel_id=CHANNEL,formats=dict(short=dict(known_count=9007199254740993)),
   inventory_formats=dict(stream=dict(scan_boundary_at='10000-01-01T00:00:00+00:00')))
  self.queue(one(invalid=False),one(value='off'),one(value=value))
  self.assertEqual(reads.get_channel_initial_typed_hydration(self.database,' '+CHANNEL+' '),value)
  self.assertEqual(len(self.connection.queries),3)
  for (sql,params),expected in zip(self.connection.queries,(reads._INPUT,"set_config('jit','off',true)",reads._READ)):self.assertIn(expected,sql)
  self.assertTrue(any('READ ONLY' in sql for sql,params in self.connection.setup))
 def test_hydration_cancelled_read_rolls_back_once_under_existing_five_second_budget(self):
  self.queue(one(invalid=False),one(value='off'),Result(execute_error=ServerFailure('57014')))
  with self.assertRaises(DatabaseError) as caught:
   reads.get_channel_initial_typed_hydration(self.database,CHANNEL)
  self.assertEqual(caught.exception.code,'57014')
  self.assertEqual((self.pool.borrowed,self.connection.commits,self.connection.rollbacks),(1,0,1))
  self.assertEqual(len(self.connection.queries),3)
  spec=reads.get_operation('get_channel_initial_typed_hydration')
  self.assertEqual(spec.statement_timeout_ms,5000)
  setup=' '.join(sql for sql,params in self.connection.setup)
  self.assertIn('READ ONLY',setup)
 def test_hydration_invalid_input_does_not_query_catalogue(self):
  self.queue(one(invalid=True))
  with self.assertRaisesRegex(DatabaseError,'initial_typed_channel_id_invalid'):reads.get_channel_initial_typed_hydration(self.database,None)
  self.assertEqual(len(self.connection.queries),1);self.assertEqual(self.connection.commits,0)
 def test_hydration_borrow_uses_existing_transaction_and_empty_envelope(self):
  value=dict(status='not_found',formats=dict(short={},stream={}),inventory_formats=dict(long={},short={},stream={}))
  self.queue(one(invalid=False),one(value='on'),one(value='off'),one(value=value),one(value='on'))
  with self.database.transaction(Operation('read_parent',read_only=True)) as tx:
   self.assertEqual(reads.get_channel_initial_typed_hydration(None,CHANNEL,transaction=tx),value)
  self.assertEqual((self.pool.borrowed,self.connection.commits),(1,1))
  self.assertEqual(len(self.connection.queries),5)
  for (sql,params),expected in zip(self.connection.queries,(reads._INPUT,"current_setting('jit')","set_config('jit','off',true)",reads._READ,"set_config('jit',%s,true)")):self.assertIn(expected,sql)
  self.assertEqual(self.connection.queries[-1][1],('on',))
 def test_hydration_source_identity_clocks_and_generation_rules_remain_native(self):
  sql=reads._READ
  schema=(ROOT/'schema/schema.sql').read_text(encoding='utf-8')
  import re
  proof=re.findall(r'CREATE (?:OR REPLACE )?VIEW readable\.video_hydration_proof .*?;',schema,re.S)[-1]
  for predicate in ('state.stats_source_checked_at = video.checked_at','state.metadata_source_checked_at = video.metadata_checked_at',
    'state.typed_collection_checked_at = state.metadata_source_checked_at'):
   self.assertIn(predicate,proof)
  self.assertIn('readable.video_hydration_proof',sql)
  for predicate in ("least(completed.source_checked_at,completed.started_at)>=now()-interval '696 hours'",
    'canonical.video_type is distinct from observed.video_type','review.decision_type = source.video_type','full_generation.present'):
   self.assertIn(predicate,sql)
  self.assertNotIn('public.typed_inventory_channel_eligible(',sql);self.assertNotIn('FOR UPDATE',sql)
 def test_automatic_decisions_are_addressed_current_and_preserve_hydration_accounting(self):
  sql=reads._READ
  automatic=sql.split('automatic_decisions AS MATERIALIZED(',1)[1].split('source_deadline AS',1)[0]
  for predicate in ('readable.video_inventory_type_resolution AS proof',
                    'proof.video_id=source.video_id OFFSET 0',
                    'source.channel_id=(SELECT channel_id FROM args)',
                    'resolution.automatic_valid',"resolution.resolution_status='resolved'",
                    'resolution.canonical_channel_id=source.channel_id',
                    'resolution.canonical_video_type=source.video_type'):
   self.assertIn(predicate,automatic)
  self.assertNotIn('public.',automatic)
  self.assertIn('UNION ALL SELECT min(source_expires_at) FROM automatic_decisions',sql)
  unresolved=sql.split('unresolved_membership as materialized (',1)[1].split('membership as materialized (',1)[0]
  self.assertIn('select 1 from automatic_decisions as decision',unresolved)
  self.assertIn('decision.video_id = canonical.video_id',unresolved)
  self.assertIn('or automatic_decisions.video_id is not null',sql)
  self.assertIn('state.video_id is null or state.identity_matches',sql)

 def test_fresh_and_supported_upgrade_keep_the_same_protected_hydration_view(self):
  import re
  fresh=(ROOT/'schema/schema.sql').read_text(encoding='utf-8')
  def definition(text):
   matches=re.findall(r'CREATE (?:OR REPLACE )?VIEW readable\.video_hydration_proof .*?;',text,re.S)
   return matches[-1] if matches else None
  upgrades=[definition(path.read_text(encoding='utf-8')) for path in sorted((ROOT/'schema/steps').glob('*.sql'))]
  current=definition(fresh);upgraded=next(value for value in reversed(upgrades) if value is not None)
  self.assertEqual(current.replace('CREATE VIEW','CREATE OR REPLACE VIEW',1),upgraded)
  self.assertIn('security_barrier=true, security_invoker=false',current)
  self.assertIn('SELECT state.video_id,state.channel_id,',current)
  self.assertIn('GRANT SELECT ON readable.video_hydration_proof TO lic_reader;',fresh)
  self.assertNotIn('GRANT ',upgraded)
 def test_priority_keeps_exact_daily_quota_labels_live_lease_and_final_native_predicates(self):
  sql=priority._UPDATE
  for value in ('youtube_daily_quota_exceeded_error','youtube_quota_exhausted',"coalesce(discovery.next_due_at,'infinity'::timestamptz)<=now()",
    "coalesce(discovery.source_next_after,'infinity'::timestamptz)<=now()",'greatest(discovery.priority, 100)',"discovery.lease_expires_at > now()"):
   self.assertIn(value,sql)
  self.assertNotIn('public.typed_inventory_quota_claim_allowed(',sql);self.assertNotIn('FOR UPDATE',sql)
 def test_fixed_sql_passes_driver_and_psycopg_parameter_binding(self):
  statements=[(reads._INPUT,(CHANNEL,CHANNEL),True),(reads._READ,(CHANNEL,),True),
   (priority._INPUT,([CHANNEL],Jsonb(SCOPES)),True),(priority._SCOPES,dict(channel_ids=[CHANNEL],scopes=Jsonb(SCOPES)),True),
   (priority._SCOPES_VALUE,(Jsonb(SCOPES),),True),(priority._UPDATE,dict(channel_ids=[CHANNEL],scopes=None),False)]
  for sql,params,read in statements:
   _reviewed_statement(sql,read=True,max_rows=1500,returning=not read)
   PostgresQuery(Transformer()).convert(sql,params)

if __name__=='__main__':unittest.main()
