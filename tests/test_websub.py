"""WebSub primitives, Hub management and the public ingress (offline)."""
import hashlib
import hmac
import io
import os
import stat
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from contextlib import contextmanager
from datetime import datetime, timezone
from email.message import Message
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))

import websub
from db.websub import classify, failure_wait, Admission
from websub_service import WebSubService, WebSubKey

CHANNEL = 'UC' + 'a' * 22
OTHER = 'UC' + 'b' * 22
KEY = bytes(range(32))
FEED = """<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns="http://www.w3.org/2005/Atom">
 <link rel="hub" href="https://pubsubhubbub.appspot.com"/>
 <link rel="self" href="https://www.youtube.com/xml/feeds/videos.xml?channel_id={ch}"/>
 <entry><id>yt:video:{vid}</id><yt:videoId>{vid}</yt:videoId><yt:channelId>{ch}</yt:channelId>
  <title>Never stored</title><published>2026-10-29T10:00:00+00:00</published><updated>2026-10-29T10:00:05.123+00:00</updated></entry>
</feed>"""
DELETED = """<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns:at="http://purl.org/atompub/tombstones/1.0" xmlns="http://www.w3.org/2005/Atom">
 <at:deleted-entry ref="yt:video:{vid}" when="2026-10-29T11:00:00+00:00">
  <link href="https://www.youtube.com/watch?v={vid}"/><at:by><name>x</name><uri>https://www.youtube.com/channel/{ch}</uri></at:by>
 </at:deleted-entry>
</feed>"""


