"""A single due queue packs both operations across bounded candidate windows."""
from collections import Counter
from types import SimpleNamespace
import unittest

import test_common_feeder as fixtures
from collection.auto_pass import CommonCandidateCursor


class MixedRepository(fixtures.Repository):
    """Synthetic eligible queue; coordinator and frontier implementation are real."""
    def __init__(self, populations, *, physical=None):
        super().__init__({}, physical=physical or sorted(populations))
        self.queue = {channel:list(modes) for channel,modes in populations.items()}
        self.operations = {}

    def due_candidate_page(self, **request):
        self.pages.append(request)
        page = [key for key in self.physical if key > (request['after'] or '')][:request['limit']]
        rows = []
        for key in page:
            if request['video_type'] != 'long':
                continue
            ready = []
            for number, (operation, due) in enumerate(self.queue.get(key, ())):
                frontier = request.get('frontiers', {}).get(operation, {})
                if (request.get('operation') in (None, operation)
                        and not frontier.get('exhausted', False)
                        and key > (frontier.get('after') or '')):
                    ready.append((due, number, operation))
            ready.sort()
            if ready:
                rows.append(dict(channel_id=key, video_type='long', operation=ready[0][2],
                    due_at=ready[0][0], ready_count=min(50, len(ready))))
        if 'operation' not in request:
            rows=[{key:row[key] for key in ('channel_id','video_type')} for row in rows]
        return dict(rows=rows, next_after=page[-1] if page else request['after'],
                    inspected_count=len(page), scan_exhausted=len(page)<request['limit'])

    def claim_due_batch(self, **request):
        self.claims.append(request)
        scopes = request['scopes']
        operation = request.get('operation')
        if operation is None:
            heads = [(self.queue[s['channel_id']][0][1], self.queue[s['channel_id']][0][0])
                     for s in scopes if self.queue.get(s['channel_id'])]
            operation = min(heads)[1] if heads else None
        rows = []
        while len(rows) < request['limit']:
            before = len(rows)
            for scope in scopes:
                key = scope['channel_id']
                matches = [(due, n) for n,(mode,due) in enumerate(self.queue.get(key, ())) if mode==operation]
                if not matches or len(rows) >= request['limit']:
                    continue
                _, n = min(matches)
                self.queue[key].pop(n)
                identity = key+':'+str(self.issued.get(key,0))
                self.issued[key] = self.issued.get(key,0)+1
                self.operations[identity] = operation
                rows.append(SimpleNamespace(video_id=identity,channel_id=key,video_type='long'))
            if before == len(rows):
                break
        return dict(rows=rows, inspected_count=len(rows), paused_count=0,
                    scan_exhausted=len(rows)<request['limit'])


class OperationWindowTests(unittest.TestCase):
    setUp = fixtures.FeederTests.setUp
    create = fixtures.FeederTests.create
    drive = fixtures.FeederTests.drive

    def collect(self, populations, expected, **kwargs):
        repo = MixedRepository(populations, **kwargs)
        feeder,owner,executor = self.create(repo,limit=20000,concurrency=8)
        self.drive(feeder,owner,executor,maximum=10000)
        sent = [value for batch in self.sent for value in batch]
        self.assertEqual(len(sent),sum(expected.values()))
        self.assertEqual(len(set(sent)),len(sent))
        shapes = {}
        for batch in self.sent:
            modes = {repo.operations[value] for value in batch}
            self.assertEqual(len(modes),1)
            shapes.setdefault(modes.pop(),[]).append(len(batch))
        for mode,count in expected.items():
            full,tail = divmod(count,50)
            self.assertEqual(Counter(shapes.get(mode,[])),Counter([50]*full+([tail] if tail else [])))
        self.assertFalse(any(repo.queue.values()))
        self.assertFalse(any(request['operation']=='counters' and request['video_type']=='unknown'
                             for request in repo.pages))
        self.assertTrue(all(len(request['scopes'])<=50 for request in repo.claims))
        self.assertLessEqual(feeder.state.candidate_inspected,feeder.state.candidate_scan_limit)
        return repo,feeder

    def test_540_general_806_stats_fill_across_mixed_500_channel_windows(self):
        populations = {fixtures.channel(n):[] for n in range(1,501)}
        for n in range(540):
            populations[fixtures.channel(n%500+1)].append(('metadata',1000+n))
        for n in range(806):
            populations[fixtures.channel((n*73)%500+1)].append(('counters',900+n))
        self.collect(populations,dict(metadata=540,counters=806))

    def test_one_49_50_51_83_and_sparse_opposite_operation(self):
        for size in (1,49,50,51,83):
            with self.subTest(size=size):
                self.sent=[]
                population={fixtures.channel(n):[('counters',n)] for n in range(1,121)}
                for n in range(size):
                    population[fixtures.channel(n%120+1)].append(('metadata',1000+n))
                self.collect(population,dict(metadata=size,counters=120))

    def test_large_early_channel_and_small_tail_top_up_after_eof(self):
        population={fixtures.channel(1):[('metadata',n) for n in range(117)]}
        population.update({fixtures.channel(n):[('metadata',1000+n),('counters',500+n)]
                           for n in range(2,112)})
        repo,_=self.collect(population,dict(metadata=227,counters=110))
        # The tail is included before the large first scope can monopolize all work.
        tail=fixtures.channel(111)
        self.assertLess(next(n for n,r in enumerate(repo.claims) if any(
            scope['channel_id']==tail for scope in r['scopes'])),6)

    def test_seed_uses_oldest_head_not_fixed_general_preference(self):
        for oldest in ('metadata','counters'):
            with self.subTest(oldest=oldest):
                self.sent=[]
                other='metadata' if oldest=='counters' else 'counters'
                repo,_=self.collect({fixtures.channel(1):[(other,20)],
                                     fixtures.channel(2):[(oldest,10)]},dict(metadata=1,counters=1))
                self.assertEqual(repo.claims[0]['operation'],oldest)

    def test_exhausted_operation_head_cannot_hide_other_mode_same_channel(self):
        cursor=CommonCandidateCursor()
        for kind in ('long','short','stream','unknown'):
            cursor._exhausted.add(('metadata',kind))
        repo=MixedRepository({fixtures.channel(1):[('metadata',1),('counters',2)]})
        feeder,owner,executor=self.create(repo,limit=1,cursor=cursor)
        self.drive(feeder,owner,executor)
        self.assertEqual(repo.claims[0]['operation'],'counters')
        self.assertEqual(repo.queue[fixtures.channel(1)],[('metadata',1)])


if __name__=='__main__':unittest.main()
