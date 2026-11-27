"""Rules compare confirmed observations in one measurement definition."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'application'))
from alerts import AlertRule, Measurement, crossed
from hot_storage import HOT_DATA_WINDOW
from pydantic import ValidationError


class WorkerScheduling(unittest.TestCase):
    def test_yields_on_each_budget_and_resets_after_idle_or_failure(self):
        import alert_worker as worker
        repo = Mock()
        repo.process.return_value = 1
        ready = Mock()
        runner = worker.AlertWorker(repo, ready)
        with patch.object(worker, 'monotonic', return_value=10) as clock:
            for _ in range(worker.MAX_BATCHES_PER_BURST-1):
                self.assertEqual(runner.step(), 0)
            self.assertEqual(runner.step(), worker.YIELD_SECONDS)
            self.assertEqual(runner.step(), 0)
            clock.return_value += worker.BURST_SECONDS
            self.assertEqual(runner.step(), worker.YIELD_SECONDS)
            repo.process.return_value = 0
            self.assertEqual(runner.step(), worker.IDLE_SECONDS)
            repo.process.side_effect = RuntimeError()
            self.assertEqual(runner.step(), worker.RETRY_SECONDS)
            self.assertEqual(runner.failure, "RuntimeError")
            repo.process.side_effect = None
            repo.process.return_value = 1
            clock.return_value += 30
            self.assertEqual(runner.step(), 0)
            self.assertIsNone(runner.failure)
        self.assertEqual(ready.call_count, repo.process.call_count)


class ThresholdContract(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.started = self.now - timedelta(minutes=2)
        self.definition = dict(name='Views', event='video.views', video_type='short',
                               scope={'kind':'video', 'id':'s0000000001'}, threshold='100', direction='up')

    def test_measurements_and_boundaries(self):
        before_at = self.now - timedelta(minutes=3)
        for direction, old, new, expected in [
            ('up', 99, 100, True), ('up', 100, 101, False), ('up', 100, 100, False),
            ('down', 101, 100, True), ('down', 100, 99, False), ('up', 0, 100, True),
        ]:
            rule = AlertRule(**{**self.definition, 'direction':direction})
            self.assertEqual(crossed(rule, Measurement(old,before_at,'v'), Measurement(new,self.now,'v'),
                                     started_at=self.started, as_of=self.now), expected)

    def test_unknown_stale_and_future(self):
        rule = AlertRule(**self.definition)
        current = Measurement(101,self.now,'new')
        for previous in (None, Measurement(1,self.now-HOT_DATA_WINDOW,'new'),
                         Measurement(99,self.started,'old'), Measurement(99,self.now,'new')):
            self.assertFalse(crossed(rule,previous,current,started_at=self.started,as_of=self.now))
        self.assertFalse(crossed(rule,Measurement(1,self.started,'new'),current,
                                 started_at=self.now,as_of=self.now))

    def test_unsupported_conditions_and_large_thresholds(self):
        for change in ({'threshold':'9223372036854775808'}, {'threshold':9007199254740993},
                       {'video_type':'stream'}, {'event':'channel.subscribers'},
                       {'event':'video.published'}, {'min_duration':61,'max_duration':60},
                       {'direction':None}, {'min_duration':-1}, {'threshold':'01'}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                AlertRule(**{**self.definition, **change})
        self.assertEqual(AlertRule(**{**self.definition, 'threshold':'9223372036854775807'}).threshold,
                         '9223372036854775807')
