"""Scoped native PG17 test entrypoint, ONLY in a dedicated disposable cluster.

Never auto-discovered and never accepts a production DSN. The runner supplies
only loopback access to its freshly created cluster, never an existing database.
"""
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
import re
from pathlib import Path
import sys
import unittest
import uuid
from unittest.mock import patch

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'application'))
sys.path.insert(0, str(ROOT/'tests'))
from research_query.validation import AccessContext, QueryRejected, canonical, validate
from research_query.service import QueryService
from research_query.operations import OPERATIONS
from db.reads.research_query import ResearchQueryRepository
from db.base.postgres import PostgresConfig, PostgresDatabase
from test_research_query_contract import FIXTURE, query

# Independent expected values, not computed through the registry/SQL mapper.
# NULL, collected empty arrays, false, zero and >2**53 counters are all distinct.
EXTRA_FIELDS = ['video.likes', 'video.comments', 'video.tags', 'video.topic_urls',
                'video.category_id', 'video.audio_language', 'video.metadata_language', 'video.caption_available']
EXTRA_VALUES = {
    'a0000000001': ['9007199254740993', '0', ['Straße', '%_', "x'; SELECT 1; --"], ['https://example.test/Topic'], '22', 'en-US', 'en', False],
    'a0000000002': ['0', '12', [], [], '10', 'de', '', True],
    'a0000000003': [None]*8,
    'a0000000005': ['777', '7', ['fresh'], [], '20', 'en', 'en', False],
    'a0000000006': ['23', '3', ['expired'], [], '20', 'en', 'en', True],
    'b0000000001': ['9007199254740992', '1', ['Straße'], [], '22', 'en', 'en', False],
    'd0000000001': ['999', '99', ['hidden'], [], '22', 'en', 'en', True],
    's0000000001': ['15', '0', ['Straße'], ['https://example.test/Topic'], '22', 'en-US', 'en', False],
    's0000000002': ['0', None, [], [], None, None, None, True],
    't0000000001': ['15', '0', ['Straße'], ['https://example.test/Topic'], '22', 'en-US', 'en', False],
}

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
    for video, values in EXTRA_VALUES.items():
        admin.execute('''UPDATE public.videos SET like_count=%s,comment_count=%s,tags=%s,youtube_topic_categories=%s,
            category_id=%s,default_audio_language=%s,default_language=%s,caption_available=%s WHERE video_id=%s''',
            (*values, video))
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
        cls.context=AccessContext('synthetic-installation','owner-1',enabled=True,allowed_operations=frozenset(OPERATIONS))
        cls.db=PostgresDatabase(PostgresConfig(f'host=127.0.0.1 port={port} dbname=leti-cook user=authenticator password={password}',max_size=1),
                                cadence_snapshot=__import__('collection_schedule').CollectionSchedules().cadence_snapshot(), admission=lambda _:None).open()
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
        # The production refresh, as the backend role through the gateway, in the
        # installed batch: 10,000 queued rows exceed the statement's own 10 s limit.
        from db.inventory.search_projection import REFRESH_LIMIT, SearchProjectionRepository
        repository = SearchProjectionRepository(cls.db)
        while repository.refresh(REFRESH_LIMIT):
            pass

    def run_query(self, value):
        return QueryService(self.repo,lambda:self.context,codec=self.service.codec).execute(canonical(shift_dates(value,self.delta)))

    @contextmanager
    def collection(self, videos=()):
        identity = str(uuid.uuid4())
        self.admin.execute("INSERT INTO public.saved_objects(kind,id,name,create_fingerprint) VALUES('collection',%s,'Fixture',%s)",
                           (identity, 'a'*64))
        try:
            for video in videos:
                self.admin.execute('INSERT INTO public.saved_collection_members(collection_id,video_id,added_at) VALUES(%s,%s,clock_timestamp())',
                                   (identity, video))
            self.admin.execute('UPDATE public.saved_objects SET member_count=%s WHERE id=%s AND kind=\'collection\'', (len(videos), identity))
            yield {'kind':'collection', 'id':identity}
        finally:
            self.admin.execute('DELETE FROM public.saved_collection_members WHERE collection_id=%s', (identity,))
            self.admin.execute("DELETE FROM public.saved_objects WHERE kind='collection' AND id=%s", (identity,))

    @contextmanager
    def label(self, channels=()):
        identity = 'custom:'+str(uuid.uuid4())
        self.admin.execute("INSERT INTO public.channel_labels(label_key,name,label_type) VALUES(%s,'Fixture','custom')", (identity,))
        try:
            for index in channels:
                self.admin.execute('INSERT INTO public.channel_label_assignments(channel_id,label_key) VALUES(%s,%s)',
                                   (FIXTURE['channels'][index]['id'], identity))
            yield {'kind':'label', 'id':identity}
        finally:
            self.admin.execute('DELETE FROM public.channel_labels WHERE label_key=%s', (identity,))

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
        self.assertEqual(result['rows'][0]['id'],'a0000000005')
        # An empty title orders with the missing ones, last in both directions (Library rule).
        for direction in ('asc','desc'):
            rows=self.run_query(query(select=['video.id','video.title'],order_by=[{'field':'video.title','direction':direction}],page={'size':50}))['rows']
            known=[r['id'] for r in rows if r['cells']['video.title']['value']]
            self.assertEqual([r['id'] for r in rows][:len(known)],known)
            self.assertIn('a0000000003',[r['id'] for r in rows][len(known):])
        result=self.run_query(query(where={'field':'video.views','op':'gte','value':'18446744073709551615'}))
        self.assertEqual(result['rows'],[])

    def test_07_reader_cannot_access_raw_video_payload(self):
        from db.base.postgres import Operation, DatabaseServerError
        from db.base.read_context import http_read_scope
        with self.assertRaises(DatabaseServerError),http_read_scope(),self.db.transaction(Operation('research_fixture_raw_access',read_only=True)) as tx:
            tx.fetch_one('SELECT title FROM public.videos LIMIT 1')

    def test_08_every_registered_sort_and_type_pages_without_duplicates(self):
        from research_query.registry import FIELDS, supports_source, SOURCE_ID
        for kind in ('long','short','stream'):
            for field in FIELDS:
                if not FIELDS[field].sortable or not supports_source(FIELDS[field],SOURCE_ID):
                    continue
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
        with tempfile.TemporaryDirectory() as folder, \
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
        row=self.admin.execute("SELECT v.video_id,v.title,v.duration,v.views FROM public.videos v JOIN public.video_search_projection p USING(video_id) "
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
            self.admin.execute('UPDATE public.videos SET title=%s,duration=%s,views=%s WHERE video_id=%s',(row[1],row[2],row[3],row[0]))
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

    def test_24_label_is_exact_channels_across_folders_and_all_types(self):
        with self.label((0, 1, 3, 4)) as scope:
            # B is held, but readable. D is hidden and must never appear. E is
            # in another folder and still belongs to the explicitly chosen label.
            expected = {'long':['b0000000001','e0000000001','a0000000006','a0000000001','a0000000002','a0000000003','a0000000004','a0000000005'],
                        'short':['s0000000001','s0000000002'], 'stream':['t0000000001']}
            for kind, ids in expected.items():
                with self.subTest(kind=kind):
                    result = self.run_query(query(scope=scope, video_types=[kind]))
                    self.assertEqual([r['id'] for r in result['rows']], ids)
                    self.assertEqual(result['coverage']['channels_in_scope'], 3)
            result = self.run_query(query(scope=scope, where={'field':'video.views','op':'eq','value':'100'}))
            self.assertEqual([r['id'] for r in result['rows']], ['a0000000001','a0000000002'])

    def test_25_empty_missing_deleted_sources_are_distinct(self):
        for make in (self.label, self.collection):
            with make() as scope:
                result = self.run_query(query(scope=scope, coverage={'mode':'require_complete'}))
                self.assertEqual(result['rows'], [])
                self.assertEqual(result['coverage']['status'], 'complete')
            with self.assertRaisesRegex(QueryRejected, 'source_unavailable'):
                self.run_query(query(scope=scope))
        with self.collection() as scope:
            self.admin.execute("UPDATE public.saved_objects SET deleted_at=now(),name=NULL WHERE kind='collection' AND id=%s", (scope['id'],))
            with self.assertRaisesRegex(QueryRejected, 'source_unavailable'):
                self.run_query(query(scope=scope))

    def test_26_collection_is_exact_video_ids_with_one_type_and_all_pages(self):
        videos = ['a0000000001','a0000000002','e0000000001','s0000000001','s0000000002','t0000000001']
        expected = {'long':['e0000000001','a0000000001','a0000000002'],
                    'short':['s0000000001','s0000000002'], 'stream':['t0000000001']}
        with self.collection(videos) as scope:
            for kind, ids in expected.items():
                q = query(scope=scope, video_types=[kind], page={'size':1}, coverage={'mode':'require_complete'})
                found = []
                for _ in range(10):
                    result = self.run_query(q)
                    self.assertEqual(result['coverage']['status'], 'complete')
                    found.extend(r['id'] for r in result['rows'])
                    if not result['page']['has_more']: break
                    q['page']['after'] = result['page']['next_cursor']
                self.assertEqual(found, ids)
            filtered = self.run_query(query(scope=scope, where={'field':'video.views','op':'eq','value':'100'}))
            self.assertEqual([r['id'] for r in filtered['rows']], ['a0000000001','a0000000002'])

    def test_27_collection_unresolved_refs_and_hidden_members_are_partial(self):
        with self.collection(['a0000000001','z0000000001','d0000000001','a0000000010']) as scope:
            result = self.run_query(query(scope=scope))
            self.assertEqual([r['id'] for r in result['rows']], ['a0000000001'])
            self.assertEqual(result['coverage']['reasons'], {'collection_members_unavailable':2, 'collection_members_hidden':1})
            with self.assertRaisesRegex(QueryRejected, 'incomplete_source'):
                self.run_query(query(scope=scope, coverage={'mode':'require_complete'}))
        with self.collection(['z0000000001']) as scope:
            result = self.run_query(query(scope=scope))
            self.assertEqual(result['rows'], [])
            self.assertEqual(result['coverage']['status'], 'partial')
            self.assertEqual(result['coverage']['channels_in_scope'], 0)

    def test_28_collection_does_not_rebind_to_a_different_channel(self):
        video = 'a0000000001'
        owner, foreign = (FIXTURE['channels'][i]['id'] for i in (0,1))
        with self.collection([video]) as scope:
            try:
                self.admin.execute('UPDATE public.video_inventory SET channel_id=%s WHERE video_id=%s', (foreign, video))
                result = self.run_query(query(scope=scope))
                self.assertEqual(result['rows'], [])
                self.assertEqual(result['coverage']['reasons'], {'collection_members_unavailable':1})
            finally:
                self.admin.execute('UPDATE public.video_inventory SET channel_id=%s WHERE video_id=%s', (owner, video))

    def test_29_membership_changes_invalidate_continuation_even_unresolved(self):
        for video in ('a0000000002', 'z0000000001'):
            with self.subTest(removed=video), self.collection(['a0000000001','a0000000002','e0000000001','z0000000001']) as scope:
                q = query(scope=scope, page={'size':1})
                first = self.run_query(q)
                self.assertTrue(first['page']['has_more'])
                self.admin.execute('DELETE FROM public.saved_collection_members WHERE collection_id=%s AND video_id=%s', (scope['id'],video))
                q['page']['after'] = first['page']['next_cursor']
                with self.assertRaisesRegex(QueryRejected, 'cursor_context_changed'):
                    self.run_query(q)
        with self.label((0,1)) as scope:
            q = query(scope=scope, page={'size':1})
            first = self.run_query(q)
            self.admin.execute('DELETE FROM public.channel_label_assignments WHERE label_key=%s AND channel_id=%s',
                               (scope['id'], FIXTURE['channels'][1]['id']))
            q['page']['after'] = first['page']['next_cursor']
            with self.assertRaisesRegex(QueryRejected, 'cursor_context_changed'):
                self.run_query(q)

    def test_30_change_committed_during_page_discards_whole_result(self):
        for make, members in ((self.label, (0,1)), (self.collection, ('a0000000001','a0000000002'))):
            with make(members) as scope:
                read = self.repo.read_page
                def change_after_read(*args, **kwargs):
                    result = read(*args, **kwargs)
                    if scope['kind'] == 'label':
                        self.admin.execute('DELETE FROM public.channel_label_assignments WHERE label_key=%s', (scope['id'],))
                    else:
                        self.admin.execute('DELETE FROM public.saved_collection_members WHERE collection_id=%s', (scope['id'],))
                    return result
                with patch.object(self.repo, 'read_page', side_effect=change_after_read):
                    with self.assertRaisesRegex(QueryRejected, 'cursor_context_changed'):
                        self.run_query(query(scope=scope))

    def test_31_hidden_owner_invalidates_collection_cursor(self):
        channel = FIXTURE['channels'][0]['id']
        with self.collection(['a0000000001','a0000000002','e0000000001']) as scope:
            q = query(scope=scope, page={'size':1})
            first = self.run_query(q)
            try:
                self.admin.execute('UPDATE public.channel_collection_state SET is_hidden=true WHERE channel_id=%s', (channel,))
                q['page']['after'] = first['page']['next_cursor']
                with self.assertRaisesRegex(QueryRejected, 'cursor_context_changed'):
                    self.run_query(q)
                result = self.run_query(query(scope=scope))
                self.assertEqual([r['id'] for r in result['rows']], ['e0000000001'])
                self.assertEqual(result['coverage']['reasons'], {'collection_members_hidden':2})
            finally:
                self.admin.execute('UPDATE public.channel_collection_state SET is_hidden=false WHERE channel_id=%s', (channel,))

    def test_32_other_format_proof_expiry_limits_collection_page(self):
        from hot_storage import HOT_DATA_WINDOW
        video = 's0000000001'
        previous = self.admin.execute('SELECT first_seen_at,last_seen_at,last_present_at FROM public.video_inventory_memberships WHERE video_id=%s', (video,)).fetchone()
        boundary = self.admin.execute("SELECT clock_timestamp()+interval '40 seconds'").fetchone()[0].astimezone(timezone.utc)
        with self.collection(['a0000000001','a0000000002',video]) as scope:
            try:
                observed = boundary-HOT_DATA_WINDOW
                self.admin.execute('UPDATE public.video_inventory_memberships SET first_seen_at=%s,last_seen_at=%s,last_present_at=%s WHERE video_id=%s', (observed,observed,observed,video))
                result = self.run_query(query(scope=scope, page={'size':1}, coverage={'mode':'require_complete'}))
                self.assertEqual(datetime.fromisoformat(result['context']['valid_before']), boundary)
            finally:
                self.admin.execute('UPDATE public.video_inventory_memberships SET first_seen_at=%s,last_seen_at=%s,last_present_at=%s WHERE video_id=%s', (*previous,video))

    def test_33_member_addition_and_restored_owner_binding_invalidate_cursor(self):
        with self.collection(['a0000000001','a0000000002','z0000000001']) as scope:
            q = query(scope=scope, page={'size':1})
            first = self.run_query(q)
            self.admin.execute('INSERT INTO public.saved_collection_members(collection_id,video_id,added_at) VALUES(%s,%s,clock_timestamp())',
                               (scope['id'],'e0000000001'))
            q['page']['after'] = first['page']['next_cursor']
            with self.assertRaisesRegex(QueryRejected, 'cursor_context_changed'):
                self.run_query(q)
            # The owner binding changes independently of the saved-object
            # revision during reconnect. Even an unavailable ID must fence it.
            q['page'] = {'size':1}
            first = self.run_query(q)
            owner = self.admin.execute('SELECT user_work_id FROM public.channels WHERE channel_id=%s',
                                       (FIXTURE['channels'][0]['id'],)).fetchone()[0]
            self.admin.execute('UPDATE public.saved_collection_members SET user_channel_ref=%s WHERE collection_id=%s AND video_id=%s',
                               (owner,scope['id'],'z0000000001'))
            q['page']['after'] = first['page']['next_cursor']
            with self.assertRaisesRegex(QueryRejected, 'cursor_context_changed'):
                self.run_query(q)

    def test_34_collection_rejected_payload_is_not_a_complete_empty_result(self):
        for video, kind in [('a0000000008','long'), ('a0000000009','long'),
                            ('a0000000007','long'), ('t0000000002','stream'), ('t0000000003','stream')]:
            with self.subTest(video=video), self.collection([video]) as scope:
                result = self.run_query(query(scope=scope, video_types=[kind]))
                self.assertEqual(result['rows'], [])
                self.assertEqual(result['coverage']['status'], 'partial')
                self.assertEqual(sum(result['coverage']['reasons'].values()), 1)

    def test_35_preset_label_can_be_empty(self):
        channel = FIXTURE['channels'][4]['id']
        scope = {'kind':'label','id':'monetized'}
        self.admin.execute("INSERT INTO public.channel_label_assignments(channel_id,label_key) VALUES(%s,'monetized')", (channel,))
        try:
            result = self.run_query(query(scope=scope))
            self.assertEqual([r['id'] for r in result['rows']], ['e0000000001'])
        finally:
            self.admin.execute("DELETE FROM public.channel_label_assignments WHERE channel_id=%s AND label_key='monetized'", (channel,))
        result = self.run_query(query(scope=scope, coverage={'mode':'require_complete'}))
        self.assertEqual(result['rows'], [])
        self.assertEqual(result['coverage']['status'], 'complete')

    def test_36_extra_fields_three_formats_and_precise_values(self):
        for kind, video in [('long','a0000000001'), ('short','s0000000001'), ('stream','t0000000001')]:
            result = self.run_query(query(video_types=[kind], select=EXTRA_FIELDS,
                where={'field':'video.id','op':'eq','value':video}))
            self.assertEqual(len(result['rows']), 1)
            cells = result['rows'][0]['cells']
            self.assertEqual([cells[f]['value'] for f in EXTRA_FIELDS], EXTRA_VALUES[video])
            self.assertTrue(all(c['state']=='known' for c in cells.values()))
        for video in ('a0000000002', 'a0000000003'):
            result = self.run_query(query(select=EXTRA_FIELDS, where={'field':'video.id','op':'eq','value':video}))
            cells = result['rows'][0]['cells']
            self.assertEqual([cells[f]['value'] for f in EXTRA_FIELDS], EXTRA_VALUES[video])
            self.assertTrue(all(c['state']==('unknown' if video.endswith('3') else 'known') for c in cells.values()))

    def test_37_array_membership_exact_null_logic_and_no_duplicate_rows(self):
        scope = {'kind':'channel','id':FIXTURE['channels'][0]['id']}
        cases = [
            ({'field':'video.tags','op':'has_any','value':['Straße']}, ['a0000000001']),
            ({'field':'video.tags','op':'has_any','value':['STRASSE']}, []),
            ({'field':'video.tags','op':'has_all','value':['Straße','%_','Straße']}, ['a0000000001']),
            ({'field':'video.tags','op':'has_all','value':['Straße','absent']}, []),
            ({'field':'video.tags','op':'has_any','value':["x'; SELECT 1; --"]}, ['a0000000001']),
            ({'not':{'field':'video.tags','op':'has_any','value':['Straße']}}, ['a0000000002','a0000000005']),
            ({'field':'video.tags','op':'state_is','value':'expired'}, ['a0000000006']),
            ({'field':'video.topic_urls','op':'has_all','value':['https://example.test/Topic']}, ['a0000000001']),
            ({'field':'video.topic_urls','op':'has_any','value':['https://example.test/topic']}, []),
            ({'any':[{'field':'video.tags','op':'has_any','value':['Straße']},
                     {'field':'video.category_id','op':'eq','value':'10'}]}, ['a0000000001','a0000000002']),
        ]
        for predicate, expected in cases:
            with self.subTest(predicate=predicate):
                response = self.run_query(query(scope=scope, select=['video.id'], where=predicate,
                    order_by=[{'field':'video.id','direction':'asc'}]))
                self.assertEqual([r['id'] for r in response['rows']], expected)

    def test_38_all_scalar_sorts_keyset_and_boolean_false(self):
        scope = {'kind':'channel','id':FIXTURE['channels'][0]['id']}
        expected = {'video.likes':['a0000000002','a0000000006','a0000000001','a0000000003','a0000000004','a0000000005'],
                    'video.comments':['a0000000001','a0000000006','a0000000002','a0000000003','a0000000004','a0000000005'],
                    'video.category_id':['a0000000002','a0000000005','a0000000001','a0000000003','a0000000004','a0000000006'],
                    'video.audio_language':['a0000000002','a0000000005','a0000000001','a0000000003','a0000000004','a0000000006'],
                    'video.metadata_language':['a0000000002','a0000000001','a0000000005','a0000000003','a0000000004','a0000000006'],
                    'video.caption_available':['a0000000001','a0000000005','a0000000002','a0000000003','a0000000004','a0000000006']}
        for field, ids in expected.items():
            request = query(scope=scope, select=[field], order_by=[{'field':field,'direction':'asc'}], page={'size':1})
            found = []
            for _ in range(len(ids)+1):
                response = self.run_query(request)
                found.extend(r['id'] for r in response['rows'])
                if not response['page']['has_more']: break
                request['page']['after'] = response['page']['next_cursor']
            self.assertEqual(found, ids, field)
        for value, ids in [(False, ['a0000000001','a0000000005']), (True, ['a0000000002'])]:
            response = self.run_query(query(scope=scope, select=['video.caption_available'],
                where={'field':'video.caption_available','op':'eq','value':value}, order_by=[{'field':'video.id','direction':'asc'}]))
            self.assertEqual([r['id'] for r in response['rows']], ids)
            self.assertTrue(all(r['cells']['video.caption_available']['value'] is value for r in response['rows']))

    def test_39_independent_clocks_required_fields_and_invalid_values(self):
        for video, expired in [('a0000000005', EXTRA_FIELDS[:2]), ('a0000000006', EXTRA_FIELDS[2:])]:
            response = self.run_query(query(select=EXTRA_FIELDS, where={'field':'video.id','op':'eq','value':video}))
            cells = response['rows'][0]['cells']
            for field in EXTRA_FIELDS:
                self.assertEqual(cells[field]['state'], 'expired' if field in expired else 'known')
                if field in expired: self.assertIsNone(cells[field]['value'])
        scope = {'kind':'channel','id':FIXTURE['channels'][0]['id']}
        response = self.run_query(query(scope=scope, select=['video.tags','video.caption_available'],
            coverage={'required_fields':['video.tags','video.caption_available']}))
        self.assertEqual({r['id'] for r in response['rows']}, {'a0000000001','a0000000002','a0000000005'})
        self.assertEqual(response['coverage']['reasons']['required_fields_missing'], 3)
        self.admin.execute("UPDATE public.videos SET like_count=-1,category_id=%s WHERE video_id='a0000000003'", ('x'*65,))
        try:
            response = self.run_query(query(select=['video.likes','video.category_id'], where={'field':'video.id','op':'eq','value':'a0000000003'}))
            self.assertTrue(all(c['state']=='unavailable' and c['value'] is None for c in response['rows'][0]['cells'].values()))
        finally:
            self.admin.execute("UPDATE public.videos SET like_count=NULL,category_id=NULL WHERE video_id='a0000000003'")

    def test_40_payload_change_is_live_with_current_projection_and_near_expiry(self):
        from hot_storage import HOT_DATA_WINDOW
        self.refresh()
        video = 'a0000000001'
        saved = self.admin.execute('SELECT tags,metadata_checked_at FROM public.videos WHERE video_id=%s', (video,)).fetchone()
        try:
            self.admin.execute('UPDATE public.videos SET tags=%s WHERE video_id=%s', (['changed'], video))
            response = self.run_query(query(select=['video.tags'], where={'field':'video.id','op':'eq','value':video}))
            self.assertEqual(response['rows'][0]['cells']['video.tags']['value'], ['changed'])
            clock = self.admin.execute('SELECT clock_timestamp()').fetchone()[0].astimezone(timezone.utc)-HOT_DATA_WINDOW+timedelta(seconds=35)
            self.admin.execute('UPDATE public.videos SET metadata_checked_at=%s WHERE video_id=%s', (clock,video))
            response = self.run_query(query(select=['video.tags'], where={'field':'video.id','op':'eq','value':video}))
            cell = response['rows'][0]['cells']['video.tags']
            self.assertEqual(datetime.fromisoformat(cell['expires_at']), clock+HOT_DATA_WINDOW)
            self.assertLessEqual(datetime.fromisoformat(response['context']['valid_before']), clock+HOT_DATA_WINDOW)
        finally:
            self.admin.execute('UPDATE public.videos SET tags=%s,metadata_checked_at=%s WHERE video_id=%s', (*saved,video))

    def test_41_future_clock_masks_new_metadata_without_masking_counters(self):
        video = 'a0000000001'
        saved = self.admin.execute('SELECT metadata_checked_at FROM public.videos WHERE video_id=%s', (video,)).fetchone()[0]
        try:
            self.admin.execute("UPDATE public.videos SET metadata_checked_at=clock_timestamp()+interval '10 minutes' WHERE video_id=%s", (video,))
            response = self.run_query(query(select=EXTRA_FIELDS, where={'field':'video.id','op':'eq','value':video}))
            cells = response['rows'][0]['cells']
            for field in EXTRA_FIELDS[2:]:
                self.assertEqual(cells[field]['state'], 'unavailable')
                self.assertIsNone(cells[field]['value'])
            self.assertEqual(cells['video.likes']['value'], EXTRA_VALUES[video][0])
        finally:
            self.admin.execute('UPDATE public.videos SET metadata_checked_at=%s WHERE video_id=%s', (saved,video))

    def test_42_collection_completeness_ignores_unfinished_or_expired_channel_scan(self):
        channel = FIXTURE['channels'][2]['id']
        saved = self.admin.execute('SELECT video_type,source_checked_at FROM public.channel_playlist_state WHERE channel_id=%s', (channel,)).fetchall()
        try:
            for expired in (False, True):
                if expired:
                    self.admin.execute("UPDATE public.channel_playlist_state SET source_checked_at=clock_timestamp()-interval '700 hours' WHERE channel_id=%s", (channel,))
                for members in (['c0000000001'], ['a0000000001','c0000000001']):
                    with self.collection(members) as scope:
                        for kind in ('long','short','stream'):
                            for mode in ('available','require_complete'):
                                with self.subTest(expired=expired,members=members,kind=kind,mode=mode):
                                    response = self.run_query(query(scope=scope, video_types=[kind], coverage={'mode':mode}))
                                    self.assertEqual({r['id'] for r in response['rows']}, set(members) if kind=='long' else set())
                                    self.assertEqual(response['coverage']['status'], 'complete')
                                    self.assertEqual(response['coverage']['reasons'], {})
                                    self.assertEqual(response['coverage']['basis'], 'collection_members')
                                    self.assertEqual(response['coverage']['channels_in_scope'], len(members))
                                    self.assertEqual(response['coverage']['channels_complete'], len(members)-1)
        finally:
            for kind, clock in saved:
                self.admin.execute('UPDATE public.channel_playlist_state SET source_checked_at=%s WHERE channel_id=%s AND video_type=%s', (clock,channel,kind))

    def test_43_collection_member_and_required_field_failures_still_block_strict(self):
        for member in ('z0000000001','a0000000007','a0000000008','d0000000001'):
            with self.collection(['c0000000001',member]) as scope:
                response = self.run_query(query(scope=scope))
                self.assertEqual(response['coverage']['status'], 'partial')
                self.assertNotIn('inventory_incomplete', response['coverage']['reasons'])
                self.assertEqual(sum(response['coverage']['reasons'].values()), 1)
                with self.assertRaisesRegex(QueryRejected, 'incomplete_source'):
                    self.run_query(query(scope=scope, coverage={'mode':'require_complete'}))
        with self.collection(['c0000000001']) as scope:
            coverage = {'required_fields':['video.tags']}
            response = self.run_query(query(scope=scope, coverage=coverage))
            self.assertEqual(response['coverage']['reasons'], {'required_fields_missing':1})
            with self.assertRaisesRegex(QueryRejected, 'incomplete_source'):
                self.run_query(query(scope=scope, coverage={**coverage, 'mode':'require_complete'}))

    def test_44_channel_folder_label_still_require_channel_inventory(self):
        with self.label((2,)) as label:
            for scope in (label, {'kind':'channel','id':FIXTURE['channels'][2]['id']},
                          {'kind':'folder','id':FIXTURE['folders'][0]}):
                response = self.run_query(query(scope=scope))
                self.assertEqual(response['coverage']['basis'], 'channel_inventory')
                self.assertEqual(response['coverage']['status'], 'partial')
                self.assertEqual(response['coverage']['reasons']['inventory_incomplete'], 1)
                with self.assertRaisesRegex(QueryRejected, 'incomplete_source'):
                    self.run_query(query(scope=scope, coverage={'mode':'require_complete'}))

    @contextmanager
    def channel_profiles(self):
        columns = 'title,subscribers,views,video_count,avatar_url,channel_created_at,country_code,channel_metadata_checked_at,channel_metadata_status,country_collected_at'
        saved = self.admin.execute('SELECT channel_id,'+columns+' FROM public.channels').fetchall()
        try:
            for i, row in enumerate(saved):
                self.admin.execute("""UPDATE public.channels SET title=%s,subscribers=%s,views=%s,video_count=0,
                    avatar_url='https://yt3.googleusercontent.com/fixture',channel_created_at='2013-01-02Z',country_code='UA',
                    channel_metadata_checked_at=clock_timestamp()-interval '3 hours',channel_metadata_status='ok',
                    country_collected_at=clock_timestamp()-interval '7 hours' WHERE channel_id=%s""",
                    ('Straße '+row[0], 9007199254740993+i,100+i,row[0]))
            yield
        finally:
            assignments=','.join(c+'=%s' for c in columns.split(','))
            for row in saved:
                self.admin.execute('UPDATE public.channels SET '+assignments+' WHERE channel_id=%s', (*row[1:],row[0]))

    def test_45_related_channel_fields_all_formats_and_thumbnail_reference(self):
        from video_thumbnails import video_thumbnail_url
        fields=['channel.title','channel.avatar_url','channel.subscribers','channel.views','channel.video_count',
                'channel.country_code','channel.created_at','video.thumbnail_url','video.actual_end_at','video.scheduled_start_at']
        with self.channel_profiles():
            for kind, video in [('long','a0000000001'),('short','s0000000001'),('stream','t0000000001')]:
                result=self.run_query(query(video_types=[kind],select=fields,where={'field':'video.id','op':'eq','value':video}))
                cells=result['rows'][0]['cells']
                self.assertEqual(cells['channel.title']['value'], 'Straße '+FIXTURE['channels'][0]['id'])
                self.assertGreater(int(cells['channel.subscribers']['value']),2**53)
                self.assertEqual(cells['channel.video_count']['value'],'0')
                self.assertEqual(cells['channel.country_code']['value'],'UA')
                self.assertEqual(cells['channel.created_at']['value'],'2013-01-02T00:00:00Z')
                self.assertEqual(cells['video.thumbnail_url']['value'],video_thumbnail_url(video))
                self.assertEqual(cells['video.scheduled_start_at']['state'],'unknown')
                self.assertEqual(cells['video.actual_end_at']['state'],'known' if kind=='stream' else 'unknown')
                self.assertNotEqual(cells['channel.title']['observed_at'],cells['channel.country_code']['observed_at'])
                self.assertNotEqual(cells['channel.title']['observed_at'],cells['video.thumbnail_url']['observed_at'])

    def test_46_channel_filters_sort_pages_and_hidden_scope(self):
        with self.channel_profiles():
            predicates=[{'field':'channel.title','op':'contains_casefold_v1','value':'STRASSE'},
                        {'field':'channel.country_code','op':'eq','value':'UA'},
                        {'field':'channel.subscribers','op':'gt','value':'9007199254740992'},
                        {'field':'channel.created_at','op':'lt','value':'2014-01-01T00:00:00Z'}]
            for kind in ('long','short','stream'):
                request=query(video_types=[kind],select=['video.channel_id','channel.views'],where={'all':predicates},
                    order_by=[{'field':'channel.views','direction':'desc'},{'field':'channel.title','direction':'asc'}],page={'size':200})
                result=self.run_query(request)
                expected=[(r['id'],int(r['cells']['channel.views']['value'])) for r in result['rows']]
                self.assertTrue(expected)
                self.assertEqual([v for _,v in expected], sorted([v for _,v in expected],reverse=True))
                self.assertNotIn(FIXTURE['channels'][3]['id'], [r['cells']['video.channel_id']['value'] for r in result['rows']])
                request['page']={'size':1}; actual=[]
                for _ in range(30):
                    result=self.run_query(request); actual.extend((r['id'],int(r['cells']['channel.views']['value'])) for r in result['rows'])
                    if not result['page']['has_more']: break
                    request['page']['after']=result['page']['next_cursor']
                self.assertEqual(actual,expected)
            # Channel title matching must not accidentally search video.title.
            self.assertEqual(self.run_query(query(where={'field':'channel.title','op':'contains_casefold_v1','value':'no such profile'}))['rows'],[])

    def test_47_channel_family_expiry_future_unknown_and_required_only(self):
        from hot_storage import HOT_DATA_WINDOW
        channel=FIXTURE['channels'][0]['id']
        request=query(scope={'kind':'channel','id':channel},select=['channel.title','channel.views','channel.country_code','video.views'],
                      where={'field':'video.id','op':'eq','value':'a0000000001'})
        with self.channel_profiles():
            self.admin.execute("UPDATE public.channels SET channel_metadata_checked_at=clock_timestamp()-interval '700 hours' WHERE channel_id=%s",(channel,))
            result=self.run_query(request); cells=result['rows'][0]['cells']
            for f in ('channel.title','channel.views'): self.assertEqual(cells[f]['state'],'expired'); self.assertIsNone(cells[f]['value'])
            self.assertEqual(cells['channel.country_code']['state'],'known')
            self.assertEqual(cells['video.views']['state'],'known')
            request['coverage']={'required_fields':['channel.title']}
            self.assertEqual(self.run_query(request)['rows'],[])
            request.pop('coverage')
            self.admin.execute("UPDATE public.channels SET channel_metadata_checked_at=clock_timestamp()+interval '5 minutes',country_collected_at=clock_timestamp()-interval '700 hours' WHERE channel_id=%s",(channel,))
            cells=self.run_query(request)['rows'][0]['cells']
            self.assertEqual(cells['channel.title']['state'],'unavailable')
            self.assertEqual(cells['channel.country_code']['state'],'expired')
            clock=self.admin.execute('SELECT clock_timestamp()').fetchone()[0].astimezone(timezone.utc)-HOT_DATA_WINDOW+timedelta(seconds=40)
            self.admin.execute('UPDATE public.channels SET channel_metadata_checked_at=%s WHERE channel_id=%s',(clock,channel))
            # Profile used only in a predicate still bounds the whole response.
            result=self.run_query(query(scope={'kind':'channel','id':channel},select=['video.id'],where={'field':'channel.title','op':'is_known'}))
            self.assertLessEqual(datetime.fromisoformat(result['context']['valid_before']),clock+HOT_DATA_WINDOW)
            self.admin.execute("UPDATE public.channels SET channel_metadata_status='error' WHERE channel_id=%s",(channel,))
            self.assertEqual(self.run_query(request)['rows'][0]['cells']['channel.title']['state'],'unknown')

    def test_48_type_provenance_membership_automatic_and_manual(self):
        channel=FIXTURE['channels'][0]['id']; video='a0000000001'
        request=query(select=['video.type'],where={'field':'video.id','op':'eq','value':video})
        def proof():
            result=self.run_query(request)
            return result['provenance'][result['rows'][0]['cells']['video.type']['provenance_ref']]
        self.assertEqual(proof()['method'],'typed_playlist_membership_v1')
        duration=self.admin.execute('SELECT duration FROM public.videos WHERE video_id=%s',(video,)).fetchone()[0]
        try:
            self.admin.execute("UPDATE public.videos SET duration='181' WHERE video_id=%s",(video,))
            self.admin.execute("""INSERT INTO public.video_inventory_memberships(video_id,channel_id,video_type,playlist_id,first_seen_at,last_seen_at,last_present_at,evidence_source)
                VALUES(%s,%s,'short',%s,clock_timestamp(),clock_timestamp(),clock_timestamp(),'legacy_inventory_backfill')""",(video,channel,'UUSH'+channel[2:]))
            automatic=proof(); self.assertEqual(automatic['method'],'automatic_long_short_v1')
            self.assertEqual(automatic['type_rule'],{'id':'long_short_overlap_duration','version':1,'max_short_seconds':180})
            self.admin.execute("""INSERT INTO public.video_type_review_cases(video_id,channel_id,state,revision,decision_type,decision_reason,decision_at,decision_revision)
                VALUES(%s,%s,'resolved',2,'long','fixture',clock_timestamp(),2)""",(video,channel))
            manual=proof(); self.assertEqual(manual['method'],'manual_decision_v1'); self.assertIsNone(manual['type_rule'])
        finally:
            self.admin.execute('DELETE FROM public.video_type_review_cases WHERE video_id=%s',(video,))
            self.admin.execute("DELETE FROM public.video_inventory_memberships WHERE video_id=%s AND video_type='short'",(video,))
            self.admin.execute('UPDATE public.videos SET duration=%s WHERE video_id=%s',(duration,video))

    def test_49_lifecycle_projection_does_not_admit_upcoming_or_infer_end(self):
        video='t0000000001'
        saved=self.admin.execute('SELECT scheduled_start_at FROM public.videos WHERE video_id=%s',(video,)).fetchone()[0]
        # The stored schedule is nullable after publication; don't invent it.
        result=self.run_query(query(video_types=['stream'],select=['video.actual_end_at','video.scheduled_start_at'],
            where={'field':'video.actual_end_at','op':'is_known'}))
        self.assertTrue(result['rows'])
        self.assertNotIn('t0000000002',[r['id'] for r in result['rows']])
        self.assertTrue(all(r['cells']['video.scheduled_start_at']['state']=='unknown' for r in result['rows']))
        self.assertIsNone(saved)

    def test_50_short_rule_requires_overlap_and_matching_canonical_type(self):
        channel=FIXTURE['channels'][0]['id']; video='s0000000001'
        request=query(video_types=['short'],select=['video.type'],where={'field':'video.id','op':'eq','value':video})
        duration=self.admin.execute('SELECT duration FROM public.videos WHERE video_id=%s',(video,)).fetchone()[0]
        try:
            self.admin.execute("UPDATE public.videos SET duration='181' WHERE video_id=%s",(video,))
            result=self.run_query(request)
            ref=result['rows'][0]['cells']['video.type']['provenance_ref']
            self.assertEqual(result['provenance'][ref]['method'],'typed_playlist_membership_v1')
            self.assertIsNone(result['provenance'][ref]['type_rule'])
            self.admin.execute("""INSERT INTO public.video_inventory_memberships(video_id,channel_id,video_type,playlist_id,first_seen_at,last_seen_at,last_present_at,evidence_source)
                VALUES(%s,%s,'long',%s,clock_timestamp(),clock_timestamp(),clock_timestamp(),'legacy_inventory_backfill')""",(video,channel,'UULF'+channel[2:]))
            # The automatic proposal says Long, but no writer has changed the
            # canonical type. The reader cannot silently relabel or admit it.
            self.assertEqual(self.run_query(request)['rows'],[])
            self.admin.execute("UPDATE public.videos SET duration='180' WHERE video_id=%s",(video,))
            result=self.run_query(request); ref=result['rows'][0]['cells']['video.type']['provenance_ref']
            self.assertEqual(result['provenance'][ref]['method'],'automatic_long_short_v1')
            self.assertEqual(result['provenance'][ref]['type_rule']['max_short_seconds'],180)
        finally:
            self.admin.execute("DELETE FROM public.video_inventory_memberships WHERE video_id=%s AND video_type='long'",(video,))
            self.admin.execute('UPDATE public.videos SET duration=%s WHERE video_id=%s',(duration,video))


    def test_51_observations_real_pairs_age_corrections_epochs_and_cursor(self):
        from dataclasses import replace
        from research_query.operations import OPERATIONS
        previous=self.context
        self.context=replace(previous,allowed_operations=frozenset(OPERATIONS))
        videos=['a0000000001','s0000000001','t0000000001']
        originals=self.admin.execute('SELECT video_id,views,checked_at,published_at,stream_actual_end_at FROM public.videos WHERE video_id=ANY(%s)',(videos,)).fetchall()
        epoch='youtube.views.play_start.20260824'
        now=self.admin.execute('SELECT statement_timestamp()').fetchone()[0]
        endpoint=now-timedelta(hours=1)
        try:
            for video in videos:
                self.admin.execute('UPDATE public.videos SET views=700,checked_at=%s,published_at=%s,stream_actual_end_at=CASE WHEN video_id=%s THEN %s ELSE stream_actual_end_at END WHERE video_id=%s',
                    (endpoint,endpoint-timedelta(hours=24),'t0000000001',endpoint-timedelta(hours=4),video))
                for at,value in [(endpoint-timedelta(minutes=80),900),(endpoint,700)]:
                    self.admin.execute('INSERT INTO public.video_snapshots(video_id,recorded_at,views,views_definition_epoch) VALUES(%s,%s,%s,%s)',(video,at,value,epoch))
            for kind,video in zip(['long','short','stream'],videos):
                request=query(video_types=[kind],select=['observation.views_delta','observation.views_per_hour','observation.views_at_age'],
                    where={'field':'video.id','op':'eq','value':video})
                result=self.run_query(request)
                cells=result['rows'][0]['cells']
                self.assertEqual(cells['observation.views_delta']['value'],'-200')
                self.assertEqual(cells['observation.views_per_hour']['value'],'-150.000000')
                self.assertEqual(cells['observation.views_delta']['observation']['actual_seconds'],'4800.000000')
                if kind=='stream':
                    # A Stream's age counts from its actual end, 4 h before the endpoint; 24 h after it is still ahead.
                    age=cells['observation.views_at_age']
                    self.assertEqual((age['state'],age['observation']['reason']),('unavailable','missing_age_observation'))
                    self.assertEqual(datetime.fromisoformat(age['observation']['from_at'].replace('Z','+00:00')),endpoint-timedelta(hours=4))
                    continue
                self.assertEqual(cells['observation.views_at_age']['value'],'700')
                self.assertEqual(cells['observation.views_at_age']['observation']['actual_seconds'],'86400.000000')
            request=query(select=['observation.views_delta'],page={'size':1},order_by=[{'field':'observation.views_delta','direction':'asc'}])
            page=self.run_query(request); self.assertTrue(page['page']['has_more'])
            self.admin.execute('UPDATE public.video_snapshots SET views_definition_epoch=NULL WHERE video_id=%s', (videos[0],))
            # Like the ordinary catalogue, changed history does not invalidate the next page.
            following=self.run_query({**request,'page':{'size':1,'after':page['page']['next_cursor']}})
            self.assertEqual(len(following['rows']),1)
            one=self.run_query(query(select=['observation.views_delta'],where={'field':'video.id','op':'eq','value':videos[0]}))
            # Unstamped snapshots after 2026-08-24 are read with the writer's date rule.
            self.assertEqual(one['rows'][0]['cells']['observation.views_delta']['value'],'-200')
            self.assertEqual(one['rows'][0]['cells']['observation.views_delta']['observation']['reason'],'known')
            self.assertEqual(one['rows'][0]['cells']['observation.views_delta']['observation']['definition_epoch'],epoch)
            self.admin.execute('UPDATE public.video_snapshots SET views_definition_epoch=CASE WHEN recorded_at=%s THEN %s ELSE %s END WHERE video_id=%s',(endpoint,epoch,'incompatible.old',videos[0]))
            one=self.run_query(query(select=['observation.views_delta'],where={'field':'video.id','op':'eq','value':videos[0]}))
            self.assertEqual(one['rows'][0]['cells']['observation.views_delta']['observation']['reason'],'incompatible_definition')
        finally:
            self.admin.execute('DELETE FROM public.video_snapshots WHERE video_id=ANY(%s)',(videos,))
            for video,views,checked,published,end in originals:
                self.admin.execute('UPDATE public.videos SET views=%s,checked_at=%s,published_at=%s,stream_actual_end_at=%s WHERE video_id=%s',(views,checked,published,end,video))
            self.context=previous

    def test_52_observation_page_and_cursor_survive_concurrent_data_write(self):
        repo,admin=self.repo,self.admin
        class ConcurrentObservation:
            def read_page(self,*args,**kwargs):
                result=repo.read_page(*args,**kwargs)
                admin.execute("INSERT INTO public.video_snapshots(video_id,recorded_at,views) VALUES('s0000000001',clock_timestamp(),42)")
                return result
            def recheck_scope(self,*args,**kwargs):return repo.recheck_scope(*args,**kwargs)
        request=query(select=['video.id','observation.views_at_age'],page={'size':1})
        try:
            result=QueryService(ConcurrentObservation(),lambda:self.context,codec=self.service.codec).execute(canonical(request))
            self.assertTrue(result['rows']);self.assertTrue(result['page']['has_more'])
            self.assertEqual(result['context']['consistency'],'current')
            # A later observation does not invalidate the next page (ordinary catalogue behaviour).
            following=self.run_query({**request,'page':{'size':1,'after':result['page']['next_cursor']}})
            self.assertTrue(following['rows'])
            self.assertNotEqual(following['rows'][0]['cells']['video.id']['value'],result['rows'][0]['cells']['video.id']['value'])
        finally:
            admin.execute("DELETE FROM public.video_snapshots WHERE video_id='s0000000001' AND views=42")

    def test_53_optional_policy_write_fence_clocks_and_cleanup(self):
        from dataclasses import replace
        from research_field_policy import defaults,capture,FIELDS as optional,TYPES,FieldPolicyError
        from collection.optional_fields import observe,payload
        from db.user_work.research_preferences import ResearchPreferencesRepository
        from db.videos.video_metadata_completion import complete_video_metadata_collection
        from db.hot.hot_retention import HotRetentionRepository
        preferences=ResearchPreferencesRepository(self.db);old_context=self.context
        before=preferences.read();doc=defaults();doc['workspace']={field:'current' for field in optional};doc['description_setup']='chosen'
        command=str(uuid.uuid4());enabled=preferences.apply(doc,before['revision'],command)
        self.assertEqual(preferences.apply(doc,before['revision'],command)['status'],'replayed')
        with self.assertRaisesRegex(FieldPolicyError,'revision_conflict'):preferences.apply(doc,before['revision'],str(uuid.uuid4()))
        self.assertEqual(preferences.preview_inputs()[0]['retained_descriptions'],0)
        video='a0000000001';channel=FIXTURE['channels'][0]['id']
        self.admin.execute("INSERT INTO public.video_collection_state(video_id,channel_id,video_type) VALUES(%s,%s,'long') ON CONFLICT DO NOTHING",(video,channel))
        old=self.admin.execute('SELECT title,metadata_checked_at,checked_at,views FROM public.videos WHERE video_id=%s',(video,)).fetchone()
        at=self.admin.execute("SELECT clock_timestamp()-interval '10 minutes'").fetchone()[0]
        def row(proof,title='Updated title',paid=False,description='Straße research'):
            token=str(uuid.uuid4());attempt=str(uuid.uuid4())
            self.admin.execute("UPDATE public.video_collection_state SET lease_token=%s,lease_owner='synthetic-fields',lease_started_at=now(),lease_expires_at=now()+interval '5 minutes',lease_channel_epoch=(SELECT control_epoch FROM public.channel_collection_state WHERE channel_id=%s) WHERE video_id=%s",(token,channel,video))
            item=observe([{'snippet':{'description':description},'paidProductPlacementDetails':{'hasPaidProductPlacement':paid}}],proof)[0]
            return dict(video_id=video,channel_id=channel,lease_token=token,collection_attempt_id=attempt,title=title,
                views=700,like_count=10,comment_count=1,recorded_at=at.isoformat(),metadata_observed_at=at.isoformat(),
                published_at=(at-timedelta(days=2)).isoformat(),duration='PT1M',tags=[],category_id='22',youtube_topic_categories=[],
                default_audio_language='en',default_language='en',caption_available=False,availability_status='public',privacy_status='public',upload_status='processed',availability_checked_at=at.isoformat(),**payload(item))
        try:
            proof=capture(doc,enabled['applied_revision']);first=row(proof)
            self.assertEqual(complete_video_metadata_collection(self.db,[first]),1)
            self.assertEqual(complete_video_metadata_collection(self.db,[first]),1)
            q=query(select=['video.title','video.description','video.paid_promotion'],where={'field':'video.description','op':'contains_casefold_v1','value':'STRASSE'})
            cells=self.run_query(q)['rows'][0]['cells'];self.assertEqual(cells['video.description']['value'],'Straße research');self.assertIs(cells['video.paid_promotion']['value'],False)
            self.admin.execute("UPDATE public.video_optional_metadata SET description_checked_at=clock_timestamp()-interval '700 hours' WHERE video_id=%s",(video,))
            allq=query(select=['video.title','video.description','video.paid_promotion'],where={'field':'video.id','op':'eq','value':video})
            cells=self.run_query(allq)['rows'][0]['cells'];self.assertEqual(cells['video.title']['state'],'known');self.assertEqual(cells['video.description']['state'],'expired');self.assertIs(cells['video.paid_promotion']['value'],False)
            # The response began with revision 2, but a settings change wins.
            inflight=row(proof,description='Must not be stored')
            off=preferences.apply(defaults(),preferences.read()['revision'],str(uuid.uuid4()))
            self.assertEqual(complete_video_metadata_collection(self.db,[inflight]),1)
            value=self.admin.execute('SELECT description FROM public.video_optional_metadata WHERE video_id=%s',(video,)).fetchone()[0]
            self.assertEqual(value,'Straße research')
            self.context=replace(old_context,preferences_revision=str(off['applied_revision']),disabled_type_fields=frozenset((kind,field) for kind in TYPES for field in optional))
            with self.assertRaisesRegex(QueryRejected,'field_disabled'):self.run_query(allq)
            retention=HotRetentionRepository(self.db);result=retention.cleanup_page('video_description',limit=100)
            self.assertEqual(result.scrubbed,1)
            self.assertIsNone(self.admin.execute('SELECT description FROM public.video_optional_metadata WHERE video_id=%s',(video,)).fetchone()[0])
            # Off does not delete other fresh values until expiry or an explicit purge.
            self.assertIs(self.admin.execute('SELECT paid_promotion FROM public.video_optional_metadata WHERE video_id=%s',(video,)).fetchone()[0],False)
            preferences.apply(defaults(),preferences.read()['revision'],str(uuid.uuid4()),purge=['video.paid_promotion'])
            self.assertEqual(retention.cleanup_page('video_paid_promotion',limit=100).scrubbed,1)
        finally:
            self.context=old_context
            self.admin.execute('DELETE FROM public.video_optional_metadata WHERE video_id=%s',(video,))
            self.admin.execute('DELETE FROM public.video_snapshots WHERE video_id=%s AND recorded_at=%s',(video,at))
            self.admin.execute('UPDATE public.video_collection_state SET lease_token=NULL,lease_owner=NULL,lease_started_at=NULL,lease_expires_at=NULL,lease_channel_epoch=NULL WHERE video_id=%s',(video,))
            self.admin.execute('UPDATE public.videos SET title=%s,metadata_checked_at=%s,checked_at=%s,views=%s WHERE video_id=%s',(*old,video))
            preferences.apply(defaults(),preferences.read()['revision'],str(uuid.uuid4()))

    def test_54_snapshot_writers_on_different_videos_do_not_serialize(self):
        import psycopg
        connect=lambda:psycopg.connect(host='127.0.0.1',port=int(os.environ['LIC_QUERY_TEST_PORT']),dbname='leti-cook',user='postgres',password=os.environ['LIC_QUERY_TEST_PASSWORD'])
        insert="INSERT INTO public.video_snapshots(video_id,recorded_at,views) VALUES(%s,clock_timestamp(),1)"
        with connect() as first,connect() as second:
            try:
                first.execute(insert,('a0000000001',))
                second.execute("SET lock_timeout='1s'")
                second.execute(insert,('s0000000002',))
                second.execute("UPDATE public.videos SET published_at=published_at-interval '1 second' WHERE video_id='e0000000001'")
            finally:
                first.rollback();second.rollback()

    def test_55_metadata_refresh_keeps_the_counter_rhythm(self):
        from db.videos.video_metadata_completion import complete_video_metadata_collection
        from research_field_policy import defaults,capture
        from collection.optional_fields import observe,payload
        from db.user_work.research_preferences import ResearchPreferencesRepository
        video='a0000000002';channel=FIXTURE['channels'][0]['id']
        self.admin.execute("INSERT INTO public.video_collection_state(video_id,channel_id,video_type) VALUES(%s,%s,'long') ON CONFLICT DO NOTHING",(video,channel))
        old=self.admin.execute('SELECT title,metadata_checked_at,checked_at,views FROM public.videos WHERE video_id=%s',(video,)).fetchone()
        at=self.admin.execute("SELECT date_trunc('second',clock_timestamp()-interval '10 minutes')").fetchone()[0]
        proof=capture(defaults(),ResearchPreferencesRepository(self.db).read()['revision'])
        def complete(scheduled):
            token=str(uuid.uuid4())
            self.admin.execute("UPDATE public.video_collection_state SET stats_due_at=%s,lease_token=%s,lease_owner='synthetic-rhythm',lease_started_at=now(),"
                "lease_expires_at=now()+interval '5 minutes',lease_channel_epoch=(SELECT control_epoch FROM public.channel_collection_state WHERE channel_id=%s) WHERE video_id=%s",(scheduled,token,channel,video))
            item=observe([{'snippet':{'description':''},'paidProductPlacementDetails':{}}],proof)[0]
            row=dict(video_id=video,channel_id=channel,lease_token=token,collection_attempt_id=str(uuid.uuid4()),title='Rhythm',
                views=800,like_count=1,comment_count=0,recorded_at=at.isoformat(),metadata_observed_at=at.isoformat(),
                published_at=(at-timedelta(days=200)).isoformat(),duration='PT1M',tags=[],category_id='22',youtube_topic_categories=[],
                default_audio_language='en',default_language='en',caption_available=False,availability_status='public',privacy_status='public',
                upload_status='processed',availability_checked_at=at.isoformat(),**payload(item))
            self.assertEqual(complete_video_metadata_collection(self.db,[row]),1)
            return self.admin.execute('SELECT stats_due_at FROM public.video_collection_state WHERE video_id=%s',(video,)).fetchone()[0]
        try:
            # A daily counter refresh still 30 minutes ahead keeps its time.
            self.assertEqual(complete(at+timedelta(minutes=30)),at+timedelta(minutes=30))
            # A counter refresh already due restarts from this observation.
            self.assertGreater(complete(at-timedelta(hours=1)),at)
        finally:
            self.admin.execute("DELETE FROM public.video_snapshots WHERE video_id=%s AND recorded_at=%s",(video,at))
            self.admin.execute('UPDATE public.video_collection_state SET lease_token=NULL,lease_owner=NULL,lease_started_at=NULL,lease_expires_at=NULL,lease_channel_epoch=NULL WHERE video_id=%s',(video,))
            self.admin.execute('UPDATE public.videos SET title=%s,metadata_checked_at=%s,checked_at=%s,views=%s WHERE video_id=%s',(*old,video))

    def test_56_change_windows_only_offer_videos_refreshed_that_often(self):
        # Default schedule: hourly counters up to 7 days, daily up to a year.
        video='a0000000003'
        old=self.admin.execute('SELECT published_at,metadata_checked_at FROM public.videos WHERE video_id=%s',(video,)).fetchone()
        one=lambda select,observations=None:self.run_query({**query(select=select,where={'field':'video.id','op':'eq','value':video}),
            **({'observations':observations} if observations else {})})['rows']
        try:
            self.admin.execute("UPDATE public.videos SET published_at=now()-interval '30 days',metadata_checked_at=now() WHERE video_id=%s",(video,))
            self.assertEqual(one(['video.id','observation.views_per_hour']),[])
            self.assertEqual(len(one(['video.id','observation.views_delta'],{'version':1,'window_seconds':86400,'tolerance_seconds':7200,'age_seconds':86400,'age_tolerance_seconds':7200})),1)
            self.assertEqual(len(one(['video.id','observation.views_at_age'])),1)
            # A change used only in a condition narrows the query too, and the response says so.
            condition=self.run_query(query(select=['video.id'],where={'all':[{'field':'video.id','op':'eq','value':video},
                {'not':{'field':'observation.views_delta','op':'lt','value':'-1000000000'}}]}))
            self.assertEqual(condition['rows'],[])
            self.assertEqual(condition['context']['observation_age_limit_seconds'],604800)
            self.assertIsNone(self.run_query(query(select=['video.id'],where={'field':'video.id','op':'eq','value':video}))['context']['observation_age_limit_seconds'])
            self.admin.execute("UPDATE public.videos SET published_at=now()-interval '2 days' WHERE video_id=%s",(video,))
            self.assertEqual(len(one(['video.id','observation.views_per_hour'])),1)
        finally:
            self.admin.execute('UPDATE public.videos SET published_at=%s,metadata_checked_at=%s WHERE video_id=%s',(*old,video))

    def test_57_displayed_optional_fields_are_read_for_the_page_only(self):
        # A field only displayed is read for the returned page. The answer equals
        # the whole-scope path that a condition on the same field still uses.
        from research_field_policy import defaults,FIELDS as optional
        from db.user_work.research_preferences import ResearchPreferencesRepository
        from db.reads.research_sql import compile_query
        from research_query.validation import validate
        preferences=ResearchPreferencesRepository(self.db)
        doc=defaults();doc['workspace']={field:'current' for field in optional};doc['description_setup']='chosen'
        revision=preferences.apply(doc,preferences.read()['revision'],str(uuid.uuid4()))['applied_revision']
        order=[{'field':'video.id','direction':'asc','nulls':'last'}]
        ids=[row['id'] for row in self.run_query(query(select=['video.id'],order_by=order))['rows']][:5]
        self.assertEqual(len(ids),5)
        try:
            for video,text,age,paid in ((ids[0],'Known text','1 hour',True),(ids[1],'Old text','700 hours',False),(ids[2],None,'1 hour',None)):
                self.admin.execute("INSERT INTO public.video_optional_metadata(video_id,description,description_folded,description_checked_at,description_policy_revision,"
                    "paid_promotion,paid_promotion_checked_at,paid_promotion_policy_revision) VALUES(%s,%s,lower(%s),clock_timestamp()-%s::interval,%s,%s,clock_timestamp()-%s::interval,%s)",
                    (video,text,text,age,revision,paid,age,revision))
            shown=query(select=['video.id','video.description','video.paid_promotion'],order_by=order,where={'field':'video.id','op':'in','value':ids})
            either=lambda field:{'any':[{'field':field,'op':'is_known'},{'field':field,'op':'is_unknown'}]}
            filtered={**shown,'where':{'all':[shown['where'],either('video.description'),either('video.paid_promotion')]}}
            sql=compile_query(validate(canonical(shift_dates(shown,self.delta)),self.context))[0]
            self.assertIn('page_optional ON true',sql);self.assertNotIn(') optional ON true',sql)
            self.assertNotIn('page_optional',compile_query(validate(canonical(shift_dates(filtered,self.delta)),self.context))[0])
            page,scope=self.run_query(shown),self.run_query(filtered)
            self.assertEqual([row['id'] for row in page['rows']],ids)
            self.assertEqual(page['rows'],scope['rows'])
            self.assertEqual([row['cells']['video.description']['state'] for row in page['rows']],['known','expired','unknown','unknown','unknown'])
            self.assertEqual([row['cells']['video.paid_promotion']['value'] for row in page['rows']],[True,None,None,None,None])
            # Only the page's clocks bound its expiry: never sooner than the whole-scope
            # bound (each relative to its own evaluation), never after a shown value expires.
            span=lambda r:datetime.fromisoformat(r['context']['valid_before'])-datetime.fromisoformat(r['context']['evaluated_at'])
            self.assertGreaterEqual(span(page),span(scope))
            self.assertLessEqual(page['context']['valid_before'],page['rows'][0]['cells']['video.description']['expires_at'])
        finally:
            self.admin.execute('DELETE FROM public.video_optional_metadata WHERE video_id=ANY(%s)',(ids,))
            preferences.apply(defaults(),preferences.read()['revision'],str(uuid.uuid4()))

if __name__=='__main__': unittest.main(verbosity=2)
