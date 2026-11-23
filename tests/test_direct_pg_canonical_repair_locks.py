"""Canonical repair locks are finite, ordered and share admission/deadline."""
from pathlib import Path
import sys,unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'application'))
from db.base import postgres as pg
from test_direct_pg_transactions import Connection,Pool,Result,ServerFailure,DSN

class RepairLockTests(unittest.TestCase):
 def setUp(self):
  self.trace=[];self.connection=Connection(self.trace);self.pool=Pool(self.connection,self.trace);self.admissions=[]
  self.db=pg.PostgresDatabase(pg.PostgresConfig(DSN),admission=lambda name:self.admissions.append(name),pool_factory=lambda config:self.pool).open();self.addCleanup(self.db.close)
 def queue(self,count=6,error_at=None):
  for i in range(count):self.connection.results.append(Result(has_columns=False,execute_error=ServerFailure('55P03') if i==error_at else None))
 def test_exact_six_locks_no_interpolation_and_same_transaction(self):
  self.queue()
  with self.db.transaction(pg.Operation('repair',total_timeout_ms=1000)) as tx:self.assertIsNone(tx.lock_typed_inventory_repair())
  self.assertEqual(self.connection.queries,[(statement,()) for statement in pg._CANONICAL_REPAIR_LOCKS]);self.assertEqual(self.pool.borrowed,1);self.assertEqual(self.connection.commits,1)
  self.assertEqual(len([x for x in self.trace if x[0]=='dispatch']),6);self.assertGreaterEqual(len(self.admissions),6)
 def test_readonly_and_rpc_contexts_reject_before_any_lock(self):
  for readonly,rpc in ((True,None),(False,'repair_typed_inventory_canonical_types')):
   tx=pg.Transaction(self.connection,lambda name:None,read_only=readonly,rpc_name=rpc)
   with self.assertRaisesRegex(pg.DatabaseError,'database_lock_capability_invalid'):tx.lock_typed_inventory_repair()
  self.assertEqual(self.connection.queries,[])
 def test_generic_lock_and_arbitrary_private_arguments_stay_forbidden(self):
  for params,statement in ((('public.channels',),None),((True,),None),((-1,),None),((6,),None),([],None),((0,),'LOCK TABLE public.channels')):
   tx=pg.Transaction(self.connection,lambda name:None)
   with self.assertRaisesRegex(pg.DatabaseError,'database_lock_capability_invalid'):tx._run(statement,params,'canonical_repair_lock',0)
  tx=pg.Transaction(self.connection,lambda name:None)
  with self.assertRaises(pg.DatabaseError):tx.execute(pg._CANONICAL_REPAIR_LOCKS[0])
  self.assertEqual(self.connection.queries,[])
 def test_middle_lock_timeout_rolls_back_and_never_continues(self):
  self.queue(error_at=2)
  with self.assertRaises(pg.DatabaseServerError) as error:
   with self.db.transaction(pg.Operation('repair',total_timeout_ms=1000)) as tx:tx.lock_typed_inventory_repair()
  self.assertEqual(error.exception.code,'55P03');self.assertEqual(len(self.connection.queries),3);self.assertEqual((self.connection.commits,self.connection.rollbacks),(0,1))
 def test_deadline_expires_between_fixed_locks_without_more_dispatch(self):
  self.queue();now=[1.0];original=pg.Transaction._run
  def advance(tx,*args,**kwargs):
   value=original(tx,*args,**kwargs);now[0]+=0.6;return value
  with patch.object(pg.time,'monotonic',side_effect=lambda:now[0]),patch.object(pg.Transaction,'_run',advance):
   with self.assertRaises(pg.DatabaseServerError) as error:
    with self.db.transaction(pg.Operation('repair',total_timeout_ms=1000)) as tx:tx.lock_typed_inventory_repair()
  self.assertEqual(error.exception.code,'57014');self.assertEqual(len(self.connection.queries),2);self.assertEqual(self.connection.commits,0)
if __name__=='__main__':unittest.main()
