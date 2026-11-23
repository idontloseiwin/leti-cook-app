"""Library Overview and channel header on the shared backend. Source only.

The browser adapter (libraryData/overview.ts) must name exactly the server's
operations and vocabularies, the Overview and header must read them
through the shared query API. Every type mounts the same Overview, and
the responsive layout rules stay in place. No server, database or browser.
"""
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
EXPLORER = ROOT / 'application/frontend/src/explorer'
sys.path.insert(0, str(ROOT / 'application'))


def source(name):
    return (EXPLORER / name).read_text(encoding='utf-8')


def ts_object(text, name):
    """String values of a flat `export const NAME = { key: 'value', ... }` object."""
    body = re.search(r'export const ' + name + r'[^=]*= \{(.*?)\n\}', text, re.S).group(1)
    return dict(re.findall(r"^\s*'?([A-Za-z0-9_]+)'?: '([^']+)',?", body, re.M))


def ts_list(text, name):
    body = re.search(r'(?:export )?const ' + name + r' = \[(.*?)\]', text, re.S).group(1)
    return re.findall(r"'([^']+)'", body)


class AdapterVocabularyTests(unittest.TestCase):
    """One owner of each name: the TypeScript adapter repeats the server's, never its own."""

    @classmethod
    def setUpClass(cls):
        cls.adapter = source('libraryData/overview.ts')

    def test_operation_ids_are_registered_with_their_policies(self):
        from research_query.operation_kinds import OPERATION_KINDS
        from web.research import POLICY_FLAGS
        # The adapter names its operations through wire.ts OPERATIONS (one place for every id).
        wire = ts_object(source('libraryData/wire.ts'), 'OPERATIONS')

        def named(name):
            body = re.search(r'export const ' + name + r'[^=]*= \{(.*?)\n\}', self.adapter, re.S).group(1)
            return {key: wire[ref] for key, ref in re.findall(r'^\s*([A-Za-z0-9_]+): OPERATIONS\.([A-Za-z0-9_]+),?', body, re.M)}
        overview = named('OVERVIEW_OPERATIONS')
        header = named('HEADER_OPERATIONS')
        self.assertEqual(set(overview.values()), {'video.fingerprint.v1', 'video.publication_rhythm.v1',
                                                  'video.publication_years.v1', 'video.content_breakdown.v1'})
        self.assertEqual(set(header.values()), {'channel.counter_change.v1', 'channel.format_totals.v1'})
        for operation in overview.values():
            self.assertEqual(OPERATION_KINDS[operation].definition.policy, 'descriptive_analytics', operation)
        for operation in header.values():
            # The header was never gated.
            self.assertEqual(OPERATION_KINDS[operation].definition.policy, 'channel_header', operation)
        self.assertEqual(POLICY_FLAGS['channel_header'], ())
        self.assertEqual(POLICY_FLAGS['descriptive_analytics'], ('CHANNEL_BASELINE_ANALYTICS_ENABLED',))

    def test_result_vocabularies_match_the_operations(self):
        from research_query.operation_kinds import content_breakdown, counter_change, fingerprint, format_totals
        from research_query.operation_kinds import overview, summary
        self.assertEqual(tuple(ts_list(self.adapter, 'FINGERPRINT_METRICS')), summary.METRICS)
        self.assertEqual(tuple(ts_list(self.adapter, 'REJECTIONS')), fingerprint.REJECTIONS)
        sections = dict(re.findall(r"^\s+([a-z_]+): '([a-z_.A-Za-z]+)',", re.search(
            r'const BREAKDOWN_SECTIONS = \{(.*?)\n\}', self.adapter, re.S).group(1), re.M))
        self.assertEqual(sections, content_breakdown.SECTIONS)
        for key, labels in content_breakdown.FIXED_LABELS.items():
            for group, label in labels.items():
                self.assertRegex(self.adapter, r"'?" + re.escape(group) + r"'?: '" + re.escape(label) + "'")
        self.assertIn(f'BREAKDOWN_GROUP_LIMIT = {content_breakdown.GROUP_LIMIT};', self.adapter)
        periods = dict(re.findall(r"'?([0-9a-z]+)'?: \{ label: '([0-9A-Z]+)', days: [0-9a-z]+ \}", self.adapter))
        self.assertEqual(periods, {key: label for key, (label, _) in overview.PERIODS.items()})
        self.assertEqual(set(ts_list(self.adapter, 'COUNTER_STATUSES')),
                         {'ready', 'partial', 'zero_baseline', 'not_enough_history', 'stale_current', 'not_reported'})
        reasons = set(re.findall(r"'([a-z_]+)'", (ROOT / 'application/research_query/operation_kinds/counter_change.py')
                                 .read_text(encoding='utf-8').split('def _metric', 1)[1].split('def check', 1)[0]))
        self.assertLessEqual(set(ts_list(self.adapter, 'COUNTER_REASON_CODES')), reasons)
        self.assertEqual(counter_change.DEFINITION_RULE, 'reported_channel_counter_without_definition_v1')
        self.assertIn(f"'{counter_change.DEFINITION_RULE}'", self.adapter)
        exclusions = set(ts_list(self.adapter, 'FORMAT_EXCLUSIONS'))
        self.assertEqual(exclusions, set(format_totals.GAPS.values()) | {'unavailable_excluded_count', 'type_resolution_withheld'})

    def test_adapter_stays_pure(self):
        self.assertNotRegex(self.adapter, r"from 'react'|\bwindow\.|\bdocument\.|from '\.\./api'")


