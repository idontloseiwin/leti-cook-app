"""Offline checks of the retention benchmark harness. No database, no network."""
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
import io
from pathlib import Path
import re
import sys
import unittest
import unittest.mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT / 'application'))
import retention_benchmark as bench  # noqa: E402
import check_direct_pg_baseline as safety  # noqa: E402

SOURCE = (ROOT / 'tools/retention_benchmark.py').read_text(encoding='utf-8')
RUNNER = (ROOT / 'tools/retention_benchmark.sh').read_text(encoding='utf-8')


class Repository:
    def cleanup_page(self, target, **kw):
        return target


class Cycle:
    def __init__(self, repository, pages):
        self.repository, self.pages = repository, list(pages)

    def run_slice(self, **kw):
        page = self.pages.pop(0)
        self.repository.cleanup_page(page['target'])
        return {'status': 'scan_exhausted' if not self.pages else 'in_progress', 'pages': [page]}


def page(target, **counts):
    return {'target': target, 'scanned': 0, 'deleted': 0, 'scrubbed': 0, 'deferred': 0, **counts}


class RetentionBenchmarkTests(unittest.TestCase):
    def test_a_pass_adds_up_pages_per_target_and_ends_when_the_scan_is_exhausted(self):
        pages = [page('videos', scanned=100, scrubbed=3), page('videos', scanned=40), page('sync_runs', scanned=7, deleted=7)]
        result = bench.full_pass(Repository(), cycle_factory=lambda repository: Cycle(repository, pages))
        self.assertEqual(result['steps'], 3)
        videos = dict(result['targets']['videos'])
        self.assertGreaterEqual(videos.pop('seconds'), 0)
        self.assertEqual(videos, {'pages': 2, 'scanned': 140, 'deleted': 0, 'scrubbed': 3, 'deferred': 0})
        self.assertEqual(result['targets']['sync_runs']['deleted'], 7)
        self.assertGreaterEqual(result['runtime_estimate_seconds'], 0.3)  # three paced steps

    def test_each_page_is_timed_under_its_own_target(self):
        clock = iter(range(100))
        with unittest.mock.patch.object(bench.time, 'perf_counter', lambda: next(clock)):
            result = bench.full_pass(Repository(), cycle_factory=lambda repository: Cycle(
                repository, [page('videos'), page('sync_runs')]))
        # Each page call spans one tick of the fake clock.
        self.assertEqual((result['targets']['videos']['seconds'], result['targets']['sync_runs']['seconds']), (1, 1))

    def test_a_failed_page_or_an_endless_pass_stops_the_benchmark(self):
        failing = type('Failing', (), {'run_slice': lambda self, **kw: {'status': 'error', 'failed_target': 'videos'}})
        with self.assertRaisesRegex(RuntimeError, 'cleanup_failed:videos'):
            bench.full_pass(object(), cycle_factory=lambda repository: failing())
        endless = type('Endless', (), {'run_slice': lambda self, **kw: {'status': 'in_progress', 'pages': []}})
        with self.assertRaisesRegex(RuntimeError, 'cleanup_pass_too_long'):
            bench.full_pass(object(), cycle_factory=lambda repository: endless(), limit=5)

    def test_only_a_disposable_database_is_accepted(self):
        environ = {'LIC_DIRECT_PG_PROBE_ADMIN_DSN': 'host=127.0.0.1 port=5432 dbname=let_it_cook user=postgres password=x',
                   'LIC_DIRECT_PG_PROBE_DSN': 'host=127.0.0.1 port=5432 dbname=let_it_cook user=authenticator password=y'}
        args = type('Args', (), {'scenario': 'steady', 'downtime_days': 10})()
        for database_name in ('leti-cook', 'let_it_cook'):
            named={key:value.replace('dbname=let_it_cook', 'dbname='+database_name) for key,value in environ.items()}
            with self.subTest(database=database_name), self.assertRaises(safety.BaselineFailure):
                bench.run(args, environ=named)

    def test_seed_statements_bind_every_value(self):
        for name, statement in {**bench.SEED_SQL, **bench.SHARED_SQL}.items():
            names = set(re.findall(r'%\((\w+)\)s', statement))
            self.assertTrue(names, name)
            given = bench.seed_parameters(bench.BIG_CHANNEL, 'BB', 10, at=None, offset=None, snapshots=6) \
                if name in bench.SEED_SQL else {'at', 'offset', 'history', *bench.QUOTA_SEED}
            self.assertLessEqual(names, set(given), name)
            self.assertNotRegex(statement, r"'UC[A-Za-z0-9_-]{22}'|'20\d\d-", name)  # no literal channels or clocks

    def test_seeded_api_records_use_labels_the_collector_records(self):
        from operational_copy_policy import QUOTA_LABELS
        self.assertIn(bench.QUOTA_SEED['bucket'], QUOTA_LABELS['quota_bucket'])
        self.assertIn(bench.QUOTA_SEED['phase'], QUOTA_LABELS['phase'])
        self.assertIn(bench.QUOTA_SEED['endpoint'], QUOTA_LABELS['endpoint'])

    def test_seeded_video_ids_are_eleven_characters_and_distinct_per_channel(self):
        prefixes = [prefix for _, prefix, _ in bench.channels(20_000, 800)]
        self.assertEqual(len(set(prefixes)), 5)
        self.assertTrue(all(len(prefix) + 9 == 11 for prefix in prefixes))

    def test_reference_is_fresh_in_steady_run_and_expires_after_default_downtime(self):
        import json
        from youtube_category_reference import validate_reference
        at = datetime(2026, 10, 9, tzinfo=timezone.utc)
        current = bench.category_reference_parameters(at, timedelta(0))
        old = bench.category_reference_parameters(at, timedelta(days=10))
        self.assertEqual(current['received'], at - timedelta(days=21))
        self.assertEqual(old['received'], at - timedelta(days=31))
        validate_reference(json.loads(current['document']), now=at)
        with self.assertRaises(ValueError):
            validate_reference(json.loads(old['document']), now=at)
        self.assertIn('public.youtube_category_reference', bench.COUNTED)

    def test_sizes_are_bounded(self):
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            bench.main(['--videos', '0'])

    def test_no_provider_requests_or_processes(self):
        self.assertNotRegex(SOURCE, r'requests\.|youtube_client|subprocess|urllib')

    def test_runner_uses_disposable_unpublished_containers(self):
        self.assertIn('lic_direct_pg_probe_bench', RUNNER)
        self.assertIn('--network none', RUNNER)
        self.assertIn('schema_ci_resources_init', RUNNER)
        self.assertNotIn('--publish', RUNNER)
        self.assertNotRegex(RUNNER, r'\s-p\s')


if __name__ == '__main__':
    unittest.main()
