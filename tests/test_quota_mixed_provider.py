"""Mixed recurring channels/types through real dispatch and service; offline."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, get_ident
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'application'), str(ROOT / 'tests')]
from collection.common_video_dispatch import CommonBatchUnit
from collection.dispatch import Ack, Completion, Prepared
import collection_shutdown
from db import VideoCollectionClaim
from test_common_video_dispatch import Repository
from test_initial_video_parallel import END, STAMP, service, video


class MixedRepository(Repository):
    def __init__(self, claims):
        super().__init__()
        self.claims = claims

    def claim(self):
        assert get_ident() == self.owner
        self.claimed += 1
        return {'rows': list(self.claims), 'inspected_count': len(self.claims),
                'paused_count': 0, 'scan_exhausted': True}

    def complete_typed_metadata(self, rows):
        return self.record('typed', rows)

    def complete_stream_lifecycle(self, rows):
        return self.record('lifecycle', rows)

    def complete_identity_refresh(self, rows):
        return self.record('identity', rows)


class MixedProviderTests(unittest.TestCase):
    def setUp(self):
        gate = patch.object(collection_shutdown, '_requested', Event())
        gate.start()
        self.addCleanup(gate.stop)
        kinds = [('long', 'metadata'), ('long', 'counters'), ('short', 'metadata'),
                 ('stream', 'metadata'), ('stream', 'stream_lifecycle'), ('unknown', 'identity_refresh')]
        self.claims = [VideoCollectionClaim(video_id=f'video{index:06d}', channel_id='UC' + letter * 22,
            video_type=kind, collection_mode=mode, lease_token=f'original-token-{index}',
            lease_expires_at=STAMP, next_due_at=STAMP)
            for index, ((kind, mode), letter) in enumerate(zip(kinds, 'abcdef'))]
        self.by_id = {claim.video_id: claim for claim in self.claims}
        self.views = {claim.video_id: 1000 + index for index, claim in enumerate(self.claims)}
        self.repo, self.network = MixedRepository(self.claims), []
        self.collector = service(self.repo, Mock(side_effect=AssertionError('initial provider must not run')))
        self.bad_identity = None
        self.collector.fetch_metadata = self.metadata
        self.collector.fetch_counters = self.counters
        self.collector.build_counter_payload = self.counter_payload
        self.claimed, self.settled = Mock(), Mock()
        # The descriptor is ordered under the counter-only channel; it does not
        # own the metadata or type of the other five claims.
        anchor = self.claims[1]
        self.unit = CommonBatchUnit(self.collector, key='mixed-recurring', lane='auto',
            channel=anchor.channel_id, video_type=anchor.video_type, claim_batch=self.repo.claim,
            require_ready=Mock(), on_claimed=self.claimed, on_settled=self.settled)

    def metadata(self, ids, reason):
        self.assertNotEqual(get_ident(), self.repo.owner)
        self.network.append(('general', list(ids), reason))
        result = []
        for identity in reversed(ids):
            claim = self.by_id[identity]
            item = video(identity, ended=claim.video_type == 'stream' and claim.collection_mode == 'metadata')
            item['snippet']['channelId'] = (self.claims[0].channel_id if identity == self.bad_identity
                                           else claim.channel_id)
            item['snippet']['title'] = identity
            item['statistics']['viewCount'] = str(self.views[identity])
            result.append(item)
        return result

    def counters(self, ids, reason):
        self.assertNotEqual(get_ident(), self.repo.owner)
        self.network.append(('stats', list(ids), reason))
        return {'items': [{'id': identity, 'views': self.views[identity], 'like_count': 7,
                           'comment_count': 2} for identity in reversed(ids)],
                'summary': {'failed_ids': []}}

    def counter_payload(self, item, recorded_at):
        self.assertEqual(get_ident(), self.repo.owner)
        return {'snapshot': {**item, 'recorded_at': recorded_at}}

    def run_batch(self):
        self.assertEqual(self.repo.claimed, 0)
        prepared = self.unit.prepare()
        with ThreadPoolExecutor(max_workers=1) as executor:
            steps = 0
            while isinstance(prepared, Prepared):
                steps += 1
                self.assertLessEqual(steps, 2, 'mixed batch must not add provider requests')
                result = executor.submit(prepared.fetch, Event()).result(timeout=5)
                prepared = self.unit.settle(Completion('response', value=result, fetch_started=True))
        self.assertIsInstance(prepared, Ack)
        self.assertEqual(self.repo.claimed, 1)
        self.claimed.assert_called_once()
        self.settled.assert_called_once()
        self.collector.forbidden.assert_not_called()
        self.collector.fetch_initial_profiles.assert_not_called()
        self.assertFalse(self.unit.summary['settlement_unknown'])
        self.assertEqual((self.unit.summary['metadata_requests'], self.unit.summary['batch_requests']), (1, 1))
        self.assertEqual([call[0] for call in self.network], ['general', 'stats'])
        self.assertEqual({call[2] for call in self.network}, {'auto_video_collection'})
        rows = {row['video_id']: (operation, row) for operation, batch in self.repo.writes for row in batch}
        self.assertEqual(sum(len(batch) for _, batch in self.repo.writes), len(self.claims))
        self.assertEqual(set(rows), set(self.by_id))
        for identity, (operation, row) in rows.items():
            claim = self.by_id[identity]
            self.assertEqual(row['lease_token'], claim.lease_token)
            if operation in ('counters', 'failure'):
                # These repository contracts resolve the owner from the exact
                # video/token, rather than accepting a descriptor channel.
                self.assertNotIn('channel_id', row)
            else:
                self.assertEqual(row['channel_id'], claim.channel_id)
            if operation in ('metadata', 'typed', 'counters'):
                self.assertEqual(row['views'], self.views[identity])
            if operation == 'typed':
                self.assertEqual(row['video_type'], claim.video_type)
                self.assertEqual(row['title'], identity)
        return rows

    def test_mixed_types_keep_exact_provider_subsets_and_original_owners(self):
        rows = self.run_batch()
        ids = [claim.video_id for claim in self.claims]
        self.assertEqual(self.network[0][1], [ids[0], *ids[2:]])
        self.assertEqual(self.network[1][1], [ids[1]])
        self.assertEqual([rows[identity][0] for identity in ids],
                         ['metadata', 'counters', 'typed', 'typed', 'lifecycle', 'identity'])
        self.assertEqual(rows[ids[3]][1]['stream_actual_end_at'], END)
        self.assertIsNone(rows[ids[4]][1]['stream_actual_end_at'])
        self.assertFalse(rows[ids[5]][1]['missing'])
        self.assertEqual({kind: self.unit.summary[kind + '_updated'] for kind in ('long', 'short', 'stream', 'unknown')},
                         {'long': 2, 'short': 1, 'stream': 1, 'unknown': 1})
        self.assertEqual((self.unit.summary['updated'], self.unit.summary['stream_lifecycle_probed'],
                          self.unit.summary['errors']), (5, 1, 0))

    def test_foreign_provider_channel_refuses_only_its_original_claim(self):
        self.bad_identity = self.claims[2].video_id
        rows = self.run_batch()
        ids = [claim.video_id for claim in self.claims]
        self.assertEqual(self.network[0][1], [ids[0], *ids[2:]])
        self.assertEqual(self.network[1][1], [ids[1]])
        self.assertEqual(rows[ids[2]][0], 'failure')
        self.assertEqual(rows[ids[2]][1]['error_type'], 'provider_identity_or_lifecycle_error')
        self.assertEqual([rows[identity][0] for identity in ids if identity != ids[2]],
                         ['metadata', 'counters', 'typed', 'lifecycle', 'identity'])
        self.assertEqual((self.unit.summary['updated'], self.unit.summary['stream_lifecycle_probed'],
                          self.unit.summary['errors']), (4, 1, 1))


if __name__ == '__main__':
    unittest.main()
