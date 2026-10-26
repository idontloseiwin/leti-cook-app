"""Offline checks of the manual fixture and honest measurement summaries."""
import sys
import os
from pathlib import Path
import unittest
import tempfile
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).parent))
from research_query_benchmark import SIZES, generated_row, oracle, workloads, summarize, NOW
from research_query.validation import validate,canonical,AccessContext

class BenchmarkFixtureTests(unittest.TestCase):
    def test_no_tail_claim_from_few_measurements(self):
        self.assertIsNone(summarize([1,2,500])['p95_ms'])
        self.assertEqual(summarize(list(range(1,21)))['p95_ms'],19)
        self.assertEqual(summarize([]),{'n':0})

    def test_deterministic_valid_id_and_skew(self):
        rows=[generated_row(i,100) for i in range(1000)]
        self.assertEqual(rows,[generated_row(i,100) for i in range(1000)])
        self.assertEqual(len({r['id'] for r in rows}),1000)
        self.assertTrue(all(len(r['id'])==11 and len(r['channel_id'])==24 for r in rows))
        self.assertGreaterEqual(sum(r['channel']<10 for r in rows),500)
        self.assertEqual({r['type'] for r in rows},{'long','short','stream'})

    def test_oracle_excludes_hidden_conflicting_and_unproven_rows(self):
        for q in workloads().values():
            ids=oracle(q,100,10000,NOW)
            for identifier in ids:
                row=generated_row(int(identifier[1:]),100)
                self.assertFalse(row['hidden'])
                self.assertEqual(row['proof'],'resolved')
                self.assertEqual(row['availability'],'public')
                if row['type']=='stream':
                    self.assertEqual(row['stream_completion'],'proven')
                    self.assertFalse(row['states'])

    def test_cyrillic_titles_take_the_unicode_path_and_default_is_unchanged(self):
        import research_query_benchmark as benchmark
        self.assertEqual(benchmark.TITLES,'mixed')
        self.assertEqual(generated_row(1,100)['title'],'Synthetic video 1')
        try:
            benchmark.TITLES='cyrillic'
            titles=[generated_row(i,100)['title'] for i in range(1100)]
        finally:
            benchmark.TITLES='mixed'
        present=[t for t in titles if t is not None]
        self.assertGreater(sum(not t.isascii() for t in present)/len(present),.99)
        self.assertGreater(sum('Ё' in t for t in present)/len(present),.85)
        self.assertEqual(generated_row(22,100)['title'],'Straße 測試 22')

    def test_sizes_are_finite_and_large_run_not_accepted(self):
        self.assertNotIn('L',SIZES)
        self.assertEqual(SIZES['M'][:2],(1000,250000))

    def test_all_measured_queries_obey_real_contract(self):
        for q in workloads().values():
            validate(canonical(q),AccessContext('fixture','owner',enabled=True))

    def test_executor_keeps_protected_sources(self):
        from db.reads.research_sql import _SOURCE
        source=_SOURCE
        self.assertIn('FROM readable.hot_video_inventory p',source)
        self.assertIn('JOIN readable.videos v',source)
        self.assertNotIn('FROM public.videos',source)
        # Type proof is read per row by its keys, not materialized for the scope.
        self.assertEqual(source.count('readable.hot_video_inventory'),1)
        self.assertIn('p.video_id=i.video_id AND p.channel_id=i.channel_id AND p.video_type=i.video_type LIMIT 1) h ON true',source)
        self.assertNotIn('hot_scoped',source)

    def test_browser_fixture_setup_crosses_a_second_boundary(self):
        from research_query_preview import create_preview_app
        from db.self_hosted_auth import OwnerAuthRepository
        clock=[1_800_000_000]
        original=OwnerAuthRepository.claim_setup
        def claim(owner,*args):
            clock[0]+=1  # Committed session is newer than initialization.
            return original(owner,*args)
        with tempfile.TemporaryDirectory() as folder, \
             patch.dict(os.environ,LIC_DEPLOYMENT_PROFILE='self-hosted'), \
             patch('research_query_preview.time.time',lambda:clock[0]), \
             patch.object(OwnerAuthRepository,'_now',lambda self,now:clock[0]), \
             patch.object(OwnerAuthRepository,'claim_setup',claim):
            path=Path(folder)/'owner.sqlite3'
            create_preview_app(None,path,'http://127.0.0.1:9123',[])
            self.assertEqual(OwnerAuthRepository(path).access_state(clock[0])['mode'],'local')

if __name__=='__main__': unittest.main()
