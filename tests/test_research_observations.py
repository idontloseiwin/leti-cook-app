"""Independent observation contracts: no database or provider required."""
from dataclasses import replace
import unittest
from test_research_query_contract import query
from research_query.validation import AccessContext, QueryRejected, canonical, validate
from research_query.contracts import signed_decimal
from research_query.operations import OPERATIONS
from db.reads.research_sql import compile_query, compile_scope_recheck

class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.context=AccessContext('fixture','owner',enabled=True)
    def test_permission_is_server_bound(self):
        raw=canonical(query(select=['observation.views_per_hour']))
        with self.assertRaises(QueryRejected) as error: validate(raw,self.context)
        self.assertEqual(error.exception.code,'grant_required')
        plan=validate(raw,replace(self.context,allowed_operations=frozenset(OPERATIONS)))
        self.assertIn('observation.views_per_hour',plan.referenced_fields)
        validate(canonical(query(select=['observation.views_at_age'])),self.context)
    def test_invalid_windows_and_precision_are_rejected(self):
        for options in ({'version':2},{'age_seconds':0},{'tolerance_seconds':7200},{'window_seconds':86400,'tolerance_seconds':7201}):
            with self.assertRaises(QueryRejected): validate(canonical(query(observations=options)),self.context)
        for value in ('01','-00','1e3','NaN','1.1234567','18446744073709551616'):
            with self.assertRaises(ValueError): signed_decimal(value)
        self.assertEqual(str(signed_decimal('-9007199254740993.125')),'-9007199254740993.125')
    def test_unstamped_snapshot_view_uses_the_writer_date_rule(self):
        from pathlib import Path
        from db.videos.observation_definition import view_epoch_sql
        root=Path(__file__).resolve().parents[1]
        rule=view_epoch_sql('input.recorded_at').replace('input.recorded_at','s.recorded_at')
        view='coalesce(s.views_definition_epoch,'+rule+') AS views_definition_epoch'
        fresh=(root/'schema/schema.sql').read_text(encoding='utf-8')
        active=fresh.rsplit('CREATE OR REPLACE VIEW readable.video_snapshots',1)[1].split(';',1)[0]
        self.assertEqual(active.count(view),1)
        self.assertGreater(fresh.index(view),fresh.index('ALTER TABLE public.video_snapshots ADD COLUMN views_definition_epoch'))
    def test_whole_scope_sql_without_global_generation(self):
        plan=validate(canonical(query(select=['observation.views_at_age'],order_by=[{'field':'observation.views_at_age','direction':'desc'}])),self.context)
        sql,params=compile_query(plan)
        self.assertLess(sql.index('age_point'),sql.index('page AS MATERIALIZED'))
        self.assertIn('readable.video_snapshots',sql)
        self.assertNotIn('FROM public.video_snapshots',sql)
        # Like the ordinary catalogue: no global generation joins the page revision.
        self.assertNotIn('research_observation_generation',sql)
        self.assertNotIn("|| ':' ||",sql)
        self.assertEqual(params['observation_age'],86400)
        raw=validate(canonical(query()),self.context)
        self.assertNotIn('video_snapshots',compile_query(raw)[0])
        self.assertNotIn('research_observation_generation',compile_scope_recheck(raw)[0])
