"""Current-format parsing and settings contracts; no Docker or provider calls."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'application'))
import config_migrations as migration

CURRENT = b"""# Keep this comment
[format]
version = 3
[collection]
automatic_collection = false
interval_minutes = 30
manual_stats_quota_units = 200
[limits]
metadata_limit = 2
inventory_page_budget = 12
stats_limit = 100
new_video_stats_limit = 100
operator_chunk_size = 50
inventory_pages_per_chunk = 2
drain_max_chunks = 16
drain_max_seconds = 240
# Trailing comment
"""


class CurrentFormatParsingTests(unittest.TestCase):
    def test_invalid_input_type_and_size_are_rejected(self):
        from self_hosted_settings import read_settings_bytes, SettingsError
        for raw in (None, 'not bytes', bytearray(CURRENT), CURRENT + b'x' * migration.MAX_CONFIG_BYTES):
            with self.subTest(kind=type(raw).__name__):
                with self.assertRaisesRegex(migration.ConfigMigrationError, '^invalid_config_size$'):
                    migration.parse_config_bytes(raw)
                with self.assertRaisesRegex(SettingsError, '^invalid_config_size$'):
                    read_settings_bytes(raw)
        at_limit = CURRENT + b'#' + b'x' * (migration.MAX_CONFIG_BYTES - len(CURRENT) - 1)
        self.assertEqual(len(at_limit), migration.MAX_CONFIG_BYTES)
        self.assertEqual(read_settings_bytes(at_limit), read_settings_bytes(CURRENT))

    def test_invalid_encoding_duplicates_and_structure_are_rejected(self):
        from self_hosted_settings import read_settings_bytes, SettingsError
        invalid = (
            CURRENT + b'\xff',
            CURRENT + b'inventory_page_budget = 3\n',
            CURRENT + b'INVENTORY_PAGE_BUDGET = 3\n',
            CURRENT + b'[limits]\ninventory_page_budget = 3\n',
            b'no_section = value\n' + CURRENT,
            CURRENT + b'not an option\n',
        )
        for index, raw in enumerate(invalid):
            with self.subTest(case=index):
                with self.assertRaisesRegex(migration.ConfigMigrationError, '^invalid_config_format$'):
                    migration.parse_config_bytes(raw)
                with self.assertRaisesRegex(SettingsError, '^invalid_config_format$'):
                    read_settings_bytes(raw)

    def test_required_sections_and_defaults_are_rejected(self):
        from self_hosted_settings import read_settings_bytes, SettingsError
        invalid = (
            b'',
            CURRENT.replace(b'[collection]', b'[unrelated]'),
            CURRENT.replace(b'[limits]', b'[unrelated]'),
            b'[DEFAULT]\nkey = value\n' + CURRENT,
        )
        for index, raw in enumerate(invalid):
            with self.subTest(case=index):
                with self.assertRaisesRegex(migration.ConfigMigrationError, '^invalid_config_sections$'):
                    migration.parse_config_bytes(raw)
                with self.assertRaisesRegex(SettingsError, '^invalid_config_sections$'):
                    read_settings_bytes(raw)

    def test_current_version_is_required_without_conversion(self):
        from self_hosted_settings import read_settings_bytes, SettingsError
        self.assertEqual(migration.config_version(migration.parse_config_bytes(CURRENT)), migration.CONFIG_VERSION)
        for version in (b'1', b'2'):
            raw = CURRENT.replace(b'version = 3', b'version = ' + version)
            self.assertEqual(migration.config_version(migration.parse_config_bytes(raw)), int(version))
            with self.assertRaisesRegex(SettingsError, '^unsupported_config_version$'):
                read_settings_bytes(raw)
        for version in (b'', b'0', b'4', b'3.0', b'invalid'):
            raw = CURRENT.replace(b'version = 3', b'version = ' + version)
            with self.subTest(version=version):
                with self.assertRaisesRegex(migration.ConfigMigrationError, '^unsupported_config_version$'):
                    migration.config_version(migration.parse_config_bytes(raw))
                with self.assertRaisesRegex(SettingsError, '^unsupported_config_version$'):
                    read_settings_bytes(raw)

    def test_crlf_and_final_line_without_newline(self):
        from self_hosted_settings import read_settings_bytes
        expected = read_settings_bytes(CURRENT)
        for raw in (CURRENT.replace(b'\n', b'\r\n'), CURRENT.rstrip(b'\n')):
            self.assertEqual(read_settings_bytes(raw), expected)

    def test_indented_options_keep_their_section_semantics(self):
        from self_hosted_settings import read_settings_bytes
        raw = CURRENT.replace(b'version = 3\n', b'version = 3\nextension = keep\n')
        raw = b'\n'.join(b'    ' + line if b'=' in line else line for line in raw.split(b'\n'))
        parser = migration.parse_config_bytes(raw)
        self.assertEqual(parser.get('format', 'extension'), 'keep')
        self.assertEqual(read_settings_bytes(raw), read_settings_bytes(CURRENT))

    def test_current_format_rejects_active_retired_keys_but_permits_comments(self):
        from self_hosted_settings import read_settings_bytes, SettingsError
        for name, code in ((b'channel_limit', 'mixed_inventory_config'),
                           (b'typed_inventory_limit', 'mixed_inventory_config'),
                           (b'operator_max_items', 'retired_operator_max_items')):
            for value in (b'0', b'1', b'invalid obsolete value'):
                with self.subTest(name=name, value=value), self.assertRaisesRegex(SettingsError, '^' + code + '$'):
                    read_settings_bytes(CURRENT.replace(b'[limits]\n', b'[limits]\n' + name + b' = ' + value + b'\n'))
            self.assertEqual(read_settings_bytes(CURRENT + b'# ' + name + b' = 1\n'), read_settings_bytes(CURRENT))

    def test_inventory_page_budget_bounds_and_types_remain_strict(self):
        from self_hosted_settings import read_settings_bytes, browser_settings, SettingsError
        base = read_settings_bytes(CURRENT)
        for budget in (0, 12, 6000, 100000):
            raw = CURRENT.replace(b'inventory_page_budget = 12', b'inventory_page_budget = ' + str(budget).encode())
            self.assertEqual(read_settings_bytes(raw)['limits']['inventory_page_budget'], budget)
        for invalid in (True, False, -1, 100001, 1.5, '2', None):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(SettingsError, '^invalid_inventory_page_budget$'):
                browser_settings({**base, 'limits': {**base['limits'], 'inventory_page_budget': invalid}})
        for invalid in (b'-1', b'100001', b'1.5', b'true', b'nope'):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(SettingsError, '^invalid_inventory_page_budget$'):
                read_settings_bytes(CURRENT.replace(b'inventory_page_budget = 12', b'inventory_page_budget = ' + invalid))


class RuntimeValidationTests(unittest.TestCase):
    LEGACY_COMPLETE = b"""[collection]
