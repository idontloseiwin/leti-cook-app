"""The quota reset is sent as a moment, not as Kyiv wall-clock time (tester finding T-18). No server."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
from http_test_support import endpoint, system_http
from unittest.mock import patch
from types import SimpleNamespace
SOURCE = (ROOT / 'application/web/system.py').read_text(encoding='utf-8')
PT = ZoneInfo('America/Los_Angeles')


class QuotaResetTests(unittest.TestCase):
    def test_the_reset_is_the_next_pacific_midnight_in_utc(self):
        ledger = {'warnings': [], 'units_used': 0, 'read_status': 'ok', 'bucket_units': {},
                  'ledger_attempts': 0, 'ledger_truncated': False, 'read_model_source': 'test'}
        with patch('web.system.load_quota_read_model', return_value=ledger), \
             patch('web.system.SyncRunRepository', return_value=SimpleNamespace(list_recent=lambda **kw: [])), \
             patch('web.system.pipeline_health_env_flag', return_value=False), \
             patch('self_hosted_configuration.key_refusal', return_value=None), \
             patch('self_hosted_configuration.collection_paused', return_value=False):
            quota = endpoint(system_http(), 'system_summary')()['quota']
        reset = datetime.fromisoformat(quota['reset_at'])
        self.assertEqual(reset.utcoffset(), timedelta(0))
        local = reset.astimezone(PT)
        self.assertEqual((local.hour, local.minute), (0, 0))
        self.assertTrue(timedelta(0) < reset - datetime.now(timezone.utc) <= timedelta(hours=25))

    def test_no_fixed_city_time_zone_is_left(self):
        self.assertNotIn('Kyiv', SOURCE)
        self.assertNotIn('reset_at_kyiv', SOURCE)


if __name__ == '__main__':
    unittest.main()
