"""Focused handoff admission/projection tests; native probe owns SQL semantics."""
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'application'))
from db.videos import video_initial_policy as policy
from db.jobs import operator_job_chunks as chunks
from db.jobs import operator_job_chunk_settlement as settlement
from db.videos import video_initial_claims as initial
from db.videos import video_scheduled_claims as scheduled
from db.videos import video_claims as addressed
from db.videos import video_queue_initialization as queue
from db.videos import typed_video_read_policy as typed
from channel_sync_policy import channel_sync_eligibility
from test_video_scheduled_claims import Tx


class HandoffTests(unittest.TestCase):
    def test_completed_target_can_override_only_channel_initial_pause(self):
        tx=Tx();tx.rows=[dict(video_id='v',channel_id='channel',video_type='long')];tx.ids=['v']
        tx.pause['channel']='channel_inventory_not_ready';tx.handoff.add('v')
        with patch.object(scheduled,'assert_channel_scopes'):
            result=scheduled.claim_due_video_collection_batch(None,50,'worker','run',transaction=tx)
        self.assertEqual([r['video_id'] for r in result['rows']],['v'])
        self.assertEqual(result['paused_count'],0)
        self.assertFalse(any(event[0]==scheduled._PAUSE for event in tx.events))

    def test_handoff_does_not_override_wrong_inventory_identity(self):
        tx=Tx();tx.rows=[dict(video_id='v',channel_id='channel',video_type='long')];tx.ids=['v']
        tx.eligible['v']=False;tx.handoff.add('v')
        with patch.object(scheduled,'assert_channel_scopes'):
            result=scheduled.claim_due_video_collection_batch(None,50,'worker','run',transaction=tx)
        self.assertEqual(result['rows'],[])
        self.assertEqual([event[1]['pause_reasons'] for event in tx.events if event[0]==scheduled._PAUSE],[['inventory_not_long']])

    def test_queue_initializer_keeps_handoff_unpaused_and_source_rules_intact(self):
        source=dict(is_unavailable=False,needs_hydration=False,availability_status='public',
            collection_mode='operator_hold',owner_permission='accepted',initial_handoff=True)
        self.assertIsNone(queue._pause(source))
        self.assertEqual(queue._pause(dict(source,initial_handoff=False)),'channel_inventory_not_ready')
        self.assertIsNone(queue._pause(dict(source,is_unavailable=True)))
        self.assertEqual(queue._pause(dict(source,availability_status='private')),'availability_unsupported')

    def test_ui_channel_gate_is_not_opened_by_per_video_handoff(self):
        result=channel_sync_eligibility(dict(collection_mode='initial_inventory',
            collection_permission='accepted',collection_control_epoch=1))
        self.assertFalse(result.allow_video_collection)
        self.assertFalse(result.allow_discovery)

    def test_all_writes_recheck_same_shared_error_backoff(self):
        for query in (chunks._FULL_CHUNK,chunks._SELECT,initial._CLAIM):
            self.assertIn(policy.INITIAL_RETRY_ALLOWED_SQL,query)
        self.assertIn("state.last_error_type IS NOT NULL",policy.INITIAL_RETRY_ALLOWED_SQL)
        self.assertIn('state.next_due_at>now()',policy.INITIAL_RETRY_ALLOWED_SQL)

    def test_scope_one_attempts_are_diagnostic_and_parent_wait_uses_queue(self):
        for query in (chunks._RETRIES,settlement._EXHAUSTED):
            self.assertIn("NOT(j.job_type='channel_initial_long_inventory' AND j.initial_scope_version=1)",query)
        self.assertIn(policy.INITIAL_JOB_NEXT_DUE_SQL,chunks._DEFER)
        self.assertIn(policy.INITIAL_JOB_NEXT_DUE_SQL,settlement._UPDATE)
        self.assertIn('state.lease_expires_at',policy.INITIAL_JOB_NEXT_DUE_SQL)
        self.assertIn('state.next_due_at',policy.INITIAL_JOB_NEXT_DUE_SQL)

    def test_native_predicates_require_exact_progress_and_current_owner(self):
        sql=policy.VIDEO_HANDOFF_STATE_SQL
        for guard in ("owner_permission='accepted'",'NOT handoff_control.is_hidden',
                'handoff_job.accepted_epoch=handoff_control.control_epoch',
                'handoff_job.channel_id=handoff_control.channel_id',
                'handoff_target.video_type=state.video_type','handoff_target.video_id=state.video_id',
                'handoff_job.targets_frozen_at IS NOT NULL',"handoff_job.initial_scope_version=1"):
            self.assertIn(guard,sql)
        self.assertNotIn("'pending'",sql)
        self.assertNotIn("'blocked'",sql)
        self.assertIn("'error'",sql)
        self.assertIn(policy.VIDEO_INITIAL_PENDING_STATE_SQL,scheduled._WINDOW[False])
        self.assertIn(policy.VIDEO_INITIAL_PENDING_STATE_SQL,addressed._CLAIM_METADATA)

    def test_paused_discovery_needs_exact_live_initial_chunk_or_terminal_target(self):
        fact=typed._IDENTITY_FACT_SQL['playlist_enabled']
        self.assertIn('d.runtime_enabled',fact)
        self.assertIn(policy.VIDEO_HANDOFF_TYPED_SQL,fact)
        self.assertIn(policy.VIDEO_INITIAL_CHUNK_TYPED_SQL,fact)
        for guard in ("owner_permission='accepted'",'NOT handoff_control.is_hidden',
                'handoff_job.accepted_epoch=handoff_control.control_epoch',
                'handoff_job.lease_channel_epoch=handoff_control.control_epoch',
                'handoff_job.lease_expires_at>now()',"handoff_job.status='running'",
                "handoff_job.phase='hydrate_videos'","handoff_target.status='pending'",
                'handoff_target.video_id=i.video_id','handoff_target.video_type=i.video_type',
                "(handoff_job.summary_json->'full_hydration_chunk') ? i.video_id"):
            self.assertIn(guard,policy.VIDEO_INITIAL_CHUNK_TYPED_SQL)
        # The exception changes only discovery permission. Source identity and
        # unfinished-target automatic exclusion remain separate requirements.
        for fact_name in ('inventory_ready','scan_complete','foreign_present','exact_present','conflicting_present_type'):
            self.assertNotIn(policy.VIDEO_INITIAL_CHUNK_TYPED_SQL,typed._IDENTITY_FACT_SQL[fact_name])
        self.assertIn(policy.VIDEO_INITIAL_PENDING_STATE_SQL,scheduled._WINDOW[False])
        self.assertIn(policy.VIDEO_INITIAL_PENDING_STATE_SQL,addressed._CLAIM_METADATA)

    def test_composed_statements_compile_real_driver_parameters(self):
        for module in (initial,chunks,settlement,scheduled,queue):
            for name,value in vars(module).items():
                if not isinstance(value,str) or not value.lstrip().startswith(('SELECT','UPDATE','WITH','INSERT')):
                    continue
                if name in ('_FILTER','_SCOPE','_RECORD','_J') or '{typed}' in value:
                    continue
                names=re.findall(r'%\(([^)]+)\)s',value)
                values={key:None for key in names} if names else tuple(None for _ in re.findall('%s',value))
                PostgresQuery(Transformer()).convert(value,values)


if __name__=='__main__':unittest.main()
