"""Pure schedule component tests against the held current-schema defaults."""
from dataclasses import FrozenInstanceError, asdict, fields, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(os.environ.get('F11_SOURCE_ROOT', ROOT))
sys.path[:0] = [str(ROOT / 'application'), str(SOURCE / 'application')]
import collection_schedule as schedule
import collection_policy as policy

TEMPLATE = (ROOT / 'config/collection-schedules.ini').read_bytes()
SQL = (ROOT / 'tests/fixtures/collection_schedule_defaults.sql').read_bytes()
GOLDEN = (ROOT / 'tests/fixtures/collection_schedule_snapshot.json').read_bytes()
NOW = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)


def native_defaults():
    # This is held INSERT source, not a second Python policy implementation.
    values = SQL.decode().split(' VALUES', 1)[1]
    rows = [policy.CadenceRule(*json.loads('[' + text.replace("'", '"').replace('NULL', 'null') + ']'))
            for text in re.findall(r'\(([^()]+)\)', values)]
    return policy.CadenceSnapshot(tuple(rows))


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def edited(section, key, value):
    text = TEMPLATE.decode()
    prefix, body = text.split('[' + section + ']\n', 1)
    here, separator, rest = body.partition('\n[')
    here, count = re.subn(r'(?m)^' + re.escape(key) + r' = .*$', key + ' = ' + value, here)
    assert count == 1
    return (prefix + '[' + section + ']\n' + here + separator + rest).encode()


class DefaultsTests(unittest.TestCase):
    def test_fixture_is_exact_current_schema_bytes_and_imports_existing_owner(self):
        pins = json.loads((ROOT / 'tests/fixtures/collection_schedule_source.json').read_text())
        self.assertEqual(Path(policy.__file__).resolve(), (SOURCE / 'application/collection_policy.py').resolve())
        for name, pin in pins['source_files'].items():
            self.assertEqual(digest((SOURCE / name).read_bytes()), pin)
        actual = re.search(rb'INSERT INTO public\.video_collection_cadence\([^;]+? VALUES\s*.*?;\r?\n',
                           (SOURCE / 'schema/schema.sql').read_bytes(), re.S).group()
        self.assertEqual(SQL, actual)
        self.assertEqual(digest(SQL), pins['default_sql_fixture_sha256'])

    def test_default_model_preserves_sixteen_existing_rules_and_adds_fourteen_counters(self):
        value = schedule.parse_schedule_file(TEMPLATE)
        self.assertEqual(value, schedule.CollectionSchedules())
        compiled = value.cadence_snapshot()
        self.assertEqual(compiled.rules[:16], native_defaults().rules)
        self.assertEqual(len(compiled.rules), 30)
        self.assertIsNone(compiled.captured_at)
        self.assertEqual(sum(len(fields(type(getattr(value, key)))) for key in (
            'long_counters', 'long_metadata', 'short_metadata', 'stream_metadata', 'rechecks')), 20)
        self.assertEqual(len([r for r in compiled.rules
                              if r.video_type != 'long' and r.collection_kind == 'counters']), 14)

    def test_default_child_payload_matches_held_canonical_bytes(self):
        value = schedule.parse_schedule_file(TEMPLATE)
        self.assertEqual(schedule.serialize_snapshot(value), GOLDEN)
        self.assertEqual(schedule.snapshot_digest(value), digest(GOLDEN))
        self.assertEqual(schedule.deserialize_snapshot(GOLDEN, digest(GOLDEN)), value)

    def test_existing_selector_semantics_match_at_all_seed_boundaries(self):
        old, new = native_defaults(), schedule.CollectionSchedules().cadence_snapshot()
        boundaries = {0, 1, 7776000}
        for rule in old.rules:
            boundaries.add(rule.min_age_seconds)
            if rule.max_age_seconds is not None:
                boundaries.add(rule.max_age_seconds)
        anchors = [None, policy.Infinity.POSITIVE, policy.Infinity.NEGATIVE]
        anchors += [NOW - timedelta(seconds=seconds, microseconds=offset)
                    for seconds in boundaries for offset in (-500001, -1, 0, 1, 499999, 500000)]
        def selected(snapshot, kind, mode, availability, anchor):
            try:
                return policy.select_policy(snapshot, kind, mode, availability, anchor, NOW, NOW)
            except policy.PolicyError as error:
                return error.code, error.sqlstate
        for kind in ('long', 'short', 'stream'):
            for mode in ('counters', 'metadata'):
                if kind != 'long' and mode == 'counters':
                    continue  # The newly accepted schedules are tested separately.
                for availability in (None, 'public', 'unlisted', 'private'):
                    for anchor in anchors:
                        with self.subTest(kind=kind, mode=mode, availability=availability, anchor=anchor):
                            self.assertEqual(selected(new,kind,mode,availability,anchor),
                                             selected(old,kind,mode,availability,anchor))

    def test_model_and_nested_values_are_immutable(self):
        value = schedule.CollectionSchedules()
        with self.assertRaises(FrozenInstanceError): value.short_metadata = schedule.TypedMetadata()
        with self.assertRaises(FrozenInstanceError): value.short_metadata.jitter_seconds = 0
        with self.assertRaises(FrozenInstanceError): value.rechecks.identity_seconds = 1
        with self.assertRaises(FrozenInstanceError): value.cadence_snapshot().rechecks = schedule.RecheckSchedules(1, 1)
        copied = json.loads(schedule.serialize_snapshot(value))
        copied['schedules']['schedule.short.metadata']['jitter_seconds'] = 0
        self.assertEqual(value.short_metadata.jitter_seconds, 21600)

    def test_later_source_bytes_do_not_change_already_parsed_snapshot(self):
        original = schedule.parse_schedule_file(TEMPLATE)
        changed = schedule.parse_schedule_file(edited('schedule.short.metadata','age_through_90d_seconds','345600'))
        self.assertEqual(original.short_metadata.age_through_90d_seconds, 604800)
        self.assertEqual(changed.short_metadata.age_through_90d_seconds, 345600)
        self.assertNotEqual(schedule.snapshot_digest(original), schedule.snapshot_digest(changed))


