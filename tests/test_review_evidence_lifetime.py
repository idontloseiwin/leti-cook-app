"""Review source deadlines survive real writer dispatch and safe read projection.

Native SQL expiry semantics require the scoped PG lifecycle acceptance;
these offline tests exercise the scalar/batch owner boundary and wire contract.
"""
from contextlib import nullcontext
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'application'))
from db.reviews import video_review_evidence as scalar
from db.inventory import review_effect_batches as batch
from video_type_reviews import VideoTypeReviewService, ReviewError

DEADLINE = '2026-10-15T00:00:00+00:00'
CASE = 'f8900000-0000-4000-8000-000000000001'
VIDEO = 'rv000000001'
CHANNEL = 'UC0000000000000000000001'


class ScalarTransaction:
    def __init__(self, deadline=DEADLINE):
        self.deadline, self.writes = deadline, []
    def borrow(self):
        return nullcontext()
    def fetch_one(self, query, params):
        if query == scalar._ADVISORY:
            return {'acquired': True}
        if query == scalar._INVENTORY:
            return dict(channel_id=CHANNEL, video_type='long')
        if query == scalar._EVIDENCE:
            return dict(types=['long', 'short'], channels=[CHANNEL], evidence_expires_at=self.deadline)
        if query == scalar._BLOCKED:
            return dict(blocked=None)
        if query == scalar._PRESENT:
            return dict(present=True)
        raise AssertionError('Unexpected source read')
    def execute(self, query, params):
        self.writes.append((query, params))
        return 1


class BatchTransaction:
    def __init__(self, deadline=DEADLINE):
        self.deadline, self.writes = deadline, []
    def borrow(self):
        return nullcontext()
    def fetch_all(self, query, params, *, max_rows):
        if query == batch._ADVISORY:
            return [{'acquired': True}]
        if query == batch._FACTS:
            return [dict(ordinality=1, video_id=VIDEO, channel_id=CHANNEL, video_type='long',
                         types=['long', 'short'], channels=[CHANNEL], blocked=None,
                         present=True, evidence_expires_at=self.deadline)]
        raise AssertionError('Unexpected source read')
    def execute(self, query, params):
        self.writes.append((query, params))
        return 1


def case(**overrides):
    row = dict(case_id=CASE, video_id=VIDEO, channel_id=CHANNEL, source_detached_at=None,
               canonical_type='long', observed_types=[], state='open', revision=3,
               first_seen_at='2026-09-01T00:00:00+00:00', last_seen_at='2026-09-02T00:00:00+00:00',
               can_decide=False, blocked_reason='source_unavailable', decision=None)
    return {**row, **overrides}


class EvidenceLifetimeTests(unittest.TestCase):
    def test_scalar_dispatch_preserves_native_deadline_with_payload(self):
        tx = ScalarTransaction()
        scalar.refresh_review_case(tx, VIDEO)
        self.assertEqual(len(tx.writes), 1)
        query, params = tx.writes[0]
        self.assertEqual(params['evidence_expires_at'], DEADLINE)
        self.assertIn('evidence_expires_at', query)
        self.assertIn('evidence_expires_at=excluded.evidence_expires_at', query)
        self.assertEqual(params['types'], ['long', 'short'])

    def test_batch_dispatch_preserves_same_source_deadline(self):
        tx = BatchTransaction()
        batch.refresh_review_cases_batch(tx, [VIDEO])
        self.assertEqual(len(tx.writes), 1)
        query, params = tx.writes[0]
        self.assertEqual(params[0].obj[0]['evidence_expires_at'], DEADLINE)
        self.assertIn('evidence_expires_at=excluded.evidence_expires_at', query)
        self.assertEqual(params[0].obj[0]['types'], ['long', 'short'])

    def test_invalid_batch_deadline_shape_fails_before_mutation(self):
        tx = BatchTransaction(deadline={'local_clock': 'invented'})
        with self.assertRaises(Exception):
            batch.refresh_review_cases_batch(tx, [VIDEO])
        self.assertEqual(tx.writes, [])

    def test_source_removal_clears_deadline_without_touching_decision(self):
        tx = ScalarTransaction()
        scalar.refresh_review_case(tx, VIDEO, source_removed=True)
        self.assertEqual(len(tx.writes), 1)
        query = tx.writes[0][0]
        self.assertIn('evidence_expires_at=NULL', query)
        self.assertNotIn('decision_type=', query)
        self.assertNotIn('last_seen_at=', query)
        self.assertIn('evidence_expires_at=NULL', batch._WITHDRAW)
        self.assertIn('evidence_expires_at=NULL', scalar._DETACH)

    def test_attached_case_can_withhold_stale_reason_without_claiming_eligibility(self):
        result = VideoTypeReviewService._case(case())
        self.assertEqual(result['video_id'], VIDEO)
        self.assertFalse(result['can_decide'])
        self.assertEqual(result['blocked_reason'], 'source_unavailable')
        self.assertEqual(result['observed_types'], [])

    def test_saved_decision_survives_unavailable_current_eligibility(self):
        saved = dict(video_type='long', reason='Owner decision', decided_at='2026-09-02T00:00:00+00:00', revision=3)
        result = VideoTypeReviewService._case(case(state='resolved', decision=saved))
        self.assertEqual(result['decision'], saved)
        self.assertFalse(result['can_decide'])

    def test_inconsistent_or_detached_identity_still_refused(self):
        for row in (case(can_decide=True), case(source_detached_at='2026-10-14T00:00:00+00:00')):
            with self.assertRaises(ReviewError):
                VideoTypeReviewService._case(row)


if __name__ == '__main__':
    unittest.main()
