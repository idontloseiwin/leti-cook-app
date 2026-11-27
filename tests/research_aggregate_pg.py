"""Catalogue aggregation, numeric precision and facet contracts on PostgreSQL."""
import sys, unittest
from unittest.mock import patch
from pathlib import Path
from dataclasses import replace
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'application'),str(Path(__file__).resolve().parent)]
import research_query_pg as native
from test_research_query_contract import query
from test_research_channels import channel_query
from research_query.validation import canonical,QueryRejected
from research_query.registry import preset_buckets
from research_query.aggregate import SUMMARY,CALENDAR,HISTORY,SUMMARY_FIELDS,METRICS
from fractions import Fraction
from statistics import median
import json as _json, os, time as _time, uuid as _uuid
from collections import Counter
from datetime import datetime as _dt, timedelta as _td


def operation(q, name=SUMMARY, parameters=None):
    return canonical(dict(version=1,operation=name,query=q,parameters=parameters or {}))

class AggregatePostgres(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        native.NativeQueryTests.setUpClass.__func__(cls)
        cls.admin.execute("INSERT INTO public.video_snapshots(video_id,recorded_at,views,views_definition_epoch) SELECT video_id,checked_at,views,'fixture.current.v1' FROM public.videos WHERE views>=0 AND checked_at IS NOT NULL")
    tearDownClass=classmethod(native.NativeQueryTests.tearDownClass.__func__)
    refresh=classmethod(native.NativeQueryTests.refresh.__func__)

    def test_whole_population_numeric_summary_all_formats(self):
        for kind in ('long','short','stream'):
            with self.subTest(kind=kind):
                q=query(video_types=[kind],select=list(SUMMARY_FIELDS),page={'size':100})
                rows=self.service.execute(canonical(q))['rows']
                q['page']={'size':1}
                out=self.service.execute_operation(operation(q))['result']
                self.assertEqual(out['population'],len(rows))
                for field,metric in zip(SUMMARY_FIELDS,METRICS):
                    values=[r['cells'][field]['value'] for r in rows if r['cells'][field]['state']=='known']
                    values=[len(v) if field in {'video.title','video.tags'} else int(v) for v in values]
                    metric_result = out['metrics'][metric]
                    self.assertEqual(metric_result['known_count'], len(values))
                    self.assertEqual(metric_result['missing_count'], len(rows) - len(values))
                    if values:
                        self.assertEqual(int(metric_result['total']), sum(values))
                        self.assertEqual(int(metric_result['min']), min(values))
                        self.assertEqual(int(metric_result['max']), max(values))
                        self.assertEqual(Fraction(metric_result['median']), median([Fraction(v) for v in values]))
                    presets = preset_buckets('duration' if metric == 'duration_seconds' else metric, kind)
                    buckets = [(b.key, sum((b.min is None or v >= b.min) and (b.max is None or v < b.max)
                                           for v in values)) for b in presets]
                    self.assertEqual([(b['key'], b['count']) for b in metric_result['buckets']], buckets)

    def test_histogram_counts_values_at_every_matching_boundary(self):
        from decimal import Decimal
        from psycopg.types.json import Jsonb
        from db.reads.research_operations.summary import HISTOGRAM_CTES
        # Include repeated/unique values, a missing metric, overlaps, exact
        # edges and an integer too large for a binary float to distinguish.
        large = 9007199254740993
        values = [None, 0, 0, Decimal('0.25'), 1, 2, 2, *range(3, 204), large-1, large]
        bounds = [('metric', 'below', None, 0), ('metric', 'one', 0, 1),
                  ('metric', 'overlap', 0, 3), ('metric', 'two', 2, 3),
                  ('metric', 'empty', 300, 301), ('metric', 'large', large, None),
                  ('metric', 'all', None, None), ('missing', 'all', None, None),
                  ('unbounded', 'all', None, None)]
        buckets = [dict(metric=metric, key=key, label=key, lower=low, upper=high, ordinal=i)
                   for i,(metric,key,low,high) in enumerate(bounds)]
        known = {'metric': [v for v in values if v is not None], 'unbounded': [0], 'missing': []}
        expected = [sum((lo is None or v >= lo) and (hi is None or v < hi) for v in known[metric])
                    for metric, key, lo, hi in bounds]
        sql = ("WITH metric_values AS (SELECT 'metric'::text AS metric,value FROM unnest(%(values)s::numeric[]) AS data(value) "
               "UNION ALL SELECT 'unbounded',0::numeric),"
               + HISTOGRAM_CTES + " SELECT count FROM buckets ORDER BY ordinal")
        params = {'values': [Decimal(v) if v is not None else None for v in values], 'buckets': Jsonb(buckets)}
        actual = self.admin.execute(sql, params).fetchall()
        self.assertEqual([row[0] for row in actual], expected)

    def test_02_calendar_uses_whole_population_timezone_and_year(self):
        q=query(select=['video.published_at'],page={'size':100})
        rows=self.service.execute(canonical(q))['rows']
        from datetime import datetime
        from zoneinfo import ZoneInfo
        zone='America/New_York'
        dates=[datetime.fromisoformat(r['cells']['video.published_at']['value'].replace('Z','+00:00')).astimezone(ZoneInfo(zone)) for r in rows if r['cells']['video.published_at']['state']=='known']
        year=dates[0].year
        q['page']={'size':1}
        result=self.service.execute_operation(operation(q,CALENDAR,dict(year=year,time_zone=zone)))['result']
        self.assertEqual(sum(x['count'] for x in result['days']),sum(d.year==year for d in dates))
        self.assertEqual(sum(x['count'] for x in result['hours']),len(dates))
        self.assertEqual(sum(x['count'] for x in result['weekdays']),len(dates))

    def test_03_channel_history_is_separate_real_source(self):
        q=channel_query();identity=q['scope']['id']
        self.admin.execute("INSERT INTO public.channel_snapshots(channel_id,recorded_at,views,subscribers,source) VALUES(%s,clock_timestamp()-interval '2 hours',9007199254740993,NULL,'youtube_channel_metadata_refresh'),(%s,clock_timestamp()-interval '1 hour',9007199254740995,0,'youtube_channel_metadata_refresh')",(identity,identity))
        try:
            response=self.service.execute_operation(operation(q,HISTORY,dict(metric='views',hours=24,bucket_hours=1)))
            self.assertIsNone(response['context']['observation_age_limit_seconds'])
            result=response['result']
            self.assertIsNone(result['definition_epoch']);self.assertEqual([p['value'] for p in result['points']],['9007199254740993','9007199254740995'])
            result=self.service.execute_operation(operation(q,HISTORY,dict(metric='subscribers',hours=24,bucket_hours=1)))['result']
            self.assertEqual([p['value'] for p in result['points']],['0'])
        finally:self.admin.execute('DELETE FROM public.channel_snapshots WHERE channel_id=%s',(identity,))

    def test_04_view_definitions_never_mix_or_guess(self):
        q=query()
        video='a0000000001'
        try:
            # A stored stamp always wins; different stamps are never mixed.
            for epoch,state in [('fixture.other.v1','mixed_definition_epochs'),(None,'mixed_definition_epochs')]:
                self.admin.execute('UPDATE public.video_snapshots SET views_definition_epoch=%s WHERE video_id=%s',(epoch,video))
                result=self.service.execute_operation(operation(q))['result']
                self.assertEqual(result['metrics']['views']['state'],state)
                self.assertEqual(result['metrics']['views']['known_count'],0)
                self.assertIsNone(result['metrics']['views']['total'])
                self.assertGreater(result['metrics']['duration_seconds']['known_count'],0)
            # Snapshots without a views definition stamp are read with the writer's date rule.
            self.admin.execute('UPDATE public.video_snapshots SET views_definition_epoch=NULL')
            result=self.service.execute_operation(operation(q))['result']
            self.assertEqual(result['metrics']['views']['state'],'known')
            self.assertEqual(result['metrics']['views']['definition_epoch'],'youtube.views.play_start.20260824')
            self.assertGreater(result['metrics']['views']['known_count'],0)
        finally:self.admin.execute("UPDATE public.video_snapshots SET views_definition_epoch='fixture.current.v1'")

    def test_05_denied_and_cancelled_reads_return_no_data(self):
        from research_query.service import QueryService
        service=QueryService(self.repo,lambda:replace(self.context,allowed_operations=frozenset()))
        with self.assertRaisesRegex(QueryRejected,'policy_denied'):service.execute_operation(operation(query()))
        with self.assertRaisesRegex(QueryRejected,'query_budget_exceeded'):self.service.execute_operation(operation(query()),cancelled=lambda:True)
        with self.assertRaisesRegex(QueryRejected,'invalid_query'):self.service.execute_operation(operation(query(),HISTORY,dict(metric='views',hours=24,bucket_hours=1)))

    def test_06_operation_age_limit_matches_the_selected_population(self):
        from research_query import observation_limits
        from test_observation_limits import edited_schedule
        from collection_schedule import CollectionSchedules
        video='a0000000003'
        old=self.admin.execute('SELECT published_at,metadata_checked_at FROM public.videos WHERE video_id=%s',(video,)).fetchone()
        identity={'field':'video.id','op':'eq','value':video}
        # True for known and unknown changes: the implicit age cap alone removes old rows.
        condition={'all':[identity,{'any':[{'field':'observation.views_delta','op':'is_known'},
                                          {'field':'observation.views_delta','op':'is_unknown'}]}]}
        unlimited=replace(CollectionSchedules(),long_counters=edited_schedule().stream_counters)
        try:
            for age in (30,2):
                self.admin.execute("UPDATE public.videos SET published_at=now()-make_interval(days=>%s),metadata_checked_at=now() WHERE video_id=%s",(age,video))
                self.refresh()
                for schedule,limit in ((CollectionSchedules(),604800),(edited_schedule(),0),(unlimited,None)):
                    with patch.object(observation_limits,'process_schedules',return_value=schedule):
                        for name,parameters in ((SUMMARY,{}),(CALENDAR,dict(year=2026,time_zone='UTC'))):
                            with self.subTest(age=age,limit=limit,operation=name):
                                result=self.service.execute_operation(operation(query(select=['video.id'],where=condition),name,parameters))
                                self.assertEqual(result['context']['observation_age_limit_seconds'],limit)
                                self.assertEqual(result['result']['population'],int(limit is None or limit>0 and age==2))
                                raw=self.service.execute_operation(operation(query(select=['video.id'],where=identity),name,parameters))
                                self.assertIsNone(raw['context']['observation_age_limit_seconds'])
                                self.assertEqual(raw['result']['population'],1)
        finally:
            self.admin.execute('UPDATE public.videos SET published_at=%s,metadata_checked_at=%s WHERE video_id=%s',(*old,video))

    def test_07_exact_summaries_keep_counters_and_fractions(self):
        """The cases of test_baseline_exact_summaries
        through the shared statistics SQL (population_stats.SUMMARIES) and its presentation (summary.round_metric):
        exact counters above 2**53, fractions, half-even hundredths, empty and single samples."""
        from decimal import Decimal, localcontext
        from db.reads.research_operations.population_stats import SUMMARIES
        from research_query.operation_kinds.summary import round_metric
        from test_baseline_exact_summaries import CASES, KEYS
        sql = ("WITH metric_values AS (SELECT 'x'::text AS metric,v AS value FROM unnest(%s::numeric[]) v),\n" + SUMMARIES
               + "\nSELECT known_count,total,mean,median,q1,q3,min,max FROM summaries")
        texts = {}
        for name, (values, sample_count, expected) in CASES.items():
            with self.subTest(case=name):
                with localcontext() as context:
                    context.prec = 40  # A fraction enters SQL as a 40-digit numeric.
                    numbers = [str(Decimal(v.numerator) / Decimal(v.denominator)) if hasattr(v, 'denominator') else str(v) for v in values]
                row = self.admin.execute(sql, (numbers,)).fetchone()
                known, *statistics = row if row else (0, *[None] * 7)
                texts[name] = dict(zip(('total', 'mean', 'median', 'q1', 'q3', 'min', 'max'), statistics))
                metric = round_metric(dict(texts[name], known_count=known, missing_count=sample_count - known))
                self.assertEqual({key: metric[key] for key in KEYS},
                                 {key: None if expected[key] is None else expected[key] if key.endswith('_count') else str(expected[key])
                                  for key in KEYS})
        print('EXACT_SUMMARY_SQL ' + _json.dumps(texts), flush=True)

    def test_quartile_ranks_for_small_populations(self):
        from decimal import Decimal
        from db.reads.research_operations.population_stats import SUMMARIES
        # Two equal values, a fraction and neighboring integers above the
        # precision of a binary float exercise all four rank remainders.
        known = [Decimal(x) for x in ('0', '0', '-0.25', '2.25', '9007199254740993',
                                      '9007199254740994', '4', '9')]
        sql = ("WITH metric_values AS (SELECT 'x' AS metric,v AS value FROM unnest(%s::numeric[]) v "
               "WHERE v IS NOT NULL)," + SUMMARIES + " SELECT known_count,total,min,max,q1,median,q3 FROM summaries")
        for size in range(9):
            with self.subTest(size=size):
                sample = known[:size]
                row = self.admin.execute(sql, ([*sample, None],)).fetchone()
                if not size:
                    self.assertIsNone(row)
                    continue
                count, total, low, high, *quartiles = row
                self.assertEqual((count, Decimal(total), Decimal(low), Decimal(high)),
                                 (size, sum(sample), min(sample), max(sample)))
                ordered = sorted(map(Fraction, sample))
                expected = []
                for p in (Fraction(1, 4), Fraction(1, 2), Fraction(3, 4)):
                    position, fraction = divmod((size - 1) * p, 1)
                    following = min(position + 1, size - 1)
                    expected.append(ordered[position] * (1 - fraction) + ordered[following] * fraction)
                self.assertEqual(list(map(Fraction, quartiles)), expected)

    @classmethod
    def facet_seed(cls):
        """Own channels in their own folder, seeded once: other tests never see them."""
        if getattr(cls, '_facet', None):
            return cls._facet
        from research_frozen_pg import add_channel
        now = cls.admin.execute('SELECT clock_timestamp()').fetchone()[0]
        folder = str(_uuid.uuid4())
        cls.admin.execute("INSERT INTO public.folders(id,name) VALUES(%s,'Aggregate fixture')", (folder,))
        started = _time.perf_counter()
        for channel in FACET_CHANNELS.values():
            add_channel(cls.admin, channel, folder, now)
        for kind in ('long', 'short', 'stream'):
            fresh, stale = facet_rows(kind), facet_stale_rows()
            facet_insert(cls.admin, FACET_CHANNELS['clean'], 'wc', kind, fresh, now)
            facet_insert(cls.admin, FACET_CHANNELS['mixed'], 'wm', kind, fresh + stale, now)
            facet_insert(cls.admin, FACET_CHANNELS['stale'], 'ws', kind, stale, now)
        facet_insert(cls.admin, FACET_CHANNELS['rates'], 'wr', 'long', facet_rate_rows(), now)
        excluded = facet_rows('long', 1)[-1]  # One kept row, then a private row that also left the uploads playlist.
        facet_insert(cls.admin, FACET_CHANNELS['gone'], 'wg', 'long', [facet_rows('long', 1)[0], dict(excluded, state='private_gone')], now)
        cls._facet = dict(now=now)
        return cls._facet

    def test_facets_count_twenty_thousand_rows(self):
        from research_query.aggregate_service import DEADLINE_SECONDS, MAX_OUTPUT_BYTES
        self.facet_seed()
        channel = FACET_CHANNELS['big']
        now = self.admin.execute('SELECT clock_timestamp()').fetchone()[0]
        facet_big(self.admin, channel, now)
        from db.inventory.search_projection import SearchProjectionRepository
        projection = SearchProjectionRepository(self.db)
        # These rows carry every facet field; small batches stay inside the refresh time limit on a slow runner.
        while projection.refresh(250):
            pass
        expected = sum(1 for n in range(1, 20001) if (n*7919) % 20000000 >= 10000000)
        for condition, count in (([], 20000), ([{'field':'video.views','op':'gte','value':'10000000'}], expected)):
            scenario = dict(base=condition, zone='UTC', conditions={})
            started = _time.perf_counter()
            result = self.query_facets(channel, 'long', scenario)
            self.assertLess(_time.perf_counter() - started, DEADLINE_SECONDS)
            self.assertLess(len(canonical(result)), MAX_OUTPUT_BYTES)
            self.assertEqual(result['result']['population']['matched_count'], count)

    def query_facets(self, channel, kind, scenario, groups=None):
        q = query(scope={'kind': 'channel', 'id': channel}, video_types=[kind], select=['video.id'],
                  where=facet_all(scenario['base']), time={'mode': 'current', 'timezone': scenario['zone']})
        parameters = {'population': {'kind': 'query'}, 'conditions': scenario['conditions']}
        if groups:
            parameters = {**parameters, 'groups': groups,
                          'conditions': {g: c for g, c in scenario['conditions'].items() if g in groups}}
        return self.service.execute_operation(operation(q, FACETS, parameters))


    def query_rows(self, channel, kind, where, zone, order=('video.published_at', 'desc'), select=('video.id', 'video.category_id')):
        q = query(scope={'kind': 'channel', 'id': channel}, video_types=[kind], select=list(select), where=where,
                  order_by=[{'field': order[0], 'direction': order[1]}], time={'mode': 'current', 'timezone': zone}, page={'size': 200})
        rows = []
        while True:
            page = self.service.execute(canonical(q))
            rows += page['rows']
            if not page['page']['has_more']:
                return rows
            q['page'] = {'size': 200, 'after': page['page']['next_cursor']}

    def test_facets_cover_the_selected_population_and_zone(self):
        self.facet_seed()
        for scenario in FACET_SCENARIOS:
            for kind in ('long', 'short', 'stream'):
                with self.subTest(scenario=scenario['name'], kind=kind):
                    new = self.query_facets(FACET_CHANNELS['clean'], kind, scenario)
                    result = new['result']
                    conditions = scenario['base'] + list(scenario['conditions'].values())
                    rows = self.query_rows(FACET_CHANNELS['clean'], kind, facet_all(conditions), scenario['zone'])
                    self.assertEqual(result['population']['matched_count'], len(rows))
                    if scenario['name'] == 'all':
                        seeded = [row for row in facet_rows(kind) if row['state'] == 'fresh']
                        self.assertEqual(result['population']['matched_count'], len(seeded))
                        for name in ('duration', 'views', 'likes', 'comments', 'title_length', 'tag_count'):
                            field = {'title_length': 'title', 'tag_count': 'tags'}.get(name, name)
                            values = [row[field] for row in seeded if row[field] is not None]
                            if name in ('title_length', 'tag_count'):
                                values = [len(value) for value in values]
                            presets = PRESETS['by_video_type'].get(kind, {}).get(name, PRESETS[name])
                            expected = {
                                bucket['key']: sum((bucket['min'] is None or bucket['min'] <= value)
                                                   and (bucket['max'] is None or value < bucket['max'])
                                                   for value in values)
                                for bucket in presets
                            }
                            group = result['groups'][name]
                            self.assertEqual(group['unknown_count'], len(seeded) - len(values), name)
                            self.assertEqual(facet_bucket_counts(group), expected, name)
                        counts = Counter(row['category'] for row in seeded if row['category'])
                        self.assertEqual({c['id']: c['count'] for c in result['groups']['category']['categories']}, counts)
                    elif scenario['name'] == 'search_views_duration':
                        seeded = [row for row in facet_rows(kind) if row['state'] == 'fresh']
                        title_matches = [row for row in seeded if 'strasse' in (row['title'] or '').casefold()]
                        self.assertEqual(result['population']['matched_count'],
                                         sum(row['duration'] is not None and row['duration'] >= 60
                                             and row['views'] is not None and 1000 <= row['views'] < 100000
                                             for row in title_matches))
                    self.assertEqual(result['time_zone'], scenario['zone'])

    def test_sorting_preserves_filtered_membership(self):
        self.facet_seed()
        for kind in ('long', 'short', 'stream'):
            for scenario in FACET_SCENARIOS:
                where = facet_all(scenario['base'] + list(scenario['conditions'].values()))
                for field in ('video.published_at', 'video.views', 'video.duration_seconds'):
                    ordered = []
                    for direction in ('asc', 'desc'):
                        rows = self.query_rows(FACET_CHANNELS['clean'], kind, where, scenario['zone'], (field, direction), ('video.id', field))
                        ordered.append({row['id'] for row in rows})
                        values = [row['cells'][field]['value'] for row in rows if row['cells'][field]['state'] == 'known']
                        if field != 'video.published_at':
                            values = list(map(int, values))
                        self.assertEqual(values, sorted(values, reverse=direction == 'desc'))
                    self.assertEqual(*ordered)

    def test_facet_03_stale_rows_stay_in_the_population_as_unknown_values(self):
        self.facet_seed()
        grew = Counter()
        for scenario in FACET_SCENARIOS:
            for kind in ('long', 'short', 'stream'):
                with self.subTest(scenario=scenario['name'], kind=kind):
                    mixed, clean, stale = (self.query_facets(FACET_CHANNELS[name], kind, scenario)['result'] for name in ('mixed', 'clean', 'stale'))
                    self.assertEqual(mixed['population']['matched_count'], clean['population']['matched_count'] + stale['population']['matched_count'])
                    for group in ('views', 'likes', 'comments', 'title_length', 'tag_count', 'duration'):
                        a, b, c = (facet_bucket_counts(r['groups'][group]) for r in (mixed, clean, stale))
                        self.assertEqual(a, {k: b[k] + c[k] for k in a}, group)
                        grew[kind] += sum(c.values())
                    years = {y['year']: y['count'] for y in stale['groups']['published']['years']}
                    for entry in mixed['groups']['published']['years']:
                        clean_years = {y['year']: y['count'] for y in clean['groups']['published']['years']}
                        self.assertEqual(entry['count'], clean_years.get(entry['year'], 0) + years.get(entry['year'], 0))
                    self.assertEqual(mixed['population']['expired_counters'], clean['population']['expired_counters'] + stale['population']['expired_counters'])
        # Long and Shorts keep stale rows (values unknown); a Stream needs fresh clocks to be admitted at all.
        self.assertGreater(grew['long'], 0)
        self.assertGreater(grew['short'], 0)
        self.assertEqual(grew['stream'], 0)

    def test_facet_04_calculated_fields_and_exact_rates(self):
        self.facet_seed()
        rows = self.query_rows(FACET_CHANNELS['rates'], 'long', None, 'UTC', ('video.id', 'asc'),
                                 select=('video.id', 'video.like_rate', 'video.comment_rate', 'video.title_length', 'video.tag_count', 'video.likes', 'video.views'))
        seeded = {f'wrl{n:08d}': row for n, row in enumerate(facet_rate_rows(), 1)}
        checked = 0
        for row in rows:
            sample = seeded[row['id']]
            for field, name in (('video.like_rate', 'likes'), ('video.comment_rate', 'comments')):
                cell = row['cells'][field]
                value = Fraction(sample[name] * 100, sample['views']) if sample[name] is not None and sample['views'] else None
                if value is not None and value > 2**64 - 1:
                    self.assertEqual((cell['state'], cell['value']), ('unavailable', None))
                elif value is None:
                    self.assertIsNone(cell['value'])
                    self.assertIn(cell['state'], {'unknown', 'unavailable'})
                else:
                    self.assertEqual(cell['value'], facet_decimal(round(value, 6)), (row['id'], field))
                    checked += 1
            self.assertEqual(row['cells']['video.title_length']['value'], str(len(sample['title'])))
            self.assertEqual(row['cells']['video.tag_count']['value'], str(len(sample['tags'])))
        self.assertGreaterEqual(checked, 12)
        for bound in ('0', '0.000062', '0.000063', '2.5', '12.5', '33.333333', '33.333334', '66.666667', '9223372036854.775807', '9223372036854.775808'):
            for op, key in (('gte', 'min_like_rate'), ('lte', 'max_like_rate')):
                with self.subTest(bound=bound, op=op):
                    ids = {r['id'] for r in self.query_rows(FACET_CHANNELS['rates'], 'long', {'field': 'video.like_rate', 'op': op, 'value': bound}, 'UTC')}
                    expected = set()
                    for video_id, sample in seeded.items():
                        if sample['state'] != 'fresh' or not sample['views'] or sample['likes'] is None:
                            continue
                        rate = Fraction(sample['likes'] * 100, sample['views'])
                        if rate <= 2**64 - 1 and (rate >= Fraction(bound) if op == 'gte' else rate <= Fraction(bound)):
                            expected.add(video_id)
                    self.assertEqual(ids, expected)
        where = {'all': [{'field': 'video.like_rate', 'op': 'gte', 'value': '12.5'}, {'field': 'video.comment_rate', 'op': 'lte', 'value': '33.333333'},
                         {'field': 'video.title_length', 'op': 'gte', 'value': '3'}, {'field': 'video.tag_count', 'op': 'lte', 'value': '15'}]}
        selected = self.query_rows(FACET_CHANNELS['rates'], 'long', where, 'UTC', ('video.views', 'desc'))
        expected = [key for key, sample in seeded.items() if sample['state'] == 'fresh' and sample['views']
                    and sample['likes'] is not None and Fraction('12.5') <= Fraction(sample['likes'] * 100, sample['views']) <= 2**64-1
                    and Fraction(sample['comments'] * 100, sample['views']) <= Fraction('33.333333') and len(sample['tags']) <= 15]
        self.assertEqual({r['id'] for r in selected}, set(expected))

    def test_facet_05_local_dates_years_and_search_in_the_viewer_zone(self):
        from zoneinfo import ZoneInfo
        self.facet_seed()
        for zone in ('UTC', 'Pacific/Kiritimati', 'Pacific/Pago_Pago', 'Europe/Berlin', 'America/Santiago', 'Asia/Kathmandu'):
            rows = self.query_rows(FACET_CHANNELS['clean'], 'long', None, zone, ('video.id', 'asc'),
                                     select=('video.id', 'video.published_at', 'video.published_date', 'video.published_year'))
            for row in rows:
                with self.subTest(zone=zone, video=row['id']):
                    published = row['cells']['video.published_at']['value']
                    date, year = row['cells']['video.published_date'], row['cells']['video.published_year']
                    if published is None:
                        self.assertEqual((date['state'], year['state']), ('unknown', 'unknown'))
                        continue
                    local = _dt.fromisoformat(published.replace('Z', '+00:00')).astimezone(ZoneInfo(zone))
                    self.assertEqual((date['value'], year['value']), (local.date().isoformat(), str(local.year)))
        candidates = self.query_rows(FACET_CHANNELS['clean'], 'long', None, 'UTC', select=('video.id','video.title'))
        for text in FACET_SEARCHES:
            with self.subTest(text=text):
                expected = {r['id'] for r in candidates if ' '.join(text.split()).casefold() in (r['cells']['video.title']['value'] or '').casefold()}
                new = [r['id'] for r in self.query_rows(FACET_CHANNELS['clean'], 'long', {'field': 'video.title', 'op': 'contains_casefold_v1', 'value': text}, 'UTC')]
                self.assertEqual(set(new), expected)

    def test_facet_06_one_reason_for_rows_outside_the_population(self):
        self.facet_seed()
        # private, stale availability, a future date, a scheduled premiere (+ a Stream not ended);
        # the names are the shared admission stages (research_sql.ADMISSION_STAGES).
        expected = {'long': {'source_unavailable': 1, 'source_stale': 1, 'source_unpublished': 2},
                    'short': {'source_unavailable': 1, 'source_stale': 1, 'source_unpublished': 2},
                    'stream': {'source_unavailable': 1, 'source_stale': 1, 'source_unpublished': 2, 'stream_incomplete': 1}}
        for kind, reasons in expected.items():
            with self.subTest(kind=kind):
                new = self.query_facets(FACET_CHANNELS['clean'], kind, FACET_SCENARIOS[0])
                excluded = new['result']['population']['excluded']
                self.assertEqual({k: v for k, v in excluded.items() if v}, reasons)
                self.assertEqual(new['coverage']['reasons'], {'source_rows_unavailable': sum(reasons.values())})
                self.assertEqual(new['coverage']['status'], 'partial')
                frozen = self.service.execute(canonical(query(scope={'kind': 'channel', 'id': FACET_CHANNELS['clean']}, video_types=[kind],
                                                              consistency='frozen', page={'size': 50}))) if self.service.frozen else None
                if frozen is not None:
                    self.assertEqual(frozen['coverage']['reasons'].get('source_rows_unavailable'), sum(reasons.values()))
        new = self.query_facets(FACET_CHANNELS['rates'], 'long', FACET_SCENARIOS[0])
        self.assertEqual({k: v for k, v in new['result']['population']['excluded'].items() if v}, {'source_unpublished': 1})

    def test_facet_07_facet_counts_match_the_rows_a_preset_selects(self):
        """Choosing a preset shows exactly the counted rows; Shorts and Streams use their own duration sets."""
        self.facet_seed()
        for kind in ('long', 'short', 'stream'):
            for scenario in (FACET_SCENARIOS[0], FACET_SCENARIOS[2]):
                result = self.query_facets(FACET_CHANNELS['clean'], kind, scenario)['result']
                others = [c for g, c in scenario['conditions'].items() if g != 'duration']
                for bucket in result['groups']['duration']['buckets']:
                    with self.subTest(kind=kind, scenario=scenario['name'], bucket=bucket['key']):
                        bound = [{'field': 'video.duration_seconds', 'op': op, 'value': str(value)}
                                 for op, value in (('gte', bucket['min']), ('lt', bucket['max'])) if value is not None]
                        rows = self.query_rows(FACET_CHANNELS['clean'], kind, facet_all(scenario['base'] + others + bound), scenario['zone'])
                        self.assertEqual(len(rows), bucket['count'])

    def test_facet_09_growth_group_counts_equal_the_growth_summary(self):
        """Growth facets count the statuses of rows that satisfy the other conditions."""
        from research_query import growth
        self.facet_seed()
        for kind in ('long', 'short', 'stream'):
            for views in (None, {'field': 'video.views', 'op': 'gte', 'value': '1000'}):
                with self.subTest(kind=kind, views=views):
                    q = query(scope={'kind': 'channel', 'id': FACET_CHANNELS['clean']}, video_types=[kind], select=['video.id'], where=views)
                    result = self.service.execute_operation(operation(q, growth.SUMMARY_ID, {}))['result']
                    summary = {status: result[status + '_count'] for status in growth.STATUSES}
                    conditions = {'growth': {'field': growth.STATUS, 'op': 'eq', 'value': 'ready'}}
                    if views:
                        conditions['views'] = views
                    q['where'] = None
                    facets = self.service.execute_operation(operation(q, FACETS, {'population': {'kind': 'query'},
                        'groups': ['views', 'growth'], 'conditions': conditions}))['result']['groups']['growth']
                    self.assertEqual({v['value']: v['count'] for v in facets['values']},
                                     summary)
                    self.assertEqual(sum(summary.values()), result['population'])
                    self.assertEqual(facets['unknown_count'], 0)

    def test_facet_10_a_private_row_without_its_playlist_proof_is_not_scanned(self):
        self.facet_seed()
        q = query(scope={'kind': 'channel', 'id': FACET_CHANNELS['gone']}, video_types=['long'], select=['video.id'])
        new = self.service.execute_operation(operation(q, FACETS, {'population': {'kind': 'query'}}))
        population = new['result']['population']
        self.assertEqual((population['count'], {k: v for k, v in population['excluded'].items() if v}), (1, {}))
        self.assertEqual((new['coverage']['status'], new['coverage']['reasons']), ('complete', {}))
        fingerprint = self.service.execute_operation(operation(q, 'video.fingerprint.v1', {'period': 'max'}))
        self.assertEqual({k: v for k, v in fingerprint['result']['population']['rejected'].items() if v}, {})


FACETS = 'video.facets.v1'
def facet_big(admin, channel, now):
    """20,000 long videos: varied titles, tags, categories, counters and four and a half years of dates.
    Publication is fixed (two hours apart back from '2026-10-01T00:00:00Z') to keep the distribution deterministic;
    the clocks are relative to `now`."""
    params = dict(channel=channel, now=now, playlist='UULF' + channel[2:], last='2026-10-01T00:00:00Z')
    ids = "'wbl'||lpad(n::text,8,'0')"
    admin.execute(f"INSERT INTO public.video_inventory(video_id,channel_id,video_type) SELECT {ids},%(channel)s,'long' FROM generate_series(1,20000) n", params)
    admin.execute(f"""INSERT INTO public.video_inventory_memberships
        (video_id,channel_id,video_type,playlist_id,source_published_at,first_seen_at,last_seen_at,last_present_at,evidence_source)
        SELECT {ids},%(channel)s,'long',%(playlist)s,%(last)s::timestamptz-make_interval(hours=>n*2),
         %(now)s::timestamptz-interval '1 hour',%(now)s::timestamptz-interval '1 hour',%(now)s::timestamptz-interval '1 hour','legacy_inventory_backfill'
        FROM generate_series(1,20000) n""", params)
    admin.execute(f"""INSERT INTO public.videos (video_id,channel_id,title,views,like_count,comment_count,tags,category_id,
        checked_at,metadata_checked_at,published_at,duration,availability_status,availability_checked_at)
        SELECT {ids},%(channel)s,repeat('t',n%%120)||' video '||n,(n*7919)%%20000000,(n*31)%%300000,(n*17)%%30000,
         ARRAY(SELECT 't'||k FROM generate_series(1,n%%25) k),(ARRAY['1','10','22','27'])[n%%4+1],
         %(now)s::timestamptz-interval '1 hour',%(now)s::timestamptz-interval '1 hour',%(last)s::timestamptz-make_interval(hours=>n*2),
         'PT'||((n*13)%%7200)||'S','public',%(now)s::timestamptz-interval '1 hour'
        FROM generate_series(1,20000) n""", params)


FACET_CHANNELS = {name: 'UC' + ('Aggregate' + name.upper()).ljust(22, '0') for name in ('clean', 'mixed', 'stale', 'rates', 'big', 'gone')}
FACET_TITLES = ['Straße Guide', 'STRASSE notes', 'İstanbul diary', 'double  space  title', '', None,
              'éclair recipe', '\U0001f3ac Clip of the day', 'x' * 39, 'y' * 40, 'z' * 59, 'w' * 60, 'v' * 79, 'u' * 80,
              'Short', 'A much longer descriptive title']
FACET_VIEWS = [None, 0, 5, 999, 1000, 1001, 9999, 10000, 50000, 99999, 100000, 999999, 1000000, 9999999, 10000000, 25000000]
FACET_LIKES = [None, 0, 1, 25, 99, 100, 999, 1000, 9999, 10000, 99999, 100000, 250000]
FACET_COMMENTS = [None, 0, 3, 9, 10, 99, 100, 999, 1000, 9999, 10000, 20000]
FACET_TAGS = [None, [], ['a'], *([f't{k}' for k in range(n)] for n in (5, 6, 15, 16, 30))]
FACET_CATEGORIES = ['22', '10', '27', '1', None, '']
FACET_DURATIONS = {'long': [None, 0, 59, 299, 300, 599, 600, 1199, 1200, 2399, 2400, 3599, 3600, 7200],
                 'short': [None, 0, 14, 15, 29, 30, 59, 60, 180, 181, 100],
                 'stream': [None, 1799, 1800, 3599, 3600, 7199, 7200, 14399, 14400, 90000, 500]}
# Around new year in UTC+14 and UTC-11, Berlin and Santiago DST changes, a leap day.
FACET_PUBLISHED = ['2019-05-01T12:00:00Z', '2021-07-04T08:00:00Z', '2023-02-14T20:00:00Z', '2024-02-29T23:30:00Z',
                 '2025-06-15T12:00:00Z', '2025-12-31T23:30:00Z', '2026-01-01T10:30:00Z', '2026-03-29T00:30:00Z',
                 '2026-03-29T01:30:00Z', '2026-04-05T02:30:00Z', '2026-09-06T03:30:00Z', '2026-09-06T04:30:00Z',
                 '2026-10-01T00:00:00Z', None]


def facet_rows(kind, count=48):
    rows = [dict(state='fresh', title=FACET_TITLES[(i * 3) % 16], views=FACET_VIEWS[i % 16], likes=FACET_LIKES[(i * 7) % 13],
                 comments=FACET_COMMENTS[(i * 5) % 12], tags=FACET_TAGS[(i * 5) % 8], category=FACET_CATEGORIES[i % 6],
                 duration=FACET_DURATIONS[kind][(i * 7) % len(FACET_DURATIONS[kind])], published=FACET_PUBLISHED[(i * 5) % 14])
            for i in range(count)]
    excluded = dict(title='Excluded row', views=10, likes=1, comments=1, tags=['x'], category='22', duration=60,
                    published='2025-06-15T12:00:00Z')
    return rows + [dict(excluded, state=state) for state in ('private', 'stale_availability', 'future', 'upcoming')
                   + (('not_ended',) if kind == 'stream' else ())]


def facet_stale_rows():
    return [dict(state=s, title=t, views=v, likes=l, comments=c, tags=g, category=y, duration=d, published=p)
            for s, t, v, l, c, g, y, d, p in (
                ('stale_counters', 'Stale counters one', 5000, 50, 5, ['a', 'b'], '22', 120, '2026-05-01T00:00:00Z'),
                ('stale_counters', 'Stale counters two', 50, 1, 0, [], '10', 30, '2025-05-01T00:00:00Z'),
                ('stale_counters', 'Straße stale', 2000000, 9000, 900, None, '27', 4000, '2026-02-01T00:00:00Z'),
                ('stale_metadata', 'Stale metadata one', 15000, 150, 15, ['a'], '22', 600, '2026-05-01T00:00:00Z'),
                ('stale_metadata', 'Stale metadata two', 3, 0, 0, [], '1', 45, '2025-05-01T00:00:00Z'))]


def facet_rate_rows():
    cases = [(1, 3), (1, 8), (1, 1600000), (25, 1000), (2, 3), (9223372036854775807, 100000000), (5, 0), (None, 100), (0, 7),
             (333333, 1000000), (1, 6), (7, 1600000), (125, 1000), (1, 7), (3, 1600000)]
    rows = [dict(state='fresh', title='Rate ' + str(n), views=views, likes=likes, comments=None if likes is None else likes // 3,
                 tags=[f't{k}' for k in range(n % 20)], category='22', duration=100, published='2026-06-01T00:00:00Z')
            for n, (likes, views) in enumerate(cases)]
    # Above the exact wire range (2**64): the value is unavailable, never rounded or capped.
    rows.append(dict(rows[0], title='Rate over range', likes=9223372036854775807, views=3, comments=1))
    # A live premiere has no completed long-form inventory membership.
    return rows + [dict(rows[1], state='live', title='Live premiere')]


def facet_insert(admin, channel, code, kind, rows, now):
    from research_frozen_pg import PREFIX
    letter, playlist = {'long': 'l', 'short': 's', 'stream': 't'}[kind], PREFIX[kind] + channel[2:]
    fresh, stale = now - _td(hours=1), now - _td(hours=700)
    for n, row in enumerate(rows, 1):
        video, state = f'{code}{letter}{n:08d}', row['state']
        published = (now + _td(days=2) if state == 'future' else now + _td(days=1) if state == 'upcoming'
                     else None if row['published'] is None else _dt.fromisoformat(row['published'].replace('Z', '+00:00')))
        admin.execute('INSERT INTO public.video_inventory(video_id,channel_id,video_type) VALUES(%s,%s,%s)', (video, channel, kind))
        admin.execute('''INSERT INTO public.video_inventory_memberships
            (video_id,channel_id,video_type,playlist_id,source_published_at,first_seen_at,last_seen_at,last_present_at,evidence_source)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'legacy_inventory_backfill')''', (video, channel, kind, playlist, published, *(
            (stale,) * 3 if state == 'private_gone' else (fresh,) * 3)))
        admin.execute('''INSERT INTO public.videos (video_id,channel_id,title,views,like_count,comment_count,tags,category_id,
            checked_at,metadata_checked_at,published_at,duration,availability_status,availability_checked_at,stream_actual_end_at,publication_status)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
            (video, channel, row['title'], row['views'], row['likes'], row['comments'], row['tags'], row['category'],
             stale if state == 'stale_counters' else fresh, stale if state == 'stale_metadata' else fresh, published,
             None if row['duration'] is None else f"PT{row['duration']}S", 'private' if state in ('private', 'private_gone') else 'public',
             stale if state == 'stale_availability' else fresh,
             now - _td(hours=2) if kind == 'stream' and state != 'not_ended' else None,
             {'upcoming': 'upcoming', 'live': 'live'}.get(state)))




# The shared bucket registry. The top-level ranges are for long-form videos.
PRESETS = _json.loads((Path(__file__).resolve().parents[1] / 'application' / 'shared' / 'catalogue-filter-presets.json').read_text(encoding='utf-8'))
FACET_BIG_LAST = '2026-10-01T00:00:00Z'
# Title searches of test_facet_05 (Unicode casefold, collapsed whitespace).
FACET_SEARCHES = ('STRASSE', '  straße 　', 'i̇stanbul', 'istanbul', 'double space', 'double  space', 'ÉCLAIR', '\U0001f3ac clip')


def facet_presets(group, keys, field):
    alternatives = []
    for bucket in PRESETS[group]:
        if bucket['key'] in keys:
            leaves = [{'field': field, 'op': op, 'value': str(bucket[end])} for op, end in (('gte', 'min'), ('lt', 'max'))
                      if bucket[end] is not None]
            alternatives.append(leaves[0] if len(leaves) == 1 else {'all': leaves})
    return alternatives[0] if len(alternatives) == 1 else {'any': alternatives}


def facet_all(nodes):
    nodes = [node for node in nodes if node]
    return None if not nodes else nodes[0] if len(nodes) == 1 else {'all': nodes}


def facet_cohort(start, through):
    window = [{'field': 'video.published_at', 'op': 'lte', 'value': through}]
    if start is None:  # Library keeps unknown dates only in a cohort without a start.
        return {'any': [{'field': 'video.published_at', 'op': 'is_unknown'}, window[0]]}
    return {'all': [{'field': 'video.published_at', 'op': 'gte', 'value': start}, *window]}


def facet_bucket_counts(group):
    return {bucket['key']: bucket['count'] for bucket in group['buckets']}


def facet_decimal(value):
    from decimal import Decimal
    text = format(Decimal(value.numerator) / Decimal(value.denominator), 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


FACET_SCENARIOS = [
    dict(name='all', zone='UTC', custom={}, cf={}, base=[], conditions={}),
    dict(name='search_views_duration', zone='UTC', custom={'q': '  straSSe  　 ', 'min_duration_seconds': 60},
         cf={'view_buckets': ['1k_10k', '10k_100k']},
         base=[{'field': 'video.title', 'op': 'contains_casefold_v1', 'value': '  straSSe  　 '}],
         conditions={'views': facet_presets('views', ['1k_10k', '10k_100k'], 'video.views'),
                     'duration': {'field': 'video.duration_seconds', 'op': 'gte', 'value': '60'}}),
    dict(name='category_years_title_tags', zone='Pacific/Kiritimati', custom={'category_id': '22'},
         cf={'published_years': [2026, 2025], 'published_older_than': 2023, 'title_length_buckets': ['under_40', '60_80'],
             'bounds': {'min_tag_count': '1', 'max_tag_count': '15'}},
         base=[],
         conditions={'category': {'field': 'video.category_id', 'op': 'eq', 'value': '22'},
                     'published': {'any': [{'field': 'video.published_year', 'op': 'in', 'value': ['2026', '2025']},
                                           {'field': 'video.published_year', 'op': 'lt', 'value': '2023'}]},
                     'title_length': facet_presets('title_length', ['under_40', '60_80'], 'video.title_length'),
                     'tag_count': {'all': [{'field': 'video.tag_count', 'op': 'gte', 'value': '1'},
                                           {'field': 'video.tag_count', 'op': 'lte', 'value': '15'}]}}),
    dict(name='likes_comments_rates_dates', zone='Europe/Berlin', custom={'published_from': '2024-03-01', 'published_before': '2026-03-30'},
         cf={'like_buckets': ['100_1k', '1k_10k'],
             'bounds': {'min_comments': '10', 'min_like_rate': '2.5', 'max_like_rate': '50', 'max_comment_rate': '1'}},
         base=[{'field': 'video.like_rate', 'op': 'gte', 'value': '2.5'}, {'field': 'video.like_rate', 'op': 'lte', 'value': '50'},
               {'field': 'video.comment_rate', 'op': 'lte', 'value': '1'}],
         conditions={'likes': facet_presets('likes', ['100_1k', '1k_10k'], 'video.likes'),
                     'comments': {'field': 'video.comments', 'op': 'gte', 'value': '10'},
                     'published': {'all': [{'field': 'video.published_date', 'op': 'gte', 'value': '2024-03-01'},
                                           {'field': 'video.published_date', 'op': 'lt', 'value': '2026-03-30'}]}}),
    dict(name='cohort_open_years_views', zone='America/Santiago', custom={'max_views': 100000},
         cf={'cohort': {'kind': 'overview', 'published_from': None, 'published_through': '2026-09-06T04:00:00Z'},
             'published_years': [2026]},
         base=[facet_cohort(None, '2026-09-06T04:00:00Z')],
         conditions={'published': {'field': 'video.published_year', 'op': 'in', 'value': ['2026']},
                     'views': {'field': 'video.views', 'op': 'lte', 'value': '100000'}}),
    dict(name='cohort_search_tags_dates', zone='Pacific/Pago_Pago',
         custom={'q': 'title', 'published_from': '2025-12-31', 'published_before': '2026-01-02'},
         cf={'cohort': {'kind': 'overview', 'published_from': '2019-01-01T00:00:00Z', 'published_through': '2026-10-01T00:00:00Z'},
             'tag_count_buckets': ['zero', '16_plus']},
         base=[{'field': 'video.title', 'op': 'contains_casefold_v1', 'value': 'title'},
               facet_cohort('2019-01-01T00:00:00Z', '2026-10-01T00:00:00Z')],
         conditions={'tag_count': facet_presets('tag_count', ['zero', '16_plus'], 'video.tag_count'),
                     'published': {'all': [{'field': 'video.published_date', 'op': 'gte', 'value': '2025-12-31'},
                                           {'field': 'video.published_date', 'op': 'lt', 'value': '2026-01-02'}]}}),
]

if __name__=='__main__':unittest.main(verbosity=2)
