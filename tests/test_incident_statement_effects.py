"""Finite incident batching and actual closed-driver statement checks."""
from pathlib import Path
import ast,re,sys,unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.videos import incident_statement_effects as effects
from db.videos import video_missing_completion as missing,video_profile_completion as profile,video_typed_metadata_completion as typed
from db.base.postgres import DatabaseError,DatabaseServerError,PostgresDatabase,PostgresConfig,Operation,_reviewed_statement
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
from test_direct_pg_transactions import Connection,Pool,Result,DSN

def event(operation='update'):
 return dict(operation=operation,old_text=None if operation=='insert' else 'NATIVE_OLD',new_text=None if operation=='delete' else 'NATIVE_NEW')
def facts(index,video_id,*,operation='update',old_closed=False,new_closed=False,withdrawal=False,state_type='long'):
 return dict(ordinality=index,operation=operation,old_video_id=video_id,new_video_id=video_id,old_closed=old_closed,new_closed=new_closed,withdrawal=withdrawal,state_type=state_type)
def rows(*values):return Result(values)
def changed(n):return Result(has_columns=False,rowcount=n)
class Batch(unittest.TestCase):
 def setUp(self):
  self.events=[];self.connection=Connection(self.events);self.pool=Pool(self.connection,self.events)
  self.database=PostgresDatabase(PostgresConfig(DSN),admission=lambda name:None,pool_factory=lambda config:self.pool).open();self.addCleanup(self.database.close)
 def run_effect(self,events):
  with self.database.transaction(Operation('incident_owner')) as tx:effects.after_incident_statement(tx,events)
 def test_history_events_do_not_own_queue_or_reset_retry_episode(self):
  self.connection.results.append(rows(facts(1,'a'),facts(2,'b',new_closed=True),facts(3,'c',operation='insert')))
  self.run_effect([event(),event(),event('insert')])
  self.assertEqual(len(self.connection.queries),1)
  self.assertFalse(any('UPDATE public.video_collection_state' in q for q,_ in self.connection.queries))
 def test_open_incident_retention_delete_does_not_resume_stopped_id(self):
  self.connection.results.append(rows(facts(1,'a',operation='delete',state_type=None)))
  self.run_effect([event('delete')])
  self.assertEqual(len(self.connection.queries),1)
 def test_thousand_history_events_keep_one_bounded_validation(self):
  self.connection.results.append(rows(*(facts(i+1,str(i)) for i in range(1000))))
  self.run_effect([event() for _ in range(1000)])
  self.assertEqual(len(self.connection.queries),1)
 def test_duplicate_active_video_is_rejected_before_any_mutation(self):
  self.connection.results.append(rows(facts(1,'a'),facts(2,'a')))
  with self.assertRaisesRegex(DatabaseError,'database_result_shape_invalid'):self.run_effect([event(),event()])
  self.assertEqual(self.connection.commits,0)
 def test_bound_is_checked_before_dispatch(self):
  with self.assertRaises(ValueError):self.run_effect([event() for _ in range(1001)])
  self.assertEqual(self.connection.queries,[])
 def test_empty_batch_has_no_sql(self):
  self.run_effect([]);self.assertEqual(self.connection.queries,[])
class Source(unittest.TestCase):
 def test_every_new_query_passes_actual_driver_and_psycopg_bind(self):
  queries=[(effects._FACTS,False),(missing._INCIDENT_EFFECT_UPDATE,True),(missing._INSERT_INCIDENT_EFFECT,True),(profile._INCIDENT_EFFECT_UPDATE,True),(typed._INCIDENT_EFFECT_UPDATE,True)]
  for query,returned in queries:
   with self.subTest(query=query[:60]):
    params=(None,)*query.count('%s')
    if returned is None:sql=_reviewed_statement(query,read=False,max_rows=0)
    else:sql=_reviewed_statement(query,read=True,max_rows=1000,returning=returned)
    PostgresQuery(Transformer()).convert(sql.as_string(),params)
 def test_native_facts_are_exact_accepted78_expression(self):
  from db.inventory.inventory_incident_queue_effects import _INCIDENT_FACTS
  self.assertEqual(effects._NATIVE_FACTS.replace('event.old_text::jsonb','%s::jsonb').replace('event.new_text::jsonb','%s::jsonb'),_INCIDENT_FACTS)
 def test_update_inverse_recovers_original_statement_targets_predicates_casts(self):
  for module,name in ((missing,'_UPDATE_INCIDENT'),(profile,'_RESTORE'),(typed,'_RESTORE')):
   restored=module._INCIDENT_EFFECT_UPDATE.removesuffix(' AND effect_previous.id=incident.id RETURNING to_jsonb(effect_previous)::text AS old_text,to_jsonb(incident)::text AS new_text').replace(' FROM public.video_availability_incidents effect_previous, ',' FROM ',1)
   self.assertEqual(restored,getattr(module,name));self.assertTrue(restored.lstrip().startswith('UPDATE'))
 def test_each_effect_is_before_original_next_event_history_statement(self):
  for module,name in ((missing,'complete_typed_video_missing'),(profile,'complete_video_profile_metadata_collection'),(typed,'complete_typed_video_metadata_collection')):
   tree=ast.parse(Path(module.__file__).read_text());function=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==name)
   text=ast.unparse(function);self.assertLess(text.index('after_incident_statement'),text.index('tx.execute(_EVENTS' if module!=missing else 'for statement in (_DETECTED'))
   self.assertLess(text.index('_INCIDENT_LOCK'),text.index('_INCIDENT_EFFECT_UPDATE'))
if __name__=='__main__':unittest.main()
