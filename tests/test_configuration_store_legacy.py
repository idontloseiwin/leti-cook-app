"""Current encrypted settings and refusal of old limits; no provider or database."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
from self_hosted_configuration import ConfigurationStore, STARTER_CHANNEL_URLS
from self_hosted_settings import SettingsError, browser_settings, preset, settings_ini

KEY = 'synthetic_legacy_key_no_requests'


def current_settings():
    return {'preset': 'custom', 'limits': preset('test', {'inventory_page_budget': 19}),
            'automatic_collection': False, 'interval_minutes': 15,
            'manual_stats_quota_units': 321, 'handoff_grace_seconds': 87}


def legacy_settings(version=1):
    value = current_settings()
    value['limits']['operator_max_items'] = 123
    if version == 1:
        value['limits'].pop('inventory_page_budget')
        value['limits'].update(channel_limit=2, typed_inventory_limit=7)
    return value


class LegacyConfigurationStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.directory.chmod(0o700)
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.key = Fernet.generate_key()
        self.write_bytes(self.directory / 'settings.key', self.key)
        self.workspace = {'phase': 'loading', 'channels': {
            sorted(STARTER_CHANNEL_URLS)[0]: 'UC' + '7' * 22}}

    def write_bytes(self, path, value):
        path.write_bytes(value)
        path.chmod(0o600)

    def record(self, settings=None, version=1, workspace=True):
        value = {'version': version, 'revision': 8, 'api_key': KEY,
                 'prepared': True, 'phase': 'ready', 'error': None}
        if version == 1:
            value['settings'] = current_settings() if settings is None else settings
        if workspace:
            value['workspace'] = copy.deepcopy(self.workspace)
        self.write_bytes(self.directory / 'settings.enc',
                         Fernet(self.key).encrypt(json.dumps(value).encode()))
        return value

    def managed(self, settings=None, path=None):
        path = path or self.directory / 'config.ini'
        self.write_bytes(path, settings_ini(settings or current_settings()).encode())
        return path

    def store(self):
        return ConfigurationStore(self.directory)

    def test_old_limit_formats_are_rejected_without_rewriting_owner_state(self):
        for version in (1, 2):
            with self.subTest(settings_format=version):
                self.record(legacy_settings(version))
                before = {path.name: path.read_bytes() for path in self.directory.iterdir()}
                with self.assertRaisesRegex(SettingsError, '^configuration_unreadable$'):
                    self.store().read()
                self.assertEqual({path.name: path.read_bytes() for path in self.directory.iterdir()}, before)

    def test_current_embedded_settings_are_validated_without_legacy_sanitizing(self):
        self.record(current_settings())
        self.assertEqual(self.store().read()['settings'], browser_settings(current_settings()))
        invalid = current_settings()
        invalid['limits']['inventory_page_budget'] = True
        self.record(invalid)
        with self.assertRaisesRegex(SettingsError, '^configuration_unreadable$'):
            self.store().read()

    def test_previous_standard_embedded_limits_survive_a_new_default(self):
        for stats_limit in (6000, 20000):
            with self.subTest(inventory_page_budget=6000, stats_limit=stats_limit):
                settings = current_settings()
                settings.update(preset='standard', limits=preset('standard', {
                    'inventory_page_budget': 6000, 'stats_limit': stats_limit,
                }))
                self.record(settings)
                before = {path.name: path.read_bytes() for path in self.directory.iterdir()}
                store = self.store()
                actual = store.read()['settings']
                self.assertEqual(actual['preset'], 'custom')
                self.assertEqual(actual['limits'], settings['limits'])
                self.assertEqual({path.name: path.read_bytes() for path in self.directory.iterdir()}, before)
                store.update(8, lambda value: value.update(phase='saved', prepared=False))
                self.assertEqual(self.store().read()['settings']['limits'], settings['limits'])
                self.assertFalse(store.settings_path.exists())

    def test_existing_managed_file_overrides_embedded_snapshot(self):
        self.record(legacy_settings())
        self.managed()
        self.assertEqual(self.store().read()['settings'], browser_settings(current_settings()))
        self.assertFalse(self.store().read()['settings']['automatic_collection'])

    def test_explicit_managed_path_wins_and_missing_file_never_falls_back(self):
        self.record(legacy_settings())
        self.managed()
        selected = self.directory / 'selected.ini'
        settings = current_settings()
        settings['limits']['inventory_page_budget'] = 23
        self.managed(settings, selected)
        with patch.dict(os.environ, {'LIC_SETTINGS_FILE': str(selected)}):
            store = self.store()
        self.assertEqual(store.read()['settings']['limits']['inventory_page_budget'], 23)
        selected.unlink()
        with self.assertRaises(FileNotFoundError):
            store.read()

    def test_invalid_current_managed_file_never_uses_valid_encrypted_fallback(self):
        valid = settings_ini(current_settings()).encode()
        cases = (valid + b'operator_max_items = 123\n',
                 valid + b'channel_limit = 2\ntyped_inventory_limit = 2\n',
                 valid.replace(b'inventory_page_budget = 19', b'inventory_page_budget = nope'),
                 valid.replace(b'version = 3', b'version = 2'))
        for version in (1, 2):
            for raw in cases:
                with self.subTest(record_version=version, invalid_case=cases.index(raw)):
                    self.record(current_settings(), version=version)
                    self.write_bytes(self.directory / 'config.ini', raw)
                    with self.assertRaisesRegex(SettingsError, '^configuration_unreadable$'):
                        self.store().read()

    def test_invalid_legacy_shapes_types_and_ambiguous_limits_fail_closed(self):
        cases = []
        for updates in ({'channel_limit': 0}, {'channel_limit': True}, {'channel_limit': '2'},
                        {'operator_max_items': 0}, {'operator_max_items': 20},
                        {'inventory_page_budget': 12}, {'unknown': 3}):
            value = legacy_settings()
            value['limits'].update(updates)
            cases.append(value)
        value = legacy_settings()
        value['interval_minutes'] = '15'
        cases.append(value)
        value = legacy_settings()
        value['preset'] = 'standard'
        cases.append(value)
        value = legacy_settings()
        value['unknown'] = 'ignored'
        cases.append(value)
        for index, value in enumerate(cases):
            with self.subTest(invalid_case=index):
                self.record(value)
                with self.assertRaisesRegex(SettingsError, '^configuration_unreadable$'):
                    self.store().read()

    def test_update_keeps_key_workspace_and_managed_file_and_advances_revision(self):
        self.record(legacy_settings())
        path = self.managed()
        original_config = path.read_bytes()
        store = self.store()
        result = store.update(8, lambda value: value.update(error='reference_unavailable'))
        self.assertEqual((result['revision'], result['api_key'], result['workspace']),
                         (9, KEY, self.workspace))
        self.assertEqual(path.read_bytes(), original_config)
        self.assertEqual(store.key_path.read_bytes(), self.key)
        self.assertNotIn(KEY.encode(), store.path.read_bytes())
        restarted = self.store().read()
        self.assertEqual(restarted['settings'], browser_settings(current_settings()))
        self.assertEqual(restarted['error'], 'reference_unavailable')

    def test_embedded_only_owner_update_preserves_current_settings_without_creating_ini(self):
        self.record()
        store = self.store()
        result = store.update(8, lambda value: value.update(phase='saved', prepared=False))
        self.assertFalse(store.settings_path.exists())
        self.assertEqual(self.store().read(), result)
        self.assertEqual(store.key_path.read_bytes(), self.key)

    def test_settings_mutation_and_stale_revision_remain_rejected(self):
        self.record()
        store = self.store()
        original = store.path.read_bytes()
        with self.assertRaisesRegex(SettingsError, '^settings_are_managed_in_config_file$'):
            store.update(8, lambda value: value['settings'].update(automatic_collection=True))
        with self.assertRaisesRegex(SettingsError, '^configuration_changed_reload$'):
            store.update(7, lambda value: value.update(phase='saved'))
        self.assertEqual(store.path.read_bytes(), original)

    def test_old_workspace_default_and_modern_record_contract_remain_intact(self):
        self.record(workspace=False)
        self.assertEqual(self.store().read()['workspace'], {'phase': 'complete', 'channels': {}})
        self.record(version=2)
        with self.assertRaises(FileNotFoundError):
            self.store().read()
        self.managed()
        self.assertEqual(self.store().read()['settings'], browser_settings(current_settings()))


if __name__ == '__main__':
    unittest.main()
