"""Format-5 defaults preserve format-4 settings and trusted updater boundaries."""
import configparser
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
import collection_schedule as schedule
from collection_schedule_model import InventorySchedule, MAX_FULL_COVERAGE_SECONDS, MAX_FULL_COVERAGE_DAYS
from discovery_policy import DiscoverySettings

TEMPLATE = (ROOT / 'tests/fixtures/schedule_format4_example.ini').read_bytes()
PUBLISHED_TEMPLATE = (ROOT / 'config/collection-schedules.ini').read_bytes()


def ini_edit(raw, section, key, value):
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(raw.decode())
    if value is None:
        parser.remove_option(section, key)
    else:
        parser[section][key] = value
    output = io.StringIO()
    parser.write(output)
    return output.getvalue().encode()


class DiscoveryFileTests(unittest.TestCase):
    def test_format4_template_remains_v4_without_discovery_settings(self):
        value = schedule.parse_schedule_file(TEMPLATE)
        self.assertIsNone(value.discovery)
        self.assertEqual(value.long_inventory.reconciliation_seconds, 21 * 86400)
        self.assertEqual(value.short_inventory.found_new_seconds, 3600)
        self.assertEqual(json.loads(schedule.serialize_snapshot(value))['format'], 4)
        self.assertIn(b'version = 4', TEMPLATE)
        self.assertNotIn(b'[discovery]', TEMPLATE)

    def test_new_profile_is_explicit_independent_and_does_not_change_stream_defaults(self):
        value = schedule.discovery_schedules()
        self.assertEqual(schedule.parse_schedule_file(PUBLISHED_TEMPLATE),value)
        self.assertEqual(json.loads((ROOT/'application/update-compatibility.json').read_bytes())[
            'schedules_format_version'],5)
        old = schedule.CollectionSchedules()
        self.assertEqual(value.discovery, DiscoverySettings())
        self.assertEqual(value.discovery.activation_grace_seconds,172800)
        for kind in ('long', 'short'):
            inventory = getattr(value, kind + '_inventory')
            self.assertEqual((inventory.reconciliation_seconds, inventory.missing_playlist_seconds),
                             (7 * 86400, 86400))
            self.assertEqual(inventory.found_new_seconds, 86400)
            self.assertEqual(inventory.quiet_1_seconds, 86400)
            self.assertEqual(inventory.quiet_6_plus_seconds, 86400)
            self.assertEqual(inventory.discovery_continuation_seconds,
                             getattr(old, kind + '_inventory').discovery_continuation_seconds)
        self.assertEqual(value.stream_inventory, old.stream_inventory)
        self.assertEqual(value.cadence_snapshot().rules, old.cadence_snapshot().rules)
        raw = schedule.serialize_schedule_file(value)
        self.assertIn(b'version = 5', raw)
        self.assertIn(b'full_coverage_max_age_days = 7', raw)
        self.assertEqual(schedule.parse_schedule_file(raw), value)

    def test_activation_grace_is_separate_roundtripped_seconds_not_inventory_period(self):
        value=schedule.discovery_schedules()
        changed=replace(value,discovery=replace(value.discovery,activation_grace_seconds=93717))
        self.assertEqual(changed.long_inventory.reconciliation_seconds,7*86400)
        self.assertEqual(changed.short_inventory.missing_playlist_seconds,86400)
        raw=schedule.serialize_schedule_file(changed)
        self.assertIn(b'activation_grace_seconds = 93717',raw)
        self.assertEqual(schedule.parse_schedule_file(raw),changed)
        snapshot=schedule.serialize_snapshot(changed)
        self.assertEqual(schedule.deserialize_snapshot(snapshot,hashlib.sha256(snapshot).hexdigest()),changed)
        for bad in ('0','-1','true','2.5','2147483648'):
            with self.subTest(bad=bad),self.assertRaises(schedule.ScheduleError):
                schedule.parse_schedule_file(ini_edit(raw,'discovery','activation_grace_seconds',bad))

    def test_new_days_only_expand_full_coverage_not_other_intervals(self):
        raw = schedule.serialize_schedule_file(schedule.discovery_schedules())
        for days in (1, 7, 28, 29, 30, MAX_FULL_COVERAGE_DAYS):
            with self.subTest(days=days):
                changed = schedule.parse_schedule_file(ini_edit(
                    raw, 'schedule.long.inventory', 'full_coverage_max_age_days', str(days)))
                self.assertEqual(changed.long_inventory.reconciliation_seconds, days * 86400)
                self.assertEqual(changed.short_inventory.reconciliation_seconds, 7 * 86400)
                self.assertEqual(schedule.parse_schedule_file(schedule.serialize_schedule_file(changed)), changed)
        with self.assertRaises(schedule.ScheduleError):
            schedule.parse_schedule_file(ini_edit(raw, 'schedule.long.inventory', 'found_new_seconds', '2419201'))

    def test_days_reject_zero_fractional_sign_boolean_overflow_and_two_owners(self):
        raw = schedule.serialize_schedule_file(schedule.discovery_schedules())
        for bad in ('0', '-1', '7.5', 'true', '+7', '24_855', '24856', '2147483647', '\u0667'):
            with self.subTest(value=bad), self.assertRaises(schedule.ScheduleError):
                schedule.parse_schedule_file(ini_edit(raw, 'schedule.long.inventory',
                                                      'full_coverage_max_age_days', bad))
        both = ini_edit(raw, 'schedule.long.inventory', 'reconciliation_seconds', '1234')
        with self.assertRaisesRegex(schedule.ScheduleError, '^schedule_fields_invalid$'):
            schedule.parse_schedule_file(both)

    def test_old_seconds_survive_export_and_explicit_v5_opt_in_without_rounding(self):
        for seconds in (1, 1234, 1814400, 2419200):
            old = schedule.parse_schedule_file(ini_edit(TEMPLATE, 'schedule.long.inventory',
                                                       'reconciliation_seconds', str(seconds)))
            self.assertIsNone(old.discovery)
            self.assertEqual(old.long_inventory.reconciliation_seconds, seconds)
            opted_in = replace(old, discovery=DiscoverySettings())
            for value in (old, opted_in):
                self.assertEqual(schedule.parse_schedule_file(schedule.serialize_schedule_file(value)), value)
            if seconds == 1234:
                self.assertIn(b'reconciliation_seconds = 1234', schedule.serialize_schedule_file(opted_in))
        with self.assertRaises(schedule.ScheduleError):
            schedule.parse_schedule_file(ini_edit(TEMPLATE, 'schedule.long.inventory',
                                                  'reconciliation_seconds', '2419201'))

    def test_legacy_v2_v3_remain_closed_and_extend_only_legacy_defaults(self):
        for version, sections in ((2, schedule._V2_SECTIONS), (3, schedule._V3_SECTIONS)):
            parser = configparser.ConfigParser(interpolation=None)
            parser.read_string(TEMPLATE.decode())
            parser['format']['version'] = str(version)
            allowed = {'format', *(item[0] for item in sections)}
            for section in tuple(parser.sections()):
                if section not in allowed:
                    parser.remove_section(section)
            parser['schedule.long.counters']['age_0_1d_seconds'] = '1234'
            output = io.StringIO()
            parser.write(output)
            value = schedule.parse_schedule_file(output.getvalue().encode())
            self.assertEqual(value.long_counters.age_0_1d_seconds, 1234)
            self.assertEqual(value.long_inventory.reconciliation_seconds, 1814400)
            self.assertIsNone(value.discovery)

    def test_updater_check_is_not_bypassed_by_accepting_new_template(self):
        # Format 4 stays readable by older updaters. Changing its marker alone
        # cannot supply the required v5 registry or bypass the trusted upgrade.
        self.assertEqual(schedule.FORMAT_VERSION, 4)
        with self.assertRaises(schedule.ScheduleError):
            schedule.parse_schedule_file(TEMPLATE.replace(b'version = 4', b'version = 5'))
        bad_legacy = ini_edit(TEMPLATE, 'schedule.long.inventory', 'full_coverage_max_age_days', '7')
        with self.assertRaises(schedule.ScheduleError):
            schedule.parse_schedule_file(bad_legacy)

    def test_discovery_registry_is_complete_closed_and_bounded(self):
        raw = schedule.serialize_schedule_file(schedule.discovery_schedules())
        for key, bad in (('events_general_window_limit', '2501'),
                         ('events_stats_daily_limit', '0'), ('retry_first_seconds', 'true'),
                         ('full_general_daily_limit', '1'), ('repair_target_limit', '1000001'),
                         ('technical_retry_first_seconds','21601'),
                         ('technical_retry_max_seconds','119'),
                         ('review_retry_seconds','21599'), ('unresolved_pause_seconds','604800'),
                         ('retry_sparse_seconds', '1'), ('unknown', '10')):
            with self.subTest(key=key), self.assertRaises(schedule.ScheduleError):
                schedule.parse_schedule_file(ini_edit(raw, 'discovery', key, bad))
        with self.assertRaises(schedule.ScheduleError):
            schedule.parse_schedule_file(ini_edit(raw, 'discovery', 'repair_stats_limit', None))

    def test_technical_review_and_pause_are_explicit_serialized_settings(self):
        value=schedule.discovery_schedules()
        changed=replace(value,discovery=replace(value.discovery,
            technical_retry_first_seconds=45,technical_retry_max_seconds=7200,
            review_retry_seconds=172800,unresolved_pause_seconds=5184000))
        raw=schedule.serialize_schedule_file(changed)
        for key,value in (('technical_retry_first_seconds',45),('technical_retry_max_seconds',7200),
                          ('review_retry_seconds',172800),('unresolved_pause_seconds',5184000)):
            self.assertIn(f'{key} = {value}'.encode(),raw)
        self.assertEqual(schedule.parse_schedule_file(raw),changed)
        snapshot=schedule.serialize_snapshot(changed)
        self.assertEqual(schedule.deserialize_snapshot(snapshot,hashlib.sha256(snapshot).hexdigest()),changed)

    def test_short_custom_policy_roundtrips_without_default_sparse_threshold(self):
        value=schedule.discovery_schedules()
        changed=replace(value,discovery=replace(value.discovery,retry_first_seconds=30,
            retry_second_seconds=60,retry_third_seconds=120,retry_sparse_seconds=600,
            needs_review_seconds=1800,review_retry_seconds=3600,unresolved_pause_seconds=7200))
        self.assertEqual(schedule.parse_schedule_file(schedule.serialize_schedule_file(changed)),changed)
        raw=schedule.serialize_snapshot(changed)
        self.assertEqual(schedule.deserialize_snapshot(raw,hashlib.sha256(raw).hexdigest()),changed)