automatic_collection = false
interval_minutes = 30
manual_stats_quota_units = 200
[limits]
metadata_limit = 2
channel_limit = 2
typed_inventory_limit = 2
stats_limit = 100
new_video_stats_limit = 100
operator_max_items = 100
operator_chunk_size = 50
inventory_pages_per_chunk = 2
drain_max_chunks = 16
drain_max_seconds = 240
"""

    def test_current_format_uses_real_runtime_validator_and_environment(self):
        from self_hosted_settings import read_settings_bytes, browser_environment
        settings = read_settings_bytes(CURRENT)
        self.assertFalse(settings['automatic_collection'])
        self.assertEqual(settings['limits']['inventory_page_budget'], 12)
        self.assertNotIn('operator_max_items', settings['limits'])
        self.assertEqual(settings['limits']['operator_chunk_size'], 50)
        self.assertEqual(settings['manual_stats_quota_units'], 200)
        environment = browser_environment(settings)
        self.assertEqual(environment['LIC_AUTO_SYNC_INVENTORY_PAGE_BUDGET'], '12')
        self.assertNotIn('LIC_AUTO_SYNC_CHANNEL_LIMIT', environment)
        self.assertNotIn('LIC_AUTO_SYNC_TYPED_INVENTORY_LIMIT', environment)
        self.assertNotIn('OPERATOR_JOB_MAX_ITEMS', environment)
        self.assertEqual(environment['OPERATOR_JOB_MAX_QUOTA_UNITS'], '200')

    def test_initial_concurrency_defaults_and_custom_value_current_format(self):
        from self_hosted_settings import read_settings_bytes, browser_environment
        for count in (None, 1, 8):
            raw = CURRENT
            if count is not None:
                raw = raw.replace(b'[collection]\n', b'[collection]\ninitial_load_concurrency = ' + str(count).encode() + b'\n')
            settings = read_settings_bytes(raw)
            self.assertEqual(settings['youtube_request_concurrency'], count or 8)
            self.assertNotIn('initial_load_concurrency', settings)
            self.assertEqual(browser_environment(settings)['LIC_YOUTUBE_REQUEST_CONCURRENCY'], str(count or 8))

    def test_initial_concurrency_bounds_and_worker_snapshot(self):
        from self_hosted_settings import read_settings_bytes, browser_settings, worker_environment, SettingsError
        default = (Path(__file__).resolve().parents[1] / 'config/config.ini').read_bytes()
        base = read_settings_bytes(default)
        self.assertEqual(base['youtube_request_concurrency'], 8)
        old = default.replace(b'youtube_request_concurrency = 8\n', b'')
        self.assertEqual(read_settings_bytes(old)['youtube_request_concurrency'], 8)
        for count in (1, 8, 16, 32, 50):
            self.assertEqual(browser_settings({**base, 'youtube_request_concurrency': count})['youtube_request_concurrency'], count)
        for invalid in (0, 51, True, '2', 1.5):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(SettingsError, 'invalid_youtube_request_concurrency'):
                browser_settings({**base, 'youtube_request_concurrency': invalid})
        for invalid in (b'0', b'51', b'true', b'1.5'):
            with self.assertRaisesRegex(SettingsError, 'invalid_youtube_request_concurrency'):
                read_settings_bytes(default.replace(b'youtube_request_concurrency = 8', b'youtube_request_concurrency = ' + invalid))
        changed = {**base, 'youtube_request_concurrency': 1}
        captured = {'LIC_COLLECTION_SUPERVISED': '1', 'LIC_YOUTUBE_REQUEST_CONCURRENCY': '8'}
        self.assertEqual(worker_environment(changed, captured)['LIC_YOUTUBE_REQUEST_CONCURRENCY'], '8')
        self.assertEqual(worker_environment(changed, {})['LIC_YOUTUBE_REQUEST_CONCURRENCY'], '1')
        for invalid in ('', '0', '51', '1.0'):
            with self.assertRaisesRegex(SettingsError, 'youtube_request_concurrency_snapshot_required'):
                worker_environment(changed, {**captured, 'LIC_YOUTUBE_REQUEST_CONCURRENCY': invalid})

    def test_legacy_concurrency_is_only_an_input_alias(self):
        from self_hosted_settings import read_settings_bytes, settings_ini, SettingsError
        for count in (1, 8):
            raw = CURRENT.replace(b'[collection]\n', b'[collection]\ninitial_load_concurrency = ' + str(count).encode() + b'\n')
            value = read_settings_bytes(raw)
            self.assertEqual(value['youtube_request_concurrency'], count)
            self.assertNotIn('initial_load_concurrency', settings_ini(value))
            with self.assertRaisesRegex(SettingsError, 'conflicting_request_concurrency'):
                read_settings_bytes(raw.replace(b'[collection]\n', b'[collection]\nyoutube_request_concurrency = 8\n'))
        with self.assertRaisesRegex(SettingsError, 'invalid_initial_load_concurrency'):
            read_settings_bytes(CURRENT.replace(b'[collection]\n', b'[collection]\ninitial_load_concurrency = 9\n'))

    def test_real_validator_rejects_invalid_unrelated_limit(self):
        from self_hosted_settings import read_settings_bytes, SettingsError
        bad = CURRENT.replace(b'\nstats_limit = 100\n', b'\nstats_limit = 0\n')
        with self.assertRaisesRegex(SettingsError, '^invalid_stats_limit$'):
            read_settings_bytes(bad)

    def test_unknown_options_survive_but_do_not_become_runtime_limits(self):
        from self_hosted_settings import read_settings_bytes
        extra = (b'future_limit = keep this\n'
                 b'    channel_limit = 999\n'
                 b'    operator_max_items = text\n'
                 b'    [not_a_section]\n'
                 b'[future]\ncustom = 100% untouched\noperator_max_items = keep unknown\n')
        parser = migration.parse_config_bytes(CURRENT + extra)
        self.assertEqual(parser.get('limits', 'future_limit'),
                         'keep this\nchannel_limit = 999\noperator_max_items = text\n[not_a_section]')
        self.assertEqual(parser.get('future', 'custom'), '100% untouched')
        self.assertEqual(parser.get('future', 'operator_max_items'), 'keep unknown')
        settings = read_settings_bytes(CURRENT + extra)
        self.assertEqual(settings, read_settings_bytes(CURRENT))
        self.assertNotIn('future_limit', settings['limits'])

    def test_chunk_bounds_and_manual_statistics_bound_remain_strict(self):
        from self_hosted_settings import preset, browser_settings, SettingsError
        for chunk in (1, 250):
            self.assertEqual(preset('standard', {'operator_chunk_size': chunk})['operator_chunk_size'], chunk)
        for chunk in (0, 251, True):
            with self.assertRaisesRegex(SettingsError, '^invalid_operator_chunk_size$'):
                preset('standard', {'operator_chunk_size': chunk})
        base = {'preset':'standard', 'limits':preset('standard'), 'automatic_collection':False,
                'interval_minutes':30, 'manual_stats_quota_units':200}
        for units in (1, 1000000):
            self.assertEqual(browser_settings({**base, 'manual_stats_quota_units':units})['manual_stats_quota_units'], units)
        for units in (0, 1000001, True):
            with self.assertRaisesRegex(SettingsError, '^invalid_manual_stats_quota_units$'):
                browser_settings({**base, 'manual_stats_quota_units':units})

    def test_current_runtime_rejects_active_cap_and_default_matches_generator(self):
        from self_hosted_settings import read_settings_bytes, settings_ini, SettingsError
        default = (Path(__file__).resolve().parents[1] / 'config/config.ini').read_bytes()
        settings = read_settings_bytes(default)
        self.assertEqual(settings_ini(settings).encode(), default)
        with self.assertRaisesRegex(SettingsError, '^retired_operator_max_items$'):
            read_settings_bytes(default.replace(b'[limits]\n', b'[limits]\noperator_max_items = 10000\n'))
        self.assertEqual(read_settings_bytes(default + b'# operator_max_items = 10000\n'), settings)

    def test_old_file_format_is_refused_without_a_silent_rewrite(self):
        from self_hosted_settings import read_settings_bytes, SettingsError
        with self.assertRaisesRegex(SettingsError, '^unsupported_config_version$'):
            read_settings_bytes(self.LEGACY_COMPLETE)


if __name__ == '__main__':
    unittest.main()
