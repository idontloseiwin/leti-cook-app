"""Sign-in protection (SECURITY.md "Access").

Wrong passwords wait in lanes: a browser with a known-browser mark has its own
count, all others share one. A wait is a refusal that names it: nothing is held
meanwhile. Setup codes are never counted. The owner signs in, and enters the
setup code, while other callers keep sending wrong ones.

SQLite in a temporary folder; the real SelfHostedAuthMiddleware over httpx's
ASGI transport, with the password hash simulated (the production Argon2 cost is
pinned below). No server, Docker or network.
"""
import asyncio
import importlib.util
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
import db.self_hosted_auth as auth
from db.self_hosted_auth import OwnerAuthRepository

NOW = 1_800_000_000
SERVER_STACK = all(importlib.util.find_spec(name) for name in ('argon2', 'starlette', 'httpx', 'fastapi'))
ORIGIN = 'http://127.0.0.1:8002'
HEADERS = {'Origin': ORIGIN, 'X-LIC-Auth': '1'}
PASSWORD = 'correct horse battery'
CODE = 'c' * 43
# Measured well below a second; the bound leaves room for a slow CI runner.
BOUND = 3.0


def wrong_password():
    return 'wrong password ' + secrets.token_hex(4)


class Store(unittest.TestCase):
    """An owner 'owner' whose password hash is 'H:secret'; hashing is simulated."""
    START = NOW

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.clock = [self.START]
        self.repository = OwnerAuthRepository.initialize(self.folder / 'auth.sqlite3', 'H:dummy', self.START)
        self.repository.clock = lambda: self.clock[0]
        self.repository.issue_setup('code', self.START)
        self.assertEqual(self.repository.register('owner', lambda s: s == 'code', lambda: 'H:secret', 'first', self.START), 'ok')
        self.checks = []
        self.lock = threading.Lock()

    def sign_in(self, password='secret', marks=(), username='owner', now=None):
        attempt = auth.SignInAttempt(marks)

        def verify(stored):
            with self.lock:
                self.checks.append(password)
            return stored == 'H:' + password
        result = self.repository.login(lambda u: u == username, verify, secrets.token_hex(8),
                                       self.clock[0] if now is None else now, attempt=attempt)
        return result, attempt

    def wrong(self, count, marks=()):
        for _ in range(count):
            self.assertEqual(self.sign_in('wrong', marks)[0], 'invalid_credentials')

    def tries(self, count, marks=()):
        """`count` wrong passwords in a row, each sent when its wait is over."""
        for _ in range(count):
            result, attempt = self.sign_in('wrong', marks)
            if result == 'rate_limited':
                self.clock[0] += attempt.retry_after
                result = self.sign_in('wrong', marks)[0]
            self.assertEqual(result, 'invalid_credentials')

    def shared_count(self):
        with self.repository.transaction() as db:
            return tuple(db.execute('SELECT attempt_count,attempt_start FROM owner').fetchone())


