"""The in-repo provider registry. Local files only: no network, no database."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
from operational_copy_policy import QUOTA_LABELS  # noqa: E402
import telemetry_contract  # noqa: E402
from provider_sources import registry as registry_module  # noqa: E402
from youtube_errors import OUTCOME_BUCKETS  # noqa: E402

REGISTRY = ROOT / 'application/provider_sources/registry'
UNCONFIRMED_PARTS = {'youtube.playlistImages.list', 'youtube.thirdPartyLinks.list'}
JOURNAL_LABEL = re.compile(r'^[A-Za-z][A-Za-z0-9_.:-]{0,119}$')  # db/quota/youtube_quota_journal.py _LABEL


def document(name):
    return json.loads((REGISTRY / name).read_text(encoding='utf-8'))


class DeterminismTests(unittest.TestCase):


    def test_content_sha256_is_correct(self):
        for name in ('youtube.methods.json', 'youtube.fields.json', 'youtube.states.json'):
            value = document(name)
            body = {key: item for key, item in value.items() if key != 'content_sha256'}
            digest = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False,
                                               separators=(',', ':')).encode('utf-8')).hexdigest()
            self.assertEqual(value['content_sha256'], digest, name)



class ContentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.methods = document('youtube.methods.json')
        cls.fields = document('youtube.fields.json')
        cls.states = document('youtube.states.json')

    def test_read_and_control_method_counts(self):
        self.assertEqual(len(self.methods['read_methods']), 38)
        self.assertEqual(len(self.methods['control_methods']), 61)
        self.assertEqual(self.methods['counts']['read_by_api'], {'analytics': 3, 'data': 29, 'reporting': 6})
        entries = [entry for method in self.fields['methods'].values()
                   for group in method['parts'].values() for entry in group.values()]
        self.assertEqual(sum(entry['reference_status'] != 'reference_only' for entry in entries), 1644)
        self.assertEqual(self.fields['counts']['fields'], len(entries))
        self.assertEqual(self.fields['counts']['doc_only_fields'],
                         sum(e['reference_status'] == 'reference_only' for e in entries))

    def test_every_read_method_is_get_with_a_sourced_or_explicit_quota(self):
        for method_id, method in self.methods['read_methods'].items():
            with self.subTest(method=method_id):
                self.assertEqual(method['http_method'], 'GET')
                quota = method['quota']
                self.assertIn(quota['status'], ('verified', 'unverified', 'conflict', 'no_data_api_units'))
                if quota['status'] == 'verified':
                    self.assertTrue(quota['sources'])
                self.assertIn(quota['bucket'], self.methods['buckets'])

    def test_parameters_follow_the_reference_rules(self):
        for method_id, method in self.methods['read_methods'].items():
            parameters = method['parameters']
            with self.subTest(method=method_id):
                self.assertNotIn('key', parameters)
                # The planner requires a confirmed part list for Data API reads.
                if method['api'] == 'data' and method['parts'] and method_id not in UNCONFIRMED_PARTS:
                    self.assertEqual(sorted(parameters['part'].get('allowed', [])), sorted(method['parts']))
                pagination = method['pagination']
                size = pagination.get('page_size_param')
                if pagination['style'] == 'pageToken' and size in parameters and pagination.get('max') is not None:
                    self.assertEqual((parameters[size]['min'], parameters[size]['max']),
                                     (pagination['min'], pagination['max']))
                for name, entry in parameters.items():
                    for other in entry.get('not_with', []) + entry.get('only_with', []):
                        self.assertIn(other, parameters, f'{name} refers to {other}')
                for name in (method.get('filters') or {}).get('exactly_one_of', []):
                    self.assertIn(name, parameters)

    def test_labels_and_buckets_are_reviewed_vocabulary(self):
        for method_id, method in self.methods['read_methods'].items():
            label = method['ledger_endpoint_label']
            with self.subTest(method=method_id):
                self.assertRegex(label, JOURNAL_LABEL)
                self.assertIn(label, QUOTA_LABELS['endpoint'])
                self.assertIn(label, telemetry_contract._TRACE_ENDPOINTS)
                self.assertIn(method['quota']['bucket'], OUTCOME_BUCKETS)
                self.assertIn(method['quota']['bucket'], QUOTA_LABELS['quota_bucket'])
                self.assertIn(method['quota']['bucket'], telemetry_contract._TRACE_BUCKETS)

    def test_vocabularies_are_closed(self):
        absence = set(self.fields['absence_vocabulary'])
        kinds = set(self.fields['value_kind_vocabulary']) | {'string', 'integer', 'boolean', 'object', 'float', 'any'}
        exposure = set(self.fields['exposure_vocabulary'])
        for method_id, method in self.fields['methods'].items():
            for part, entries in method['parts'].items():
                for path, entry in entries.items():
                    self.assertIn(entry['absence'], absence, (method_id, path))
                    self.assertIn(entry['value_kind'], kinds, (method_id, path))
                    self.assertIn(entry['exposure'], exposure, (method_id, path))
                    if entry['sensitive'] or 'secret' in entry['roles'] or entry['deprecated']:
                        self.assertEqual(entry['exposure'], 'never', (method_id, path))
                    if 'html' in entry['roles']:
                        self.assertNotEqual(entry['exposure'], 'default', (method_id, path))

    def test_normalizers_name_existing_owners(self):
        names = {entry['normalizer'] for method in self.fields['methods'].values()
                 for entries in method['parts'].values() for entry in entries.values() if 'normalizer' in entry}
        self.assertEqual(names, {'youtube_source_fields.youtube_counter_integer', 'youtube_source_fields.parse_dt',
                                 'youtube_source_fields.youtube_duration_seconds', 'youtube_source_fields.youtube_unsigned_long'})
        for name in names:
            module, symbol = name.split('.')
            self.assertTrue(callable(getattr(importlib.import_module(module), symbol, None)), name)




    def test_described_state_covers_the_whole_registry(self):
        described = {method_id: record['described']['value'] for method_id, record in self.states['methods'].items()}
        self.assertEqual(set(described), set(self.methods['read_methods']))
        self.assertEqual(sorted(method for method, value in described.items() if value == 'discovery_only'),
                         ['youtube.thirdPartyLinks.list', 'youtube.videoTrainability.get'])
        self.assertTrue(all(value in ('yes', 'discovery_only') for value in described.values()))


class LoaderTests(unittest.TestCase):
    def test_loader_builds_frozen_methods_and_rejects_tampering(self):
        loaded = registry_module.load_registry()
        method = loaded.methods['youtube.playlists.list']
        self.assertEqual((method.endpoint, method.ledger_endpoint_label, method.quota.bucket), ('playlists', 'playlists', 'general'))
        self.assertEqual(method.identity_field.path, 'id')
        with self.assertRaises(Exception):
            method.parameters['channelId'] = None
        tampered = document('youtube.fields.json')
        tampered['methods']['youtube.playlists.list']['field_count'] += 1
        payload = json.dumps(tampered).encode('utf-8')
        with patch.object(Path, 'read_bytes', lambda self: payload):
            with self.assertRaisesRegex(registry_module.RegistryError, 'checksum'):
                registry_module._verified('youtube.fields.json')

    def test_the_registry_host_is_the_transport_base(self):
        import youtube_client
        loaded = registry_module.load_registry()
        for method in loaded.methods.values():
            if method.api == 'data':
                self.assertEqual(method.host + '/' + method.path.split('/', 2)[0] + '/' + method.path.split('/', 2)[1],
                                 youtube_client.YOUTUBE_DATA_API_BASE_URL)


if __name__ == '__main__':
    unittest.main()