class SnapshotTests(unittest.TestCase):
    def test_v5_canonical_transfer_preserves_registry_and_full_seconds(self):
        value = schedule.discovery_schedules()
        value = replace(value, long_inventory=replace(value.long_inventory, reconciliation_seconds=MAX_FULL_COVERAGE_SECONDS))
        raw = schedule.serialize_snapshot(value)
        self.assertEqual(json.loads(raw)['format'], 5)
        self.assertEqual(schedule.deserialize_snapshot(raw, hashlib.sha256(raw).hexdigest()), value)
        self.assertEqual(schedule.parse_schedule_file(schedule.serialize_schedule_file(value)), value)

    def test_child_does_not_accept_v5_values_as_old_payload_or_drop_registry(self):
        payload = json.loads(schedule.serialize_snapshot(schedule.discovery_schedules()))
        variants = []
        for mutate in (
            lambda p: p.update(format=4),
            lambda p: p['schedules'].pop('discovery'),
            lambda p: p['schedules']['discovery'].update(events_general_daily_limit=True),
            lambda p: p['schedules']['schedule.long.inventory'].update(reconciliation_seconds=2 ** 31),
        ):
            changed = json.loads(json.dumps(payload))
            mutate(changed)
            variants.append(changed)
        for changed in variants:
            raw = json.dumps(changed, sort_keys=True, separators=(',', ':')).encode()
            with self.assertRaises(schedule.ScheduleError):
                schedule.deserialize_snapshot(raw, hashlib.sha256(raw).hexdigest())
        with self.assertRaises(schedule.ScheduleError):
            replace(schedule.CollectionSchedules(), long_inventory=InventorySchedule(reconciliation_seconds=29 * 86400))


if __name__ == '__main__':
    unittest.main()
