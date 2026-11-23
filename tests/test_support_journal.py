"""Private bounded evidence; domain state and browser reports remain separate."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
import support_journal as journal
from collection.provider_steps import ProviderStep
import pipeline_trace

VIDEO = 'd3boot00001'
CHANNEL = 'UC' + 'a' * 22


class JournalSetup(unittest.TestCase):
    def setUp(self):
        journal.close()
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.addCleanup(journal.close)
        self.path = Path(self.folder.name) / 'discovery-support.jsonl'
        journal.initialize(environ={}, path=self.path, key=b'x' * 32)

    def events(self):
        self.assertTrue(journal._emitter.flush(1))
        return journal.snapshot()['events']

    def result(self):
        return {'context': {'valid_before': (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()},
                'rows': [{'id': VIDEO, 'cells': {'video.title': {'value': 'PRIVATE TITLE'}}}]}

class SupportJournalTests(JournalSetup):
    def test_closed_envelope_stable_refs_and_no_source_payload(self):
        journal.event('signal', 'accepted', video_id=VIDEO, channel_id=CHANNEL,
                      work_id='17', origin='websub', reason='websub')
        journal.rows('claim', 'accepted', [{'video_id': VIDEO, 'channel_id': CHANNEL,
                     'work_id': '17', 'origin': 'websub', 'title': 'PRIVATE TITLE'}])
        journal.event('arbitrary', 'accepted', video_id=VIDEO)
        journal.event('wait', 'waiting', video_id=VIDEO, reason='private_word')
        events = self.events()
        self.assertEqual(len(events), 3)
        self.assertEqual(events[0]['subject_ref'], events[1]['subject_ref'])
        self.assertEqual(events[0]['work_ref'], events[1]['work_ref'])
        self.assertNotIn('reason', events[-1])
        encoded = self.path.read_text()
        for private in (VIDEO, CHANNEL, 'PRIVATE TITLE', 'private_word'):
            self.assertNotIn(private, encoded)

    def test_actual_provider_context_and_writer_confirmation_share_reference(self):
        def fetch(ids, reason):
            pipeline_trace.event('http_started', 'started', request_attempt_id='a'*32)
            pipeline_trace.event('http_finished', 'received', http_status=200, request_attempt_id='a'*32)
            return ids
        result = ProviderStep(fetch, (VIDEO,), 'discovery_hydration').execute()
        self.assertEqual(result.value, [VIDEO])
        pipeline_trace.event('video_write', 'unknown', video_id=VIDEO)
        pipeline_trace.event('video_write', 'confirmed', video_id=VIDEO)
        events = self.events()
        self.assertEqual([(e['operation'], e['status']) for e in events],
                         [('provider', 'started'), ('provider', 'received'), ('writer', 'unknown'), ('writer', 'confirmed')])
        self.assertEqual(len({e['subject_ref'] for e in events}), 1)
        self.assertEqual(events[1]['request_attempt_id'], 'a'*32)

    def test_head_provider_without_known_video_ids_keeps_channel_correlation(self):
        from collection.inventory_steps import InventoryPageStep
        def fetch(playlist, **kwargs):
            pipeline_trace.event('http_started', 'started', request_attempt_id='a'*32)
            pipeline_trace.event('http_finished', 'received', http_status=200, request_attempt_id='a'*32)
            return {}
        now = lambda: datetime.now(timezone.utc)
        step = InventoryPageStep(fetch, 'UUSH' + CHANNEL[2:], None, 'inventory_head_short', now, now(),
                                 dict(channel_id=CHANNEL, reason='head'))
        self.assertIsNone(step.execute().error)
        events = self.events()
        self.assertEqual([(e['operation'], e['status']) for e in events], [('provider', 'started'), ('provider', 'received')])
        self.assertEqual({e['channel_ref'] for e in events}, {journal.reference('channel', CHANNEL)})
        self.assertTrue(all('subject_ref' not in event for event in events))

    def test_count_signal_requires_existing_owner_confirmation(self):
        pipeline_trace.event('channel_count_change', 'unconfirmed', channel_id=CHANNEL)
        pipeline_trace.event('channel_metadata_write', 'confirmed', channel_id=CHANNEL)
        self.assertEqual(self.events(), [])
        pipeline_trace.event('channel_count_change', 'confirmed', channel_id=CHANNEL)
        event, = self.events()
        self.assertEqual((event['operation'], event['status'], event['reason']), ('signal', 'accepted', 'count'))
        self.assertEqual(event['channel_ref'], journal.reference('channel', CHANNEL))

    def test_browser_reports_require_exact_delivered_receipt_are_deduplicated_and_bounded(self):
        receipt = journal.api_result(self.result())
        report = dict(read_id=receipt, status='rendered', indices=[0])
        journal.browser_report(report)
        self.assertEqual(len(self.events()), 1)  # Prepared alone is not delivery.
        journal.api_delivery(receipt, 'delivered')
        for _ in range(100):
            journal.browser_report(report)
            journal.browser_report(dict(report, indices=[49]))
            journal.browser_report(dict(report, read_id='b'*32))
            journal.browser_report(dict(report, status='confirmed'))
            journal.browser_report(dict(report, indices=[True]))
        events = self.events()
        self.assertEqual([e['status'] for e in events], ['prepared', 'delivered', 'rendered'])
        self.assertEqual(events[-1]['reason'], 'client_report')
        self.assertEqual(len({e['subject_ref'] for e in events}), 1)
        for _ in range(journal.RECEIPT_LIMIT + 10):
            journal.api_result(self.result())
        self.assertEqual(len(journal._receipts), journal.RECEIPT_LIMIT)
        self.assertNotIn(receipt, journal._receipts)

    def test_expired_and_unknown_receipts_do_not_create_evidence(self):
        receipt = journal.api_result(self.result())
        journal.api_delivery(receipt, 'delivered')
        subjects, _, seen = journal._receipts[receipt]
        journal._receipts[receipt] = subjects, 0, seen
        journal.browser_report(dict(read_id=receipt, status='received', indices=[0]))
        self.assertEqual(len(self.events()), 2)
        self.assertNotIn(receipt, journal._receipts)

    def test_rotation_and_sink_failure_are_bounded_and_fail_soft(self):
        journal._emitter.journal.max_bytes = 2048
        for _ in range(30):
            journal.event('wait', 'waiting', video_id=VIDEO, reason='budget')
        self.events()
        self.assertLessEqual(self.path.stat().st_size, 2048)
        self.assertEqual({p.name for p in self.path.parent.iterdir()}, {self.path.name, self.path.name+'.lock'})
        with patch.object(journal._emitter.journal, 'append_batch', side_effect=OSError('private disk error')):
            journal.event('writer', 'unknown', video_id=VIDEO)
            self.events()
        self.assertGreater(journal.snapshot()['metrics']['sink_errors'], 0)
        self.assertGreater(journal.snapshot()['metrics']['dropped'], 0)

    def test_explicit_off_does_not_open_files_or_old_telemetry(self):
        journal.close()
        with patch.object(journal, '_private_file', side_effect=AssertionError('I/O forbidden')):
            journal.initialize(environ={'LIC_SUPPORT_JOURNAL_ENABLED': 'false'}, path=self.path)
            journal.event('signal', 'accepted', video_id=VIDEO)
        self.assertEqual(journal.snapshot()['status'], 'off')
        import telemetry_sink
        self.assertTrue(telemetry_sink.PIPELINE_TELEMETRY_OFF)

    def test_key_survives_process_restart_and_permissions_fail_closed(self):
        journal.close()
        journal.initialize(environ={}, path=self.path)
        reference = journal.reference('video', VIDEO)
        journal.close()
        journal.initialize(environ={}, path=self.path)
        self.assertEqual(journal.reference('video', VIDEO), reference)
        if os.name != 'nt':
            journal.close()
            self.path.with_suffix('.key').chmod(0o644)
            journal.initialize(environ={}, path=self.path)
            self.assertEqual(journal.snapshot()['status'], 'unavailable')


class JournalHttpTests(JournalSetup):
    def test_asgi_delivery_and_failure_keep_distinct_evidence(self):
        receipt = journal.api_result(self.result())
        async def app(scope, receive, send):
            await send({'type': 'http.response.start', 'status': 200,
                        'headers': [(journal.HEADER.lower().encode(), receipt.encode())]})
            await send({'type': 'http.response.body', 'body': b'{}'})
        async def send(message): pass
        async def receive(): return {}
        asyncio.run(journal.observe_http(app, {}, receive, send))
        self.assertEqual([e['status'] for e in self.events()], ['prepared', 'delivered'])
        receipt = journal.api_result(self.result())
        async def failed_send(message):
            if message['type'] == 'http.response.body':
                raise RuntimeError('delivery_failed')
        with self.assertRaisesRegex(RuntimeError, 'delivery_failed'):
            asyncio.run(journal.observe_http(app, {}, receive, failed_send))
        self.assertEqual([e['status'] for e in self.events()], ['prepared', 'delivered', 'prepared', 'unknown'])

    def test_routes_require_owner_and_bound_untrusted_body(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from support_journal_routes import router
        app = FastAPI()
        access = {'allowed': False}
        @app.middleware('http')
        async def owner(request, call_next):
            if access['allowed']:
                request.state.installation_access = True
                request.state.owner_access_recheck = lambda: 1
            return await call_next(request)
        app.include_router(router)
        client = TestClient(app)
        for path in ('/diagnostics/discovery-journal', '/diagnostics/discovery-journal/browser'):
            response = client.get(path) if not path.endswith('/browser') else client.post(path, json={})
            self.assertEqual(response.status_code, 401)
        access['allowed'] = True
        self.assertEqual(client.post('/diagnostics/discovery-journal/browser', content=b'x'*2049).status_code, 413)
        self.assertEqual(client.post('/diagnostics/discovery-journal/browser', json={}).status_code, 204)
        response = client.get('/diagnostics/discovery-journal')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['cache-control'], 'no-store')

    def test_actual_owner_middleware_requires_same_origin_for_browser_report(self):
        import time
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from db.self_hosted_auth import OwnerAuthRepository
        from self_hosted_auth import SelfHostedAuthMiddleware
        from support_journal_routes import router
        origin = 'http://127.0.0.1:18021'
        owner = OwnerAuthRepository.initialize(Path(self.folder.name)/'owner.sqlite3', 'dummy', int(time.time()))
        owner.issue_setup('setup', int(time.time()))
        self.assertEqual(owner.claim_setup(lambda value: value == 'setup', 'session', int(time.time())), 'ok')
        self.assertEqual(owner.complete_setup('session', 'local', None, lambda: None, None, int(time.time())), 'ok')
        app = FastAPI()
        app.include_router(router)
        app.add_middleware(SelfHostedAuthMiddleware, repository=owner, origin=origin)
        client = TestClient(app, base_url=origin)
        route = '/diagnostics/discovery-journal/browser'
        self.assertEqual(client.post(route, json={}).status_code, 403)
        self.assertEqual(client.post(route, json={}, headers={'Origin': 'https://foreign.example'}).status_code, 403)
        self.assertEqual(client.post(route, json={}, headers={'Origin': origin}).status_code, 204)
        self.assertEqual(client.get('/diagnostics/discovery-journal').status_code, 200)


if __name__ == '__main__': unittest.main()
