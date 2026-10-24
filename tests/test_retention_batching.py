"""RR14 data-statement budgets and post-lock proof, offline driver fakes."""
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'application'))
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
from psycopg.types.json import Jsonb
from db.retention import inventory_retention as cleanup
from db.base.postgres import _reviewed_statement
import test_inventory_retention as support
from test_inventory_retention import one, Result, row, inventory, member, AT, LATER, FLOOR, OLD, CHANNEL, VIDEO


class BatchedProofTests(unittest.TestCase):
    setUp=support.RetentionTests.setUp
    queue=support.RetentionTests.queue
    header=support.RetentionTests.header
    run_page=support.RetentionTests.run_page
    start=support.RetentionTests.start

    def test_hundred_held_memberships_use_one_remaining_clock_batch_and_keep_all_exact_causes(self):
        keys=[row(video='video-'+str(i).zfill(3)) for i in range(100)]
        self.header();self.queue(Result(rows=tuple(keys)))
        for candidate in keys:self.queue(one(noop=False),one(acquired=False))
        remaining=[dict(video_id=k['video_id'],channel_id=CHANNEL,video_type='long',oldest=OLD,invalid=i==99)
                   for i,k in enumerate(keys)]
        self.queue(Result(rows=tuple(remaining)),one(value=OLD))
        result=self.run_page()
        self.assertEqual((result['scanned'],result['deferred'],result['invalid_clock_remaining']),(100,100,1))
        self.assertEqual(len(result['priority_references']),100)
        self.assertTrue(result['priority_complete'])
        batches=[p for q,p in self.connection.queries if cleanup._MEMBERSHIP_LAG_BATCH in q]
        self.assertEqual(len(batches),1)
        self.assertEqual(len(batches[0]['identities'].obj),100)
        self.assertEqual(batches[0]['cutoff'],FLOOR)
        self.assertEqual(self.connection.fetch_sizes.count(101),2)  # page plus exact remaining batch
        self.assertEqual(len(self.connection.queries),204)  # previous protocol: 803 data statements
        self.assertEqual(self.pool.borrowed,1)
        self.assertEqual(self.connection.rollbacks,100)
        self.assertFalse(any(q.startswith(('UPDATE','INSERT','DELETE')) for q,p in self.connection.queries))

    def test_hundred_rows_refreshed_after_selection_need_no_owner_locks(self):
        for target in ('inventory','memberships'):
            before=len(self.connection.queries)
            self.header(target);self.queue(Result(rows=tuple(row(video=str(i)) for i in range(100))))
            for _ in range(100):self.queue(one(noop=True))
            result=self.run_page(target)
            statements=self.connection.queries[before:]
            self.assertEqual(len(statements),102)  # previous protocol: 402
            self.assertEqual((result['scanned'],result['deferred'],result['scrubbed']),(100,0,0))
            self.assertFalse(any('FOR UPDATE' in q or 'advisory' in q for q,p in statements))
            self.assertFalse(any(cleanup._MEMBERSHIP_LAG_BATCH in q for q,p in statements))
            self.assertTrue(all('clock_timestamp()' in q for q,p in statements[2:]))

    def test_six_playlist_leases_are_sampled_once_after_ordered_locks_then_source_is_rechecked(self):
        self.start()
        states=tuple(dict(value='{"lease_token":null}') for _ in range(6))
        self.queue(one(acquired=True),one(channel_id=CHANNEL),Result(rows=states),one(busy=False),Result(),
                   inventory(),one(at=LATER),Result(),member(),
                   one(source_valid=True,first_valid=True,positive_valid=True),Result(),Result())
        result=self.run_page()
        queries=self.connection.queries
        locked=next(i for i,(q,p) in enumerate(queries) if cleanup._PLAYLISTS in q)
        checked=next(i for i,(q,p) in enumerate(queries) if cleanup._PLAYLISTS_BUSY in q)
        queue=next(i for i,(q,p) in enumerate(queries) if cleanup._QUEUE+' FOR UPDATE NOWAIT' in q)
        self.assertEqual(checked,locked+1)
        self.assertLess(checked,queue)
        self.assertEqual(len(queries[checked][1][0]),6)
        self.assertEqual(sum(cleanup._PLAYLISTS_BUSY in q for q,p in queries),1)
        self.assertIn('ORDER BY s.channel_id,s.video_type FOR UPDATE NOWAIT',queries[locked][0])
        post=[p for q,p in queries if cleanup._MEMBER_FACTS in q]
        self.assertEqual(len(post),1)
        self.assertEqual(post[0]['upper'],LATER)
        self.assertEqual((result['deleted'],result['scrubbed'],result['deferred']),(0,0,0))
        self.after_membership_source_write.assert_not_called()

    def test_composite_remaining_query_and_exact_queue_miss_bind_without_casting_indexed_keys(self):
        identities=[dict(video_id=VIDEO,channel_id=CHANNEL,video_type='long')]
        statements=(
            (cleanup._PREFLIGHT_INVENTORY,dict(video_id=VIDEO,cutoff=FLOOR)),
            (cleanup._PREFLIGHT_MEMBERSHIP,dict(video_id=VIDEO,channel_id=CHANNEL,video_type='long',cutoff=FLOOR)),
            (cleanup._MEMBERSHIP_LAG_BATCH,dict(identities=Jsonb(identities),cutoff=FLOOR)),
            (cleanup._PLAYLISTS_BUSY,(['{"lease_token":null}'],)),
        )
        for sql,params in statements:
            _reviewed_statement(sql,read=True,max_rows=100)
            PostgresQuery(Transformer()).convert(sql,params)
        self.assertIn('ORDER BY q.video_id LIMIT 1) qs ON qs.video_id=iv.video_id',cleanup._PREFLIGHT_INVENTORY)
        self.assertIn('USING(video_id,channel_id,video_type)',cleanup._MEMBERSHIP_LAG_BATCH)
        self.assertNotIn('::text=',cleanup._MEMBERSHIP_LAG_BATCH)
        self.assertNotIn('FOR UPDATE',cleanup._MEMBERSHIP_LAG_BATCH)


if __name__=='__main__':unittest.main()
