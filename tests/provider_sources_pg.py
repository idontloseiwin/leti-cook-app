"""Disposable PG17: one-off acquisition stores nothing but quota attempts.

Runs only through the native runner on a fresh disposable cluster. The provider is faked at
requests.get with synthetic fixture pages; the gateway, the quota attempt recorder (direct mode)
and the daily-cap ledger read are real.
"""
import contextlib
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo
from datetime import datetime, timezone

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'application'), str(Path(__file__).resolve().parent)]
import research_query_pg as native  # noqa: E402
import requests  # noqa: E402
import provider_runtime  # noqa: E402
import youtube_client  # noqa: E402
from db.base.read_context import http_method_scope, http_read_scope  # noqa: E402
from db.quota.youtube_quota import YouTubeQuotaRepository  # noqa: E402
from provider_sources.daily_cap import CAPPED_BUCKET, PHASE  # noqa: E402
from provider_sources.service import AcquisitionService, ServiceRefused  # noqa: E402
from youtube_quota import YouTubeQuotaAttemptRecorder  # noqa: E402
from provider_sources_support import load  # noqa: E402

FIXTURE = load('youtube.playlists.list', 'full_two_pages')
PROJECT = 'native-acquisition'


class Response(requests.Response):
    def __init__(self, body):
        super().__init__()
        self.status_code = 200
        self.headers['Date'] = 'Thu, 29 Oct 2026 10:00:00 GMT'
        self._raw = json.dumps(body).encode('utf-8')

    def iter_content(self, chunk_size):
        yield self._raw

    def close(self):
        pass


