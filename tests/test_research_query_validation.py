"""Negative requests must fail before database work; values never become SQL."""
from dataclasses import replace
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'application'))
sys.path.insert(0, str(ROOT/'tests'))
from test_research_query_contract import query, FIXTURE
from research_query.validation import AccessContext, QueryRejected, validate, canonical, capabilities
from research_query.cursor import CursorCodec
from db.reads.research_sql import compile_query, compile_scope_recheck, casefold_map

CONTEXT = AccessContext('local-test', 'owner-1', enabled=True)


class ValidatorTests(unittest.TestCase):
    def test_related_field_plan_is_opt_in_and_values_are_parameters(self):
        statement, _ = compile_query(self.plan(query(select=['video.id'])))
        self.assertNotIn('channel_payload', statement)
        self.assertNotIn('type_method', statement)
        marker = "Straße %_'; --"
        statement, params = compile_query(self.plan(query(select=['channel.title'],
            where={'field':'channel.title','op':'contains_casefold_v1','value':marker})))
        self.assertNotIn(marker, statement)
        self.assertIn(marker.casefold(), params.values())
        self.assertIn('strpos(channel_title_key', statement)
        self.assertIn('country_collected_at', compile_query(self.plan(query(select=['channel.country_code'])))[0])
        for field in ('channel.avatar_url','video.thumbnail_url'):
            self.rejects(query(order_by=[{'field':field,'direction':'asc'}]), 'unsupported_operator')
        context = replace(CONTEXT, disabled_dependencies=frozenset({'channel_profile'}))
        with self.assertRaisesRegex(QueryRejected, 'dependency_disabled'):
            validate(canonical(query(select=['channel.views'])), context)
        validate(canonical(query(select=['channel.country_code'])), context)

    def test_array_and_boolean_operations_are_strict_and_parameterized(self):
        marker = "x'; DROP TABLE videos; -- %_"
        for field in ('video.tags', 'video.topic_urls'):
            for op in ('has_any', 'has_all'):
                plan = self.plan(query(where={'field':field, 'op':op, 'value':[marker, 'Straße', marker]}))
                statement, params = compile_query(plan)
                self.assertNotIn(marker, statement)
                self.assertIn(sorted([marker, 'Straße']), params.values())
                self.assertIn(' && ' if op == 'has_any' else ' @> ', statement)
                for value in ([], ['x']*1001):
                    self.rejects(query(where={'field':field, 'op':op, 'value':value}), 'query_budget_exceeded')
                for value in ('x', True, [None], [1]):
                    self.rejects(query(where={'field':field, 'op':op, 'value':value}),
                                 'query_budget_exceeded' if value in ('x', True) else 'invalid_query')
            self.rejects(query(order_by=[{'field':field,'direction':'asc'}]), 'unsupported_operator')
            self.rejects(query(where={'field':field,'op':'eq','value':'x'}), 'unsupported_operator')
        for value in (True, False):
            plan = self.plan(query(where={'field':'video.caption_available','op':'eq','value':value}))
            self.assertIs(plan.request.where.value, value)
        for value in ('false', 'true', 0, 1, ['false']):
            self.rejects(query(where={'field':'video.caption_available','op':'eq','value':value}))
        for field in ('video.category_id','video.audio_language','video.metadata_language'):
            self.rejects(query(where={'field':field,'op':'eq','value':'a'*65}))

    def test_extra_fields_follow_capabilities_and_do_not_cost_existing_queries_a_join(self):
        from research_query.registry import FIELDS
        visible = {f['id']: f for f in capabilities(CONTEXT)['fields']}
        self.assertFalse(visible['video.tags']['sortable'])
        self.assertEqual(visible['video.caption_available']['value_type'], 'boolean')
        disabled = replace(CONTEXT, disabled_dependencies=frozenset({'metadata'}))
        available = {f['id'] for f in capabilities(disabled)['fields']}
        self.assertIn('video.likes', available)
        self.assertNotIn('video.tags', available)
        self.rejects(query(select=['video.tags']), 'dependency_disabled', disabled)
        blocked = replace(CONTEXT, allowed_operators=CONTEXT.allowed_operators-{'has_all'})
        self.rejects(query(where={'field':'video.tags','op':'has_all','value':['x']}), 'policy_denied', blocked)
        old, _ = compile_query(self.plan())
        self.assertNotIn('FROM readable.videos v WHERE', old)
        self.assertNotIn('p.v_channel_id=p.channel_id THEN', old)
        # The payload comes from the projection view's own readable.videos row,
        # kept for the video's own channel only; no join is added.
        for placement, column, alias in ((query(select=['video.likes']), 'like_count', 'likes'),
                                         (query(where={'field':'video.tags','op':'is_unknown'}), 'tags', 'tags'),
                                         (query(order_by=[{'field':'video.comments','direction':'desc'}]), 'comment_count', 'comments'),
                                         (query(coverage={'required_fields':['video.caption_available']}),
                                          'caption_available', 'caption_available')):
            sql, _ = compile_query(self.plan(placement))
            self.assertIn(f',CASE WHEN p.v_channel_id=p.channel_id THEN p.{column} END AS {alias}', sql)
            self.assertNotIn('FROM readable.videos v WHERE', sql)
            self.assertNotIn('extra.', sql)
            self.assertNotIn('{extra_', sql)
        # One channel keeps the per-row lookup by key: a few thousand probes instead of the view's join
        # masking the payload of every catalogue video.
        sql, _ = compile_query(self.plan(query(select=['video.likes'], scope={'kind': 'channel', 'id': 'UC' + 'A' * 22})))
        self.assertIn('LEFT JOIN LATERAL (SELECT v.like_count AS likes FROM readable.videos v WHERE v.video_id=p.video_id'
                      ' AND v.channel_id=p.channel_id LIMIT 1) extra ON true', sql)
        self.assertIn(',extra.likes', sql)
        self.assertNotIn('p.v_channel_id=p.channel_id THEN', sql)
        for field in ('video.likes','video.comments'):
            self.plan(query(where={'field':field,'op':'gte','value':'9007199254740993'}))
            self.rejects(query(where={'field':field,'op':'eq','value':'-1'}))
            self.assertEqual(FIELDS[field].clock_family, 'counters')

    def plan(self, value=None, context=CONTEXT):
        return validate(canonical(query() if value is None else value), context)

    def rejects(self, value, code='invalid_query', context=CONTEXT):
        with self.assertRaises(QueryRejected) as caught:
            self.plan(value, context)
        self.assertEqual(caught.exception.code, code)

    def test_all_accepted_goldens_validate(self):
        for case in FIXTURE['cases']:
            with self.subTest(case=case['name']):
                self.plan(query(**case['patch'])).request

    def test_body_limits_duplicate_keys_and_parser_depth(self):
        for raw, code in [(b' '*65537,'query_budget_exceeded'),
                          (b'{"query_version":1,"query_version":1}','invalid_query'),
                          (b'['*30+b']'*30,'query_budget_exceeded'),
                          (b'{"value":NaN}','invalid_query'),(b'\xff','invalid_query')]:
            with self.subTest(raw=raw[:20]), self.assertRaises(QueryRejected) as caught:
                validate(raw, CONTEXT)
            self.assertEqual(caught.exception.code, code)

    def test_unknown_source_types_field_and_shape(self):
        self.rejects(query(source={'id':'owner.private'}),'unsupported_source')
        self.rejects(query(video_types=['long','short']),'unsupported_video_types')
        self.rejects(query(query_version=True),'unsupported_version')
        self.rejects(query(select=['video.sql']),'unsupported_field')
        self.rejects(query(sql='select secrets'))
        self.rejects(query(time={'mode':'as_of','at':'2020-01-01'}))
        self.rejects(query(order_by=[{'field':'video.views','direction':'desc; DROP TABLE videos'}]))

    def test_label_collection_scope_ids_and_capabilities(self):
        collection = '11111111-1111-4111-8111-111111111111'
        for kind, identity in [('label', 'monetized'), ('label', 'custom:'+collection), ('collection', collection)]:
            with self.subTest(kind=kind, identity=identity):
                plan = self.plan(query(scope={'kind':kind, 'id':identity}))
                statement, params = compile_query(plan)
                recheck, check_params = compile_scope_recheck(plan)
                self.assertNotIn(identity, statement)
                self.assertNotIn(identity, recheck)
                self.assertEqual(params['scope_id'], identity)
                self.assertEqual(check_params, {'scope_id':identity})
                self.assertNotIn('readable.videos', recheck)
        self.assertEqual(capabilities(CONTEXT)['scopes'], ['channel', 'folder', 'label', 'collection', 'library'])
        for scope in [{'kind':'label','id':'My label'}, {'kind':'label','id':collection},
                      {'kind':'collection','id':'AAAAAAAA-1111-4111-8111-111111111111'},
                      {'kind':'collection','id':'11111111-1111-1111-1111-111111111111'},
                      {'kind':'collection','id':collection,'video_ids':['a0000000001']},
                      {'kind':'label','id':"custom:'; SELECT secrets --"},
                      {'kind':'library','id':'UC'+'0'*22}, {'kind':'library','id':None}]:
            self.rejects(query(scope=scope))

    def test_permissions_precede_parse_and_all_usages_resolve_dependencies(self):
        with self.assertRaisesRegex(QueryRejected,'policy_denied'):
            validate(b'broken',replace(CONTEXT,enabled=False))
        context=replace(CONTEXT,allowed_fields=frozenset({'video.id'}))
        self.rejects(query(), 'field_disabled',context)
        context=replace(CONTEXT,disabled_dependencies=frozenset({'counters'}))
        self.rejects(query(), 'dependency_disabled',context)
        context=replace(CONTEXT,allowed_operators=frozenset({'eq'}))
        self.rejects(query(where={'field':'video.views','op':'gt','value':'2'}),'policy_denied',context)
        context=replace(CONTEXT,disabled_dependencies=frozenset({'stream_completion'}))
        self.rejects(query(video_types=['stream']), 'dependency_disabled',context)
        self.assertNotIn('video.views',{f['id'] for f in capabilities(replace(CONTEXT,disabled_dependencies=frozenset({'counters'})))['fields']})

    def test_exact_numbers_dates_and_text(self):
        for value in [1,True,'01','-1','1.0','1e5','18446744073709551616','١']:
            self.rejects(query(where={'field':'video.views','op':'eq','value':value}))
        for value in ['9007199254740993','18446744073709551615','0']:
            self.plan(query(where={'field':'video.views','op':'gte','value':value}))
        for value in ['2026-02-30T00:00:00Z','2026-10-20T00:00:00+00:00','2026-10-20']:
            self.rejects(query(where={'field':'video.published_at','op':'gte','value':value}))
        for value in ['\x00','\ud800','a'*1001]:
            with self.assertRaises((UnicodeError,QueryRejected)):
                self.plan(query(where={'field':'video.title','op':'eq','value':value}))

    def test_cursor_shape_and_capability_type_restrictions(self):
        raw=json.dumps(query(page={'size':2,'after':'\ud800'})).encode()
        with self.assertRaisesRegex(QueryRejected,'invalid_query'): validate(raw,CONTEXT)
        self.rejects(query(page={'size':2,'after':'!'}),'cursor_expired')
        self.rejects(query(where={'field':'video.views','op':'rank','value':'1'}),'unsupported_operator')
        self.rejects(query(where={'field':'video.views','op':'state_is','value':['known']}))
        context=replace(CONTEXT,disabled_dependencies=frozenset({'stream_completion'}))
        self.assertEqual(capabilities(context)['video_types'],['long','short'])
        with self.assertRaisesRegex(ValueError,'invalid_trusted_query_context'):
            replace(CONTEXT,disabled_dependencies=frozenset({'misspelled_dependency'}))

    def test_bounded_boolean_and_in(self):
        node={'field':'video.views','op':'eq','value':'1'}
        for _ in range(8): node={'not':node}
        self.rejects(query(where=node),'query_budget_exceeded')
        self.rejects(query(where={'all':[{'field':'video.views','op':'eq','value':'1'}]*129}),'query_budget_exceeded')
        self.rejects(query(where={'field':'video.views','op':'in','value':['1']*1001}),'query_budget_exceeded')
        self.rejects(query(select=['video.id','video.id']))
        self.rejects(query(where={'field':'video.title','op':'gte','value':'x'}),'unsupported_operator')
        self.rejects(query(where={'field':'video.title','op':'state_is','value':'disabled'}))

    def test_plan_immutable_canonicalized_and_no_literal_logs(self):
        plan=self.plan(query(where={'field':'video.views','op':'in','value':['2','1','2']}))
        copy=plan.request
        copy.select.append('video.sql')
        self.assertNotIn('video.sql',plan.request.select)
        other=self.plan(query(where={'field':'video.views','op':'in','value':['1','2']}))
        self.assertEqual(plan.fingerprint,other.fingerprint)
        other=self.plan(query(where={'field':'video.views','op':'in','value':['3','4']}))
        self.assertNotEqual(plan.fingerprint,other.fingerprint)
        self.assertEqual(plan.shape_hash,other.shape_hash)

    def test_sql_parameters_and_no_full_python_scope(self):
        text="x'; DROP TABLE videos; -- %_"
        plan=self.plan(query(where={'field':'video.title','op':'contains_casefold_v1','value':text}))
        statement,params=compile_query(plan)
        self.assertNotIn(text,statement)
        self.assertIn(text.casefold(),params.values())
        self.assertIn('LIMIT %(limit)s',statement)
        self.assertIn('readable.hot_video_inventory',statement)
        # Keyset pages, never OFFSET paging. The one OFFSET 0 fences the coverage subquery so its
        # deadline aggregate runs once; it skips no rows.
        self.assertEqual(statement.count(' OFFSET '),1)
        self.assertIn(' AS data_valid_before OFFSET 0) c),',statement)
        self.assertIn('::numeric',compile_query(self.plan(query(where={'field':'video.views','op':'gt','value':'18446744073709551615'})))[0])
        self.assertEqual(casefold_map()['ß'],'ss')

    def test_coverage_reads_every_scanned_row_once(self):
        # The rejected count and the earliest data deadline come from one pass over raw
        # (raw_summary): per row the earliest finite clock later than the evaluation, of the same nine
        # clocks the earlier VALUES lateral listed, then the earliest over the rows.
        from db.reads.research_sql import RAW_DEADLINES, future_deadline
        clocks = ('identity_expiry', 'video_expiry', '(checked_at+%(window)s::interval)',
                  '(metadata_checked_at+%(window)s::interval)', '(availability_checked_at+%(window)s::interval)',
                  'checked_at', 'metadata_checked_at', 'availability_checked_at', 'published_at')
        self.assertEqual(RAW_DEADLINES, ','.join(future_deadline(clock) for clock in clocks))
        self.assertEqual(future_deadline('x'), 'CASE WHEN isfinite(x) AND x>evaluated_at THEN x END')
        for consistency, counts in (('current', 'count(*) FILTER (WHERE NOT admitted) AS rejected_rows,\n'),
                                    ('frozen', 'count(*) FILTER (WHERE NOT admitted_now) AS rejected_rows,'
                                               'count(*) FILTER (WHERE admitted_now AND NOT admitted) AS expiring_rows,\n')):
            with self.subTest(consistency=consistency):
                statement, _ = compile_query(self.plan(query(consistency=consistency, select=['video.id', 'video.description'],
                                                             where={'field': 'video.description', 'op': 'is_known'})))
                self.assertEqual(statement.count('raw_summary AS MATERIALIZED ('), 1)
                self.assertIn('raw_summary AS MATERIALIZED (\n SELECT ' + counts + ' min(least(' + RAW_DEADLINES, statement)
                self.assertIn('(SELECT data_deadline FROM raw_summary)) AS data_valid_before OFFSET 0) c),', statement)
                self.assertIn('(SELECT rejected_rows FROM raw_summary) AS rejected_rows', statement)
                self.assertNotIn('VALUES(identity_expiry)', statement)
                self.assertNotIn('FROM raw WHERE NOT admitted', statement)
                # A filtered optional field's own clocks join the same per-row deadline.
                self.assertIn(',' + future_deadline('(description_checked_at)') + ',' +
                              future_deadline('(description_checked_at+%(window)s::interval)') + ')) AS data_deadline FROM raw)', statement)

    def test_source_window_is_one_bound_hot_parameter(self):
        from hot_storage import HOT_DATA_WINDOW, RETENTION_HOURS
        statement,params=compile_query(self.plan(query()))
        # Hours, never days: '29 days' is 697 hours across a DST change.
        self.assertEqual(params['window'],f'{RETENTION_HOURS} hours')
        self.assertEqual(HOT_DATA_WINDOW,timedelta(hours=RETENTION_HOURS))
        self.assertNotIn('696',statement)
        self.assertNotRegex(statement,r"interval '\d+ (hours?|days?)'")
        self.assertIn('%(window)s::interval',statement)

    def test_stored_title_key_follows_the_title_and_unicode_rule(self):
        from db.reads.research_sql import casefold_special
        special=casefold_special()
        self.assertEqual(len(special),298)
        for char in 'ßẞςµſİŉǰﬁꭰ':
            self.assertIn(char,special)
        for char in 'АаЁёЖжЯяΣσΑαÄäŁł測試ÉéÇç':
            self.assertNotIn(char,special)
        statement,params=compile_query(self.plan(query(order_by=[{'field':'video.title','direction':'asc'}])))
        # A current read-model row supplies the key; any other row folds live.
        self.assertIn('CASE WHEN p.proof_current THEN p.title_key ELSE CASE WHEN p.title IS NULL',statement)
        self.assertIn('WHEN p.title !~ %(fold_special)s THEN lower(p.title COLLATE pg_c_utf8)',statement)
        self.assertEqual(params['fold_special'],'['+special+']')
        self.assertIn('CASE WHEN metadata_fresh AND char_length(title)<=1000 THEN stored_title_key END AS title_key',statement)
        statement,params=compile_query(self.plan(query()))
        self.assertIn('SELECT NULL::text AS title_key,video_id',statement)
        self.assertIn('NULL::text AS stored_title_key',statement)
        self.assertFalse({'fold_map','fold_special'}&set(params))

    def test_channel_scope_bounds_inventory_and_folder_does_not(self):
        channel,_=compile_query(self.plan(query(scope={'kind':'channel','id':'UC'+'0'*22})))
        folder,_=compile_query(self.plan(query()))
        bound=' AND p.channel_id=%(scope_id)s::text'
        self.assertIn('c.channel_id=%(scope_id)s::text',channel)
        self.assertIn(bound,channel)
        self.assertNotIn(bound,folder)
        self.assertIn('c.folder_id=%(scope_id)s::uuid',folder)
        for statement in (channel,folder):
            self.assertNotIn('{inventory_scope}',statement)
            self.assertNotIn('{1,20}',statement)


class CursorTests(unittest.TestCase):
    def test_encryption_tamper_context_expiry_and_restart(self):
        codec=CursorCodec()
        plan=validate(canonical(query()),CONTEXT)
        now=datetime.now(timezone.utc)
        token=codec.encode(plan,scope_revision='a'*64,keys=['secret-title','id'],valid_before=(now+timedelta(seconds=60)).isoformat())
        self.assertNotIn('secret-title',token)
        self.assertEqual(codec.decode(token,plan,now)['keys'],['secret-title','id'])
        for value, decoder, future in [(token[:-3]+'xxA',codec,now),(token,CursorCodec(),now),(token,codec,now+timedelta(minutes=2))]:
            with self.assertRaisesRegex(QueryRejected,'cursor_expired'):
                decoder.decode(value,plan,future)
        changed=validate(canonical(query()),replace(CONTEXT,access_revision='owner-2'))
        with self.assertRaisesRegex(QueryRejected,'cursor_context_changed'):
            codec.decode(token,changed,now)


if __name__=='__main__': unittest.main()
