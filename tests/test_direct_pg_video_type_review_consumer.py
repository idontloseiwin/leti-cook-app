"""Named manual-review repository projections and exactly-once direct dispatch."""
import sys,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from uuid import UUID
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'application'))
from db.reviews.video_type_reviews import VideoTypeReviewsRepository
from db.reviews import video_type_review_queries as reads,video_type_review_decisions as decisions
from db.base.postgres import DatabaseServerError,DatabaseUnavailable
ID=UUID('f8900000-0000-4000-8000-000000000001')
REQUEST=UUID('f8900000-0000-4000-8000-000000000002')
class ReviewConsumerTests(unittest.TestCase):
 def setUp(self):
  self.db=Mock(spec=['call','transaction']);self.db.call.side_effect=AssertionError('RPC forbidden')
  self.repo=VideoTypeReviewsRepository(self.db)
 def test_four_readers_exact_arguments_defaults_and_unmodified_projection(self):
  rows=[{'case_id':str(ID),'extra':'preserved'}];case=rows[0];history=[{'revision':4}]
  with patch.object(reads,'list_video_type_review_cases',return_value=rows) as page,patch.object(reads,'get_video_type_review_case',return_value=case) as get,patch.object(reads,'list_video_type_review_history',return_value=history) as past,patch.object(reads,'has_open_video_type_reviews',return_value=False) as has:
   self.assertIs(self.repo.list_cases('open',101),rows);page.assert_called_once_with(self.db,state='open',limit=101,after_case_id=None)
   self.assertIs(self.repo.get_case(ID),case);get.assert_called_once_with(self.db,case_id=ID)
   self.assertIs(self.repo.list_history(ID,51),history);past.assert_called_once_with(self.db,case_id=ID,limit=51,before_revision=None)
   self.assertIs(self.repo.has_open(),False);has.assert_called_once_with(self.db)
  self.db.call.assert_not_called()
 def test_explicit_cursors_are_forwarded_without_conversion(self):
  with patch.object(reads,'list_video_type_review_cases',return_value=[]) as page,patch.object(reads,'list_video_type_review_history',return_value=[]) as past:
   self.repo.list_cases('all',3,ID);page.assert_called_once_with(self.db,state='all',limit=3,after_case_id=ID)
   self.repo.list_history(ID,2,4);past.assert_called_once_with(self.db,case_id=ID,limit=2,before_revision=4)
 def test_decision_preserves_native_inputs_and_result_identity(self):
  result={'outcome':'ready','case':{'video_id':None},'mutation':{'replayed':True,'applied_revision':2}}
  with patch.object(decisions,'decide_video_type_review',return_value=result) as action:
   self.assertIs(self.repo.decide(ID,REQUEST,1,'long',"  O'Brien  "),result)
   action.assert_called_once_with(self.db,case_id=ID,request_id=REQUEST,expected_revision=1,video_type='long',reason="  O'Brien  ")
  self.db.call.assert_not_called()
 def test_busy_changed_and_blocked_are_returned_unchanged(self):
  for outcome in ('busy','changed','blocked','request_conflict','source_unavailable','not_found'):
   result=dict(outcome=outcome,case=None,mutation=None)
   with patch.object(decisions,'decide_video_type_review',return_value=result) as action:
    self.assertIs(self.repo.decide(ID,REQUEST,1,'long','reason'),result);action.assert_called_once()
 def test_missing_case_and_empty_pages_remain_valid(self):
  with patch.object(reads,'get_video_type_review_case',return_value=None),patch.object(reads,'list_video_type_review_cases',return_value=[]),patch.object(reads,'list_video_type_review_history',return_value=[]):
   self.assertIsNone(self.repo.get_case(None));self.assertEqual(self.repo.list_cases('all',1),[]);self.assertEqual(self.repo.list_history(None,1),[])
 def test_existing_repository_shape_guards_remain(self):
  vectors=((reads,'list_video_type_review_cases',lambda:self.repo.list_cases('all',1),({},[None])),
   (reads,'get_video_type_review_case',lambda:self.repo.get_case(ID),([],False)),
   (reads,'list_video_type_review_history',lambda:self.repo.list_history(ID,1),({},[None])),
   (reads,'has_open_video_type_reviews',self.repo.has_open,(1,None)),
   (decisions,'decide_video_type_review',lambda:self.repo.decide(ID,REQUEST,1,'long','reason'),(None,[])))
  for module,name,invoke,invalid in vectors:
   for value in invalid:
    with patch.object(module,name,return_value=value) as action:
     with self.assertRaisesRegex(RuntimeError,'Invalid video type review response'):invoke()
     action.assert_called_once()
 def test_native_errors_and_unknown_ack_propagate_once_without_rpc_retry(self):
  for error in (DatabaseServerError('invalid_video_type_review_decision',code='22023'),DatabaseUnavailable('database_connection_failed',code='08006',outcome_unknown=True)):
   with patch.object(decisions,'decide_video_type_review',side_effect=error) as action:
    with self.assertRaises(type(error)) as caught:self.repo.decide(ID,REQUEST,1,'long','reason')
    self.assertIs(caught.exception,error);action.assert_called_once()
  self.db.call.assert_not_called()
if __name__=='__main__':unittest.main()