class OverrideTests(unittest.TestCase):
    def test_independent_override_changes_only_addressed_rule(self):
        base = schedule.CollectionSchedules().cadence_snapshot().rules
        for section, key, rule_index, seconds in (
            ('schedule.long.counters','age_0_1d_seconds',0,1800),
            ('schedule.long.counters','unlisted_seconds',6,604800),
            ('schedule.long.metadata','age_7_28d_seconds',8,43200),
            ('schedule.short.metadata','age_through_90d_seconds',12,345600),
            ('schedule.stream.metadata','age_over_90d_seconds',15,864000),
        ):
            with self.subTest(section=section,key=key):
                rules = schedule.parse_schedule_file(edited(section,key,str(seconds))).cadence_snapshot().rules
                self.assertEqual([i for i,(a,b) in enumerate(zip(base,rules)) if a!=b], [rule_index])
                self.assertEqual(rules[rule_index], replace(base[rule_index],cadence_seconds=seconds))

    def test_rechecks_are_independent_and_propagate_without_changing_cadence_rules(self):
        baseline = schedule.CollectionSchedules()
        for key in ('unavailable_long_max_seconds', 'identity_seconds'):
            for seconds in (1, 21600, schedule.MAX_INTERVAL_SECONDS):
                with self.subTest(key=key, seconds=seconds):
                    value = schedule.parse_schedule_file(edited('schedule.rechecks', key, str(seconds)))
                    snapshot = value.cadence_snapshot()
                    self.assertEqual(snapshot.rules, baseline.cadence_snapshot().rules)
                    self.assertIs(snapshot.rechecks, value.rechecks)
                    self.assertEqual(snapshot.rechecks, replace(baseline.rechecks, **{key: seconds}))
                    payload = schedule.serialize_snapshot(value)
                    self.assertEqual(schedule.deserialize_snapshot(payload, digest(payload)), value)

    def test_jitter_is_independent_per_type_and_uses_existing_due_algorithm(self):
        value = schedule.parse_schedule_file(edited('schedule.short.metadata','jitter_seconds','0'))
        rules = value.cadence_snapshot()
        for kind in ('short','stream'):
            result = policy.typed_due_at(rules,kind,'video_id_01',NOW-timedelta(days=2),NOW,db_now=NOW)
            if kind=='short':self.assertEqual(result,NOW+timedelta(days=7))
            else:self.assertEqual(result,policy.typed_due_at(native_defaults(),kind,'video_id_01',NOW-timedelta(days=2),NOW,db_now=NOW))
        self.assertEqual([r.jitter_window_seconds for r in rules.rules
                          if r.video_type != 'long' and r.collection_kind == 'metadata'], [0,0,21600,21600])

    def test_all_model_interval_fields_reject_boolean_float_string_and_range(self):
        for record in (schedule.LongCounters(),schedule.LongMetadata(),schedule.TypedMetadata(),schedule.RecheckSchedules()):
            for item in fields(record):
                if item.name=='jitter_seconds':continue
                for value in (True,1.0,'3600',None,0,-1,2419201):
                    with self.subTest(record=type(record).__name__,field=item.name,value=value):
                        with self.assertRaises(schedule.ScheduleError):replace(record,**{item.name:value})

    def test_interval_endpoints_and_typed_jitter_endpoints(self):
        for value in (1,2419200):
            self.assertEqual(schedule.LongCounters(age_0_1d_seconds=value).age_0_1d_seconds,value)
        self.assertEqual(schedule.TypedMetadata(1,2419200,0).jitter_seconds,0)
        self.assertEqual(schedule.TypedMetadata(1,2419200,1).jitter_seconds,1)
        for value in (-1,2,True,1.0,'1'):
            with self.subTest(jitter=value):
                with self.assertRaises(schedule.ScheduleError):schedule.TypedMetadata(1,2419200,value)

    def test_closed_nested_model_rejects_mapping_and_wrong_record(self):
        for value in ({},None,schedule.LongMetadata()):
            with self.assertRaises(schedule.ScheduleError):schedule.CollectionSchedules(long_counters=value)
            with self.assertRaises(schedule.ScheduleError):schedule.CollectionSchedules(rechecks=value)


