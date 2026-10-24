"""Sparse physical channel pages must not fragment ready provider batches.

Real feeder, pass state and coordinator; existing bounded repository/provider
fixtures. All eligible rows exist before the pass and the fake clock never moves.
"""
from collections import Counter
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'application'), str(ROOT/'tests')]
import test_common_feeder as fixtures


# Live run107's physical positions and counts, with synthetic channel identities.
RUN107 = {67:5, 72:1, 81:1, 147:1, 157:1, 247:1, 259:1, 306:1, 564:1, 708:23, 737:19}
PHYSICAL = [fixtures.channel(rank) for rank in range(1, 831)]


class PartitionedRepository:
    """Existing fixture per format; a claim accepts one provider operation.

    Operation selection is a fixture boundary, not a copy of production SQL.
    The tests below exercise whether the feeder retains the other work and
    combines sparse scopes; repository operation policy has separate tests.
    """
    def __init__(self, populations):
        self.parts = {kind:fixtures.Repository(rows, kind=kind, physical=PHYSICAL)
                      for kind, rows in populations.items()}
        self.pages, self.claims = [], []
        self.operations = {}

    def due_candidate_page(self, **request):
        self.pages.append(request)
        return self.parts[request['video_type']].due_candidate_page(**request)

    def claim_due_batch(self, **request):
        self.claims.append(request)
        scopes = request['scopes']
        ready = [scope for scope in scopes
                 if self.parts[scope['video_type']].rows.get(scope['channel_id'], 0)]
        if not ready:
            return dict(rows=[], inspected_count=0, paused_count=0, scan_exhausted=True)
        operation = lambda kind: 'stats' if kind == 'long' else 'general'
        chosen = operation(ready[0]['video_type'])
        rows = []
        for scope in ready:
            kind = scope['video_type']
            if operation(kind) != chosen or len(rows) == request['limit']:
                continue
            options = {key:value for key,value in request.items() if key != 'scopes'}
            options.update(scope=scope, limit=request['limit']-len(rows))
            batch = self.parts[kind].claim_due_batch(**options)
            for row in batch['rows']:
                row.video_id = kind+':'+row.video_id
                self.operations[row.video_id] = chosen
            rows.extend(batch['rows'])
        exhausted = not any(self.parts[scope['video_type']].rows.get(scope['channel_id'], 0)
                            for scope in scopes)
        return dict(rows=rows, inspected_count=len(rows), paused_count=0, scan_exhausted=exhausted)