class PrimitiveTests(unittest.TestCase):
    def test_callback_base(self):
        self.assertIsNone(websub.callback_base(''))
        self.assertIsNone(websub.callback_base(None))
        self.assertEqual(websub.callback_base(' https://hooks.example.com/leti-cook '), 'https://hooks.example.com/leti-cook')
        for bad in ('http://hooks.example.com/x', 'https://hooks.example.com/x/', 'https://user@hooks.example.com',
                    'https://hooks.example.com/x?a=1', 'https://localhost/x', 'https://hooks.example.com/a b',
                    'https://' + 'a' * 200 + '.com'):
            with self.subTest(bad=bad), self.assertRaisesRegex(websub.WebSubError, 'websub_callback_url_invalid'):
                websub.callback_base(bad)
        self.assertEqual(websub.public_host('https://Hooks.Example.com:8443/x'), 'hooks.example.com:8443')

    def test_capabilities_are_derived_per_channel_and_generation(self):
        token = websub.capability_token(KEY, CHANNEL, 1)
        self.assertRegex(token, r'^[A-Za-z0-9_-]{43}$')
        self.assertEqual(token, websub.capability_token(KEY, CHANNEL, 1))
        self.assertNotEqual(token, websub.capability_token(KEY, CHANNEL, 2))
        self.assertNotEqual(token, websub.capability_token(KEY, OTHER, 1))
        self.assertNotEqual(websub.signing_secret(KEY, CHANNEL, 1), token)
        url = websub.callback_url('https://hooks.example.com/lc', KEY, CHANNEL, 1)
        self.assertEqual(url, f'https://hooks.example.com/lc/websub/{CHANNEL}/{token}')
        self.assertEqual(websub.parse_path(f'/websub/{CHANNEL}/{token}'), (CHANNEL, token))
        for bad in (f'/websub/{CHANNEL}/{token}/', f'/websub/{CHANNEL}', f'/websub/UCx/{token}', f'/x/websub/{CHANNEL}/{token}'):
            self.assertIsNone(websub.parse_path(bad), bad)
        self.assertTrue(websub.token_matches(KEY, CHANNEL, 1, token))
        self.assertFalse(websub.token_matches(KEY, CHANNEL, 2, token))
        self.assertEqual(websub.topic_url(CHANNEL), 'https://www.youtube.com/xml/feeds/videos.xml?channel_id=' + CHANNEL)

    def test_key_file_created_once_private(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'websub' / 'websub.key'
            first = websub.load_or_create_key(path)
            self.assertEqual(len(first), 32)
            self.assertEqual(websub.load_or_create_key(path), first)
            self.assertEqual(WebSubKey(path)(), first)
            if os.name != 'nt':
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            path.write_bytes(b'short')
            with self.assertRaisesRegex(websub.WebSubError, 'websub_key_invalid'):
                websub.load_or_create_key(path)

    def test_signature(self):
        body, secret = b'<feed/>', websub.signing_secret(KEY, CHANNEL, 1)
        sha1 = 'sha1=' + hmac.new(secret.encode(), body, hashlib.sha1).hexdigest()
        sha256 = 'sha256=' + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        self.assertTrue(websub.verify_signature(body, sha1, secret))
        self.assertTrue(websub.verify_signature(body, sha256.upper().replace('SHA256', 'sha256'), secret))
        self.assertFalse(websub.verify_signature(body, sha1, websub.signing_secret(KEY, CHANNEL, 2)))
        self.assertFalse(websub.verify_signature(body, None, secret))
        self.assertFalse(websub.verify_signature(b'', sha1, secret))
        self.assertFalse(websub.verify_signature(body, 'md5=abc', secret))

    def test_parse_entry_and_tombstone(self):
        [entry] = websub.parse_notification(FEED.format(ch=CHANNEL, vid='dQw4w9WgXcQ').encode())
        self.assertEqual((entry.channel_id, entry.video_id, entry.kind), (CHANNEL, 'dQw4w9WgXcQ', 'entry'))
        self.assertEqual(entry.published_at, datetime(2026, 10, 29, 10, 0, tzinfo=timezone.utc))
        self.assertEqual(entry.updated_at.microsecond, 123000)
        self.assertRegex(entry.dedupe_key, '^[0-9a-f]{64}$')
        [deleted] = websub.parse_notification(DELETED.format(ch=CHANNEL, vid='dQw4w9WgXcQ').encode())
        self.assertEqual((deleted.kind, deleted.channel_id, deleted.published_at), ('deleted', CHANNEL, None))
        self.assertNotEqual(entry.dedupe_key, deleted.dedupe_key)

    def test_parse_refusals(self):
        cases = {
            b'': 'websub_size_invalid',
            b'x' * (websub.MAX_NOTIFICATION_BYTES + 1): 'websub_size_invalid',
            b'<!DOCTYPE x [<!ENTITY a "b">]><feed xmlns="http://www.w3.org/2005/Atom"/>': 'websub_xml_forbidden',
            '<!DOCTYPE x><feed/>'.encode('utf-16'): 'websub_xml_forbidden',
            b'<rss/>': 'websub_root_invalid',
            b'<feed xmlns="http://www.w3.org/2005/Atom"/>': 'websub_feed_empty',
            b'not xml': 'websub_xml_invalid',
            FEED.format(ch=CHANNEL, vid='short').encode(): 'websub_entry_invalid',
            FEED.format(ch='UCbad', vid='dQw4w9WgXcQ').encode(): 'websub_entry_invalid',
        }
        for body, code in cases.items():
            with self.subTest(code=code), self.assertRaisesRegex(websub.WebSubError, code):
                websub.parse_notification(body)
        many = FEED.format(ch=CHANNEL, vid='dQw4w9WgXcQ').replace('</feed>', '').encode()
        entry = many.split(b'<entry>')[1].split(b'</entry>')[0]
        many += (b'<entry>' + entry + b'</entry>') * 100 + b'</feed>'
        with self.assertRaisesRegex(websub.WebSubError, 'websub_feed_too_large'):
            websub.parse_notification(many)


class _Response:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class HubRequestTests(unittest.TestCase):
    def request(self, behaviour, mode='subscribe'):
        sent = []

        def opener(request, timeout):
            sent.append((request, timeout))
            return behaviour()
        result = websub.hub_request(mode, CHANNEL, 'https://h.example.com/websub/x/y', secret='s3', opener=opener,
                                    clock=iter([0.0, 1.5]).__next__)
        return result, sent

    def test_subscribe_form_and_accept(self):
        result, [(request, timeout)] = self.request(lambda: _Response(202))
        form = dict(urllib.parse.parse_qsl(request.data.decode()))
        self.assertEqual(form, {'hub.mode': 'subscribe', 'hub.topic': websub.topic_url(CHANNEL),
                                'hub.callback': 'https://h.example.com/websub/x/y', 'hub.verify': 'async',
                                'hub.secret': 's3', 'hub.lease_seconds': '864000'})
        self.assertEqual(timeout, 120)
        self.assertTrue(result.accepted)
        self.assertEqual(result.seconds, 1.5)
        result, [(request, _)] = self.request(lambda: _Response(204), mode='unsubscribe')
        self.assertNotIn('hub.secret', dict(urllib.parse.parse_qsl(request.data.decode())))

    def test_refusal_retry_after_and_no_answer(self):
        def throttled():
            headers = Message()
            headers['Retry-After'] = '300'
            raise urllib.error.HTTPError('https://hub', 429, 'Throttled', headers, io.BytesIO(b''))
        result, _ = self.request(throttled)
        self.assertEqual((result.outcome, result.http_status, result.retry_after, result.ambiguous), ('http_429', 429, 300, False))

        def timeout():
            raise urllib.error.URLError(TimeoutError('timed out'))
        result, _ = self.request(timeout)
        self.assertEqual((result.outcome, result.ambiguous, result.accepted), ('timeout', True, False))

        def network():
            raise urllib.error.URLError(OSError('unreachable'))
        self.assertEqual(self.request(network)[0].outcome, 'network')


class DecisionTests(unittest.TestCase):
    def event(self, event_id, kind='entry', video='v' * 11, channel=CHANNEL):
        return dict(event_id=event_id, kind=kind, video_id=video, channel_id=channel)

    def test_classify(self):
        events = [self.event(1, video='new00000001'), self.event(2, video='new00000001'),
                  self.event(3, video='new00000002'), self.event(4, video='known000001'),
                  self.event(5, 'deleted', 'known000001'), self.event(6, 'deleted', 'gone0000001'),
                  self.event(7, video='other000001', channel=OTHER), self.event(8, video='moved000001')]
        known = {'known000001': CHANNEL, 'moved000001': OTHER}
        discovery, refresh, deltas, outcomes = classify(events, known, {'new00000002'}, {CHANNEL})
        self.assertEqual(discovery, {CHANNEL})
        self.assertEqual(refresh, {'known000001'})
        self.assertEqual(deltas, {})  # no YouTube dates: nothing may explain a count change
        self.assertEqual(outcomes, {1: ('done', 'discovery_requested'), 2: ('done', 'already_requested'),
                                    3: ('done', 'already_requested'), 4: ('done', 'refresh_requested'),
                                    5: ('done', 'deletion_check_requested'), 6: ('skipped', 'unknown_video'),
                                    7: ('skipped', 'channel_not_eligible'), 8: ('skipped', 'channel_mismatch')})

    def test_count_tally_only_takes_moments_after_the_last_count_change(self):
        changed = datetime(2026, 10, 29, 10, 30, tzinfo=timezone.utc)
        before, after = changed.replace(minute=29), changed.replace(minute=31)
        events = [dict(self.event(1, video='new00000001'), provider_published_at=after),
                  dict(self.event(2, video='new00000002'), provider_published_at=before),
                  dict(self.event(3, 'deleted', 'known000001'), provider_updated_at=after),
                  dict(self.event(4, 'deleted', 'known000002'), provider_updated_at=before)]
        known = {'known000001': CHANNEL, 'known000002': CHANNEL}
        discovery, refresh, deltas, outcomes = classify(events, known, set(), {CHANNEL}, {CHANNEL: changed})
        # Both new videos are still looked for; only the later upload and removal count.
        self.assertEqual((discovery, refresh), ({CHANNEL}, {'known000001', 'known000002'}))
        self.assertEqual(deltas, {CHANNEL: 0})
        self.assertEqual(classify(events[:1], known, set(), {CHANNEL}, {})[2], {CHANNEL: 1})  # never changed


class HubBackoffTests(unittest.TestCase):
    def test_failure_wait(self):
        self.assertEqual(failure_wait(1, None), 600)
        self.assertEqual(failure_wait(2, None), 1200)
        self.assertEqual(failure_wait(9, None), 7200)
        self.assertEqual(failure_wait(1, 9000), 9000)


class _Manager:
    def __init__(self, rows):
        self.rows, self.settled, self.plans = list(rows), [], []

    def plan(self, base):
        self.plans.append(base)

    def clean(self):
        return 0

    def reserve(self):
        if self.rows:
            return self.rows.pop(0), Admission(True, 'send')
        return None, Admission(False, 'idle')

    def settle(self, row, result):
        self.settled.append((row, result))
        return False


class _Events:
    def __init__(self, counts=()):
        self.counts = list(counts)

    def process(self):
        return self.counts.pop(0) if self.counts else 0


class _Tx:
    """A transaction that hands out the claimed events and the control row, and records writes."""
    def __init__(self, events, control):
        self.events, self.control, self.writes = events, control, []

    def fetch_all(self, statement, parameters=(), *, max_rows=None):
        import db.websub as repository
        return list(self.events) if statement == repository._CLAIM else []

    def fetch_one(self, statement, parameters=()):
        return self.control

    def execute(self, statement, parameters=()):
        self.writes.append(statement)


class _Database:
    def __init__(self, tx):
        self.tx = tx

    @contextmanager
    def transaction(self, operation):
        yield self.tx


class DiscoveryMaintenanceTests(unittest.TestCase):
    def test_cleanup_is_independent_of_websub_and_retries(self):
        from runtime.discovery import DiscoveryMaintenance
        from unittest.mock import Mock
        clock = Mock(return_value=100.0)
        ready = Mock(side_effect=[RuntimeError(), None, None, None])
        websub_worker = Mock()
        websub_worker.step.return_value = 5
        owner = DiscoveryMaintenance(None, ready=ready, websub=websub_worker, clock=clock)
        owner.cleanup = Mock(side_effect=[RuntimeError(), None, None])
        self.assertEqual(owner.step(), 5)
        owner.cleanup.assert_not_called()
        clock.return_value += 60
        with self.assertLogs('runtime.discovery', level='WARNING'):
            self.assertEqual(owner.step(), 5)
        self.assertEqual(owner.last_cleanup_error, 'RuntimeError')
        clock.return_value += 59
        self.assertEqual(owner.step(), 1)
        self.assertEqual(owner.cleanup.call_count, 1)
        clock.return_value += 1
        self.assertEqual(owner.step(), 5)
        self.assertIsNone(owner.last_cleanup_error)
        self.assertEqual(websub_worker.step.call_count, 4)
        owner.websub = None
        clock.return_value += 3599
        self.assertEqual(owner.step(), 1)
        clock.return_value += 1
        self.assertEqual(owner.step(), 3600)
        self.assertEqual(owner.cleanup.call_count, 3)


class ServiceTests(unittest.TestCase):
    def test_one_request_per_step_with_derived_capability(self):
        row = dict(channel_id=CHANNEL, generation=3, callback_base='https://h.example.com/lc', mode='subscribe', attempts=1)
        manager, calls = _Manager([row]), []

        def request(mode, channel, callback, *, secret, hub):
            calls.append((mode, channel, callback, secret, hub))
            return websub.HubResult('accepted', 202, None, 0.1)
        service = WebSubService(manager, _Events(), base='https://h.example.com/lc', key=lambda: KEY,
                                ready=lambda: None, request=request, pace=3.0)
        self.assertEqual(service.step(), 3.0)
        self.assertEqual(calls, [('subscribe', CHANNEL, websub.callback_url('https://h.example.com/lc', KEY, CHANNEL, 3),
                                  websub.signing_secret(KEY, CHANNEL, 3), websub.HUB_URL)])
        self.assertEqual(manager.plans, ['https://h.example.com/lc'])
        self.assertEqual(len(manager.settled), 1)
        self.assertEqual(service.step(), 5.0)  # idle

    def test_not_ready_or_failure_waits_without_detail(self):
        def not_ready():
            raise RuntimeError('database password=secret')
        service = WebSubService(_Manager([]), _Events(), base='https://h', key=lambda: KEY, ready=not_ready)
        self.assertEqual(service.step(), 30.0)
        self.assertEqual(service.last_error, 'RuntimeError')

    def test_events_processed_keep_the_loop_short(self):
        service = WebSubService(_Manager([]), _Events([7]), base='https://h', key=lambda: KEY, ready=lambda: None)
        self.assertEqual(service.step(), 0.2)
        self.assertEqual(service.step(), 5.0)  # nothing handled: the idle wait

    def test_repository_keeps_no_quiet_rule(self):
        import db.websub as repository
        for name in ('websub_healthy', 'WEBSUB_QUIET_SECONDS', 'WEBSUB_HEALTHY_SQL'):
            self.assertFalse(hasattr(repository, name), name)

    def test_event_handler_only_takes_rows_the_discovery_owner_never_touched(self):
        import db.websub as repository
        for clause in ("status='pending'", "work_phase='metadata_wait'", 'claim_token IS NULL', 'paused_at IS NULL',
                       'discovery_runtime_control WHERE singleton AND NOT enabled', 'FOR UPDATE SKIP LOCKED'):
            self.assertIn(clause, repository._CLAIM)
        self.assertIn('FOR SHARE', repository._NOT_ENABLED)
        self.assertIn("work_phase='settled'", repository._DONE)
        self.assertIn("THEN 'settled' ELSE work_phase END", repository._RETRY)

    def test_activation_between_claim_and_proof_leaves_events_untouched(self):
        import db.websub as repository
        event = dict(event_id=1, channel_id=CHANNEL, video_id='v' * 11, kind='entry',
                     provider_published_at=None, provider_updated_at=None)
        for control in (None, {'enabled': True}):
            tx = _Tx([event], control)
            self.assertEqual(repository.WebSubEventRepository(_Database(tx)).process(), 0)
            self.assertEqual(tx.writes, [])

    def test_count_rule_ignores_a_decrease_and_a_missing_or_busy_subscription(self):
        import db.websub as repository
        tx = _Tx([], None)
        self.assertFalse(repository.explained_increase(tx, CHANNEL, 0))
        self.assertFalse(repository.explained_increase(tx, CHANNEL, -2))
        self.assertFalse(repository.explained_increase(tx, CHANNEL, 1))  # no row, or another transaction holds it
        self.assertEqual(tx.writes, [])
        self.assertIn('FOR UPDATE SKIP LOCKED', repository._EXPLAIN)


class _Repository:
    def __init__(self, state='verified', generation=1):
        self.row = dict(channel_id=CHANNEL, generation=generation, topic=websub.topic_url(CHANNEL), state=state)
        self.verifications, self.deliveries, self.journals = [], [], []
        self.fail = False

    def subscription(self, channel_id):
        if self.fail:
            raise RuntimeError('database down')
        return self.row if channel_id == CHANNEL else None

    def verification(self, channel_id, generation, mode, lease, *, outcome):
        self.verifications.append((channel_id, generation, mode, lease, outcome))
        return outcome

    def delivery(self, channel_id, notifications, *, size, outcome='accepted'):
        self.deliveries.append((channel_id, notifications, outcome))
        return len(notifications)

    def journal(self, kind, outcome, **values):
        self.journals.append((kind, outcome))


class IngressTests(unittest.TestCase):
    def client(self, repository, base='https://hooks.example.com/lc', wake=lambda: None):
        from starlette.applications import Starlette
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route
        from starlette.testclient import TestClient
        from web.websub import WebSubIngressMiddleware
        inner = Starlette(routes=[Route('/{path:path}', lambda request: PlainTextResponse('app'), methods=['GET', 'POST'])])
        app = WebSubIngressMiddleware(inner, base=base, key=lambda: KEY, repository=repository, wake=wake)
        return TestClient(app, base_url='http://hooks.example.com')

    def path(self, generation=1, channel=CHANNEL):
        return f'/websub/{channel}/{websub.capability_token(KEY, channel, generation)}'

    def test_public_host_reaches_only_websub(self):
        client = self.client(_Repository())
        self.assertEqual(client.get('/').status_code, 404)
        self.assertEqual(client.get('/auth').status_code, 404)
        self.assertEqual(client.put(self.path()).status_code, 405)
        local = {'host': '127.0.0.1:8002'}
        self.assertEqual(client.get('/anything', headers=local).text, 'app')
        self.assertEqual(client.get(self.path(), headers=local).status_code, 404)
        disabled = self.client(_Repository(), base=None)
        self.assertEqual(disabled.get(self.path()).status_code, 404)
        self.assertEqual(disabled.get('/x').text, 'app')

    def test_verification(self):
        repository = _Repository(state='pending')
        client = self.client(repository)
        query = {'hub.mode': 'subscribe', 'hub.topic': websub.topic_url(CHANNEL), 'hub.challenge': 'abc123',
                 'hub.lease_seconds': '864000'}
        response = client.get(self.path(), params=query)
        self.assertEqual((response.status_code, response.text), (200, 'abc123'))
        self.assertEqual(repository.verifications, [(CHANNEL, 1, 'subscribe', 864000, 'verified')])
        self.assertEqual(client.get(self.path(2), params=query).status_code, 404)  # old generation
        self.assertEqual(client.get(self.path(), params={**query, 'hub.topic': query['hub.topic'] + 'x'}).status_code, 404)
        old = {**query, 'hub.topic': 'https://www.youtube.com/feeds/videos.xml?channel_id=' + CHANNEL}
        self.assertEqual(client.get(self.path(), params=old).status_code, 404)
        self.assertEqual(client.get(self.path(), params={**query, 'hub.mode': 'unsubscribe'}).status_code, 404)
        self.assertEqual(client.get(self.path(), params={**query, 'hub.mode': 'denied'}).status_code, 200)
        self.assertEqual(repository.verifications[-1][-1], 'denied')
        repository.fail = True
        self.assertEqual(client.get(self.path(), params=query).status_code, 503)

    def signed(self, body, generation=1):
        secret = websub.signing_secret(KEY, CHANNEL, generation)
        return {'X-Hub-Signature': 'sha1=' + hmac.new(secret.encode(), body, hashlib.sha1).hexdigest(),
                'Content-Type': 'application/atom+xml'}

    def test_delivery(self):
        repository = _Repository()
        client = self.client(repository)
        body = FEED.format(ch=CHANNEL, vid='dQw4w9WgXcQ').encode()
        self.assertEqual(client.post(self.path(), content=body, headers=self.signed(body)).status_code, 204)
        [(channel, notifications, outcome)] = repository.deliveries
        self.assertEqual((channel, [n.video_id for n in notifications], outcome), (CHANNEL, ['dQw4w9WgXcQ'], 'accepted'))
        self.assertEqual(client.post(self.path(), content=body, headers=self.signed(body, 2)).status_code, 204)
        self.assertEqual(repository.journals[-1], ('delivery', 'bad_signature'))
        bad = b'<rss/>'
        self.assertEqual(client.post(self.path(), content=bad, headers=self.signed(bad)).status_code, 204)
        self.assertEqual(repository.journals[-1], ('delivery', 'root_invalid'))
        foreign = FEED.format(ch=OTHER, vid='dQw4w9WgXcQ').encode()
        client.post(self.path(), content=foreign, headers=self.signed(foreign))
        self.assertEqual(repository.deliveries[-1][1:], ([], 'channel_mismatch'))
        large = b'x' * (websub.MAX_NOTIFICATION_BYTES + 1)
        self.assertEqual(client.post(self.path(), content=large, headers=self.signed(large)).status_code, 413)
        pending = self.client(_Repository(state='pending'))
        self.assertEqual(pending.post(self.path(), content=body, headers=self.signed(body)).status_code, 404)
        self.assertEqual(len(repository.deliveries), 2)

    def test_wake_follows_commit_and_failure_does_not_retry_delivery(self):
        repository = _Repository()
        wakes = []
        def wake():
            self.assertEqual(len(repository.deliveries), 1)
            wakes.append('after_commit')
            raise OSError('wake unavailable')
        client = self.client(repository, wake=wake)
        body = FEED.format(ch=CHANNEL, vid='dQw4w9WgXcQ').encode()
        self.assertEqual(client.post(self.path(), content=body, headers=self.signed(body)).status_code, 204)
        self.assertEqual(wakes, ['after_commit'])
        repository.fail = True
        self.assertEqual(client.post(self.path(), content=body, headers=self.signed(body)).status_code, 503)
        self.assertEqual(wakes, ['after_commit'])

    def test_invalid_or_foreign_notification_does_not_wake(self):
        wakes = []
        client = self.client(_Repository(), wake=lambda: wakes.append(True))
        for channel in (CHANNEL, OTHER):
            body = FEED.format(ch=channel, vid='dQw4w9WgXcQ').encode()
            headers = self.signed(body, 2 if channel == CHANNEL else 1)
            self.assertEqual(client.post(self.path(), content=body, headers=headers).status_code, 204)
        self.assertEqual(wakes, [])


class SettingTests(unittest.TestCase):
    def test_read_websub_callback_url(self):
        from self_hosted_settings import read_websub_callback_url, SettingsError, settings_problem
        default = (ROOT / 'config/config.ini').read_bytes()
        self.assertIn(b'\nwebsub_callback_url =\n', default)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.ini'
            path.write_bytes(default)
            self.assertIsNone(read_websub_callback_url(path))
            path.write_bytes(default.replace(b'websub_callback_url =', b'websub_callback_url = https://hooks.example.com/lc'))
            self.assertEqual(read_websub_callback_url(path), 'https://hooks.example.com/lc')
            path.write_bytes(default.replace(b'websub_callback_url =', b'websub_callback_url = http://x.example.com'))
            with self.assertRaisesRegex(SettingsError, 'websub_callback_url_invalid') as caught:
                read_websub_callback_url(path)
            self.assertIn('https://', settings_problem(str(caught.exception)))
            path.write_bytes(default.replace(b'\nwebsub_callback_url =\n', b'\n'))
            self.assertIsNone(read_websub_callback_url(path))


if __name__ == '__main__':
    unittest.main()
