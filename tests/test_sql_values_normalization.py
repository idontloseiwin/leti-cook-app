"""Normalizer fast paths retain native-value conversion and container protocols."""
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import math
from pathlib import Path
import sys
import unittest
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'application'))
from db.base.sql_values import application_value


class NormalizationTests(unittest.TestCase):
    def test_nested_exact_containers_keep_all_scalar_conversions(self):
        identity = UUID('82000000-0000-4000-8000-000000000001')
        value = {'values': [None, True, False, 9007199254740993, 'text',
            date(2026, 10, 6), datetime(2026, 10, 6, 4, 3, 2, 123456),
            datetime(2026, 10, 6, 4, 3, tzinfo=timezone(timedelta(hours=3))),
            identity, Decimal('9007199254740993'), Decimal('2.50'),
            Decimal('NaN'), Decimal('sNaN'), Decimal('Infinity'), Decimal('-Infinity'),
            float('nan'), float('inf'), -float('inf'), 1.25, -0.0,
            {'tuple': (Decimal('-2'), date(2026, 10, 5))}]}
        result = application_value(value)
        self.assertEqual(result, {'values': [None, True, False, 9007199254740993, 'text',
            '2026-10-06', '2026-10-06T04:03:02.123456', '2026-10-06T01:03:00+00:00',
            str(identity), 9007199254740993, 2.5, 'NaN', 'sNaN', 'Infinity', '-Infinity',
            'nan', 'inf', '-inf', 1.25, -0.0, {'tuple': [-2, '2026-10-05']}]})
        self.assertIs(type(result['values'][8]), str)
        self.assertIs(type(result['values'][9]), int)
        self.assertIs(type(result['values'][10]), float)
        self.assertEqual(math.copysign(1, result['values'][19]), -1)

    def test_new_containers_preserve_order_without_retaining_mutable_aliases(self):
        shared = {'child': [1, {'leaf': 2}]}
        value = {'second': shared, 'first': shared, 'empty': [], 'object': {}}
        result = application_value(value)
        self.assertEqual(list(result), list(value))
        self.assertEqual(result, value)
        self.assertIsNot(result, value)
        self.assertIsNot(result['second'], shared)
        self.assertIsNot(result['second'], result['first'])
        self.assertIsNot(result['second']['child'], shared['child'])
        self.assertIsNot(result['second']['child'][1], shared['child'][1])
        self.assertIsNot(result['empty'], value['empty'])
        self.assertIsNot(result['object'], value['object'])
        result['second']['child'][1]['leaf'] = 3
        self.assertEqual(shared['child'][1]['leaf'], 2)
        self.assertEqual(result['first']['child'][1]['leaf'], 2)

    def test_all_dictionary_keys_are_validated_before_any_value(self):
        events = []
        class Child(dict):
            def __iter__(self):
                events.append('child')
                return super().__iter__()
        for value in ({'first': object(), 3: None}, {'first': Child(ok=1), 3: None}):
            with self.assertRaisesRegex(ValueError, '^database_result_key_invalid$'):
                application_value(value)
        self.assertEqual(events, [])
        with self.assertRaisesRegex(ValueError, '^database_result_key_invalid$'):
            application_value([{'valid': {'first': object(), None: None}}])

    def test_dictionary_subclass_uses_its_iteration_and_items_protocol(self):
        events = []
        class CustomDict(dict):
            def __iter__(self):
                events.append('keys')
                return iter(('b', 'a'))
            def items(self):
                events.append('items')
                return iter((('b', [2]), ('a', [1])))
        value = CustomDict(ignored=9)
        self.assertEqual(application_value(value), {'b': [2], 'a': [1]})
        self.assertEqual(events, ['keys', 'items'])
        self.assertEqual(value, {'ignored': 9})

    def test_mapping_protocol_and_prevalidation_remain_separate(self):
        events = []
        class CustomMapping(Mapping):
            def __len__(self): return 2
            def __iter__(self):
                events.append('keys')
                return iter(('first', 'second'))
            def __getitem__(self, key):
                events.append(key)
                return (1, 2)
            def items(self):
                events.append('items')
                return iter((('second', (2,)), ('first', (1,))))
        self.assertEqual(application_value(CustomMapping()), {'second': [2], 'first': [1]})
        self.assertEqual(events, ['keys', 'items'])
        class InvalidMapping(CustomMapping):
            def __iter__(self):
                events.append('bad_keys')
                return iter(('first', 1))
        events.clear()
        with self.assertRaisesRegex(ValueError, '^database_result_key_invalid$'):
            application_value(InvalidMapping())
        self.assertEqual(events, ['bad_keys'])

    def test_sequence_subclasses_keep_iteration_and_become_lists(self):
        events = []
        class CustomList(list):
            def __iter__(self):
                events.append('list')
                return iter((Decimal('4'), {'nested': []}))
        class CustomTuple(tuple):
            def __iter__(self):
                events.append('tuple')
                return iter((date(2026, 10, 6),))
        self.assertEqual(application_value([CustomList([9]), CustomTuple((9,))]),
                         [[4, {'nested': []}], ['2026-10-06']])
        self.assertEqual(events, ['list', 'tuple'])

    def test_string_subclass_keys_are_kept_but_values_stay_unsupported(self):
        class Text(str): pass
        class Count(int): pass
        key = Text('key')
        result = application_value({key: [1]})
        self.assertIs(next(iter(result)), key)
        for value in (Text('text'), Count(2), b'bytes', object(), {1}, range(2)):
            for envelope in (value, [value], {'value': value}):
                with self.assertRaisesRegex(ValueError, '^database_result_value_invalid$'):
                    application_value(envelope)

    def test_supported_scalar_subclasses_keep_existing_conversion(self):
        class Day(date): pass
        class Clock(datetime): pass
        class Number(float): pass
        number = Number(2.5)
        value = application_value({'day': Day(2026, 10, 6),
            'clock': Clock(2026, 10, 6, tzinfo=timezone.utc), 'number': number})
        self.assertEqual(value, {'day': '2026-10-06', 'clock': '2026-10-06T00:00:00+00:00', 'number': 2.5})
        self.assertIs(value['number'], number)

    def test_deep_containers_keep_structure_and_cyclic_inputs_still_fail(self):
        value = {'leaf': Decimal('3')}
        for _ in range(40): value = {'items': [value]}
        result = application_value(value)
        for _ in range(40): result = result['items'][0]
        self.assertEqual(result, {'leaf': 3})
        cycle = []; cycle.append(cycle)
        with self.assertRaises(RecursionError): application_value(cycle)
        cycle = {}; cycle['self'] = cycle
        with self.assertRaises(RecursionError): application_value(cycle)


if __name__ == '__main__':
    unittest.main()