class IniTests(unittest.TestCase):
    def test_comments_whitespace_and_section_order_do_not_change_identity(self):
        # Reorder complete sections; keep their exact key meanings.
        blocks = TEMPLATE.decode().split('\n[')
        changed = ('# harmless comment\n[' + '\n['.join(reversed(blocks[1:]))).encode()
        self.assertEqual(changed.count(b'[format]'), 1)
        self.assertEqual(schedule.snapshot_digest(schedule.parse_schedule_file(changed)),
                         schedule.snapshot_digest(schedule.parse_schedule_file(TEMPLATE)))
        self.assertEqual(schedule.parse_schedule_file(TEMPLATE.replace(b' = ',b'=').replace(b'\n',b'\r\n')),
                         schedule.CollectionSchedules())

    def test_unknown_missing_typed_counter_and_case_variant_keys_fail(self):
        variants = [TEMPLATE+b'\n[schedule.short.counters]\ninterval=3600\n',
                    TEMPLATE.replace(b'age_0_1d_seconds = 3600\n',b''),
                    TEMPLATE.replace(b'age_0_1d_seconds',b'unknown_key',1),
                    TEMPLATE.replace(b'age_0_1d_seconds',b'AGE_0_1D_SECONDS',1),
                    TEMPLATE.replace(b'[schedule.short.metadata]',b'[schedule.short.unknown]'),
                    TEMPLATE.replace(b'[format]',b'[unknown]')]
        for raw in variants:
            with self.subTest(raw=raw[:20]):
                with self.assertRaises(schedule.ScheduleError):schedule.parse_schedule_file(raw)

    def test_recheck_section_is_required_and_uses_exact_whole_second_bounds(self):
        without_rechecks = TEMPLATE.split(b'[schedule.rechecks]', 1)[0]
        variants = [without_rechecks,
                    TEMPLATE.replace(b'identity_seconds = 2419200\n', b''),
                    TEMPLATE.replace(b'identity_seconds', b'identity_refresh_seconds')]
        for key in ('unavailable_long_max_seconds', 'identity_seconds'):
            variants += [edited('schedule.rechecks', key, value)
                         for value in ('0', '2419201', 'true', '1.0', '-1')]
        for raw in variants:
            with self.assertRaises(schedule.ScheduleError):
                schedule.parse_schedule_file(raw)

    def test_duplicate_sections_keys_and_defaults_are_rejected(self):
        for raw in (TEMPLATE+b'\n[format]\nversion=1\n',
                    TEMPLATE.replace(b'age_0_1d_seconds = 3600',b'age_0_1d_seconds = 3600\nage_0_1d_seconds = 7200'),
                    TEMPLATE+b'\n[DEFAULT]\n',TEMPLATE+b'\n[DEFAULT]\nextra=1\n'):
            with self.assertRaises(schedule.ScheduleError):schedule.parse_schedule_file(raw)

    def test_noninteger_forms_interpolation_and_multiline_rejected(self):
        for value in ('true','3600.0','+3600','-1','1_000','1e3','%(other)s','${other}','\u0661','3600\n  7200'):
            with self.subTest(value=value):
                with self.assertRaises(schedule.ScheduleError):schedule.parse_schedule_file(edited('schedule.long.counters','age_0_1d_seconds',value))

    def test_version_and_format_fail_closed_without_defaults(self):
        for raw in (b'',TEMPLATE.replace(b'version = 4',b'version = 1'),TEMPLATE.replace(b'version = 4',b'version = 5'),TEMPLATE.replace(b'version = 4',b'version = true'),
                    TEMPLATE.replace(b'version = 4\n',b''),TEMPLATE+b'\x00',b'\xff'+TEMPLATE,b'\xef\xbb\xbf'+TEMPLATE):
            with self.assertRaises(schedule.ScheduleError):schedule.parse_schedule_file(raw)

    def test_exact_size_boundary_and_bytes_only(self):
        raw=TEMPLATE+b'#'+b'x'*(schedule.MAX_SCHEDULE_BYTES-len(TEMPLATE)-1)
        self.assertEqual(len(raw),schedule.MAX_SCHEDULE_BYTES)
        self.assertEqual(schedule.parse_schedule_file(raw),schedule.CollectionSchedules())
        for bad in (raw+b'x',TEMPLATE.decode(),bytearray(TEMPLATE),None):
            with self.assertRaises(schedule.ScheduleError):schedule.parse_schedule_file(bad)

    def test_errors_never_echo_unknown_values_or_sections(self):
        secret='DO_NOT_ECHO_SENTINEL'
        for raw in (TEMPLATE+('\n['+secret+']\nkey=1\n').encode(),edited('schedule.long.counters','age_0_1d_seconds',secret)):
            with self.assertRaises(schedule.ScheduleError) as caught:schedule.parse_schedule_file(raw)
            self.assertNotIn(secret,str(caught.exception))
            self.assertRegex(str(caught.exception),r'^schedule_[a-z_]+$')


