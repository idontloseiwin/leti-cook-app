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
from db.reads.research_sql import compile_query, casefold_map

CONTEXT = AccessContext('local-test', 'owner-1', enabled=True)


class ValidatorTests(unittest.TestCase):
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
        self.assertNotIn(' OFFSET ',statement)
        self.assertIn('::numeric',compile_query(self.plan(query(where={'field':'video.views','op':'gt','value':'18446744073709551615'})))[0])
        self.assertEqual(casefold_map()['ß'],'ss')

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
