"""All-format missing completion keeps the existing bounded receipt replay."""
from unittest.mock import patch
import test_direct_pg_typed_completion_consumer as base
from db.videos import video_missing_completion as missing

class MissingCompletionConsumers(base.TypedCompletionConsumerTests):
 def setUp(self):
  super().setUp()
  self.cases=tuple((missing,'complete_typed_video_missing',self.repo.complete_missing,kind) for kind in ('long','short','stream'))+(
   (missing,'complete_typed_video_missing',self.repo.complete_long_missing,'long'),
   (missing,'complete_typed_video_missing',self.repo.complete_typed_missing,'short'),
   (missing,'complete_typed_video_missing',self.repo.complete_typed_missing,'stream'))
 def test_single_type_validation_before_dispatch(self):
  with self.assertRaises(ValueError):self.repo.complete_long_missing(self.rows('short'))
  with self.assertRaises(ValueError):self.repo.complete_typed_missing(self.rows('long'))
  with self.assertRaises(ValueError):self.repo.complete_missing(self.rows('unknown'))
  self.db.transaction.assert_not_called();self.db.call.assert_not_called()
 def test_common_mixed_types_share_one_batch_and_helper(self):
  rows=[dict(video_id=str(index),video_type=kind,lease_token='lease',collection_attempt_id='attempt') for index,kind in enumerate(('long','short','stream'))]
  with patch.object(missing,'complete_typed_video_missing',return_value=3) as action:
   self.assertEqual(self.repo.complete_missing(rows),3);action.assert_called_once_with(self.db,rows)