class HandoffTests(unittest.TestCase):
    def test_child_rejects_changed_byte_or_wrong_digest(self):
        for raw, expected in ((GOLDEN.replace(b'3600',b'3601',1),digest(GOLDEN)),
                              (GOLDEN,'0'*64),(GOLDEN,digest(GOLDEN).upper()),(GOLDEN,None)):
            with self.assertRaises(schedule.ScheduleError):schedule.deserialize_snapshot(raw,expected)

    def test_valid_changed_snapshot_roundtrips_with_its_own_digest(self):
        value=replace(schedule.CollectionSchedules(),short_metadata=schedule.TypedMetadata(345600,864000,0))
        raw=schedule.serialize_snapshot(value)
        self.assertEqual(schedule.deserialize_snapshot(raw,schedule.snapshot_digest(value)),value)

    def test_child_rejects_extra_missing_duplicate_bool_and_disabled_families(self):
        base=json.loads(GOLDEN)
        variants=[]
        for mutation in (
            lambda p:p.update(format=True),
            lambda p:p.update(extra=1),
            lambda p:p['schedules'].pop('schedule.stream.metadata'),
            lambda p:p['schedules'].update({'schedule.short.counters':{'interval':3600}}),
            lambda p:p['schedules']['schedule.short.metadata'].update(jitter_seconds=True),
            lambda p:p['schedules']['schedule.long.counters'].update(age_0_1d_seconds=1.0),
            lambda p:p['schedules'].pop('schedule.rechecks'),
            lambda p:p['schedules']['schedule.rechecks'].update(identity_seconds=True),
            lambda p:p['schedules']['schedule.rechecks'].update(unavailable_long_max_seconds=2419201),
        ):
            value=json.loads(GOLDEN);mutation(value);variants.append(json.dumps(value,sort_keys=True,separators=(',',':')).encode())
        variants += [GOLDEN.replace(b'"format":4',b'"format":4,"format":4'),b'[]',b'null',b'['*1500+b'0'+b']'*1500]
        for raw in variants:
            with self.assertRaises(schedule.ScheduleError):schedule.deserialize_snapshot(raw,digest(raw))

    def test_child_requires_canonical_bytes_and_bound(self):
        for raw in (json.dumps(json.loads(GOLDEN),indent=2).encode(),b' '+GOLDEN,
                    b'x'*(schedule.MAX_SCHEDULE_BYTES+1)):
            with self.assertRaises(schedule.ScheduleError):schedule.deserialize_snapshot(raw,digest(raw))

    def test_snapshot_contains_only_configured_nonsecret_values(self):
        payload=json.loads(GOLDEN)
        self.assertEqual(set(payload),{'format','schedules'})
        self.assertEqual(len(GOLDEN),len(schedule.serialize_snapshot(schedule.CollectionSchedules())))
        self.assertNotRegex(GOLDEN.decode(),r'lease|epoch|recorded_at|captured_at|api_key|password|dsn')


if __name__=='__main__':unittest.main()
