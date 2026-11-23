"""Trusted, lossless settings upgrade of an owner's schedule file."""
from dataclasses import replace
import configparser
import hashlib
import io
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'application'))
import collection_schedule as schedules
SCHEDULE = 'config/collection-schedules.ini'
COMPATIBILITY = 'application/update-compatibility.json'
# A format-4 schedule file, as an installation on that format keeps it.
FORMAT4_SCHEDULE = 'tests/fixtures/schedule_format4_example.ini'


class DiscoveryScheduleUpgradeTests(unittest.TestCase):
    def test_published_template_and_metadata_are_the_new_install_profile(self):
        raw = (ROOT/SCHEDULE).read_bytes()
        value = schedules.parse_schedule_file(raw)
        self.assertEqual(value,schedules.discovery_schedules())
        self.assertEqual(json.loads((ROOT/COMPATIBILITY).read_bytes())['schedules_format_version'],5)
        for inventory in (value.long_inventory,value.short_inventory):
            self.assertEqual(inventory.found_new_seconds,86400)
            self.assertEqual(inventory.missing_playlist_seconds,86400)
            self.assertEqual(inventory.reconciliation_seconds,604800)

    def test_custom_v4_preserves_all_existing_bytes_except_version(self):
        raw = b'# Owner comments\r\n' + (ROOT/FORMAT4_SCHEDULE).read_bytes().replace(
            b'reconciliation_seconds = 604800',b'reconciliation_seconds = 1234').replace(
            b'age_0_1d_seconds = 3600',b'age_0_1d_seconds = 1800').replace(b'\r\n',b'\n').replace(b'\n',b'\r\n')
        before = schedules.parse_schedule_file(raw,use_shipped_defaults=False)
        result = schedules.upgrade_schedule_file_for_discovery(raw)
        self.assertTrue(result.startswith(raw.replace(b'version = 4',b'version = 5')))
        self.assertEqual(schedules.parse_schedule_file(result),replace(before,discovery=schedules.DiscoverySettings()))
        self.assertNotIn(b'\n',result.replace(b'\r\n',b''))
        self.assertEqual(schedules.upgrade_schedule_file_for_discovery(result),result)

    def test_unedited_shipped_templates_keep_the_intervals_in_effect(self):
        """Unedited public templates adopt the current defaults during migration."""
        paths = sorted((ROOT/'tests/fixtures').glob('collection_schedules_v*_*.ini'))
        self.assertEqual({hashlib.sha256(path.read_bytes()).hexdigest() for path in paths},
                         set(schedules.SHIPPED_DEFAULT_TEMPLATES))
        results = set()
        for path in paths:
            with self.subTest(path=path.name):
                raw = path.read_bytes()
                in_effect = schedules.parse_schedule_file(raw)
                self.assertEqual(in_effect,schedules.CollectionSchedules())
                self.assertNotEqual(schedules.parse_schedule_file(raw,use_shipped_defaults=False),in_effect)
                result = schedules.upgrade_schedule_file_for_discovery(raw)
                after = schedules.parse_schedule_file(result)
                self.assertEqual(after,replace(in_effect,discovery=schedules.DiscoverySettings()))
                self.assertEqual(after.cadence_snapshot(),in_effect.cadence_snapshot())
                self.assertEqual(schedules.parse_schedule_file(result,use_shipped_defaults=False),after)
                self.assertNotIn(hashlib.sha256(result).hexdigest(),schedules.SHIPPED_DEFAULT_TEMPLATES)
                self.assertEqual(schedules.upgrade_schedule_file_for_discovery(result),result)
                self.assertTrue(result.startswith(b'# Written by the update to schedule format 5.\n'))
                self.assertNotIn(b'\r',result)
                results.add(result)
        self.assertEqual(len(results),1)
        # The update does not switch on the new-install list profile.
        self.assertNotEqual(schedules.parse_schedule_file(results.pop()),schedules.discovery_schedules())

    def test_one_changed_byte_makes_an_earlier_default_an_owner_file(self):
        for path in sorted((ROOT/'tests/fixtures').glob('collection_schedules_v*_*.ini')):
            with self.subTest(path=path.name):
                raw = b'# mine\n'+path.read_bytes()
                before = schedules.parse_schedule_file(raw,use_shipped_defaults=False)
                self.assertEqual(schedules.parse_schedule_file(raw),before)
                result = schedules.upgrade_schedule_file_for_discovery(raw)
                self.assertEqual(schedules.parse_schedule_file(result),replace(before,discovery=schedules.DiscoverySettings()))
                self.assertTrue(result.startswith(raw.replace(b'version = 2',b'version = 5').replace(
                    b'version = 3',b'version = 5').replace(b'version = 4',b'version = 5')))


    def test_explicit_missing_intervals_survive_upgrade_to_v5(self):
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string((ROOT/FORMAT4_SCHEDULE).read_text())
        values = dict(long=172800, short=432000, stream=1814400)
        for kind, seconds in values.items():
            parser['schedule.'+kind+'.inventory']['missing_playlist_seconds'] = str(seconds)
        output = io.StringIO()
        parser.write(output)
        raw = b'# Explicit owner intervals\n' + output.getvalue().encode()
        before = schedules.parse_schedule_file(raw)
        upgraded = schedules.upgrade_schedule_file_for_discovery(raw)
        after = schedules.parse_schedule_file(upgraded)
        self.assertEqual(after, replace(before, discovery=schedules.DiscoverySettings()))
        self.assertTrue(upgraded.startswith(raw.replace(b'version = 4', b'version = 5')))
        for kind, seconds in values.items():
            self.assertEqual(getattr(after, kind+'_inventory').missing_playlist_seconds, seconds)

    def test_malformed_or_oversized_upgrade_fails(self):
        raw = (ROOT/FORMAT4_SCHEDULE).read_bytes()
        for value in (raw.replace(b'version = 4',b'version = 9'),raw+b'\n[unexpected]\nx=1\n',
                      raw+b'#'+b'x'*(schedules.MAX_SCHEDULE_BYTES-len(raw)-1)):
            with self.subTest(size=len(value)),self.assertRaises(schedules.ScheduleError):
                schedules.upgrade_schedule_file_for_discovery(value)

    def test_v2_and_v3_add_missing_sections_without_rewriting_existing_settings(self):
        for version in (2,3):
            parser = configparser.ConfigParser(interpolation=None)
            parser.read_string((ROOT/FORMAT4_SCHEDULE).read_text())
            parser['format']['version'] = str(version)
            retained = {section for section,_,_ in schedules._VERSION_SECTIONS[str(version)]} | {'format'}
            for section in tuple(parser.sections()):
                if section not in retained:
                    parser.remove_section(section)
            parser['schedule.long.counters']['age_0_1d_seconds'] = '1800'
            output = io.StringIO()
            parser.write(output)
            raw = b'# Old owner schedule\n'+output.getvalue().encode()
            before = schedules.parse_schedule_file(raw,use_shipped_defaults=False)
            upgraded = schedules.upgrade_schedule_file_for_discovery(raw)
            self.assertTrue(upgraded.startswith(raw.replace(('version = '+str(version)).encode(),b'version = 5')))
            self.assertEqual(schedules.parse_schedule_file(upgraded),replace(before,discovery=schedules.DiscoverySettings()))


if __name__ == '__main__':
    unittest.main()
