"""Observed change limits follow the schedule; the server document is the client fixture."""
from dataclasses import replace
import json
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))

from collection_schedule import CollectionSchedules
from research_query import observation_limits
from research_query.operations import OPERATIONS
from research_query.validation import AccessContext, capabilities

FIXTURE = ROOT / 'tests/fixtures/research_capabilities_window_limits.json'


def edited_schedule():
    """Long: nothing hourly (limit 0); Short: shipped defaults; Stream: hourly at every age (null)."""
    base = CollectionSchedules()
    every_age = replace(base.stream_counters, **{f.name: 3600 for f in base.stream_counters.__dataclass_fields__.values()
                                                if f.name != 'unlisted_seconds'})
    return replace(base, long_counters=replace(base.long_counters, age_0_1d_seconds=7200, age_1_7d_seconds=7200),
                   stream_counters=every_age)


def server_document():
    context = AccessContext('fixture', 'owner', enabled=True, allowed_operations=frozenset(OPERATIONS))
    with patch.object(observation_limits, 'process_schedules', edited_schedule):
        return capabilities(context)


class ObservationLimitTests(unittest.TestCase):
    def test_zero_positive_and_every_age_limits(self):
        limits = observation_limits.age_limits(['long', 'short', 'stream'], edited_schedule())
        self.assertEqual(limits, {'long': {'3600': 0, '86400': 31536000},
                                  'short': {'3600': 604800, '86400': 31536000},
                                  'stream': {'3600': None, '86400': None}})

    def test_client_fixture_is_the_current_server_document(self):
        # tests/test_research_saved_ui.mjs feeds this exact document to the real client parser.
        self.assertEqual(json.loads(FIXTURE.read_text(encoding='utf-8')), server_document())

    def test_workshop_labels_every_field_the_server_offers(self):
        # The workshop's capabilities parser keeps only fields it can name, so an unnamed server field
        # (Library's calculations once) made a Library hand-off "not available on this server". Every field either
        # source offers must have a workshop label.
        text = (ROOT / 'application/frontend/src/explorer/researchQuery.ts').read_text(encoding='utf-8')
        block = text[text.index('export const researchLabels = {'):text.index('} as const;')]
        labels = set(re.findall(r"'([a-z_]+\.[a-z0-9_]+)':", block))
        context = AccessContext('fixture', 'owner', enabled=True, allowed_operations=frozenset(OPERATIONS))
        with patch.object(observation_limits, 'process_schedules', edited_schedule):
            for source_id in ('public.videos.current.v1', 'public.channels.current.v1'):
                offered = {field['id'] for field in capabilities(context, source_id)['fields']}
                self.assertTrue(offered)
                self.assertEqual(offered - labels, set(), source_id)


if __name__ == '__main__':
    unittest.main()
