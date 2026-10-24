"""Per-format inventory values stay strict, independent and process-owned."""
import configparser
from dataclasses import FrozenInstanceError, asdict, fields, replace
import hashlib
import io
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
import collection_schedule as schedule
from collection_schedule_model import InventorySchedule, InventorySchedules, long_inventory_schedule

TEMPLATE = (ROOT / 'config/collection-schedules.ini').read_bytes()


def encode(parser):
    output = io.StringIO()
    parser.write(output)
    return output.getvalue().encode()


def parsed_template():
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(TEMPLATE.decode())
    return parser


class InventoryScheduleTests(unittest.TestCase):
    def test_defaults_match_current_per_type_policy(self):
        value = schedule.parse_schedule_file(TEMPLATE)
        expected = InventorySchedules()
        self.assertEqual(value, schedule.CollectionSchedules())
        self.assertEqual(value.cadence_snapshot().inventory_by_type, expected)
        self.assertEqual(value.long_inventory, long_inventory_schedule())
        self.assertEqual(value.short_inventory, InventorySchedule())
        self.assertEqual(value.stream_inventory, InventorySchedule())
        self.assertEqual([getattr(value.long_inventory, 'quiet_' + step + '_seconds')
            for step in ('1', '2', '3', '4', '5', '6_plus')], [7200,14400,28800,28800,28800,86400])
        self.assertEqual([getattr(value.short_inventory, 'error_' + step + '_seconds')
            for step in ('0', '1', '2', '3', '4', '5_plus')], [3600,3600,7200,14400,28800,86400])

    def test_each_type_and_field_can_be_independently_overridden(self):
        baseline = schedule.CollectionSchedules()
        for kind in ('long', 'short', 'stream'):
            for item in fields(InventorySchedule):
                parser = parsed_template()
                # Spaced ladder values allow changing any one step upward by1.
                section = parser['schedule.' + kind + '.inventory']
                for position, name in enumerate(section):
                    section[name] = str(100 + position * 10)
                original = schedule.parse_schedule_file(encode(parser))
                section[item.name] = str(int(section[item.name]) + 1)
                changed = schedule.parse_schedule_file(encode(parser))
                with self.subTest(kind=kind, field=item.name):
                    self.assertEqual(changed, replace(original, **{kind + '_inventory':
                        replace(getattr(original, kind + '_inventory'), **{item.name: int(section[item.name])})}))
                    self.assertEqual(changed.cadence_snapshot().rules, baseline.cadence_snapshot().rules)
                    for other in {'long', 'short', 'stream'} - {kind}:
                        self.assertEqual(getattr(changed, other + '_inventory'), getattr(baseline, other + '_inventory'))

    def test_existing_v2_v3_files_preserve_every_supplied_value(self):
        for version, sections in ((2, schedule._V2_SECTIONS), (3, schedule._V3_SECTIONS)):
            parser = parsed_template()
            parser['format']['version'] = str(version)
            allowed = {name for name, _, _ in sections}
            for section in tuple(parser.sections()):
                if section != 'format' and section not in allowed:
                    parser.remove_section(section)
            for name, _, _ in sections:
                for key in parser[name]:
                    parser[name][key] = '0' if key == 'jitter_seconds' else '12345'
            raw = encode(parser)
            value = schedule.parse_schedule_file(raw)
            with self.subTest(version=version):
                for name, attribute, _ in sections:
                    self.assertEqual(asdict(getattr(value, attribute)),
                        {key: int(number) for key, number in parser[name].items()})
                self.assertEqual(value.cadence_snapshot().inventory_by_type, InventorySchedules())
                self.assertEqual(schedule.deserialize_snapshot(schedule.serialize_snapshot(value),
                    schedule.snapshot_digest(value)), value)

    def test_old_versions_do_not_silently_accept_inventory_sections(self):
        for version in ('2', '3'):
            raw = TEMPLATE.replace(b'version = 4', ('version = ' + version).encode())
            with self.subTest(version=version), self.assertRaisesRegex(schedule.ScheduleError, 'schedule_sections_invalid'):
                schedule.parse_schedule_file(raw)

    def test_interval_types_and_bounds_and_only_continuation_allows_zero(self):
        for item in fields(InventorySchedule):
            for value in (True, 1.0, '3600', None, -1, schedule.MAX_INTERVAL_SECONDS + 1):
                with self.subTest(field=item.name, value=value), self.assertRaises(schedule.ScheduleError):
                    replace(InventorySchedule(), **{item.name: value})
            if item.name != 'discovery_continuation_seconds':
                with self.subTest(field=item.name), self.assertRaises(schedule.ScheduleError):
                    replace(InventorySchedule(), **{item.name: 0})
        self.assertEqual(InventorySchedule(discovery_continuation_seconds=0).discovery_continuation_seconds, 0)
        for seconds in (1, schedule.MAX_INTERVAL_SECONDS):
            values = {item.name: seconds for item in fields(InventorySchedule)}
            self.assertEqual(asdict(InventorySchedule(**values)), values)

    def test_quiet_and_error_ladders_never_decrease(self):
        for changed in ({'quiet_1_seconds': 14401}, {'quiet_5_seconds': 86401},
                        {'error_0_seconds': 3601}, {'error_4_seconds': 86401}):
            with self.subTest(changed=changed), self.assertRaisesRegex(schedule.ScheduleError, 'schedule_backoff_order_invalid'):
                InventorySchedule(**changed)

    def test_models_are_immutable_and_closed(self):
        value = InventorySchedules()
        with self.assertRaises(FrozenInstanceError):
            value.long.found_new_seconds = 1
        with self.assertRaises(FrozenInstanceError):
            value.short = InventorySchedule()
        for invalid in ({}, None, schedule.LongCounters()):
            with self.assertRaises(schedule.ScheduleError):
                InventorySchedules(short=invalid)
            with self.assertRaises(schedule.ScheduleError):
                schedule.CollectionSchedules(stream_inventory=invalid)

    def test_new_sections_are_exact_and_errors_do_not_echo_values(self):
        for kind in ('long', 'short', 'stream'):
            for mutation in ('missing_section', 'missing_key', 'unknown_key', 'unknown_value'):
                parser = parsed_template()
                section = 'schedule.' + kind + '.inventory'
                if mutation == 'missing_section': parser.remove_section(section)
                elif mutation == 'missing_key': parser.remove_option(section, 'found_new_seconds')
                elif mutation == 'unknown_key': parser[section]['PRIVATE_SENTINEL'] = '1'
                else: parser[section]['found_new_seconds'] = 'PRIVATE_SENTINEL'
                with self.subTest(kind=kind, mutation=mutation), self.assertRaises(schedule.ScheduleError) as caught:
                    schedule.parse_schedule_file(encode(parser))
                self.assertNotIn('PRIVATE_SENTINEL', str(caught.exception))

    def test_child_format4_binds_inventory_and_rejects_old_or_noncanonical_payload(self):
        value = replace(schedule.CollectionSchedules(),
            stream_inventory=replace(InventorySchedule(), found_new_seconds=1800))
        raw = schedule.serialize_snapshot(value)
        self.assertEqual(json.loads(raw)['format'], 4)
        self.assertEqual(schedule.deserialize_snapshot(raw, schedule.snapshot_digest(value)), value)
        mutations = [raw.replace(b'"found_new_seconds":1800', b'"found_new_seconds":1801'),
            raw.replace(b'"format":4', b'"format":3'), raw + b' ']
        for changed in mutations:
            with self.assertRaises(schedule.ScheduleError):
                schedule.deserialize_snapshot(changed, schedule.snapshot_digest(value))
            if changed != mutations[0]:
                with self.assertRaises(schedule.ScheduleError):
                    schedule.deserialize_snapshot(changed, hashlib.sha256(changed).hexdigest())


if __name__ == '__main__':
    unittest.main()