class OverviewEndpointTests(unittest.TestCase):
    """The Overview and the header totals/change read the shared operations."""

    def test_overview_uses_the_operation_endpoint(self):
        for name in ('LibraryOverviewSection.tsx', 'ExplorerPanels.tsx', 'ChannelFingerprint.tsx',
                     'OverviewContentBreakdown.tsx', 'PublishingRhythm.tsx'):
            text = source(name)
            self.assertNotIn('loadExplorerBaseline', text, name)
            self.assertNotIn('ChannelBaselinePayload', text, name)
        self.assertIn('createOverviewClient(requestJson)', source('LibraryOverviewSection.tsx'))

    def test_header_reads_the_header_operations(self):
        header = source('ChannelHeader.tsx')
        self.assertNotIn('loadExplorerCounterDynamicsPeriods', header)
        self.assertIn('headerClient.counterChange(', header)
        self.assertIn('headerClient.formatTotals(', header)
        # The unlisted note keeps its list-inventory proof (one thin read, list counts only);
        # the old totals route is gone, so totals are read once per channel load.
        self.assertIn('unlistedVideoCount(', header)
        self.assertIn('loadExplorerListInventory(', header)
        self.assertNotIn('loadExplorerFormatTotals', header)
        self.assertNotIn('format-totals', source('api.ts'))
        self.assertIn('loadExplorerProfile(', header)

    def test_rhythm_is_drawn_from_the_server_summary(self):
        rhythm = source('PublishingRhythm.tsx')
        self.assertNotIn('buildPublishingRhythm(', rhythm)
        self.assertNotIn('publicationPoints', rhythm)
        self.assertNotIn('useTimeBasis', rhythm, 'the zone of the reply, not a second browser computation')


class TypeTests(unittest.TestCase):
    def test_shorts_and_streams_mount_the_library_overview(self):
        shell = source('ExplorerWorkspace.tsx')
        self.assertNotIn('initialOverviewOpen', shell)
        # One Overview and one Videos section, mounted for every format; no embedded catalogue.
        self.assertEqual(shell.count('<LibraryOverviewSection overview={overview} context={context}'), 1)
        self.assertEqual(shell.count('<LibraryVideosSection videos={videos} context={context}'), 1)
        # A format turned off in config.ini shows its stub instead of the sections.
        self.assertIn("const overviewVisible = activeSection === 'overview' && !formatOff;", shell)
        self.assertNotIn('ResearchCatalogue', shell)

    def test_long_texts_stay_as_the_reference_reads_them(self):
        adapter = source('libraryData/overview.ts')
        self.assertIn("long: { adjective: 'long-form', plural: 'long-form videos', tab: 'Videos' }", adapter)
        self.assertIn('summaries and distributions use stored current {OVERVIEW_FORMAT_WORDS[videoType].adjective} rows',
                      source('ChannelFingerprint.tsx'))
        for filename in ('ExplorerPanels.tsx', 'LibraryVideosSection.tsx', 'LibraryOverviewSection.tsx'):
            self.assertNotIn('hydrationPayload(', source(filename))
            self.assertNotIn('typeReadiness.', source(filename))

    def test_bucket_jump_is_offered_where_videos_can_open_it(self):
        overview = source('LibraryOverviewSection.tsx')
        # Library Videos is mounted for Shorts and Streams, so every format offers the jump.
        self.assertNotIn('BUCKET_JUMP_TYPES', overview)
        self.assertIn("onOverviewBucket={payload?.period?.end_at && visibleOverviewState.kind === 'ready' ? onOverviewBucket : undefined}", overview)
        self.assertIn('overviewBucketConditions(overview?.period, field, key, overviewPresets(capabilities, videoType, field))',
                      overview)


class LayoutFixTests(unittest.TestCase):
    """Small layout defects fixed without a layout change."""

    def test_time_label_has_the_period_label_style(self):
        overview = source('LibraryOverviewSection.tsx')
        self.assertIn('<span className="explorer-v2-period-label">Period</span>', overview)
        self.assertIn('<span className="explorer-v2-period-label">Time</span>', overview)
        for name in ('channel-overview.css', 'redesign.css'):
            self.assertIn('.explorer-v2-period-selector > .explorer-v2-period-label {', source(name), name)

    def test_language_column_stays_inside_its_column(self):
        css = source('channel-overview.css')
        stack = re.search(r'\.overview-language-stack \{(.*?)\}', css, re.S).group(1)
        self.assertIn('grid-template-columns: minmax(0, 1fr);', stack)
        self.assertIn('.overview-language-metric { min-width: 0; }', css)
        self.assertIn('flex-wrap: wrap;', re.search(r'\.overview-language-metric > div \{(.*?)\}', css, re.S).group(1))

    def test_ring_caption_fits_inside_the_inner_circle(self):
        rhythm = source('PublishingRhythm.tsx')
        caption = re.search(r'const centerCaption = (.*?);\n', rhythm, re.S).group(1)
        self.assertNotIn('zoneLabel', caption)
        self.assertLessEqual(max(len(text) for text in re.findall(r"'([^']+)'", caption)), 11)


if __name__ == '__main__':
    unittest.main()
