"""Current review presentation is separate from durable case history."""
import sys, unittest
from pathlib import Path
from unittest.mock import Mock
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'application'))
from video_type_reviews import VideoTypeReviewService, ReviewError
from db.reviews import video_type_review_queries as reads
from db.base.postgres import _reviewed_statement
CASE='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
def case(**changes):
 return dict(case_id=CASE,video_id='aaaaaaaaaaa',channel_id='UC'+'a'*22,source_detached_at=None,
  canonical_type='long',observed_types=['long','short'],state='open',revision=1,
  review_status='awaiting_refresh',first_seen_at='2026-10-17T00:00:00Z',last_seen_at='2026-10-17T00:00:00Z',
  can_decide=False,blocked_reason='source_unavailable',decision=None,**changes)
class ReviewPresentationTests(unittest.TestCase):
 def test_waiting_cases_do_not_need_or_claim_an_operator_decision(self):
  repository=Mock();repository.list_cases.return_value=[case()]
  result=VideoTypeReviewService(repository).list('awaiting')
  self.assertEqual(result['cases'][0]['review_status'],'awaiting_refresh')
  self.assertFalse(result['cases'][0]['can_decide'])
 def test_manual_list_fails_closed_if_repository_supplies_a_waiting_case(self):
  repository=Mock();repository.list_cases.return_value=[case()]
  with self.assertRaises(ReviewError):VideoTypeReviewService(repository).list('open')
 def test_new_read_status_cannot_be_arbitrary(self):
  row=case();row['review_status']='guess'
  with self.assertRaises(ReviewError):VideoTypeReviewService._case(row)
 def test_queries_share_current_projection_not_persisted_open_flag(self):
  for statement in (reads._GET,reads._LIST,reads._HAS):
   self.assertIn(reads._REVIEW_STATUS,statement)
   _reviewed_statement(statement,read=True,max_rows=101)
  self.assertIn("presentation.review_status IN('needs_review','channel_conflict')",reads._HAS)
  self.assertIn("presentation.review_status='awaiting_refresh'",reads._LIST)
  self.assertNotIn("WHERE state='open'",reads._HAS)
 def test_freshness_and_native_resolution_precede_manual_exception(self):
  policy=reads._REVIEW_STATUS
  self.assertLess(policy.index("r.source_expires_at IS NULL"),policy.index("WHEN r.present_type_count>1"))
  self.assertLess(policy.index("ARRAY['long','short']"),policy.index("WHEN r.present_type_count>1"))
  self.assertIn("WHEN r.resolution_status='resolved' THEN 'confirmed'",policy)
  self.assertIn("WHEN c.decision_type IS NOT NULL THEN 'history'",policy)
  self.assertIn("r.present_channel_count>1",policy)
  self.assertIn("WHEN r.present_type_count>0 AND r.source_expires_at IS NOT NULL",policy)
 def test_summary_limits_protected_evidence_to_durable_case_identities(self):
  # All presentation fields still come from the admitted case view. The
  # source-free user-work projection supplies only its unique case identity.
  self.assertIn("FROM readable.video_type_user_work candidate",reads._HAS)
  self.assertIn("current_case.case_id=candidate.case_id OFFSET 0",reads._HAS)
  self.assertIn("inventory.video_id=c.video_id OFFSET 0",reads._HAS)
  self.assertIn("resolution.video_id=c.video_id OFFSET 0",reads._HAS)
  self.assertEqual(reads._HAS.count("candidate."),1)
  self.assertNotIn("public.",reads._HAS)
  self.assertIn(reads._REVIEW_STATUS,reads._HAS)
  self.assertNotIn("candidate",reads._GET)
  self.assertNotIn("candidate",reads._LIST)
if __name__=='__main__':unittest.main()
