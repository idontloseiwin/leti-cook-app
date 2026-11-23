"""Exact addressed queue admission; recording owner, no provider/database."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'application'), str(ROOT/'tests')]
from db.videos import video_targeted_claims as subject, video_scheduled_claims as scheduled
from db.base.postgres import DatabaseError
from test_video_scheduled_claims import Tx, RUN

NOW=datetime(2026,11,9,12,tzinfo=timezone.utc)


class TargetTx(Tx):
    def fetch_all(self, sql, params=(), *, max_rows):
        if sql==subject._LOCK:
            self.events.append((sql,params))
            return [row for row in self.rows if row['video_id'] in params['ids']
                    and row['video_type'] in params['formats'] and not row['paused_reason']
                    and row['lease_token'] is None and not row['initial_pending']
                    and row['last_claim_run_id']!=params['run_id']
                    and (params['verify'] or row['discovery_hydration_requested_at'] is not None)
                    and (row['next_due_at']<=NOW or (params['verify'] and row['last_error_type'] is None
                         and row['availability_first_missing_at'] is None
                         and row['availability_resume_observed_at'] is None))]
        if sql==subject._STATUS:
            self.events.append((sql,params))
            return [row for row in self.rows if row['video_id'] in params[0]]
        return super().fetch_all(sql,params,max_rows=max_rows)
    def returning_all(self,sql,params=(),*,max_rows):
        if sql==subject._PROMOTE:
            self.events.append((sql,params))
            for identity in params[0]: self.mode[identity]='metadata'
            return [dict(video_id=identity) for identity in params[0]]
        return super().returning_all(sql,params,max_rows=max_rows)


class TargetedClaimsTests(unittest.TestCase):
    def setUp(self):
        self.tx=TargetTx()
        guard=patch.object(subject,'assert_channel_scopes')
        self.fence=guard.start();self.addCleanup(guard.stop)
        self.tx.rows=[self.row('wanted'),self.row('unrelated')]
    def row(self, identity, **changes):
        return dict(dict(video_id=identity,channel_id='channel',video_type='long',
            next_due_at=NOW-timedelta(hours=1),observed_at=NOW,paused_reason=None,
            lease_token=None,lease_expires_at=None,last_claim_run_id=None,initial_pending=False,
            initial_job_id=None,availability_first_missing_at=None,availability_resume_observed_at=None,
            availability_schedule_type=None,last_error_type=None,discovery_hydration_requested_at=NOW),**changes)
    def invoke(self,ids=None,**changes):
        return subject.claim_targeted_batch(None,['wanted'] if ids is None else ids,
            **dict(dict(worker_id='discovery',run_id=RUN,video_types=('long','short'),transaction=self.tx),**changes))
    def test_healthy_future_target_verifies_metadata_not_unrelated_due(self):
        self.tx.rows[0]['next_due_at']=NOW+timedelta(days=1)
        result=self.invoke()
        self.assertEqual([(row['video_id'],row['collection_mode']) for row in result['rows']], [('wanted','metadata')])
        claimed=[params for sql,params in self.tx.events if sql==scheduled._CLAIM]
        self.assertEqual(claimed[0]['video_ids'],['wanted'])
        self.fence.assert_called_once()
    def test_verify_false_requires_marker_and_does_not_override_future_retry(self):
        for changes in (dict(discovery_hydration_requested_at=None),dict(next_due_at=NOW+timedelta(hours=1))):
            self.tx.rows[0].update(changes)
            self.assertEqual(self.invoke(verify=False)['rows'],[])
        self.assertFalse(any(sql==scheduled._CLAIM for sql,_ in self.tx.events))
    def test_live_owner_and_initial_owner_defer_exact_id(self):
        self.tx.rows[0].update(lease_token='someone',lease_expires_at=NOW+timedelta(minutes=3))
        result=self.invoke();self.assertEqual(result['rows'],[])
        self.assertEqual(result['deferred'][0]['reason'],'video_owner_busy')
        self.assertEqual(result['deferred'][0]['next_eligible_at'],NOW+timedelta(minutes=3))
        self.tx.rows[0].update(lease_token=None,lease_expires_at=None,initial_pending=True,initial_job_id='job')
        self.assertEqual(self.invoke()['deferred'][0]['owner_job_id'],'job')
    def test_protected_retry_is_not_bypassed_by_verify(self):
        for changes in (dict(last_error_type='transient'),dict(availability_first_missing_at=NOW),
                        dict(availability_resume_observed_at=NOW)):
            self.tx.rows[0]=self.row('wanted',next_due_at=NOW+timedelta(hours=1),**changes)
            result=self.invoke()
            self.assertEqual(result['rows'],[])
            self.assertEqual(result['deferred'][0]['reason'],'canonical_retry_wait')
    def test_hidden_channel_canonical_guard_remains_authoritative(self):
        self.tx.pause['channel']='channel_hidden'; self.tx.eligible['wanted']=False;self.tx.recovery['wanted']=False
        result=self.invoke()
        self.assertEqual(result['rows'],[])
        self.assertEqual(result['paused_count'],1)
        self.assertFalse(any(sql==scheduled._CLAIM for sql,_ in self.tx.events))
    def test_disabled_stream_is_not_claimed_or_substituted(self):
        self.tx.rows[0]['video_type']='stream'
        result=self.invoke()
        self.assertEqual(result['rows'],[])
        self.assertEqual(result['deferred'][0]['reason'],'format_turned_off')
    def test_invalid_target_shapes_refuse_before_owner_transaction(self):
        for ids in ('wanted',['wanted','wanted'],['x']*51,[None],[' wanted']):
            with self.subTest(ids=ids),self.assertRaises(ValueError):self.invoke(ids)
        self.assertEqual(self.tx.events,[])
    def test_partial_lease_return_is_rejected_before_final_fence(self):
        original=self.tx.returning_all
        self.tx.returning_all=lambda sql,params=(),max_rows=50: [] if sql==scheduled._CLAIM else original(sql,params,max_rows=max_rows)
        with self.assertRaises(DatabaseError):self.invoke()
        self.assertTrue(self.tx.failed);self.fence.assert_not_called()


if __name__=='__main__':unittest.main()