class AcquisitionPostgres(unittest.TestCase):
    setUpClass = classmethod(native.NativeQueryTests.setUpClass.__func__)
    tearDownClass = classmethod(native.NativeQueryTests.tearDownClass.__func__)
    refresh = classmethod(native.NativeQueryTests.refresh.__func__)

    def snapshot(self):
        tables = self.admin.execute("""SELECT table_schema, table_name FROM information_schema.tables
            WHERE table_type='BASE TABLE' AND table_schema IN ('public','operator_private','readable')
            ORDER BY 1, 2""").fetchall()
        result = {}
        for schema, table in tables:
            row = self.admin.execute(f'SELECT count(*), md5(coalesce(string_agg(t::text, chr(10) ORDER BY t::text), \'\')) '
                                     f'FROM "{schema}"."{table}" t').fetchone()
            result[f'{schema}.{table}'] = tuple(row)
        return result

    def acquisition_service(self, pages):
        repository = YouTubeQuotaRepository(self.db, project_id=PROJECT)
        recorder = YouTubeQuotaAttemptRecorder(self.db, project_id=PROJECT)   # direct writes, no journal
        queue = [Response(item['body']) for item in pages]
        stack = contextlib.ExitStack()
        stack.enter_context(patch.object(youtube_client.requests, 'get', lambda url, **kw: queue.pop(0)))
        stack.enter_context(patch('provider_runtime.admit_request', side_effect=lambda **kw: contextlib.nullcontext(
            provider_runtime._UnregulatedRequest(kw.get('cancel')))))
        stack.enter_context(patch('hot_runtime.require_hot_runtime_ready', lambda **kw: None))
        stack.enter_context(patch.object(youtube_client.time_trust, 'record_date'))
        self.addCleanup(stack.close)
        return AcquisitionService(attempt_recorder=recorder, ledger_reader=lambda day, quota_bucket=CAPPED_BUCKET: repository.list_phase_attempts(
            day, phase=PHASE, quota_bucket=quota_bucket)), repository

    def execute(self, service, fixture=FIXTURE):
        preview = service.preview(json.dumps(fixture['request']).encode())
        body = json.dumps({'version': 1, 'request': fixture['request'], 'plan_fingerprint': preview['plan_fingerprint'],
                           'confirmed_cost': preview['worst_cost']}).encode()
        # As the HTTP route runs it: a POST request scope; the ledger read uses the reader role.
        with http_read_scope(), http_method_scope('POST'):
            return service.execute(body, cancelled=threading.Event())

    def test_01_acquisition_changes_no_table_but_the_quota_attempts(self):
        before = self.snapshot()
        service, repository = self.acquisition_service(FIXTURE['responses'])
        result = self.execute(service)
        self.assertEqual((result['completeness'], result['cost'], len(result['rows'])), ('complete', {'general': 2}, 3))
        after = self.snapshot()
        changed = sorted(name for name in set(before) | set(after) if before.get(name) != after.get(name))
        self.assertEqual(changed, ['public.youtube_api_request_attempts'])
        rows = self.admin.execute("""SELECT phase, endpoint, quota_bucket, quota_units, status, http_status
            FROM public.youtube_api_request_attempts WHERE project_id=%s ORDER BY started_at""", (PROJECT,)).fetchall()
        self.assertEqual(rows, [('explicit_acquisition', 'playlists', 'general', 1, 'success', 200)] * 2)
        text = json.dumps([list(map(str, row)) for row in self.admin.execute(
            'SELECT * FROM public.youtube_api_request_attempts WHERE project_id=%s', (PROJECT,)).fetchall()])
        for marker in ('MARKER', 'PLsynthetic', 'UCAAAA', 'TOKEN'):
            self.assertNotIn(marker, text)
        print('acquisition changed only', changed, flush=True)

    def test_02_daily_cap_reads_the_ledger_and_refuses_before_sending(self):
        service, repository = self.acquisition_service(FIXTURE['responses'])
        day = datetime.now(timezone.utc).astimezone(ZoneInfo('America/Los_Angeles')).date().isoformat()
        rows, truncated = repository.list_phase_attempts(day, phase=PHASE, quota_bucket=CAPPED_BUCKET)
        self.assertFalse(truncated)
        self.assertEqual(sum(units for _, units in rows), 2)            # the two attempts of test 01
        self.assertEqual(service.status()['daily_cap']['used_units'], 2)
        self.admin.execute("""INSERT INTO public.youtube_api_request_attempts
            (project_id, quota_bucket, quota_day, phase, endpoint, quota_units, status, started_at, finished_at)
            VALUES (%s,'general',%s::date,'explicit_acquisition','playlists',497,'success',now(),now())""", (PROJECT, day))
        # Another phase or bucket never counts towards the cap.
        self.admin.execute("""INSERT INTO public.youtube_api_request_attempts
            (project_id, quota_bucket, quota_day, phase, endpoint, quota_units, status, started_at, finished_at)
            VALUES (%s,'general',%s::date,'video_metadata','videos',900,'success',now(),now())""", (PROJECT, day))
        reading = service.status()['daily_cap']
        self.assertEqual((reading['used_units'], reading['remaining_units']), (499, 1))
        before = self.admin.execute('SELECT count(*) FROM public.youtube_api_request_attempts').fetchone()[0]
        with self.assertRaises(ServiceRefused) as refused:
            self.execute(service)
        self.assertEqual((refused.exception.code, refused.exception.status), ('daily_cap_reached', 429))
        self.assertEqual(self.admin.execute('SELECT count(*) FROM public.youtube_api_request_attempts').fetchone()[0], before)

    def test_03_bounded_read_fails_closed_when_truncated(self):
        repository = YouTubeQuotaRepository(self.db, project_id=PROJECT)
        day = datetime.now(timezone.utc).astimezone(ZoneInfo('America/Los_Angeles')).date().isoformat()
        rows, truncated = repository.list_phase_attempts(day, phase=PHASE, quota_bucket=CAPPED_BUCKET, max_rows=1)
        self.assertEqual((len(rows), truncated), (1, True))
        with self.assertRaises(ValueError):
            repository.list_phase_attempts(day, phase='not a label', quota_bucket=CAPPED_BUCKET)


    def test_04_search_spends_and_caps_its_own_bucket(self):
        search = load('youtube.search.list', 'mixed_two_pages')
        service, repository = self.acquisition_service(search['responses'])
        general_before = service.status()['daily_cap']['used_units']
        result = self.execute(service, search)
        self.assertEqual((result['completeness'], result['cost'], len(result['rows'])), ('complete', {'search_queries': 2}, 4))
        rows = self.admin.execute("""SELECT phase, endpoint, quota_bucket, quota_units, status, http_status
            FROM public.youtube_api_request_attempts WHERE project_id=%s AND quota_bucket='search_queries'
            ORDER BY started_at""", (PROJECT,)).fetchall()
        self.assertEqual(rows, [('explicit_acquisition', 'search', 'search_queries', 1, 'success', 200)] * 2)
        status = service.status()
        self.assertEqual((status['daily_cap']['used_units'], status['daily_caps']['search_queries']['used_units']),
                         (general_before, 2))      # a search never counts against the general cap
        day = datetime.now(timezone.utc).astimezone(ZoneInfo('America/Los_Angeles')).date().isoformat()
        self.admin.execute("""INSERT INTO public.youtube_api_request_attempts
            (project_id, quota_bucket, quota_day, phase, endpoint, quota_units, status, started_at, finished_at)
            VALUES (%s,'search_queries',%s::date,'explicit_acquisition','search',97,'success',now(),now())""", (PROJECT, day))
        before = self.admin.execute('SELECT count(*) FROM public.youtube_api_request_attempts').fetchone()[0]
        with self.assertRaises(ServiceRefused) as refused:
            self.execute(service, search)                  # worst case 2 > 1 remaining
        self.assertEqual((refused.exception.code, refused.exception.status, refused.exception.subject),
                         ('daily_cap_reached', 429, 'search_queries'))
        self.assertEqual(self.admin.execute('SELECT count(*) FROM public.youtube_api_request_attempts').fetchone()[0], before)
        print('search cap counted', status['daily_caps']['search_queries']['used_units'], flush=True)


if __name__ == '__main__':
    unittest.main()
