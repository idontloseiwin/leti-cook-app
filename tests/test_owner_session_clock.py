"""Owner sessions, sign-in waits and the server clock.

SQLite in a temporary folder. The store's system clocks (wall and running time)
are replaced by a fake that the tests step; no server, Docker or network.
"""
from pathlib import Path
import secrets
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
import db.self_hosted_auth as auth
from db.self_hosted_auth import IDLE_SECONDS, SESSION_SECONDS, OwnerAuthRepository

START = 1_800_000_000


class Clocks:
    """The wall clock and the running (monotonic) clock the store reads."""

    def __init__(self):
        self.wall, self.running = float(START), 5000.0

    def time(self):
        return self.wall

    def monotonic(self):
        return self.running

    def run(self, seconds):
        self.wall += seconds
        self.running += seconds

    def set_back(self, seconds):
        self.wall -= seconds

    def set_forward(self, seconds):
        self.wall += seconds

    def now(self):
        return int(self.wall)


class Store(unittest.TestCase):
    """An owner 'owner' whose password hash is 'H:x', signed in as browser 'first'."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.clocks = Clocks()
        # Replaces the time module of the store only.
        patcher = patch.object(auth, 'time', self.clocks)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.checks = []
        self.new_store()

    def new_store(self):
        self.path = self.folder / (secrets.token_hex(4) + '.sqlite3')
        self.repository = self.open()
        now = self.clocks.now()
        self.repository.issue_setup('code', now)
        self.assertEqual(self.repository.register('owner', lambda s: s == 'code', lambda: 'H:x', 'first', now), 'ok')

    def open(self, initialize=True):
        repository = (OwnerAuthRepository.initialize(self.path, 'H:dummy', self.clocks.now()) if initialize
                      else OwnerAuthRepository(self.path))
        repository.clock = self.clocks.time
        return repository

    def alive(self, browser='first', touch=True, now=None):
        now = self.clocks.now() if now is None else now
        return self.repository.session(browser, now, touch=touch) is not None

    def sign_in(self, browser):
        self.assertEqual(self.repository.login(lambda u: u == 'owner', lambda h: h == 'H:x', browser,
                                               self.clocks.now()), 'ok')

    def times(self, browser):
        with self.repository.transaction() as db:
            return dict(db.execute('SELECT created_at,last_seen,expires_at FROM sessions WHERE token_hash=?',
                                   (browser,)).fetchone())

    def try_password(self, password='x', marks=(), now=None):
        """One sign-in request; `now` is the clock the request read before it reached the store."""
        attempt = auth.SignInAttempt(marks)

        def verify(stored):
            self.checks.append(password)
            return stored == 'H:' + password
        result = self.repository.login(lambda u: u == 'owner', verify, secrets.token_hex(8),
                                       self.clocks.now() if now is None else now, attempt=attempt)
        return result, attempt

    def reach_wait(self, wait, *lanes):
        """Wrong passwords in each lane, each sent when its wait is over, until the next wait is `wait` seconds.

        A lane is the marks its browser sends: none for the shared count. On
        return the last counted try of every lane was made at this moment.
        """
        lanes = lanes or ((),)
        while True:
            answers = [self.try_password('wrong', marks) for marks in lanes]
            waits = {attempt.retry_after for _, attempt in answers}
            self.assertEqual(len(waits), 1)
            if waits == {wait}:
                self.assertEqual({result for result, _ in answers}, {'rate_limited'})
                return
            self.clocks.run(waits.pop())

    def refused(self, wait, marks=(), now=None):
        """The right password is refused without a check, and the refusal names `wait` seconds."""
        checks = len(self.checks)
        result, attempt = self.try_password('x', marks, now)
        self.assertEqual((result, attempt.retry_after, len(self.checks)), ('rate_limited', wait, checks))

    def admitted(self, marks=(), now=None):
        """The right password is checked and signs in; the new browser's mark is returned."""
        checks = len(self.checks)
        result, attempt = self.try_password('x', marks, now)
        self.assertEqual((result, len(self.checks)), ('ok', checks + 1))
        return attempt.new_mark