class SparsePhysicalPageTests(unittest.TestCase):
    # Reuse only fixture helpers, without inheriting/rerunning its test class.
    setUp = fixtures.FeederTests.setUp
    create = fixtures.FeederTests.create
    drive = fixtures.FeederTests.drive

    def collect(self, repository, expected_total):
        feeder, coordinator, executor = self.create(repository, limit=6000, concurrency=8)
        clock_before = coordinator.clock()
        self.drive(feeder, coordinator, executor)
        self.assertEqual(coordinator.clock(), clock_before)
        sent = [identity for batch in self.sent for identity in batch]
        self.assertEqual(len(sent), expected_total)
        self.assertEqual(len(set(sent)), expected_total)
        self.assertEqual(feeder.state.claimed, expected_total)
        self.assertEqual(feeder.summary['updated'], expected_total)
        self.assertTrue(all(0 < len(batch) <= 50 for batch in self.sent))
        self.assertTrue(all(len(request['scopes']) <= 50 for request in repository.claims))
        self.assertTrue(all(0 < request['limit'] <= 50 for request in repository.pages))
        self.assertLessEqual(feeder.state.candidate_inspected, feeder.state.candidate_scan_limit)
        self.assertEqual(coordinator.snapshot().inflight, 0)
        return feeder

    def test_exact_live_830_channel_layout_combines_55_ready_rows_into_50_and_5(self):
        repository = fixtures.Repository({fixtures.channel(rank):count for rank,count in RUN107.items()},
                                         physical=PHYSICAL)
        self.collect(repository, 55)
        self.assertEqual([len(batch) for batch in self.sent], [50, 5])
        self.assertEqual(sum(repository.rows.values()), 0)

    def test_83_ready_rows_cross_sparse_pages_and_send_final_33_now(self):
        population = {**RUN107, 737:RUN107[737]+28}
        repository = fixtures.Repository({fixtures.channel(rank):count for rank,count in population.items()},
                                         physical=PHYSICAL)
        self.collect(repository, 83)
        self.assertEqual([len(batch) for batch in self.sent], [50, 33])
        self.assertEqual(sum(repository.rows.values()), 0)

    def test_one_ready_row_is_sent_without_future_arrival_or_clock_advance(self):
        repository = fixtures.Repository({fixtures.channel(737):1}, physical=PHYSICAL)
        feeder = self.collect(repository, 1)
        self.assertEqual([len(batch) for batch in self.sent], [1])
        self.assertEqual(feeder.pass_stop_reason, 'sweep_exhausted')
        self.assertEqual(sum(repository.rows.values()), 0)

    def test_sparse_formats_keep_general_and_stats_separate_without_losing_revisit(self):
        populations = {
            'long': {fixtures.channel(67):5, fixtures.channel(737):48},
            'short': {fixtures.channel(147):11, fixtures.channel(708):10},
            'stream': {fixtures.channel(247):9},
            'unknown': {fixtures.channel(564):1},
        }
        repository = PartitionedRepository(populations)
        self.collect(repository, 84)
        shape = []
        for batch in self.sent:
            operations = {repository.operations[identity] for identity in batch}
            self.assertEqual(len(operations), 1)
            shape.append((operations.pop(), len(batch)))
        self.assertEqual(Counter(shape), Counter({('stats', 50):1, ('stats', 3):1, ('general', 31):1}))
        self.assertEqual(sum(sum(part.rows.values()) for part in repository.parts.values()), 0)


    def test_each_offer_reads_at_most_four_pages_and_buffers_at_most_50_scopes(self):
        population = {fixtures.channel(rank): 1 for rank in range(6, 727, 6)}
        repository = fixtures.Repository(population, physical=PHYSICAL)
        feeder, owner, executor = self.create(repository, limit=6000, concurrency=8)
        original_read = repository.due_candidate_page
        largest_window = 0

        def checked_read(**request):
            nonlocal largest_window
            page = original_read(**request)
            # The advisory buffer has no public size metric. Check its sole
            # memory bound here; all checkpoint assertions below use replay.
            window_size = len(feeder._pending_scopes) + len(page['rows'])
            largest_window = max(largest_window, window_size)
            self.assertLessEqual(window_size, 50)
            return page

        repository.due_candidate_page = checked_read
        for _ in range(1500):
            before = len(repository.pages)
            feeder.offer_to(owner)
            self.assertLessEqual(len(repository.pages) - before, 4)
            owner.step()
            for index, (future, *_) in enumerate(executor.jobs):
                if not future.done():
                    executor.run(index)
            snapshot = owner.snapshot()
            if feeder.exhausted and not snapshot.ready and not snapshot.held:
                break
        else:
            self.fail('bounded sparse pass did not finish')
        self.assertEqual(largest_window, 50)
        self.assertEqual(sum(map(len, self.sent)), len(population))
        self.assertEqual(len({video for batch in self.sent for video in batch}), len(population))
        self.assertEqual(sum(repository.rows.values()), 0)

    def test_stop_with_unclaimed_scope_and_empty_tail_replays_confirmed_frontier(self):
        repository = fixtures.Repository({fixtures.channel(67): 1, fixtures.channel(737): 1},
                                         physical=PHYSICAL)
        cursor = fixtures.CommonCandidateCursor()
        feeder, owner, executor = self.create(repository, limit=6000, concurrency=8, cursor=cursor)
        for _ in range(4):
            self.assertEqual(feeder.offer_to(owner), 0)
        # Empty physical pages beyond rank67 were visited, but its earlier
        # candidate has not reached claim ACK and must survive the handoff.
        self.assertTrue(any(request['video_type'] == 'long'
                            and (request['after'] or '') > fixtures.channel(67)
                            for request in repository.pages))
        self.assertFalse(repository.claims)
        self.assertFalse(owner.keys())
        owner.stop()
        page_count = len(repository.pages)
        self.assertEqual(feeder.offer_to(owner), 0)
        self.assertEqual(len(repository.pages), page_count)
        feeder.pause('scheduled_handoff')
        feeder.finish_pass(owner)

        resumed, next_owner, next_executor = self.create(repository, limit=6000,
                                                         concurrency=8, cursor=cursor)
        self.drive(resumed, next_owner, next_executor)
        self.assertEqual([len(batch) for batch in self.sent], [2])
        self.assertEqual(sum(repository.rows.values()), 0)
        self.assertEqual(resumed.summary['updated'], 2)

    def test_cancel_or_stop_before_claim_replays_offered_sparse_window(self):
        for action in ('cancel', 'stop_before_prepare'):
            with self.subTest(action=action):
                self.sent.clear()
                repository = fixtures.Repository({fixtures.channel(67): 1, fixtures.channel(737): 1},
                                                 physical=PHYSICAL)
                cursor = fixtures.CommonCandidateCursor()
                feeder, owner, executor = self.create(repository, limit=6000,
                                                      concurrency=8, cursor=cursor)
                for _ in range(100):
                    if feeder.offer_to(owner):
                        break
                else:
                    self.fail('sparse descriptor was never offered')
                self.assertEqual(owner.snapshot().ready, 1)
                self.assertFalse(repository.claims)
                if action == 'cancel':
                    self.assertTrue(owner.cancel(owner.keys()[0]))
                else:
                    feeder.should_stop = lambda: True
                    owner.step()
                self.assertFalse(owner.keys())
                self.assertFalse(repository.claims)
                self.assertFalse(self.sent)
                feeder.pause('scheduled_handoff')
                feeder.finish_pass(owner)

                resumed, next_owner, next_executor = self.create(repository, limit=6000,
                                                                 concurrency=8, cursor=cursor)
                self.drive(resumed, next_owner, next_executor)
                self.assertEqual([len(batch) for batch in self.sent], [2])
                self.assertEqual(sum(repository.rows.values()), 0)

    def test_candidate_scan_budget_flushes_ready_remainder_then_resumes_unread_tail(self):
        repository = fixtures.Repository({fixtures.channel(67): 1, fixtures.channel(737): 1},
                                         physical=PHYSICAL)
        cursor = fixtures.CommonCandidateCursor()
        feeder, owner, executor = self.create(repository, limit=6000, concurrency=8, cursor=cursor)
        # The seed inspects250 rows; its filtered page then finds rank67.
        # This finite scan allowance must not discard the buffered singleton.
        feeder.state.candidate_scan_limit = 300
        self.drive(feeder, owner, executor)
        self.assertEqual(feeder.state.candidate_inspected, 300)
        self.assertEqual(feeder.pass_stop_reason, 'candidate_scan_budget')
        self.assertEqual([len(batch) for batch in self.sent], [1])
        self.assertEqual(repository.rows[fixtures.channel(67)], 0)
        self.assertEqual(repository.rows[fixtures.channel(737)], 1)
        feeder.finish_pass(owner)

        resumed, next_owner, next_executor = self.create(repository, limit=6000,
                                                         concurrency=8, cursor=cursor)
        self.drive(resumed, next_owner, next_executor)
        self.assertEqual([len(batch) for batch in self.sent], [1, 1])
        self.assertEqual(len({video for batch in self.sent for video in batch}), 2)
        self.assertEqual(sum(repository.rows.values()), 0)



if __name__ == '__main__':
    unittest.main()
