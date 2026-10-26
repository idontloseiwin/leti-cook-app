"""Scoped native PG17 test entrypoint, ONLY in a dedicated disposable cluster.

Never auto-discovered and never accepts a production DSN. The runner supplies
only loopback access to its freshly created cluster, never an existing database.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
import re
from pathlib import Path
import sys
import unittest
import uuid

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'application'))
sys.path.insert(0, str(ROOT/'tests'))
from research_query.validation import AccessContext, QueryRejected, canonical, validate
from research_query.service import QueryService
from db.reads.research_query import ResearchQueryRepository
from db.base.postgres import PostgresConfig, PostgresDatabase
from test_research_query_contract import FIXTURE, query


def bootstrap(admin):
    if admin.execute("SELECT count(*) FROM pg_tables WHERE schemaname='public'").fetchone()[0]:
        raise RuntimeError('test_requires_empty_database')
    admin.execute("""CREATE ROLE anon NOLOGIN;
CREATE ROLE authenticated NOLOGIN;
CREATE ROLE service_role NOLOGIN BYPASSRLS;
CREATE ROLE lic_reader NOLOGIN NOINHERIT NOBYPASSRLS;
CREATE ROLE authenticator LOGIN NOINHERIT;
GRANT service_role,lic_reader TO authenticator;
REVOKE CONNECT,TEMPORARY ON DATABASE "leti-cook" FROM public;
GRANT CONNECT ON DATABASE "leti-cook" TO authenticator;
CREATE SCHEMA extensions;
CREATE EXTENSION pgcrypto WITH SCHEMA extensions""",prepare=False)
    admin.execute("SELECT set_config('application_name','research-query-disposable-fixture',false)")
    from psycopg import sql
    admin.execute(sql.SQL('ALTER ROLE authenticator PASSWORD {}').format(sql.Literal(os.environ['LIC_QUERY_TEST_PASSWORD'])))
    admin.execute((ROOT/'schema/baseline/schema.sql').read_text(), prepare=False)
    manifest=json.loads((ROOT/'schema/manifest.json').read_text())
    for step in manifest['steps']:
        admin.execute((ROOT/'schema'/step['file']).read_text(),prepare=False)


def seed(admin, now):
    # All identifiers and rows below are synthetic. Never disable constraints or
    # security triggers to make the query pass its native tests.
    folders=FIXTURE['folders']
    for i,f in enumerate(folders):
        admin.execute('INSERT INTO public.folders(id,name) VALUES(%s,%s)',(f,'Fixture '+str(i)))
    for i,ch in enumerate(FIXTURE['channels']):
        user_id=uuid.uuid4()
        admin.execute('INSERT INTO public.channels(channel_id,user_work_id,folder_id,added_at) VALUES(%s,%s,%s,%s)',
                      (ch['id'],user_id,folders[ch['folder']],now))
        admin.execute('INSERT INTO public.channel_collection_state(channel_id,user_work_id,owner_permission,is_hidden,control_epoch) VALUES(%s,%s,%s,%s,1)',
                      (ch['id'],user_id,'legacy_hold' if ch['collection']=='operator_hold' else 'accepted',ch['hidden']))
        for kind,prefix in [('long','UULF'),('short','UUSH'),('stream','UULV')]:
            scan=uuid.uuid4()
            playlist=prefix+ch['id'][2:]
            complete=ch['inventory']=='complete'
            admin.execute('''INSERT INTO public.channel_video_inventory_scans
                (scan_id,channel_id,video_type,playlist_id,scan_kind,status,coverage,scan_boundary_at,started_at,source_checked_at,completed_at)
                VALUES(%s,%s,%s,%s,'initial_inventory','completed','complete',%s,%s,%s,%s)''',
                (scan,ch['id'],kind,playlist,now-timedelta(hours=2),now-timedelta(hours=2),now-timedelta(hours=1),now-timedelta(hours=1)))
            admin.execute('''INSERT INTO public.channel_playlist_state
                (channel_id,video_type,playlist_id,inventory_status,last_completed_scan_id,source_checked_at,inventory_completed_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s)''',(ch['id'],kind,playlist,'completed' if complete else 'not_started',
                scan if complete else None,now-timedelta(hours=1),now-timedelta(hours=1) if complete else None))
    delta=now-datetime.fromisoformat(FIXTURE['now'])
    for row in FIXTURE['rows']:
        channel=FIXTURE['channels'][row['channel']]['id']
        pub=datetime.fromisoformat(row['published_at'])+delta if row.get('published_at') else None
        metadata=now-timedelta(hours=700 if row.get('states',{}).get('video.title')=='expired' else 1)
        counter=now-timedelta(hours=700 if row.get('states',{}).get('video.views')=='expired' else 1)
        end=now-timedelta(hours=2) if row.get('stream_completion')=='proven' else None
        if row.get('stream_completion')=='counter_precedes_end': end=now-timedelta(minutes=30)
        admin.execute('INSERT INTO public.video_inventory(video_id,channel_id,video_type) VALUES(%s,%s,%s)',(row['id'],channel,row['type']))
        member_channel=FIXTURE['channels'][1]['id'] if row.get('proof')=='wrong_channel' else channel
        kinds=[row['type']]+(['stream'] if row.get('proof')=='unresolved' else [])
        for kind in kinds:
            clock=now-timedelta(hours=700 if row.get('proof')=='expired' else 1)
            playlist={'long':'UULF','short':'UUSH','stream':'UULV'}[kind]+member_channel[2:]
            admin.execute('''INSERT INTO public.video_inventory_memberships
                (video_id,channel_id,video_type,playlist_id,source_published_at,first_seen_at,last_seen_at,last_present_at,evidence_source)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'legacy_inventory_backfill')''',
                (row['id'],member_channel,kind,playlist,pub,clock,clock,clock))
        admin.execute('''INSERT INTO public.videos
            (video_id,channel_id,title,views,checked_at,metadata_checked_at,published_at,duration,
             availability_status,availability_checked_at,stream_actual_end_at,publication_status)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
            (row['id'],channel,row.get('title'),int(row['views']) if row.get('views') else None,counter,metadata,pub,
             'PT'+row['duration_seconds']+'S' if row.get('duration_seconds') is not None else None,
             row.get('availability','public'),now-timedelta(hours=1),end,row.get('publication')))
    return delta


def shift_dates(value, delta):
    if isinstance(value,dict): return {k:shift_dates(v,delta) for k,v in value.items()}
    if isinstance(value,list): return [shift_dates(v,delta) for v in value]
    if isinstance(value,str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z', value):
        return (datetime.fromisoformat(value)+delta).isoformat().replace('+00:00','Z')
    return value


class NativeQueryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get('LIC_RESEARCH_QUERY_DISPOSABLE')!='1':
            raise RuntimeError('disposable_cluster_required')
        password=os.environ['LIC_QUERY_TEST_PASSWORD']
        port=int(os.environ['LIC_QUERY_TEST_PORT'])
        cls.admin=psycopg.connect(host='127.0.0.1',port=port,dbname='leti-cook',user='postgres',password=password,autocommit=True)
        version=cls.admin.execute('SHOW server_version_num').fetchone()[0]
        if not 170000<=int(version)<180000: raise RuntimeError('pg17_required')
        bootstrap(cls.admin)
        now=cls.admin.execute('SELECT clock_timestamp()').fetchone()[0]
        cls.delta=seed(cls.admin,now)
        cls.context=AccessContext('synthetic-installation','owner-1',enabled=True)
        cls.db=PostgresDatabase(PostgresConfig(f'host=127.0.0.1 port={port} dbname=leti-cook user=authenticator password={password}',max_size=1),
                                admission=lambda _:None).open()
        cls.repo=ResearchQueryRepository(cls.db)
        cls.service=QueryService(cls.repo,lambda:cls.context)
        # As on an installation: the background refresher keeps the read model
        # current; rows queued by a test's own writes are read from the views.
        cls.refresh()
        print('Synthetic PostgreSQL query fixture ready; server_version_num='+version,flush=True)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()
        cls.admin.close()

    @classmethod
    def refresh(cls):
        # The production refresh, as the backend role through the gateway.
        from db.inventory.search_projection import SearchProjectionRepository
        repository = SearchProjectionRepository(cls.db)
        while repository.refresh(10000):
            pass

    def run_query(self, value):
        return self.service.execute(canonical(shift_dates(value,self.delta)))

    def test_01_twenty_one_independent_goldens(self):
        for case in FIXTURE['cases']:
            with self.subTest(case=case['name']):
                if 'error' in case:
                    with self.assertRaisesRegex(QueryRejected,case['error']): self.run_query(query(**case['patch']))
                else:
                    result=self.run_query(query(**case['patch']))
                    self.assertEqual([r['id'] for r in result['rows']],case['ids'])

    def test_02_keyset_all_pages_nulls_ties_both_directions(self):
        for direction in ('asc','desc'):
            q=query(order_by=[{'field':'video.views','direction':direction}],page={'size':2,'after':None})
            expected=[r['id'] for r in self.run_query({**q,'page':{'size':50}})['rows']]
            ids=[]
            for _ in range(10):
                result=self.run_query(q)
                ids.extend(r['id'] for r in result['rows'])
                if not result['page']['has_more']: break
                q['page']['after']=result['page']['next_cursor']
            self.assertEqual(ids,expected)
            self.assertEqual(len(ids),len(set(ids)))

    def test_03_cell_expiry_and_precision(self):
        rows={r['id']:r['cells'] for r in self.run_query(query())['rows']}
        self.assertEqual(rows['b0000000001']['video.views']['value'],'9007199254740993')
        self.assertEqual(rows['a0000000005']['video.views']['state'],'expired')
        self.assertIsNone(rows['a0000000005']['video.views']['value'])
        self.assertEqual(rows['a0000000005']['video.title']['state'],'known')
        self.assertEqual(rows['a0000000006']['video.title']['state'],'expired')
        self.assertEqual(rows['a0000000006']['video.views']['state'],'known')

    def test_04_scope_move_invalidates_continuation(self):
        q=query(page={'size':2,'after':None})
        first=self.run_query(q)
        channel=FIXTURE['channels'][1]['id']
        self.admin.execute('UPDATE public.channels SET folder_id=%s WHERE channel_id=%s',(FIXTURE['folders'][1],channel))
        try:
            q['page']['after']=first['page']['next_cursor']
            with self.assertRaisesRegex(QueryRejected,'cursor_context_changed'): self.run_query(q)
        finally:
            self.admin.execute('UPDATE public.channels SET folder_id=%s WHERE channel_id=%s',(FIXTURE['folders'][0],channel))

    def test_05_current_pages_see_new_committed_counter(self):
        self.admin.execute('UPDATE public.videos SET views=7 WHERE video_id=%s',('a0000000001',))
        try:
            result=self.run_query(query(where={'field':'video.views','op':'eq','value':'7'}))
            self.assertEqual([r['id'] for r in result['rows']],['a0000000001'])
            self.assertEqual(result['context']['consistency'],'current')
        finally: self.admin.execute('UPDATE public.videos SET views=100 WHERE video_id=%s',('a0000000001',))

    def test_06_unicode_title_order_and_exact_boundary_numbers(self):
        result=self.run_query(query(order_by=[{'field':'video.title','direction':'asc'}],page={'size':2}))
        self.assertEqual(result['rows'][0]['id'],'a0000000003')
        result=self.run_query(query(where={'field':'video.views','op':'gte','value':'18446744073709551615'}))
        self.assertEqual(result['rows'],[])

    def test_07_reader_cannot_access_raw_video_payload(self):
        from db.base.postgres import Operation, DatabaseServerError
        from db.base.read_context import http_read_scope
        with self.assertRaises(DatabaseServerError),http_read_scope(),self.db.transaction(Operation('research_fixture_raw_access',read_only=True)) as tx:
            tx.fetch_one('SELECT title FROM public.videos LIMIT 1')

    def test_08_every_registered_sort_and_type_pages_without_duplicates(self):
        from research_query.registry import FIELDS
        for kind in ('long','short','stream'):
            for field in FIELDS:
                for direction in ('asc','desc'):
                    with self.subTest(kind=kind,field=field,direction=direction):
                        q=query(video_types=[kind],order_by=[{'field':field,'direction':direction}])
                        expected=[r['id'] for r in self.run_query(q)['rows']]
                        q['page']={'size':1}
                        actual=[]
                        for _ in range(25):
                            result=self.run_query(q)
                            actual.extend(r['id'] for r in result['rows'])
                            if not result['page']['has_more']: break
                            q['page']['after']=result['page']['next_cursor']
                        self.assertEqual(actual,expected)
                        self.assertEqual(len(actual),len(set(actual)))

    def test_09_unicode_casefold_definition_and_full_size_cursor(self):
        from db.reads.research_sql import casefold_map
        row='a0000000001'
        old=self.admin.execute('SELECT title FROM public.videos WHERE video_id=%s',(row,)).fetchone()[0]
        try:
            chars=list(casefold_map())
            for offset in range(0,len(chars),400):
                title=''.join(chars[offset:offset+400])
                self.admin.execute('UPDATE public.videos SET title=%s WHERE video_id=%s',(title,row))
                result=self.run_query(query(where={'field':'video.title','op':'contains_casefold_v1','value':title}))
                self.assertEqual([r['id'] for r in result['rows']],[row])
            # A legitimate title must not lose pagination because of UTF-8 or
            # casefold expansion. Ordering is kept exact, never truncated.
            self.admin.execute('UPDATE public.videos SET title=%s WHERE video_id=%s',('\u1f82'*1000,row))
            q=query(order_by=[{'field':'video.title','direction':'desc'}],page={'size':1})
            first=self.run_query(q)
            self.assertEqual(first['rows'][0]['id'],row)
            self.assertGreater(len(first['page']['next_cursor']),4096)
            q['page']['after']=first['page']['next_cursor']
            self.assertNotIn(row,[r['id'] for r in self.run_query(q)['rows']])
        finally: self.admin.execute('UPDATE public.videos SET title=%s WHERE video_id=%s',(old,row))

    def test_10_expiry_of_unreturned_predicate_input_bounds_result(self):
        row='a0000000001'
        old=self.admin.execute('SELECT checked_at FROM public.videos WHERE video_id=%s',(row,)).fetchone()[0]
        expiry=self.admin.execute("SELECT clock_timestamp()+interval '10 seconds'").fetchone()[0]
        try:
            self.admin.execute("UPDATE public.videos SET checked_at=%s-interval '696 hours' WHERE video_id=%s",(expiry,row))
            result=self.run_query(query(where={'field':'video.views','op':'gt','value':'1000'},select=['video.id']))
            self.assertNotIn(row,[r['id'] for r in result['rows']])
            self.assertEqual(datetime.fromisoformat(result['context']['valid_before']),expiry)
        finally: self.admin.execute('UPDATE public.videos SET checked_at=%s WHERE video_id=%s',(old,row))

    def test_11_channel_hidden_after_page_snapshot_discards_complete_page(self):
        channel=FIXTURE['channels'][0]['id']
        repo=self.repo
        admin=self.admin
        class ConcurrentHide:
            def read_page(self,*args,**kwargs):
                result=repo.read_page(*args,**kwargs)
                admin.execute('UPDATE public.channel_collection_state SET is_hidden=true WHERE channel_id=%s',(channel,))
                return result
            def recheck_scope(self,*args,**kwargs): return repo.recheck_scope(*args,**kwargs)
        try:
            service=QueryService(ConcurrentHide(),lambda:self.context)
            with self.assertRaisesRegex(QueryRejected,'cursor_context_changed'):
                service.execute(canonical(query()))
        finally: self.admin.execute('UPDATE public.channel_collection_state SET is_hidden=false WHERE channel_id=%s',(channel,))

    def test_12_native_statement_timeout_releases_connection(self):
        import time
        with self.assertRaisesRegex(QueryRejected,'query_budget_exceeded'):
            self.repo._read('SELECT pg_sleep(1)',{},deadline=time.monotonic()+.03,
                            cancelled=lambda:False,name='query_fixture_timeout')
        self.assertTrue(self.run_query(query())['rows'])

    def test_13_current_availability_and_incomplete_coverage(self):
        row='a0000000001'
        try:
            self.admin.execute("UPDATE public.videos SET availability_status='private' WHERE video_id=%s",(row,))
            result=self.run_query(query(where={'field':'video.id','op':'eq','value':row}))
            self.assertEqual(result['rows'],[])
            self.assertEqual(result['coverage']['status'],'partial')
        finally: self.admin.execute("UPDATE public.videos SET availability_status='public' WHERE video_id=%s",(row,))


    def test_14_owner_http_pages_match_native_executor_for_every_type(self):
        import time
        import tempfile
        from unittest.mock import patch
        from fastapi.testclient import TestClient
        from research_query_preview import create_preview_app
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, LIC_RESEARCH_QUERY_PREVIEW='1'), \
                patch('hot_runtime.require_hot_runtime_ready', lambda **kw: None):
            app = create_preview_app(self.db, Path(folder)/'owner.sqlite3', 'http://127.0.0.1:8002', FIXTURE['folders'])
            with TestClient(app, base_url='http://127.0.0.1:8002', headers={'Origin':'http://127.0.0.1:8002'}) as client:
                ready_deadline = time.monotonic()+15
                while not app.state.research_projection.initial_fill_complete:
                    self.assertLess(time.monotonic(), ready_deadline, 'initial preparation stalled')
                    time.sleep(.02)
                for kind in ('long','short','stream'):
                    q=query(video_types=[kind], select=['video.id','video.channel_id','video.type','video.title',
                            'video.published_at','video.duration_seconds','video.views'])
                    expected=[row['id'] for row in self.run_query(q)['rows']]
                    q['page']={'size':1}; actual=[]
                    for _ in range(30):
                        response=client.post('/research/queries/execute',json=q)
                        self.assertEqual(response.status_code,200,response.text)
                        data=response.json()
                        self.assertEqual(data['context']['valid_before'],data['source_expires_at'])
                        actual.extend(row['id'] for row in data['rows'])
                        if not data['page']['has_more']: break
                        q['page']['after']=data['page']['next_cursor']
                    else: self.fail('unbounded_continuation')
                    self.assertEqual(actual,expected)


    def test_15_duration_parser_preserves_exact_values_and_unknowns(self):
        row='a0000000001'
        old=self.admin.execute('SELECT duration,metadata_checked_at FROM public.videos WHERE video_id=%s',(row,)).fetchone()
        q=query(where={'field':'video.id','op':'eq','value':row},select=['video.id','video.duration_seconds'])
        cases=[(None,None),('',None),('0','0'),(' 0012 ','12'),('pt12s','12'),
               ('P1DT2H3M4S','93784'),('P0D','0'),('PT0S','0'),('P',None),('PT',None),
               ('P1DT',None),('PT1.5S',None),('-1',None),('NaN',None),('1e3',None),
               ('18446744073709551615','18446744073709551615'),
               ('18446744073709551616',None),('PT18446744073709551615S','18446744073709551615'),
               ('P99999999999999999999D',None),('9'*21,None),('PT'+'9'*21+'S',None),
               # Each part may have at most 20 digits, leading zeros included.
               ('0'*20,'0'),('0'*21,None),('0'*19+'7','7'),('0'*20+'7',None),
               ('PT'+'0'*19+'1S','1'),('PT'+'0'*20+'1S',None),('PT'+'0'*19+'1M','60'),
               ('PT'+'0'*20+'1M',None),('PT'+'0'*19+'1H','3600'),('PT'+'0'*20+'1H',None),
               ('P'+'0'*19+'1D','86400'),('P'+'0'*20+'1D',None),('P1DT'+'0'*20+'1S',None),
               ('PT'+'9'*20+'H',None),('PT1H'+'0'*21+'M',None)]
        try:
            for duration,expected in cases:
                with self.subTest(duration=duration):
                    self.admin.execute('UPDATE public.videos SET duration=%s WHERE video_id=%s',(duration,row))
                    cell=self.run_query(q)['rows'][0]['cells']['video.duration_seconds']
                    self.assertEqual(cell['value'],expected)
                    self.assertEqual(cell['state'],'known' if expected is not None else 'unknown')
            for clock,state in [("clock_timestamp()-interval '700 hours'",'expired'),
                                ("clock_timestamp()+interval '1 hour'",'unavailable')]:
                self.admin.execute('UPDATE public.videos SET duration=%s,metadata_checked_at='+clock+' WHERE video_id=%s',('PT12S',row))
                cell=self.run_query(q)['rows'][0]['cells']['video.duration_seconds']
                self.assertEqual(cell['state'],state)
                self.assertIsNone(cell['value'])
        finally:
            self.admin.execute('UPDATE public.videos SET duration=%s,metadata_checked_at=%s WHERE video_id=%s',(*old,row))

    def test_16_bound_channel_scope_matches_folder_for_all_types(self):
        for kind in ('long','short','stream'):
            q=query(video_types=[kind],select=['video.id','video.channel_id'],page={'size':200})
            folder=self.run_query(q)
            for channel in FIXTURE['channels']:
                scoped={**q,'scope':{'kind':'channel','id':channel['id']}}
                if channel['hidden']:
                    with self.assertRaises(QueryRejected): self.run_query(scoped)
                else:
                    expected=[r['id'] for r in folder['rows'] if r['cells']['video.channel_id']['value']==channel['id']]
                    # The source folder contains these channels only.
                    if channel['folder']==0:
                        self.assertEqual([r['id'] for r in self.run_query(scoped)['rows']],expected)

    def test_17_builtin_lowercase_equals_casefold_outside_special_characters(self):
        import random
        from db.reads.research_sql import casefold_special
        special=set(casefold_special())
        rows=self.admin.execute("""SELECT n,lower(chr(n) COLLATE pg_c_utf8) FROM generate_series(1,1114111) n
                                   WHERE n NOT BETWEEN 55296 AND 57343""").fetchall()
        differ={chr(n) for n,low in rows if low!=chr(n).casefold()}
        self.assertEqual(differ,special)
        # Context-free: whole strings fold like their characters, word-final Σ included.
        alphabet=[chr(n) for n,_ in rows if chr(n) not in special and chr(n)!='\x00']
        rng=random.Random(17)
        samples=['ΣΑΣ ΟΔΟΣ','Обзор: ЛУЧШЕЕ видео — Ёлка','Σ','ΑΣ.','Ǆǅǆ']
        samples+=[''.join(rng.choice(alphabet) for _ in range(rng.randint(1,40))) for _ in range(2000)]
        folded=self.admin.execute('SELECT array_agg(lower(t COLLATE pg_c_utf8) ORDER BY i) FROM unnest(%s::text[]) WITH ORDINALITY AS x(t,i)',
                                  (samples,)).fetchone()[0]
        self.assertEqual(folded,[t.casefold() for t in samples])


    def test_18_casefold_rule_equals_python_for_every_code_point(self):
        import random
        from db.reads.research_sql import casefold_parameters, casefold_special, casefold_sql
        params=casefold_parameters()
        rows=self.admin.execute("SELECT n,"+casefold_sql('chr(n)')+""" FROM generate_series(1,1114111) n
                                   WHERE n NOT BETWEEN 55296 AND 57343""",params).fetchall()
        self.assertEqual([n for n,key in rows if key!=chr(n).casefold()],[])
        rng=random.Random(18)
        alphabet=list(casefold_special())+list('AaZzЁёΣσ測 1-')
        samples=['Straße ΣΑΣ ﬁ','STRASSE','İstanbul','µ','ꭰ','','Обзор ЁЛКА']
        samples+=[''.join(rng.choice(alphabet) for _ in range(rng.randint(1,30))) for _ in range(3000)]
        folded=self.admin.execute('SELECT array_agg('+casefold_sql('x.t')+' ORDER BY x.i) FROM unnest(%(samples)s::text[]) WITH ORDINALITY AS x(t,i)',
                                  {**params,'samples':samples}).fetchone()[0]
        self.assertEqual(folded,[t.casefold() for t in samples])

    def test_19_duration_rule_matches_the_boundaries(self):
        from db.reads.research_sql import DURATION_PATTERN, duration_seconds_sql
        cases=[(None,None),('',None),('0',0),(' 0012 ',12),('pt12s',12),('P1DT2H3M4S',93784),('P0D',0),('PT0S',0),
               ('P',None),('PT',None),('P1DT',None),('PT1.5S',None),('-1',None),('NaN',None),('1e3',None),
               ('18446744073709551615',18446744073709551615),('PT'+'0'*19+'1S',1),('PT'+'0'*20+'1S',None),
               ('0'*20,0),('0'*21,None),('P'+'0'*19+'1D',86400),('P'+'0'*20+'1D',None),('PT1H'+'0'*21+'M',None)]
        sql=('SELECT '+duration_seconds_sql('d.text','m.parts')+' FROM (SELECT upper(btrim(%(duration)s::text)) AS text) d'
             ' CROSS JOIN LATERAL (SELECT regexp_match(d.text,%(pattern)s) AS parts) m')
        for duration,expected in cases:
            with self.subTest(duration=duration):
                value=self.admin.execute(sql,{'duration':duration,'pattern':DURATION_PATTERN}).fetchone()[0]
                self.assertEqual(value,expected)

    def test_20_read_model_answers_equal_the_protected_views(self):
        # Same answers from current rows, from rows all past their deadline
        # (read live), and from rows queued by writes before a refresh.
        def answers():
            out=[]
            for kind in ('long','short','stream'):
                for order in ('video.views','video.title','video.duration_seconds'):
                    for scope in (None,{'kind':'channel','id':FIXTURE['channels'][0]['id']}):
                        q=query(video_types=[kind],order_by=[{'field':order,'direction':'asc'}],page={'size':3,'after':None},
                                select=['video.id','video.title','video.views','video.duration_seconds','video.published_at'],
                                **({'scope':scope} if scope else {}))
                        pages=[]
                        for _ in range(20):
                            try: result=self.run_query(q)
                            except QueryRejected as error:
                                pages.append(error.code); break
                            pages.append((result['rows'],result['coverage'],result['page']['has_more']))
                            if not result['page']['has_more']: break
                            q['page']['after']=result['page']['next_cursor']
                        out.append(pages)
                q=query(video_types=[kind],where={'field':'video.title','op':'contains_casefold_v1','value':'STRASSE'})
                out.append(self.run_query(q)['rows'])
            return out
        self.refresh()
        current=answers()
        self.admin.execute("UPDATE public.video_search_projection SET valid_until='-infinity'")
        self.assertEqual(answers(),current)
        self.refresh()
        row=self.admin.execute("SELECT v.video_id,v.title,v.duration FROM public.videos v JOIN public.video_search_projection p USING(video_id) "
                               "WHERE v.title IS NOT NULL AND v.duration IS NOT NULL ORDER BY v.video_id LIMIT 1").fetchone()
        try:
            self.admin.execute("UPDATE public.videos SET title='Straße STRASSE',duration='PT2M' WHERE video_id=%s",(row[0],))
            self.admin.execute("UPDATE public.video_inventory_memberships SET last_present_at=clock_timestamp() WHERE video_id=%s",(row[0],))
            self.assertEqual(self.admin.execute('SELECT count(*) FROM public.video_search_projection_queue WHERE video_id=%s',(row[0],)).fetchone()[0],1)
            queued=answers()  # the queued video is read live
            self.refresh()
            self.assertEqual(answers(),queued)
            self.assertEqual(self.admin.execute('SELECT count(*) FROM public.video_search_projection_queue').fetchone()[0],0)
            stored=self.admin.execute('SELECT title_key,duration_seconds FROM public.video_search_projection WHERE video_id=%s',(row[0],)).fetchone()
            self.assertEqual(stored,('strasse strasse',120))
            # Counters are read live and do not queue the video.
            self.admin.execute('UPDATE public.videos SET views=coalesce(views,0)+1 WHERE video_id=%s',(row[0],))
            self.assertEqual(self.admin.execute('SELECT count(*) FROM public.video_search_projection_queue').fetchone()[0],0)
        finally:
            self.admin.execute('UPDATE public.videos SET title=%s,duration=%s WHERE video_id=%s',(row[1],row[2],row[0]))
            self.refresh()

    def test_21_read_model_follows_inventory_and_lic_reader_sees_only_the_view(self):
        rows=self.admin.execute('SELECT count(*) FROM public.video_inventory').fetchone()[0]
        self.assertEqual(self.admin.execute('SELECT count(*) FROM public.video_search_projection').fetchone()[0],rows)
        with self.admin.transaction():
            self.admin.execute('SET LOCAL ROLE lic_reader')
            self.assertGreater(self.admin.execute('SELECT count(*) FROM readable.video_search_projection').fetchone()[0],0)
        for statement in ('SELECT 1 FROM public.video_search_projection LIMIT 1',
                          'SELECT 1 FROM public.video_search_projection_queue LIMIT 1',
                          "UPDATE public.video_search_projection SET valid_until='infinity'"):
            with self.subTest(statement=statement), self.assertRaises(psycopg.errors.InsufficientPrivilege):
                with self.admin.transaction():
                    self.admin.execute('SET LOCAL ROLE lic_reader')
                    self.admin.execute(statement)


    def test_22_hooks_skip_detached_reviews_and_drop_derived_fields_with_sources(self):
        before=self.admin.execute('SELECT count(*) FROM public.video_search_projection_queue').fetchone()[0]
        self.admin.execute("INSERT INTO public.video_type_review_cases(video_id,channel_id,observed_canonical_type,observed_types,"
                           "observed_channels,state,source_detached_at) VALUES(NULL,NULL,NULL,ARRAY[]::text[],ARRAY[]::text[],'open',now())")
        self.assertEqual(self.admin.execute('SELECT count(*) FROM public.video_search_projection_queue').fetchone()[0],before)
        self.refresh()
        row=self.admin.execute("SELECT p.video_id,v.title,v.duration FROM public.video_search_projection p JOIN public.videos v USING(video_id) "
                               "WHERE p.title_key IS NOT NULL AND p.duration_seconds IS NOT NULL ORDER BY p.video_id LIMIT 1").fetchone()
        try:
            # A retention scrub removes the sources; their derived copies go in the same write.
            self.admin.execute('UPDATE public.videos SET title=NULL WHERE video_id=%s',(row[0],))
            self.assertEqual(self.admin.execute('SELECT title_key IS NULL,duration_seconds IS NOT NULL FROM public.video_search_projection '
                                                'WHERE video_id=%s',(row[0],)).fetchone(),(True,True))
            self.admin.execute('UPDATE public.videos SET duration=NULL WHERE video_id=%s',(row[0],))
            self.assertEqual(self.admin.execute('SELECT title_key,duration_seconds FROM public.video_search_projection '
                                                'WHERE video_id=%s',(row[0],)).fetchone(),(None,None))
        finally:
            self.admin.execute('UPDATE public.videos SET title=%s,duration=%s WHERE video_id=%s',(row[1],row[2],row[0]))
            self.admin.execute('DELETE FROM public.video_type_review_cases WHERE video_id IS NULL')
            self.refresh()


    def test_23_preparation_ends_while_collection_keeps_writing(self):
        import threading, time
        from db.inventory.search_projection import SearchProjectionRepository
        from research_projection_refresher import SearchProjectionRefresher
        repository=SearchProjectionRepository(self.db)
        videos=[r[0] for r in self.admin.execute('SELECT DISTINCT video_id FROM public.video_inventory_memberships ORDER BY video_id')]
        # Changes made while the process was down: the first batch after a start has work.
        backlog=lambda: self.admin.execute('UPDATE public.video_inventory_memberships SET updated_at=now()')
        backlog()
        # A full batch may leave work behind; preparation goes on until one is not full.
        small=SearchProjectionRefresher(repository,ready=lambda: None,limit=5)
        small.step()
        self.assertFalse(small.initial_fill_complete)
        for _ in range(len(videos)):
            if small.initial_fill_complete: break
            small.step()
        self.assertTrue(small.initial_fill_complete)
        backlog()
        stop=threading.Event()
        def collect():
            # One membership row every 0.2 s, far fewer than a playlist scan writes.
            with psycopg.connect(self.admin.info.dsn,password=os.environ['LIC_QUERY_TEST_PASSWORD'],autocommit=True) as writer:
                i=0
                while not stop.is_set():
                    writer.execute('UPDATE public.video_inventory_memberships SET updated_at=now() WHERE video_id=%s',(videos[i%len(videos)],))
                    i+=1
                    stop.wait(.2)
        thread=threading.Thread(target=collect,daemon=True)
        thread.start()
        owner=SearchProjectionRefresher(repository,ready=lambda: None)
        try:
            time.sleep(.3)
            owner.start()
            deadline=time.monotonic()+10
            while not owner.initial_fill_complete:
                self.assertLess(time.monotonic(),deadline,'preparation waited for collection to stop writing')
                time.sleep(.05)
            self.assertTrue(thread.is_alive())
        finally:
            owner.close()
            stop.set()
            thread.join(5)
        self.refresh()

if __name__=='__main__': unittest.main(verbosity=2)
