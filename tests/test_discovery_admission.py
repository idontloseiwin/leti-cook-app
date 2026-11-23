"""Durable admission failures remain fatal through catch-all provider clients."""
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from discovery_admission import DiscoveryAdmission, DiscoveryAdmissionDenied, DiscoveryAdmissionUnsafe
from youtube_errors import YouTubeDailyQuotaExceededError


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.budget=Mock()
        self.budget.reserve.return_value={'allowed':True}
        self.budget.begin_dispatch.return_value=True
        self.gate=DiscoveryAdmission(self.budget)
        self.provider=Mock(return_value=[])
    def test_binding_error_consumes_grant_but_never_sends_and_stays_fatal(self):
        with self.assertRaises(DiscoveryAdmissionUnsafe):
            self.gate.call('general',self.provider,before_dispatch=Mock(side_effect=OSError('unknown write')))
        with self.assertRaises(DiscoveryAdmissionUnsafe):self.gate.call('stats',self.provider)
        self.provider.assert_not_called();self.budget.reserve.assert_called_once()
    def test_quota_ban_failure_prevents_any_following_provider(self):
        self.provider.side_effect=YouTubeDailyQuotaExceededError('quota')
        self.budget.note_exhausted.side_effect=OSError('unknown ban commit')
        with self.assertRaises(DiscoveryAdmissionUnsafe):self.gate.call('general',self.provider)
        with self.assertRaises(DiscoveryAdmissionUnsafe):self.gate.call('stats',self.provider)
        self.assertEqual(self.provider.call_count,1)
        self.assertTrue(self.gate.failure.lic_http_attempted)
    def test_reserve_and_dispatch_unknown_fail_closed(self):
        for operation in ('reserve','begin_dispatch'):
            self.setUp();getattr(self.budget,operation).side_effect=OSError('db unavailable')
            with self.assertRaises(DiscoveryAdmissionUnsafe):self.gate.call('general',self.provider)
            self.provider.assert_not_called()
            self.assertFalse(self.gate.failure.lic_http_attempted)
    def test_known_budget_denial_does_not_become_unknown(self):
        self.budget.reserve.return_value={'allowed':False,'reason':'window_budget','next_try_at':None}
        with self.assertRaises(DiscoveryAdmissionDenied):self.gate.call('general',self.provider)
        self.gate.require_safe();self.provider.assert_not_called()
    def test_transient_provider_error_does_not_write_quota_ban(self):
        self.provider.side_effect=TimeoutError('temporary')
        with self.assertRaises(TimeoutError):self.gate.call('general',self.provider)
        self.budget.note_exhausted.assert_not_called();self.gate.require_safe()


if __name__=='__main__':unittest.main()