class SessionClockTests(Store):
    def test_a_clock_set_back_while_running_keeps_the_session_and_its_idle_time(self):
        self.clocks.run(61)
        self.assertTrue(self.alive())  # touched: the last use is now
        for step in (6, 61, 600, 7200):
            with self.subTest(step=step):
                self.clocks.set_back(step)
                self.assertTrue(self.alive(touch=False))
        # Still 30 minutes of running time after the last use, no more.
        self.clocks.run(IDLE_SECONDS - 1)
        self.assertTrue(self.alive(touch=False))
        self.clocks.run(1)
        self.assertFalse(self.alive(touch=False))

    def test_the_twelve_hour_limit_counts_running_time_across_a_step_back(self):
        self.clocks.set_back(3600)
        self.assertTrue(self.alive())
        for _ in range(SESSION_SECONDS // 1200 - 1):
            self.clocks.run(1200)
            self.assertTrue(self.alive())
        self.clocks.run(1199)
        self.assertTrue(self.alive())
        self.clocks.run(1)
        self.assertFalse(self.alive())

    def test_a_forward_jump_is_not_undone(self):
        # The server may have slept: the session ends as the clock says.
        self.clocks.set_forward(IDLE_SECONDS)
        self.assertFalse(self.alive())

    def test_a_clock_set_forward_and_back_never_lengthens_the_session(self):
        self.clocks.set_forward(600)
        self.clocks.run(120)
        self.assertTrue(self.alive())  # last use recorded 10 minutes ahead
        self.clocks.set_back(600)
        self.assertTrue(self.alive(touch=False))
        self.clocks.run(IDLE_SECONDS - 1)
        self.assertTrue(self.alive(touch=False))
        self.clocks.run(1)
        self.assertFalse(self.alive(touch=False))

    def test_a_slow_request_keeps_a_session_another_request_touched(self):
        self.clocks.run(60)
        self.assertTrue(self.alive())  # touched at START + 60
        self.clocks.run(1)
        # A request that read the clock at START + 54 and then waited for the lock.
        self.assertTrue(self.alive(now=START + 54))
        self.assertTrue(self.alive())

    def test_a_sign_in_after_a_step_back_keeps_other_browsers(self):
        self.clocks.run(61)
        self.assertTrue(self.alive())
        self.clocks.set_back(600)
        self.sign_in('second')
        self.assertTrue(self.alive('first'))
        self.assertTrue(self.alive('second'))

    def test_a_step_seen_by_a_failed_transaction_is_applied_by_the_next(self):
        self.clocks.run(61)
        self.assertTrue(self.alive())
        self.clocks.set_back(600)
        with self.assertRaises(RuntimeError):
            with self.repository.transaction():
                raise RuntimeError('fails after the step was taken')
        self.assertTrue(self.alive())

    def test_a_clock_set_back_while_stopped_signs_out_only_recent_browsers(self):
        self.clocks.run(1200)
        self.sign_in('second')  # used now; 'first' was last used 20 minutes ago
        self.repository = self.open(initialize=False)  # the app restarts...
        self.clocks.set_back(600)  # ...after the clock was set back
        self.assertFalse(self.alive('second'))
        self.assertTrue(self.alive('first'))


class SignInWaitClockTests(Store):
    """The wait a refusal names is the wait that applies, whatever the clock does while the app runs."""

    def setUp(self):
        super().setUp()
        self.mark = self.admitted()

    def test_a_wait_ends_when_the_refusal_said_after_the_clock_is_set_back(self):
        for lane in ('shared', 'known'):
            for wait in (1, 16, 300):
                for step in (600, 3600):
                    with self.subTest(lane=lane, wait=wait, step=step):
                        self.new_store()
                        marks = [self.admitted()] if lane == 'known' else ()
                        self.reach_wait(wait, marks)
                        self.clocks.set_back(step)
                        self.refused(wait, marks)
                        if wait > 1:
                            self.clocks.run(wait - 1)
                            self.refused(1, marks)
                        self.clocks.run(1)
                        self.admitted(marks)

    def test_time_already_waited_still_counts_and_refusals_do_not_add_to_the_wait(self):
        for marks in ((), [self.mark]):
            with self.subTest(known=bool(marks)):
                self.reach_wait(16, marks)
                self.clocks.run(5)
                self.clocks.set_back(3600)
                for left in (11, 8, 5, 2):
                    self.refused(left, marks)
                    self.refused(left, marks)
                    self.clocks.run(3)
                self.admitted(marks)

    def test_after_a_step_back_each_lane_waits_and_starts_again_on_its_own(self):
        self.reach_wait(16)              # everyone without a mark waits 16 s
        self.reach_wait(1, [self.mark])  # the marked browser waits 1 s
        self.clocks.set_back(3600)
        # Wrong passwords sent by others are refused and do not reach the marked browser's count.
        self.assertEqual(self.try_password('wrong')[0], 'rate_limited')
        self.clocks.run(1)
        self.admitted([self.mark])
        # Its sign-in did not start the shared count again.
        self.refused(15)
        self.clocks.run(15)
        other = self.admitted()
        # The shared sign-in did not touch a marked browser's own count either.
        self.reach_wait(1, [other])
        self.admitted([self.mark])
        self.refused(1, [other])

    def test_a_sign_in_that_waited_while_the_clock_was_set_back_gets_no_free_try(self):
        for marks in ((), [self.mark]):
            with self.subTest(known=bool(marks)):
                self.reach_wait(16, marks)
                self.clocks.run(5)
                read_before = self.clocks.now()
                self.clocks.set_back(600)
                self.refused(11, marks, now=read_before)  # the request that waited
                self.refused(11, marks)                   # and one that came after the step
                self.clocks.run(11)
                # A waiting request that is let through counts on the clock as it is now.
                read_before = self.clocks.now()
                self.clocks.set_back(600)
                self.assertEqual(self.try_password('wrong', marks, now=read_before)[0], 'invalid_credentials')
                self.refused(32, marks)
                self.clocks.run(31)
                self.refused(1, marks)
                self.clocks.run(1)
                self.admitted(marks)

    def test_a_step_seen_by_a_failed_transaction_moves_each_wait_once(self):
        self.reach_wait(16, (), [self.mark])
        self.clocks.run(5)
        self.clocks.set_back(600)
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                with self.repository.transaction():
                    raise RuntimeError('fails after the step was taken')
        for marks in ((), [self.mark]):
            self.refused(11, marks)
        self.clocks.run(10)
        for marks in ((), [self.mark]):
            self.refused(1, marks)
        self.clocks.run(1)
        for marks in ((), [self.mark]):
            self.admitted(marks)

    def test_a_password_reset_after_a_step_back_ends_marks_and_sessions(self):
        self.reach_wait(1, [self.mark])
        self.clocks.set_back(600)
        self.clocks.run(1)
        newer = self.admitted([self.mark])
        self.assertTrue(self.alive())
        self.repository.reset_password('H:new')
        self.assertFalse(self.alive())
        self.reach_wait(1)
        for mark in (self.mark, newer):
            checks = len(self.checks)
            result, attempt = self.try_password('new', [mark])
            self.assertEqual((result, len(self.checks)), ('rate_limited', checks))
        with self.repository.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM sessions').fetchone()[0], 0)


class WaitingRequestTests(Store):
    """A request reads the clock, waits for the store, and the clock is set back meanwhile."""

    def use_then_wait(self):
        """'first' was used; a request has read the clock; then the clock is set back an hour."""
        self.clocks.run(61)
        self.assertTrue(self.alive())  # touched: the last use is now
        self.clocks.run(100)
        read_before = self.clocks.now()
        self.clocks.set_back(3600)
        return read_before

    def test_the_request_that_waited_keeps_the_session_and_its_idle_time(self):
        read_before = self.use_then_wait()
        self.assertTrue(self.alive(touch=False))      # a request that came after the step
        self.assertTrue(self.alive(now=read_before))  # the one that waited; it is a use
        self.assertTrue(self.alive(touch=False))
        self.assertEqual(self.times('first')['last_seen'], self.clocks.now())
        # Still 30 minutes of running time after that use, no more.
        self.clocks.run(IDLE_SECONDS - 1)
        self.assertTrue(self.alive(touch=False))
        self.clocks.run(1)
        self.assertFalse(self.alive(touch=False))
        # Ended is ended: neither a waiting request nor another step brings it back.
        read_before = self.clocks.now()
        self.clocks.set_back(3600)
        self.assertFalse(self.alive(now=read_before))
        self.assertFalse(self.alive())

    def test_session_read_after_a_long_queue_uses_the_current_clock(self):
        read_before = self.use_then_wait()
        self.assertTrue(self.alive(touch=False))
        self.clocks.run(120)
        self.assertTrue(self.alive(now=read_before))
        self.assertEqual(self.times('first')['last_seen'], self.clocks.now())
        self.clocks.run(IDLE_SECONDS)
        self.assertFalse(self.alive(touch=False))

    def test_slow_password_verification_keeps_other_active_sessions(self):
        def verify(stored):
            self.clocks.set_back(3600)
            self.assertTrue(self.alive(touch=False))
            self.clocks.run(120)
            return stored == 'H:x'

        self.assertEqual(self.repository.login(lambda u: u == 'owner', verify,
                                               'second', self.clocks.now()), 'ok')
        self.assertTrue(self.alive(touch=False))
        self.assertEqual(self.times('second')['created_at'], self.clocks.now())

    def test_another_step_keeps_the_earlier_clock_bound_until_both_have_passed(self):
        read_before = self.use_then_wait()
        self.assertTrue(self.alive(touch=False))
        self.clocks.run(120)
        self.clocks.set_back(10)
        self.assertTrue(self.alive(touch=False))
        self.clocks.run(20)
        self.assertTrue(self.alive(now=read_before))
        self.assertEqual(self.times('first')['last_seen'], self.clocks.now())

    def test_shared_wait_status_agrees_with_the_time_used_for_sign_in(self):
        self.reach_wait(16)
        read_before = self.clocks.now()
        self.clocks.set_back(3600)
        self.assertTrue(self.repository.sign_in_limited(read_before))
        self.clocks.run(16)
        self.assertFalse(self.repository.sign_in_limited(read_before))

    def test_the_request_that_waited_is_the_first_to_see_the_step(self):
        read_before = self.use_then_wait()
        self.assertTrue(self.alive(now=read_before, touch=False))
        self.assertTrue(self.alive(touch=False))

    def test_a_request_held_up_by_a_busy_store_keeps_the_session(self):
        self.clocks.run(161)
        self.assertTrue(self.alive())
        read_before = self.clocks.now()
        answers = []
        with self.repository.queue:  # the store is busy
            waiting = threading.Thread(
                target=lambda: answers.append(self.repository.session('first', read_before, touch=False)))
            waiting.start()
            waiting.join(0.2)
            self.assertTrue(waiting.is_alive())
            self.clocks.set_back(3600)
            self.assertTrue(self.alive(touch=False))  # this thread holds the store: a request after the step
        waiting.join(10)
        self.assertEqual([answer is not None for answer in answers], [True])
        self.assertTrue(self.alive(touch=False))

    def test_the_twelve_hour_limit_is_not_lengthened_by_a_request_that_waited(self):
        read_before = self.use_then_wait()
        self.assertTrue(self.alive(now=read_before))
        used = 161
        while used + 1200 < SESSION_SECONDS:
            self.clocks.run(1200)
            used += 1200
            self.assertTrue(self.alive())
        self.clocks.run(SESSION_SECONDS - used - 1)
        self.assertTrue(self.alive())
        self.clocks.run(1)
        self.assertFalse(self.alive())

    def test_a_sign_in_finished_after_the_clock_was_set_back_keeps_other_browsers(self):
        self.clocks.run(61)
        self.assertTrue(self.alive())

        def verify(stored):
            self.clocks.set_back(3600)  # while the password is being checked
            return stored == 'H:x'
        attempt = auth.SignInAttempt()
        self.assertEqual(self.repository.login(lambda u: u == 'owner', verify, 'second', self.clocks.now(),
                                               attempt=attempt), 'ok')
        now = self.clocks.now()
        for browser in ('first', 'second'):
            self.assertTrue(self.alive(browser, touch=False))
            times = self.times(browser)
            self.assertLessEqual(max(times['created_at'], times['last_seen']), now)
        self.assertEqual(self.times('second')['expires_at'], now + SESSION_SECONDS)
        self.assertEqual(int(attempt.new_mark.split('.')[0]), now)
        # The shared count started again on the clock as it is now.
        self.reach_wait(1)
        self.clocks.run(1)
        self.admitted()

    def test_a_step_seen_by_a_failed_transaction_is_applied_once_for_a_request_that_waited(self):
        read_before = self.use_then_wait()
        with self.assertRaises(RuntimeError):
            with self.repository.transaction():
                raise RuntimeError('fails after the step was taken')
        self.assertTrue(self.alive(now=read_before))
        self.clocks.run(IDLE_SECONDS - 1)
        self.assertTrue(self.alive(touch=False))
        self.clocks.run(1)
        self.assertFalse(self.alive(touch=False))

    def test_a_request_that_waited_through_a_forward_jump_ends_the_session_as_the_clock_says(self):
        read_before = self.use_then_wait()
        self.assertTrue(self.alive(now=read_before))
        # The server may have slept: forward is not undone, also for a request that waited.
        read_before = self.clocks.now()
        self.clocks.set_forward(IDLE_SECONDS)
        self.assertFalse(self.alive(now=read_before))
        self.clocks.set_back(IDLE_SECONDS)
        self.assertFalse(self.alive())


if __name__ == '__main__':
    unittest.main()
