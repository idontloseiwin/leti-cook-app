"""Public quota writer validation/ACK ownership after native61 acceptance."""
from pathlib import Path
import sys,unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.quota.youtube_quota import YouTubeQuotaRepository
from db.base.postgres import DatabaseUnavailable
from test_direct_pg_user_repositories import TransactionDatabase

class PublicQuotaWriterTests(unittest.TestCase):
 def test_invalid_envelope_rejected_before_connection(self):
  db=TransactionDatabase();repo=YouTubeQuotaRepository(db)
  for invoke in (lambda:repo.start_attempt({'bad-name':1}),lambda:repo.start_attempt({str(i):i for i in range(257)}),lambda:repo.finish_attempt('id',{}),lambda:repo.finish_attempt('id',{'bad-name':1})):
   with self.assertRaises(ValueError):invoke()
  self.assertEqual(db.transactions,[])
 def test_false_identity_does_not_inspect_payload_or_open_transaction(self):
  db=TransactionDatabase();repo=YouTubeQuotaRepository(db)
  for identity in (None,'','  ',0,False):self.assertFalse(repo.finish_attempt(identity,None))
  self.assertEqual(db.transactions,[])
 def test_unknown_ack_propagates_once_without_replay(self):
  failure=DatabaseUnavailable('database_connection_failed',outcome_unknown=True)
  for name,invoke in (('start_quota_attempt',lambda repo:repo.start_attempt({'quota_units':1})),('finish_quota_attempt',lambda repo:repo.finish_attempt('id',{'status':'success'}))):
   db=TransactionDatabase();repo=YouTubeQuotaRepository(db)
   with patch('db.quota.youtube_quota.'+name,side_effect=failure) as helper:
    with self.assertRaises(DatabaseUnavailable) as caught:invoke(repo)
    self.assertIs(caught.exception,failure);self.assertEqual(helper.call_count,1)
   self.assertEqual(db.ends,['rollback']);self.assertEqual(len(db.transactions),1)
   self.assertEqual(db.transactions[0].total_timeout_ms,60000)

if __name__=='__main__':unittest.main()