class GrowingWaitTests(Store):
    def test_five_wrong_passwords_then_a_wait_that_doubles_up_to_five_minutes(self):
        self.wrong(auth.FREE_TRIES)
        waits = []
        for _ in range(12):
            result, attempt = self.sign_in('wrong')
            self.assertEqual(result, 'rate_limited')
            waits.append(attempt.retry_after)
            self.clock[0] += attempt.retry_after - 1
            self.assertEqual(self.sign_in('wrong')[0], 'rate_limited')
            self.clock[0] += 1
            self.wrong(1)
        self.assertEqual(waits, [1, 2, 4, 8, 16, 32, 64, 128, 256, 300, 300, 300])
        self.assertEqual(auth.LONGEST_WAIT, 300)

    def test_a_refused_try_makes_no_check_and_no_write_and_the_right_password_waits_too(self):
        self.wrong(auth.FREE_TRIES)
        checks, count = len(self.checks), self.shared_count()
        for password in ('wrong', 'secret'):
            self.assertEqual(self.sign_in(password)[0], 'rate_limited')
        self.assertEqual((len(self.checks), self.shared_count()), (checks, count))

    def test_signing_in_or_an_hour_without_tries_starts_the_count_again(self):
        self.wrong(auth.FREE_TRIES - 1)
        self.assertEqual(self.sign_in()[0], 'ok')
        self.wrong(auth.FREE_TRIES)
        self.assertEqual(self.sign_in('wrong')[0], 'rate_limited')
        self.clock[0] += auth.FORGET_TRIES
        self.wrong(auth.FREE_TRIES)
        self.assertEqual(self.sign_in('wrong')[0], 'rate_limited')

    def test_a_clock_set_back_neither_clears_nor_lengthens_a_wait(self):
        self.tries(auth.FREE_TRIES + 4)  # the next wait is 16 s
        self.clock[0] -= 3600
        result, attempt = self.sign_in('wrong')
        self.assertEqual((result, attempt.retry_after), ('rate_limited', 16))
        self.assertFalse(self.repository.sign_in_limited(self.clock[0] + 3600 + 16))
        self.assertTrue(self.repository.sign_in_limited(self.clock[0] + 3600 + 15))

    def test_wrong_sign_ins_before_setup_do_not_reach_the_new_account(self):
        repository = OwnerAuthRepository.initialize(self.folder / 'pending.sqlite3', 'H:dummy', NOW)
        repository.clock = lambda: NOW
        repository.issue_setup('code', NOW)
        self.assertEqual(repository.claim_setup(lambda s: s == 'code', 'setup', NOW), 'ok')
        for _ in range(20):
            repository.login(lambda u: True, lambda h: False, 's', NOW)
        self.assertTrue(repository.sign_in_limited(NOW))
        attempt = auth.SignInAttempt()
        self.assertEqual(repository.complete_setup('setup', 'password', 'owner', lambda: 'H:x', 'owner-session', NOW,
                                                   attempt), 'ok')
        self.assertFalse(repository.sign_in_limited(NOW))
        self.assertRegex(attempt.new_mark, auth.KNOWN_MARK)


class KnownBrowserTests(Store):
    def setUp(self):
        super().setUp()
        result, attempt = self.sign_in()
        self.assertEqual(result, 'ok')
        self.mark = attempt.new_mark

    def shared_wait(self):
        self.wrong(auth.FREE_TRIES)
        self.assertEqual(self.sign_in('secret')[0], 'rate_limited')

    def test_a_known_browser_signs_in_while_the_shared_count_waits(self):
        self.shared_wait()
        result, attempt = self.sign_in('secret', [self.mark])
        self.assertEqual(result, 'ok')
        self.assertRegex(attempt.new_mark, auth.KNOWN_MARK)
        self.assertNotEqual(attempt.new_mark, self.mark)
        # Its success does not restart the shared count.
        self.assertEqual(self.sign_in('secret')[0], 'rate_limited')

    def test_wrong_passwords_with_a_mark_wait_on_their_own(self):
        other = self.sign_in()[1].new_mark
        self.wrong(auth.FREE_TRIES, [self.mark])
        result, attempt = self.sign_in('secret', [self.mark])
        self.assertEqual((result, attempt.retry_after), ('rate_limited', 1))
        self.assertEqual(self.sign_in('secret', [other])[0], 'ok')
        self.assertEqual(self.shared_count()[0], 0)
        self.assertEqual(self.sign_in('secret')[0], 'ok')

    def test_a_password_reset_ends_the_mark(self):
        self.repository.reset_password('H:new')
        self.shared_wait()
        self.assertEqual(self.sign_in('new', [self.mark])[0], 'rate_limited')

    def test_a_mark_lasts_thirty_days(self):
        self.clock[0] += auth.KNOWN_BROWSER_SECONDS - 1
        self.shared_wait()
        self.assertEqual(self.sign_in('secret', [self.mark])[0], 'ok')
        self.clock[0] += 1
        self.repository.clear_sign_in_limit()
        self.shared_wait()
        self.assertEqual(self.sign_in('secret', [self.mark])[0], 'rate_limited')

    def test_a_changed_or_foreign_mark_is_not_known_and_the_real_one_is_found_among_others(self):
        # Another installation: the same name and password, another account.
        other = OwnerAuthRepository.initialize(self.folder / 'other.sqlite3', 'H:dummy', NOW + 1)
        other.clock = lambda: NOW + 1
        other.issue_setup('code', NOW + 1)
        self.assertEqual(other.register('owner', lambda s: True, lambda: 'H:secret', 's', NOW + 1), 'ok')
        attempt = auth.SignInAttempt()
        self.assertEqual(other.login(lambda u: True, lambda h: True, 's3', NOW + 1, attempt=attempt), 'ok')
        foreign = attempt.new_mark
        issued, nonce, signature = self.mark.split('.')
        changed = [issued + '.' + nonce + '.' + signature[:-1] + ('A' if signature[-1] != 'A' else 'B'),
                   str(int(issued) + 1) + '.' + nonce + '.' + signature,
                   issued + '.' + secrets.token_urlsafe(16) + '.' + signature,
                   foreign, '', 'x' * 4096, self.mark + '.', None]
        self.shared_wait()
        for mark in changed:
            with self.subTest(mark=str(mark)[:24]):
                self.assertEqual(self.sign_in('secret', [mark])[0], 'rate_limited')
        self.assertEqual(self.sign_in('secret', ['garbage', foreign, self.mark])[0], 'ok')

    def test_the_mark_holds_no_name_or_password(self):
        self.assertRegex(self.mark, auth.KNOWN_MARK)
        for secret in ('owner', 'secret', 'H:secret'):
            self.assertNotIn(secret, self.mark)

    def test_counts_of_marked_browsers_are_bounded(self):
        marks = [self.sign_in()[1].new_mark for _ in range(auth.KNOWN_BROWSERS + 6)]
        for mark in marks:
            self.wrong(1, [mark])
        self.assertEqual(len(self.repository._known_tries), auth.KNOWN_BROWSERS)


class SetupCodeTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.repository = OwnerAuthRepository.initialize(Path(folder.name) / 'auth.sqlite3', 'H:dummy', NOW)
        self.repository.clock = lambda: NOW
        self.repository.issue_setup('code', NOW)

    def test_wrong_codes_are_never_counted_and_never_delay_the_right_one(self):
        for index in range(300):
            self.assertEqual(self.repository.claim_setup(lambda s: s == 'other-%d' % index, 's', NOW),
                             'invalid_setup_code')
        self.assertFalse(self.repository.sign_in_limited(NOW))
        self.assertEqual(self.repository.claim_setup(lambda s: s == 'code', 'setup', NOW), 'ok')

    def test_wrong_passwords_do_not_delay_the_setup_code(self):
        for _ in range(20):
            self.repository.login(lambda u: True, lambda h: False, 's', NOW)
        self.assertEqual(self.repository.claim_setup(lambda s: s == 'code', 'setup', NOW), 'ok')


class Connections:
    """Counts store connections that are open at once (sqlite3.connect patched with a counting factory)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.open = self.peak = self.total = 0
        counter, real = self, sqlite3.connect

        class Counted(sqlite3.Connection):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                with counter.lock:
                    counter.open += 1
                    counter.total += 1
                    counter.peak = max(counter.peak, counter.open)

            def close(self):
                with counter.lock:
                    counter.open -= 1
                super().close()
        self.connect = lambda *args, **kwargs: real(*args, factory=Counted, **kwargs)


class HoldsNothingTests(Store):
    def test_no_store_connection_is_open_during_the_password_check(self):
        connections, seen = Connections(), []
        with patch.object(auth.sqlite3, 'connect', connections.connect):
            self.assertEqual(self.repository.login(lambda u: True, lambda h: seen.append(connections.open) or True,
                                                   's', NOW, attempt=auth.SignInAttempt()), 'ok')
        self.assertEqual((seen, connections.open, connections.total), ([0], 0, 2))

    def test_the_sign_in_code_never_sleeps(self):
        for name in ('application/db/self_hosted_auth.py', 'application/self_hosted_auth.py'):
            self.assertNotRegex((ROOT / name).read_text(encoding='utf-8'), r'\bsleep\(', name)


class ThreadsTests(Store):
    START = int(time.time())

    def test_the_owner_signs_in_from_a_known_browser_while_threads_send_wrong_passwords(self):
        self.repository.clock = time.time
        result, attempt = self.sign_in(now=int(time.time()))
        self.assertEqual(result, 'ok')
        mark, stop, results = attempt.new_mark, threading.Event(), []

        def attacker():
            while not stop.is_set():
                try:
                    results.append(self.repository.login(lambda u: True, lambda h: time.sleep(0.02) or False, 's',
                                                          int(time.time()), attempt=auth.SignInAttempt()))
                except auth.AuthStoreError as error:
                    results.append(str(error))
        threads = [threading.Thread(target=attacker) for _ in range(6)]
        for thread in threads:
            thread.start()
        try:
            deadline = time.monotonic() + 5
            while 'rate_limited' not in results and time.monotonic() < deadline:
                time.sleep(0.01)
            started = time.monotonic()
            result, _ = self.sign_in('secret', [mark], now=int(time.time()))
            elapsed = time.monotonic() - started
        finally:
            stop.set()
            for thread in threads:
                thread.join()
        self.assertEqual(result, 'ok')
        self.assertLess(elapsed, BOUND)
        self.assertIn('rate_limited', results)
        self.assertLessEqual(results.count('invalid_credentials'), auth.FREE_TRIES + 3)

    def test_a_flood_of_store_requests_never_turns_the_owner_away(self):
        # SQLite's own wait for its lock polls and gives up after 0.5 s; without the
        # store's queue, 32 such threads refused 9 of 40 of the owner's sign-ins.
        self.repository.clock = time.time
        mark, stop, failures = self.sign_in(now=int(time.time()))[1].new_mark, threading.Event(), []

        def attacker():
            while not stop.is_set():
                try:
                    self.repository.login(lambda u: True, lambda h: False, 's', int(time.time()), attempt=auth.SignInAttempt())
                except auth.AuthStoreError as error:
                    failures.append(str(error))
        threads = [threading.Thread(target=attacker) for _ in range(32)]
        for thread in threads:
            thread.start()
        results, worst = [], 0
        try:
            time.sleep(0.3)
            for _ in range(20):
                attempt, started = auth.SignInAttempt([mark]), time.monotonic()
                try:
                    results.append(self.repository.login(lambda u: u == 'owner', lambda h: h == 'H:secret',
                                                          secrets.token_hex(8), int(time.time()), attempt=attempt))
                except auth.AuthStoreError as error:
                    results.append(str(error))
                worst = max(worst, time.monotonic() - started)
                mark = attempt.new_mark or mark
        finally:
            stop.set()
            for thread in threads:
                thread.join()
        self.assertEqual(results, ['ok'] * 20)
        self.assertLess(worst, BOUND)
        self.assertEqual(failures, [])


@unittest.skipUnless(SERVER_STACK, 'argon2, starlette, httpx and fastapi are installed in CI')
class HttpTests(unittest.IsolatedAsyncioTestCase):
    """The real middleware; 'H:<password>' stands in for the Argon2 hash and its check."""

    async def site(self, mode='password', origin=ORIGIN, clock=None):
        from fastapi import FastAPI
        import self_hosted_auth
        from self_hosted_auth import SelfHostedAuthMiddleware
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        start = int((clock or time.time)())
        self.owner = OwnerAuthRepository.initialize(Path(folder.name) / 'owner.sqlite3', 'H:dummy', start)
        if clock:
            self.owner.clock = clock
        if mode == 'password':
            self.owner.issue_setup('code', start)
            self.assertEqual(self.owner.register('owner', lambda s: True, lambda: 'H:' + PASSWORD, 'fixture', start), 'ok')
        else:
            self.owner.issue_setup(self_hosted_auth.token_hash(CODE), start)
        self.checks = SimpleNamespace(calls=0, running=0, peak=0, lock=threading.Lock())

        def verify(encoded, value):
            with self.checks.lock:
                self.checks.calls += 1
                self.checks.running += 1
                self.checks.peak = max(self.checks.peak, self.checks.running)
            try:
                time.sleep(0.02)
                return encoded == 'H:' + value
            finally:
                with self.checks.lock:
                    self.checks.running -= 1
        for name, value in (('verify_password', verify), ('PASSWORD_HASHER', SimpleNamespace(hash=lambda v: 'H:' + v))):
            patcher = patch.object(self_hosted_auth, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        api = FastAPI()

        @api.get('/probe')
        def probe():
            return {'ok': True}
        api.add_middleware(SelfHostedAuthMiddleware, repository=self.owner, origin=origin, worker_key='k',
                           clock=clock or time.time)
        self.app, self.origin = api, origin
        self.headers = {'Origin': origin, 'X-LIC-Auth': '1'}

    def client(self):
        import httpx
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=self.origin)

    async def sign_in(self, client, password=PASSWORD, username='owner'):
        return await client.post('/auth/login', json={'username': username, 'password': password}, headers=self.headers)

    async def stranger(self, password=None):
        async with self.client() as client:
            return await self.sign_in(client, password or wrong_password())

    async def attack(self, stop, statuses, send):
        async with self.client() as client:
            while not stop.is_set():
                statuses.append((await send(client)).status_code)
                await asyncio.sleep(0)

    async def until(self, condition):
        deadline = time.monotonic() + 10
        while not condition():
            self.assertLess(time.monotonic(), deadline, 'the other callers never got going')
            await asyncio.sleep(0.01)

    async def test_the_owner_signs_in_from_a_known_browser_while_others_send_wrong_passwords(self):
        # A frozen clock: the others keep the shared count in its wait for the whole test.
        await self.site(clock=lambda: NOW)
        stop, statuses = asyncio.Event(), []
        async with self.client() as owner:
            self.assertEqual((await self.sign_in(owner)).status_code, 200)
            # Signing out ends the session and keeps the browser's mark.
            self.assertEqual((await owner.post('/auth/logout', json={}, headers=self.headers)).status_code, 200)
            tasks = [asyncio.create_task(self.attack(stop, statuses, lambda c: self.sign_in(c, wrong_password())))
                     for _ in range(8)]
            try:
                await self.until(lambda: statuses.count(429) >= 20)
                started = time.monotonic()
                answer = await self.sign_in(owner)
                elapsed = time.monotonic() - started
                probe = await owner.get('/probe')
                other = await self.stranger(PASSWORD)
            finally:
                stop.set()
                await asyncio.gather(*tasks)
        self.assertEqual(answer.status_code, 200)
        self.assertLess(elapsed, BOUND)
        self.assertEqual(probe.status_code, 200)
        # A browser without the mark shares the others' wait (SECURITY.md says so).
        self.assertEqual(other.status_code, 429)
        self.assertEqual(set(statuses), {401, 429})
        # The shared lane checked only its free tries; the owner's two checks came on top.
        self.assertEqual(statuses.count(401), auth.FREE_TRIES)
        self.assertEqual(self.checks.calls, auth.FREE_TRIES + 2)

    async def test_the_owner_enters_the_setup_code_while_others_send_wrong_codes(self):
        await self.site(mode='setup')
        stop, statuses = asyncio.Event(), []

        def claim(client):
            return client.post('/auth/claim', json={'setup_code': secrets.token_urlsafe(32)}, headers=self.headers)
        tasks = [asyncio.create_task(self.attack(stop, statuses, claim)) for _ in range(8)]
        try:
            await self.until(lambda: len(statuses) >= 40)
            async with self.client() as owner:
                started = time.monotonic()
                claimed = await owner.post('/auth/claim', json={'setup_code': CODE}, headers=self.headers)
                elapsed = time.monotonic() - started
                created = await owner.post('/auth/complete', headers=self.headers,
                                           json={'mode': 'password', 'username': 'owner', 'password': PASSWORD})
        finally:
            stop.set()
            await asyncio.gather(*tasks)
        self.assertEqual((claimed.status_code, claimed.json()), (200, {'status': 'setup_verified'}))
        self.assertLess(elapsed, BOUND)
        self.assertEqual((created.status_code, created.json()), (200, {'status': 'configured'}))
        # Wrong codes are refused (401; 409 once the account exists) and never asked to wait.
        self.assertIn(401, statuses)
        self.assertLessEqual(set(statuses), {401, 409})
        marks = [c for c in created.headers.get_list('set-cookie') if c.startswith('lic-known-')]
        self.assertEqual(len(marks), 1)  # the browser that created the account is known

    async def test_a_flood_during_a_wait_is_refused_at_once_and_holds_nothing(self):
        await self.site(clock=lambda: NOW)
        async with self.client() as client:
            for _ in range(auth.FREE_TRIES):
                self.assertEqual((await self.sign_in(client, wrong_password())).status_code, 401)
        checks, connections = self.checks.calls, Connections()
        with patch.object(auth.sqlite3, 'connect', connections.connect):
            started = time.monotonic()
            answers = await asyncio.gather(*(self.stranger() for _ in range(50)))
            elapsed = time.monotonic() - started
        self.assertEqual([a.status_code for a in answers], [429] * 50)
        self.assertEqual({a.json()['code'] for a in answers}, {'rate_limited'})
        self.assertEqual({a.headers['retry-after'] for a in answers}, {'1'})
        self.assertEqual(self.checks.calls, checks)  # no password check during the wait
        # One short transaction per refusal, none left open: nothing waits holding the store.
        self.assertEqual((connections.total, connections.open), (50, 0))
        self.assertLessEqual(connections.peak, 40)  # never more than the thread pool
        self.assertLess(elapsed, BOUND)

    async def test_a_flood_from_scratch_starts_only_the_free_checks(self):
        await self.site(clock=lambda: NOW)
        answers = await asyncio.gather(*(self.stranger() for _ in range(30)))
        statuses = [a.status_code for a in answers]
        self.assertEqual((statuses.count(401), statuses.count(429)), (auth.FREE_TRIES, 30 - auth.FREE_TRIES))
        self.assertEqual(self.checks.calls, auth.FREE_TRIES)
        self.assertLessEqual(self.checks.peak, auth.FREE_TRIES)

    async def test_wrong_name_and_wrong_password_answer_alike_and_the_wait_says_nothing_more(self):
        await self.site(clock=lambda: NOW)
        async with self.client() as client:
            answers = [await self.sign_in(client, wrong_password()), await self.sign_in(client, PASSWORD, 'someone'),
                       await self.sign_in(client, wrong_password(), 'someone')]
            for _ in range(auth.FREE_TRIES - 3):
                await self.sign_in(client, wrong_password())
            waiting = await self.sign_in(client)
        self.assertEqual({(a.status_code, a.text, 'set-cookie' in a.headers) for a in answers},
                         {(401, '{"code":"invalid_credentials"}', False)})
        self.assertEqual((waiting.status_code, waiting.json(), waiting.headers['retry-after']),
                         (429, {'code': 'rate_limited'}, '1'))
        self.assertNotIn('set-cookie', waiting.headers)

    async def test_the_wait_a_refusal_names_holds_after_the_server_clock_is_set_back(self):
        wall, running = [float(NOW)], [5000.0]
        # The store's system clocks: the wall clock steps, running time does not.
        patcher = patch.object(auth, 'time', SimpleNamespace(time=lambda: wall[0], monotonic=lambda: running[0]))
        patcher.start()
        self.addCleanup(patcher.stop)
        await self.site(clock=lambda: wall[0])
        async with self.client() as client:
            for _ in range(auth.FREE_TRIES):
                self.assertEqual((await self.sign_in(client, wrong_password())).status_code, 401)
            wall[0] -= 600
            checks = self.checks.calls
            for _ in range(3):
                refused = await self.sign_in(client)
                self.assertEqual((refused.status_code, refused.headers['retry-after']), (429, '1'))
            self.assertEqual(self.checks.calls, checks)  # no password check before the time named
            wall[0] += 1
            running[0] += 1
            self.assertEqual((await self.sign_in(client)).status_code, 200)
            self.assertEqual(self.checks.calls, checks + 1)

    async def test_the_mark_is_a_sign_in_only_cookie(self):
        for origin, name, secure in ((ORIGIN, 'lic-known-', False), ('https://leti.example', '__Secure-lic-known-', True)):
            with self.subTest(origin=origin):
                await self.site(origin=origin)
                async with self.client() as owner:
                    answer = await self.sign_in(owner)
                self.assertEqual(answer.status_code, 200)
                marks = [c for c in answer.headers.get_list('set-cookie') if c.startswith(name)]
                self.assertEqual(len(marks), 1)
                value, *attributes = [part.strip() for part in marks[0].split(';')]
                attributes = {part.split('=')[0].lower(): part.partition('=')[2] for part in attributes}
                self.assertEqual(attributes['path'], '/auth/login')
                self.assertEqual(attributes['max-age'], str(30 * 86400))
                self.assertEqual(attributes['samesite'].lower(), 'strict')
                self.assertIn('httponly', attributes)
                self.assertEqual('secure' in attributes, secure)
                self.assertRegex(value.split('=', 1)[1], auth.KNOWN_MARK)
                self.assertNotIn('owner', value.split('=', 1)[1])


class PinTests(unittest.TestCase):
    @unittest.skipUnless(SERVER_STACK, 'argon2 is installed in CI')
    def test_the_password_hash_cost_is_unchanged(self):
        from self_hosted_auth import PASSWORD_HASHER
        self.assertEqual((PASSWORD_HASHER.time_cost, PASSWORD_HASHER.memory_cost, PASSWORD_HASHER.parallelism,
                          PASSWORD_HASHER.hash_len, PASSWORD_HASHER.salt_len), (3, 65536, 1, 32, 16))

    def test_one_backend_process_with_a_bounded_number_of_requests(self):
        # Marked browsers' counts live in the process; uvicorn refuses requests beyond 64 at once.
        source = (ROOT / 'application/ops/self_hosted_container.py').read_text(encoding='utf-8')
        config = re.search(r"uvicorn\.Config\('server:app'(.*?)\)\n", source, re.S).group(1)
        self.assertIn('workers=1', config)
        self.assertIn('limit_concurrency=64', config)


if __name__ == '__main__':
    unittest.main()
