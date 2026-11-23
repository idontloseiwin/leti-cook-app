"""The inventory page budget replaces the two old CLI dimensions explicitly."""
import contextlib
import io
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'application'))
import auto_sync
from self_hosted_settings import collection_environment, SettingsError


class InventorySettingsTests(unittest.TestCase):
    def args(self, arguments, env=None):
        with patch.dict(os.environ,env or {},clear=True), patch.object(auto_sync,'selected_deployment_profile',return_value='self-hosted'):
            return auto_sync.parse_args(arguments)

    def test_standard_and_test_settings_use_one_budget(self):
        self.assertEqual(self.args([]).inventory_page_budget,20000)
        self.assertEqual(self.args([],collection_environment('test')).inventory_page_budget,12)

    def test_explicit_new_budget_including_zero(self):
        for value in (0,1,2,501,1001,100000):
            args=self.args(['--inventory-page-budget',str(value)])
            self.assertEqual(args.inventory_page_budget,value)
            self.assertEqual(args.metadata_limit,1000)

    def test_retired_inventory_flags_are_rejected(self):
        for value in ('0', '2'):
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                self.args(['--channel-limit', value, '--typed-inventory-limit', value])
            self.assertEqual(raised.exception.code, 2)

    def test_ambiguous_mixed_or_selectively_disabled_legacy_limits_fail(self):
        cases=[['--channel-limit','2'],['--typed-inventory-limit','2'],['--inventory-page-budget','12','--channel-limit','2','--typed-inventory-limit','2'],['--channel-limit','0','--typed-inventory-limit','2']]
        for args in cases:
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                self.args(args)
            self.assertEqual(raised.exception.code,2)

    def test_out_of_range_budget_fails(self):
        for value in (-1,100001):
            with self.assertRaisesRegex(SettingsError,'invalid_inventory_page_budget'):
                self.args(['--inventory-page-budget',str(value)])

    def test_dry_run_uses_new_shape_without_starting_runtime(self):
        args=self.args(['--inventory-page-budget','1','--dry-run'])
        with patch.object(auto_sync,'parse_args',return_value=args), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(auto_sync.main(),0)
        self.assertIn('no sync was run',output.getvalue())


if __name__=='__main__':
    unittest.main()
