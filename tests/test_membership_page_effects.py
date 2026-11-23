from pathlib import Path
import re,sys,unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.inventory import membership_page_effects as effects,inventory_scan_pages as page
from db.base.postgres import DatabaseError,DatabaseServerError,PostgresDatabase,PostgresConfig,Operation,_reviewed_statement
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
from test_direct_pg_transactions import Connection,Pool,Result,DSN
PARAMS=dict(channel_id='channel',video_type='long',playlist_id='playlist',source_checked_at='clock',scan_id='scan',items='[]')
class PageEffects(unittest.TestCase):
 def setUp(self):
  self.events=[];self.connection=Connection(self.events);self.pool=Pool(self.connection,self.events)
  self.database=PostgresDatabase(PostgresConfig(DSN),admission=lambda name:None,pool_factory=lambda config:self.pool).open();self.addCleanup(self.database.close)
 def run_effect(self):
  with self.database.transaction(Operation('page_owner')) as tx:effects.observe_page_memberships(tx,PARAMS)
 def test_actual_action_receipt_preserves_acquisition_order(self):
  self.connection.results.extend([Result([dict(video_id='b',inserted=False,value='ACTUAL_OLD'),dict(video_id='a',inserted=True,value='INSERTED_DEFAULTS')]),Result([dict(video_id='b',value='ACTUAL_NEW')])])
  with patch.object(effects,'after_page_membership_changes') as dispatch:self.run_effect()
  self.assertEqual(dispatch.call_args.args[1],[dict(operation='update',old_text='ACTUAL_OLD',new_text='ACTUAL_NEW'),dict(operation='insert',old_text=None,new_text='INSERTED_DEFAULTS')])
  self.assertEqual(self.connection.queries[1][1]['effect_conflict_ids'],['b']);self.assertEqual(self.connection.commits,1)
 def test_insert_only_never_runs_final_update(self):
  self.connection.results.append(Result([dict(video_id='a',inserted=True,value='INSERTED')]))
  with patch.object(effects,'after_page_membership_changes'):self.run_effect()
  self.assertEqual(len(self.connection.queries),1)
 def test_original_conflict_where_zero_admission_has_no_event(self):
  self.connection.results.append(Result())
  with patch.object(effects,'after_page_membership_changes') as dispatch:self.run_effect()
  dispatch.assert_not_called();self.assertEqual(len(self.connection.queries),1)
 def test_missing_locked_conflict_fails_atomically(self):
  self.connection.results.extend([Result([dict(video_id='a',inserted=False,value='OLD')]),Result()])
  with patch.object(effects,'after_page_membership_changes') as dispatch:
   with self.assertRaises(DatabaseError):self.run_effect()
  dispatch.assert_not_called();self.assertEqual(self.connection.commits,0)
 def test_receipt_requires_native_boolean_action(self):
  self.connection.results.append(Result([dict(video_id='a',inserted=1,value='OLD')]))
  with self.assertRaises(DatabaseError):self.run_effect()
  self.assertEqual(self.connection.commits,0)
 def test_effect_failure_cannot_commit_provisional_write(self):
  self.connection.results.append(Result([dict(video_id='a',inserted=True,value='NEW')]))
  with patch.object(effects,'after_page_membership_changes',side_effect=DatabaseServerError('failed',code='23514')):
   with self.assertRaises(DatabaseServerError):self.run_effect()
  self.assertEqual(self.connection.commits,0);self.assertEqual(self.pool.borrowed,1)
 def test_extra_receipt_row_exceeds_public_bound(self):
  self.connection.results.append(Result([dict(video_id=str(i),inserted=True,value='NATIVE') for i in range(51)]))
  with self.assertRaisesRegex(DatabaseError,'database_result_limit_exceeded'):self.run_effect()
  self.assertEqual(self.connection.commits,0)
class Source(unittest.TestCase):
 def test_all_new_statements_pass_driver_and_binding(self):
  for query in (*effects._statements(),page._INSERT_CANONICAL_EFFECT):
   params={name:None for name in re.findall(r'%\(([^)]+)\)s',query)}
   sql=_reviewed_statement(query,read=True,max_rows=50,returning=True)
   PostgresQuery(Transformer()).convert(sql.as_string(),params)
 def test_native_source_and_admission_predicate_unchanged(self):
  acquired,finalized=effects._statements();original=page._UPSERT_MEMBERSHIPS
  self.assertEqual(acquired.split(' DO UPDATE SET ',1)[0],original.split(' DO UPDATE SET ',1)[0])
  self.assertEqual(acquired.split('    WHERE ',1)[1].split(' RETURNING',1)[0],original.rsplit('    WHERE ',1)[1])
  self.assertIn('SET '+original.split(' DO UPDATE SET ',1)[1].rsplit('    WHERE ',1)[0],finalized)
  self.assertNotIn('ORDER BY',acquired);self.assertNotIn('updated_at=now()',acquired.split(' DO UPDATE SET ',1)[1])
 def test_provisional_target_does_not_fire_scan_update_guard(self):
  schema=(ROOT/'schema/schema.sql').read_text();trigger=next(line for line in schema.splitlines() if line.startswith('CREATE TRIGGER video_inventory_membership_scan_guard '))
  targets=trigger.split('UPDATE OF ',1)[1].split(' ON ',1)[0]
  self.assertNotIn('updated_at',targets);self.assertIn('membership_status',targets)
 def test_canonical_effects_precede_membership_and_owner_fence(self):
  source=Path(page.__file__).read_text();body=source.split('def _settle(',1)[1]
  self.assertLess(body.index('after_page_inventory_inserts'),body.index('observe_page_memberships'))
  self.assertLess(body.index('observe_page_memberships'),body.index('_SCAN_UPDATE'))
  self.assertIn('assert_channel_scope',body)
if __name__=='__main__':unittest.main()
