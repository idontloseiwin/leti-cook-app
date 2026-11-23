"""Disposable PostgreSQL integration for Saved v1/v2, collections and portable backup.

Also: Library v1 translations executed by the shared service keep the Library
predicate and order, and collection members of every type get one reason each.
A channel or folder written by hand with another UUID version: the backup names
it (test_12), and Deleted channels lists and restores such a channel (test_13).
"""
import json,os,sys,unittest,uuid
from datetime import datetime
from pathlib import Path
import psycopg
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'application'),str(ROOT/'tests')]
from research_query_pg import bootstrap
from db.base.postgres import PostgresConfig,PostgresDatabase
from db.user_work.saved_views import SavedViewsRepository
from db.user_work.user_work_read_queries import export_snapshot
from db.user_work.user_work_restore import import_user_work_backup
from saved_views import SavedViewsService
from saved_work import SavedWorkError
from research_query.saved import from_query,bound_query
from user_work_backup import build_backup,import_payload,encode_backup,decode_backup
from test_research_query_contract import query
from test_library_v1_translation import v1


def library_cases(rows):
    """test_07's Library conditions (filters, facets, zone) over the rows' own publication instants."""
    from datetime import timedelta
    published=sorted(r['published_at'] for r in rows if r['published_at'])
    day=lambda instant,shift=0:(datetime.fromisoformat(instant)+timedelta(days=shift)).date().isoformat()
    year=datetime.fromisoformat(published[-1]).year
    return [({'q':'strasse'},None,'UTC'),({'q':' STRAßE  %_ '},None,'UTC'),({'q':'%_'},None,'UTC'),({'q':'zz'},None,'UTC'),
            ({'category_id':'22'},None,'UTC'),({'min_views':'100','max_views':'200'},None,'UTC'),
            (None,{'view_buckets':['under_1k','1m_10m']},'UTC'),(None,{'duration_buckets':['under_5m']},'UTC'),
            ({'min_likes':'1','max_comments':'12'},None,'UTC'),(None,{'like_buckets':['under_100','100k_plus']},'UTC'),
            ({'published_from':day(published[1]),'published_before':day(published[-1])},None,'Asia/Tokyo'),
            ({'published_from':day(published[0],1)},None,'America/New_York'),
            (None,{'published_years':[year],'published_older_than':year-1},'Europe/Berlin'),
            (None,{'cohort':{'kind':'overview','published_from':None,'published_through':published[1]}},'UTC'),
            (None,{'cohort':{'kind':'overview','published_from':published[0],'published_through':published[-2]}},'UTC')]


def row_cells(row,zone):
    """The registry values a translated condition reads, from a test_07 Library row (None = not known)."""
    from zoneinfo import ZoneInfo
    published=None if row['published_at'] is None else datetime.fromisoformat(row['published_at'].replace('Z','+00:00'))
    local=None if published is None else published.astimezone(ZoneInfo(zone))
    from fractions import Fraction
    number=lambda value:None if value is None else Fraction(value)
    seconds=None if row['duration'] is None else row['duration'][2:-1]
    return {'video.title':row['title'],'video.category_id':row['category_id'],'video.published_at':published,
            'video.published_date':None if local is None else local.date().isoformat(),
            'video.published_year':None if local is None else local.year,'video.views':number(row['views']),
            'video.duration_seconds':number(seconds),'video.likes':number(row['like_count']),'video.comments':number(row['comment_count'])}

class SavedNativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get('LIC_RESEARCH_QUERY_DISPOSABLE')!='1':raise RuntimeError('disposable_only')
        password=os.environ['LIC_QUERY_TEST_PASSWORD'];port=int(os.environ['LIC_QUERY_TEST_PORT'])
        cls.admin=psycopg.connect(host='127.0.0.1',port=port,dbname='leti-cook',user='postgres',password=password,autocommit=True)
        bootstrap(cls.admin)
        cls.db=PostgresDatabase(PostgresConfig(f'host=127.0.0.1 port={port} dbname=leti-cook user=authenticator password={password}',max_size=2),admission=lambda _:None).open()
        cls.service=SavedViewsService(SavedViewsRepository(cls.db))
    @classmethod
    def tearDownClass(cls):cls.db.close();cls.admin.close()
    def test_00_intrinsic_readiness_and_missing_trigger_refusal(self):
        from db.readiness.intrinsic_capabilities import READINESS_SQL
        actual = self.admin.execute(READINESS_SQL).fetchone()
        self.assertEqual(actual, (True, True, True))
        with self.admin.transaction(force_rollback=True):
            self.admin.execute('ALTER TABLE public.saved_objects DISABLE TRIGGER user_work_bytes_before')
            self.assertEqual(self.admin.execute(READINESS_SQL).fetchone(), (True, True, False))
        self.assertEqual(self.admin.execute(READINESS_SQL).fetchone(), (True, True, True))
    def test_01_save_replay_conflict_missing_source_and_backup_restore(self):
        folder=str(uuid.uuid4());identity=str(uuid.uuid4())
        self.admin.execute("INSERT INTO public.folders(id,name) VALUES(%s,'User folder')",(folder,))
        source,definition=from_query(query(scope={'kind':'folder','id':folder},video_types=['short']))
        command={'id':identity,'name':'Shorts study','channel_id':source,'conditions':definition}
        first=self.service.create(command)['view'];self.assertTrue(first['channel_available'])
        self.assertEqual(self.service.create(command)['view'],first)
        renamed=self.service.rename(identity,{'name':'Renamed study','expected_revision':1})['view']
        self.assertEqual(self.service.create(command)['view']['name'],'Renamed study')
        with self.assertRaises(SavedWorkError):self.service.rename(identity,{'name':'Conflict','expected_revision':1})
        from db.user_work.research_preferences import ResearchPreferencesRepository
        from research_field_policy import defaults
        preferences=ResearchPreferencesRepository(self.db)
        document=defaults();document['workspace']['video.description']='current'
        preferences.apply(document,preferences.read()['revision'],str(uuid.uuid4()))
        backup=build_backup(export_snapshot(self.db))['backup']
        self.assertEqual(backup['user_work']['queries'][0]['conditions'],definition)
        self.assertNotIn('create_fingerprint',encode_backup(backup).decode())
        self.admin.execute('DELETE FROM public.folders WHERE id=%s',(folder,))
        self.assertFalse(self.service.get(identity)['view']['channel_available'])
        # Still a readable user definition after its source was deleted.
        backup=decode_backup(encode_backup(build_backup(export_snapshot(self.db))['backup']))
        self.service.delete(identity,renamed['revision'])
        preferences.apply(defaults(),preferences.read()['revision'],str(uuid.uuid4()))
        result=import_user_work_backup(self.db,import_payload(backup))
        self.assertEqual(result['outcome'],'ready')
        restored_preferences=preferences.read()
        self.assertEqual(restored_preferences['pending_import'],document)
        self.assertEqual(restored_preferences['effective']['long']['video.description'],'off')
        preferences.apply(defaults(),restored_preferences['revision'],str(uuid.uuid4()))
        restored=self.service.get(identity)['view']
        self.assertEqual(restored['name'],'Renamed study');self.assertEqual(restored['conditions'],definition)
        self.assertFalse(restored['channel_available']);self.assertGreater(restored['revision'],renamed['revision'])
        self.service.delete(identity,restored['revision'])
    def test_01b_library_presentation_and_typed_members_survive_restore(self):
        from research_field_policy import defaults
        from db.user_work.research_preferences import ResearchPreferencesRepository
        channel_ref,view_id,collection_id=(str(uuid.uuid4()) for _ in range(3))
        _,definition=from_query(query(scope={'kind':'channel','id':'UC'+'1'*22},video_types=['stream'],
            order_by=[{'field':'video.views','direction':'desc'},{'field':'video.published_at','direction':'asc'}]),
            {'version':1,'surface':'library','view':'grid','density':'compact','columns':['video.title','video.views'],'time_zone':'Asia/Tokyo'})
        stamp='2026-10-01T00:00:00+00:00'
        members=[{'video_id':video,'added_at':stamp,'channel_ref':channel_ref} for video in ('a0000000001','s0000000001','t0000000001')]
        document={'format':'leti-cook-user-work','version':5,'created_at':stamp,'browser_preferences':{'catalogue_density':'large','bookmarked_channel_refs':[],'collapsed_folder_ids':[]},
            'user_work':{'folders':[],'labels':[{'label_key':'monetized','name':'Monetized','label_type':'preset','created_at':stamp}],
                'channels':[{'id':channel_ref,'user_input':'https://www.youtube.com/channel/UC'+'1'*22,'folder_id':None,'parent_ref':None,'is_hidden':False,
                    'added_at':stamp,'label_keys':[],'saved_views':[{'id':view_id,'name':'Library streams','revision':1,'created_at':stamp,'updated_at':stamp,'conditions':definition}]}],
                'collections':[{'id':collection_id,'name':'Every type','revision':2,'created_at':stamp,'updated_at':stamp,'members':members}],
                'reviews':[],'queries':[],'field_preferences':defaults()}}
        try:
            self.assertEqual(import_user_work_backup(self.db,import_payload(document))['outcome'],'ready')
            exported=decode_backup(encode_backup(build_backup(export_snapshot(self.db))['backup']))
            [channel]=exported['user_work']['channels']
            self.assertEqual(channel['saved_views'][0]['conditions'],definition)
            self.assertEqual(channel['saved_views'][0]['conditions']['presentation']['time_zone'],'Asia/Tokyo')
            [collection]=exported['user_work']['collections']
            self.assertEqual(sorted((m['video_id'],m['channel_ref']) for m in collection['members']),
                             sorted((m['video_id'],channel_ref) for m in members))
        finally:
            for statement in ('DELETE FROM public.saved_collection_members','DELETE FROM public.saved_collection_commands',
                              'DELETE FROM public.saved_objects','DELETE FROM public.user_channel_restore_items'):
                self.admin.execute(statement)
            preferences=ResearchPreferencesRepository(self.db)
            preferences.apply(defaults(),preferences.read()['revision'],str(uuid.uuid4()))
    def test_02_schema_rejects_unbound_channel_and_result_payload_shape(self):
        from psycopg.types.json import Jsonb
        _,definition=from_query(query(scope={'kind':'channel','id':'UC'+'1'*22}))
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.admin.execute("INSERT INTO public.saved_objects(id,kind,name,conditions,create_fingerprint) VALUES(%s,'view','Wrong owner',%s,repeat('a',64))",(str(uuid.uuid4()),Jsonb(definition)))
    def test_03_portable_byte_limit_rejects_growth_before_commit(self):
        # One over-budget pending user's locator data is rejected atomically.
        # The trigger is exercised directly without bypassing production ACLs/limits.
        before=self.admin.execute('SELECT count(*) FROM public.user_channel_restore_items').fetchone()[0]
        with self.assertRaises(psycopg.Error) as failure:
            self.admin.execute("INSERT INTO public.user_channel_restore_items(id,user_input,is_hidden) SELECT gen_random_uuid(),'https://www.youtube.com/@'||repeat('x',2020),false FROM generate_series(1,8000)")
        self.assertIn('user_work_limit_reached',str(failure.exception))
        self.assertEqual(self.admin.execute('SELECT count(*) FROM public.user_channel_restore_items').fetchone()[0],before)

    def test_04_concurrent_user_work_cannot_cross_portable_capacity(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from urllib.parse import quote
        from user_channel_input import normalize_user_channel_input
        locator='https://www.youtube.com/@'+quote(chr(0x20000)*100)
        self.assertEqual(normalize_user_channel_input(locator),locator)
        labels=['custom:'+str(uuid.uuid4()) for _ in range(10)]
        for key in labels:self.admin.execute("INSERT INTO public.channel_labels(label_key,name,label_type) VALUES(%s,%s,'custom')",(key,'Fixture '+key[-8:]))
        statement="INSERT INTO public.user_channel_restore_items(id,user_input,is_hidden,label_keys) SELECT gen_random_uuid(),%s,false,%s::text[] FROM generate_series(1,%s)"
        self.admin.execute(statement,(locator,labels,6000))
        barrier=Barrier(2)
        def write():
            with psycopg.connect(host='127.0.0.1',port=int(os.environ['LIC_QUERY_TEST_PORT']),dbname='leti-cook',user='authenticator',password=os.environ['LIC_QUERY_TEST_PASSWORD'],autocommit=True) as connection:
                connection.execute('SET ROLE service_role')
                connection.execute("SET statement_timeout='15s'")
                barrier.wait(timeout=5)
                try:
                    connection.execute(statement,(locator,labels,1800))
                    return 'saved'
                except psycopg.Error as error:
                    if error.sqlstate=='P0001' and 'user_work_limit_reached' in str(error):return 'limited'
                    raise
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results=list(pool.map(lambda _:write(),range(2)))
            self.assertEqual(sorted(results),['limited','saved'])
            self.assertEqual(self.admin.execute('SELECT count(*) FROM public.user_channel_restore_items').fetchone()[0],7800)
            # Independently exported user work is still admitted by the portable codec.
            self.assertLess(len(encode_backup(build_backup(export_snapshot(self.db))['backup'])),16*1024*1024)
        finally:
            self.admin.execute('DELETE FROM public.user_channel_restore_items')
            self.admin.execute('DELETE FROM public.channel_labels WHERE label_key=ANY(%s)',(labels,))

    def ensure_seed(self):
        cls=type(self)
        if not getattr(cls,'seeded',False):
            from research_query_pg import seed
            seed(cls.admin,cls.admin.execute('SELECT clock_timestamp()').fetchone()[0])
            cls.seeded=True
    def shared(self):
        from research_query.operations import OPERATIONS
        from research_query.service import QueryService
        from research_query.validation import AccessContext
        from db.reads.research_query import ResearchQueryRepository
        context=AccessContext('synthetic-installation','owner-1',enabled=True,allowed_operations=frozenset(OPERATIONS))
        return QueryService(ResearchQueryRepository(self.db),lambda:context)
    def test_06_collector_review_case_does_not_take_user_work_lock(self):
        from research_query_pg import FIXTURE
        self.ensure_seed()
        channel=FIXTURE['channels'][0]['id']
        connect=lambda:psycopg.connect(host='127.0.0.1',port=int(os.environ['LIC_QUERY_TEST_PORT']),dbname='leti-cook',user='postgres',password=os.environ['LIC_QUERY_TEST_PASSWORD'])
        def waits(holder,args):
            with connect() as a,connect() as b:
                a.execute(holder,args)
                b.execute("SET lock_timeout='1s'")
                try:
                    b.execute("INSERT INTO public.folders(id,name) VALUES(gen_random_uuid(),'Lock probe')")
                    return False
                except psycopg.errors.LockNotAvailable:
                    return True
                finally:a.rollback();b.rollback()
        upsert=("INSERT INTO public.video_type_review_cases(video_id,channel_id,state) VALUES(%s,%s,'open') "
                "ON CONFLICT(video_id) DO UPDATE SET last_seen_at=clock_timestamp()")
        self.assertFalse(waits(upsert,('a0000000007',channel)))
        self.admin.execute(upsert,('a0000000007',channel))
        try:
            self.assertTrue(waits("UPDATE public.video_type_review_cases SET decision_type='long',decision_reason='probe',decision_at=clock_timestamp(),"
                                  "decision_revision=2,revision=2 WHERE video_id=%s",('a0000000007',)))
            self.assertEqual(self.admin.execute('SELECT count(*) FROM operator_private.user_work_bytes_pending').fetchone()[0],0)
        finally:
            self.admin.execute("DELETE FROM public.video_type_review_cases WHERE video_id='a0000000007'")

    def test_05_large_backup_restores_with_one_size_check(self):
        import time
        self.admin.execute("""INSERT INTO public.saved_objects(kind,id,name,create_fingerprint,member_count)
            SELECT 'collection',gen_random_uuid(),'Restore '||g,repeat('a',64),100 FROM generate_series(1,900) g""")
        self.admin.execute("""INSERT INTO public.saved_collection_members(collection_id,video_id,added_at)
            SELECT o.id,'r'||lpad((row_number() OVER ())::text,10,'0'),clock_timestamp()
            FROM public.saved_objects o CROSS JOIN generate_series(1,100) m WHERE o.kind='collection' AND o.name LIKE 'Restore %%'""")
        try:
            payload=import_payload(decode_backup(encode_backup(build_backup(export_snapshot(self.db))['backup'])))
            for table in ('saved_collection_members','saved_collection_commands','saved_objects'):self.admin.execute('DELETE FROM public.'+table)
            started=time.monotonic()
            self.assertEqual(import_user_work_backup(self.db,payload).get('outcome'),'ready')
            # Measured 7 s; per-statement size checks timed out at the 60 s restore limit.
            self.assertLess(time.monotonic()-started,40)
            self.assertEqual(self.admin.execute('SELECT count(*) FROM public.saved_collection_members').fetchone()[0],90000)
        finally:
            for table in ('saved_collection_members','saved_collection_commands','saved_objects'):self.admin.execute('DELETE FROM public.'+table)

    def library_rows(self,execute):
        """Library rows rebuilt from the admitted rows of the shared query; an unknown value is None."""
        def value(row,field):
            cell=row['cells'][field]
            return cell['value'] if cell['state']=='known' else None
        rows=[]
        for row in execute(v1())['rows']:
            seconds=value(row,'video.duration_seconds')
            rows.append({'video_id':row['id'],'title':value(row,'video.title'),'category_id':value(row,'video.category_id'),
                         'published_at':value(row,'video.published_at'),'views':value(row,'video.views'),
                         'duration':None if seconds is None else 'PT'+seconds+'S',
                         'like_count':value(row,'video.likes'),'comment_count':value(row,'video.comments')})
        return rows
    def test_07_library_v1_translation_keeps_the_library_predicate_and_order(self):
        from research_query.validation import canonical
        from research_query.library_v1 import translate
        from research_query_pg import FIXTURE
        from test_library_v1_translation import evaluate
        self.ensure_seed()
        service,channel=self.shared(),FIXTURE['channels'][0]['id']
        def execute(conditions,zone='UTC'):
            translated=translate(conditions,channel_id=channel,time_zone=zone)
            return service.execute(canonical({**translated.query,'page':{'size':200}}))
        rows=self.library_rows(execute)
        self.assertGreaterEqual(len(rows),5)
        nontrivial=0
        for index,(filters,facets,zone) in enumerate(library_cases(rows)):
            conditions=v1(filters,facets)
            where=translate(conditions,channel_id=channel,time_zone=zone).query.get('where')
            expected=[r['video_id'] for r in rows if evaluate(where,row_cells(r,zone)) is True]
            with self.subTest(filters=filters,facets=facets,zone=zone):
                self.assertEqual(sorted(r['id'] for r in execute(conditions,zone)['rows']),sorted(expected))
            nontrivial+=0<len(expected)<len(rows)
        self.assertGreaterEqual(nontrivial,10)
        for sort in ('views','published_at','duration','like_count','comment_count'):
            for direction in ('asc','desc'):
                with self.subTest(sort=sort,direction=direction):
                    shared=[r['id'] for r in execute(v1({'sort_by':sort,'sort_order':direction}))['rows']]
                    by_id={r['video_id']: r for r in rows}
                    field={'views':'video.views','published_at':'video.published_at','duration':'video.duration_seconds',
                           'like_count':'video.likes','comment_count':'video.comments'}[sort]
                    values=[row_cells(by_id[i],'UTC')[field] for i in shared]
                    known=[v for v in values if v is not None]
                    self.assertEqual(values, sorted(known, reverse=direction=='desc') + [None]*(len(values)-len(known)))
    def test_08_collection_members_of_every_type_have_one_reason_each(self):
        from research_query.validation import canonical
        from db.inventory.search_projection import SearchProjectionRepository
        from db.reads.analytics_read_queries import read_saved_collection_sources
        from db.user_work.saved_collections import SavedCollectionsRepository
        from saved_collections import SavedCollectionsService
        from research_query_pg import FIXTURE
        self.ensure_seed();self.maxDiff=None
        expected={'a0000000001':None,'a0000000004':None,'b0000000001':None,'s0000000001':None,'s0000000002':None,'t0000000001':None,
                  'd0000000001':'channel_hidden','z0000000001':'inventory_missing','y0000000001':'channel_unavailable',
                  'a0000000002':'source_mismatch','a0000000007':'type_conflict','a0000000010':'source_stale','a0000000011':'source_stale',
                  'a0000000008':'source_unavailable','a0000000009':'source_unpublished','t0000000002':'source_unpublished',
                  't0000000003':'stream_incomplete','a0000000005':'source_stale','a0000000006':'source_stale'}
        types={'a0000000001':'long','a0000000004':'long','b0000000001':'long','s0000000001':'short','s0000000002':'short','t0000000001':'stream'}
        service=SavedCollectionsService(SavedCollectionsRepository(self.db))
        collection=str(uuid.uuid4());pending=str(uuid.uuid4())
        owner,foreign=FIXTURE['channels'][0]['id'],FIXTURE['channels'][1]['id']
        service.create({'id':collection,'name':'Every type'})
        # Adding from the Shorts and Streams tabs uses the same command as Long-form.
        ids=sorted(set(expected)-{'y0000000001'})
        added=service.change_members(collection,'add',{'request_id':str(uuid.uuid4()),'expected_revision':1,'video_ids':ids})
        self.assertEqual(added['mutation']['changed_count'],len(ids))
        self.admin.execute("INSERT INTO public.user_channel_restore_items(id,user_input,is_hidden) VALUES(%s,'https://www.youtube.com/@pending',false)",(pending,))
        self.admin.execute('INSERT INTO public.saved_collection_members(collection_id,video_id,added_at,restore_owner_ref) VALUES(%s,%s,clock_timestamp(),%s)',
                           (collection,'y0000000001',pending))
        self.admin.execute("UPDATE public.saved_objects SET member_count=member_count+1,revision=revision+1 WHERE id=%s AND kind='collection'",(collection,))
        try:
            self.admin.execute('UPDATE public.video_inventory SET channel_id=%s WHERE video_id=%s',(foreign,'a0000000002'))
            for refreshed in (False,True):
                if refreshed:
                    projection=SearchProjectionRepository(self.db)
                    while projection.refresh():pass
                now=self.admin.execute('SELECT clock_timestamp()').fetchone()[0]
                rows=read_saved_collection_sources(self.db,collection_id=collection,video_ids=sorted(expected),as_of=now)
                with self.subTest(projection_current=refreshed):
                    self.assertEqual({r['video_id']:r['source_reason'] for r in rows},expected)
                    self.assertEqual({r['video_id']:r['video_row']['video_type'] for r in rows if r['video_row']},types)
                    self.assertTrue(all((r['channel_id'] is None)==(r['source_status']=='unavailable') for r in rows))
            # The shared query of each type shows exactly the members the reasons admit:
            # available members and those whose counters or metadata are stale.
            shown={}
            for kind in ('long','short','stream'):
                result=self.shared().execute(canonical({'query_version':1,'source':{'id':'public.videos.current.v1'},
                    'scope':{'kind':'collection','id':collection},'video_types':[kind],'select':['video.id'],
                    'order_by':[{'field':'video.id','direction':'asc'}],'page':{'size':200}}))
                shown.update({r['id']:kind for r in result['rows']})
                if kind=='long':self.assertEqual(result['coverage']['reasons'].get('collection_members_hidden'),1)
            self.assertEqual(shown,{**types,'a0000000005':'long','a0000000006':'long'})
            page=service.members(collection,limit=50)
            by_id={m['video_id']:m for m in page['members']}
            self.assertEqual({k:v['reason'] for k,v in by_id.items()},expected)
            self.assertEqual({k:v['video']['video_type'] for k,v in by_id.items() if v['status']=='available'},types)
            self.assertTrue(all(v['video'] is None and v['source_expires_at'] is None for v in by_id.values() if v['status']=='unavailable'))
            # A video first seen 10 days ago stays shown until its evidence ends.
            from hot_storage import utc_instant
            self.admin.execute("UPDATE public.video_inventory_memberships SET first_seen_at=clock_timestamp()-interval '240 hours' WHERE video_id='b0000000001'")
            ends=self.admin.execute("SELECT first_seen_at+interval '696 hours' FROM public.video_inventory_memberships WHERE video_id='b0000000001'").fetchone()[0]
            member={m['video_id']:m for m in service.members(collection,limit=50)['members']}['b0000000001']
            self.assertEqual((member['status'],utc_instant(member['source_expires_at'])),('available',ends))
        finally:
            self.admin.execute('UPDATE public.video_inventory SET channel_id=%s WHERE video_id=%s',(owner,'a0000000002'))
            self.admin.execute('DELETE FROM public.saved_collection_members WHERE collection_id=%s',(collection,))
            self.admin.execute('DELETE FROM public.saved_collection_commands WHERE collection_id=%s',(collection,))
            self.admin.execute("DELETE FROM public.saved_objects WHERE kind='collection' AND id=%s",(collection,))
            self.admin.execute('DELETE FROM public.user_channel_restore_items WHERE id=%s',(pending,))

    def test_09_saved_definition_drops_the_execution_mode_on_write_and_read(self):
        """The browser codec accepts only consistency current and page {size}."""
        from psycopg.types.json import Jsonb
        folder,stored_id,created_id=(str(uuid.uuid4()) for _ in range(3))
        self.admin.execute("INSERT INTO public.folders(id,name) VALUES(%s,'Mode folder')",(folder,))
        _,plain=from_query(query(scope={'kind':'folder','id':folder},page={'size':25}))
        frozen=json.loads(json.dumps(plain));frozen['query']['consistency']='frozen';frozen['query']['page']['anchor_id']='a0000000001'
        try:
            # Stored by the old normalizer, which kept the mode and the anchor.
            self.admin.execute("INSERT INTO public.saved_objects(id,kind,name,conditions,create_fingerprint) VALUES(%s,'view','Stored frozen',%s,repeat('a',64))",
                               (stored_id,Jsonb(frozen)))
            # Read path: the list, one view and the backup open it as the current definition.
            listed={view['id']:view for view in self.service.list(limit=100)['views']}
            self.assertEqual(listed[stored_id]['conditions'],plain)
            self.assertEqual(self.service.get(stored_id)['view']['conditions'],plain)
            # Write path: a frozen Library request is stored as the definition only.
            created=self.service.create({'id':created_id,'name':'Saved frozen','channel_id':None,'conditions':frozen})['view']
            self.assertEqual(created['conditions'],plain)
            stored=dict(self.admin.execute('SELECT id::text,conditions FROM public.saved_objects WHERE id=ANY(%s::uuid[])',([stored_id,created_id],)).fetchall())
            self.assertEqual(stored[created_id],plain)
            # No mass rewrite: the old row stays as it was and is normalized whenever it is read.
            self.assertEqual(stored[stored_id],frozen)
            backup=build_backup(export_snapshot(self.db))['backup']
            queries={item['id']:item['conditions'] for item in backup['user_work']['queries']}
            self.assertEqual((queries[stored_id],queries[created_id]),(plain,plain))
        finally:
            self.admin.execute('DELETE FROM public.saved_objects WHERE id=ANY(%s::uuid[])',([stored_id,created_id],))
            self.admin.execute('DELETE FROM public.folders WHERE id=%s',(folder,))

    def test_10_member_saved_before_its_video_was_tracked_binds_on_read(self):
        """The owner binding of a member saved before its video was in the inventory
        is resolved on every read from the inventory; the stored row is not written."""
        from datetime import timedelta
        from research_query.validation import canonical
        from db.inventory.search_projection import SearchProjectionRepository
        from db.user_work.saved_collections import SavedCollectionsRepository
        from saved_collections import SavedCollectionsService
        from research_query_pg import FIXTURE
        self.ensure_seed()
        service=SavedCollectionsService(SavedCollectionsRepository(self.db))
        collection,video=str(uuid.uuid4()),'n0000000001'
        owner=FIXTURE['channels'][0]['id']
        def member():
            return {m['video_id']:m for m in service.members(collection,limit=50)['members']}[video]
        def shown():
            result=self.shared().execute(canonical({'query_version':1,'source':{'id':'public.videos.current.v1'},
                'scope':{'kind':'collection','id':collection},'video_types':['long'],'select':['video.id'],
                'order_by':[{'field':'video.id','direction':'asc'}],'page':{'size':200}}))
            return [row['id'] for row in result['rows']],result['coverage']['reasons']
        def stored():
            return self.admin.execute('SELECT user_channel_ref,restore_owner_ref FROM public.saved_collection_members WHERE collection_id=%s AND video_id=%s',
                                      (collection,video)).fetchone()
        service.create({'id':collection,'name':'Saved before tracking'})
        service.change_members(collection,'add',{'request_id':str(uuid.uuid4()),'expected_revision':1,'video_ids':[video,'a0000000001']})
        try:
            self.assertEqual(stored(),(None,None))
            self.assertEqual((member()['status'],member()['reason']),('unavailable','inventory_missing'))
            self.assertEqual(shown(),(['a0000000001'],{'collection_members_unavailable':1}))
            # The channel's next inventory scan finds the video (as the fixture seeds one).
            now=self.admin.execute('SELECT clock_timestamp()').fetchone()[0]
            self.admin.execute("INSERT INTO public.video_inventory(video_id,channel_id,video_type) VALUES(%s,%s,'long')",(video,owner))
            self.admin.execute('''INSERT INTO public.video_inventory_memberships
                (video_id,channel_id,video_type,playlist_id,source_published_at,first_seen_at,last_seen_at,last_present_at,evidence_source)
                VALUES(%s,%s,'long',%s,%s,%s,%s,%s,'legacy_inventory_backfill')''',
                (video,owner,'UULF'+owner[2:],now-timedelta(days=10),now-timedelta(hours=1),now-timedelta(hours=1),now-timedelta(hours=1)))
            self.admin.execute('''INSERT INTO public.videos(video_id,channel_id,title,views,checked_at,metadata_checked_at,published_at,duration,
                availability_status,availability_checked_at,stream_actual_end_at,publication_status)
                VALUES(%s,%s,'Tracked later',42,%s,%s,%s,'PT61S','public',%s,NULL,NULL)''',
                (video,owner,now-timedelta(hours=1),now-timedelta(hours=1),now-timedelta(days=10),now-timedelta(hours=1)))
            for refreshed in (False,True):
                if refreshed:
                    projection=SearchProjectionRepository(self.db)
                    while projection.refresh():pass
                with self.subTest(projection_current=refreshed):
                    found=member()
                    self.assertEqual((found['status'],found['reason']),('available',None))
                    self.assertEqual((found['video']['video_type'],found['video']['title']),('long','Tracked later'))
                    self.assertEqual(shown(),(['a0000000001',video],{}))
            # A read never writes: the stored member is still unbound, and only an
            # owner whose channel holds the video is ever derived.
            self.assertEqual(stored(),(None,None))
            self.admin.execute('UPDATE public.channel_collection_state SET is_hidden=true WHERE channel_id=%s',(owner,))
            try:
                self.assertEqual((member()['status'],member()['reason']),('unavailable','channel_hidden'))
            finally:
                self.admin.execute('UPDATE public.channel_collection_state SET is_hidden=false WHERE channel_id=%s',(owner,))
        finally:
            for table in ('videos','video_inventory_memberships','video_inventory'):
                self.admin.execute(f'DELETE FROM public.{table} WHERE video_id=%s',(video,))
            self.admin.execute('DELETE FROM public.saved_collection_members WHERE collection_id=%s',(collection,))
            self.admin.execute('DELETE FROM public.saved_collection_commands WHERE collection_id=%s',(collection,))
            self.admin.execute("DELETE FROM public.saved_objects WHERE kind='collection' AND id=%s",(collection,))

    def test_11_a_collection_write_stores_the_owner_derived_on_read(self):
        """The next write of the collection (add or remove) stores the owner a
        read derives, so a video that later moves to another channel keeps its member
        with the first owner; until then the derived owner follows the inventory."""
        from datetime import timedelta
        from db.user_work.saved_collections import SavedCollectionsRepository
        from saved_collections import SavedCollectionsService
        from research_query_pg import FIXTURE
        self.ensure_seed()
        service=SavedCollectionsService(SavedCollectionsRepository(self.db))
        collection,video=str(uuid.uuid4()),'n0000000002'
        owner,other=FIXTURE['channels'][0]['id'],FIXTURE['channels'][4]['id']
        work=lambda channel:self.admin.execute('SELECT user_work_id FROM public.channels WHERE channel_id=%s',(channel,)).fetchone()[0]
        def member(name=video):
            found={m['video_id']:m for m in service.members(collection,limit=50)['members']}[name]
            return found['status'],found['reason']
        def stored(name=video):
            return self.admin.execute('SELECT user_channel_ref,restore_owner_ref FROM public.saved_collection_members WHERE collection_id=%s AND video_id=%s',
                                      (collection,name)).fetchone()
        def move(channel):
            # The inventory now lists the video under another tracked channel.
            for table in ('video_inventory','videos'):
                self.admin.execute(f'UPDATE public.{table} SET channel_id=%s WHERE video_id=%s',(channel,video))
            self.admin.execute('UPDATE public.video_inventory_memberships SET channel_id=%s,playlist_id=%s WHERE video_id=%s',(channel,'UULF'+channel[2:],video))
        def change(action,ids):
            revision=service.get(collection)['collection']['revision']
            return service.change_members(collection,action,{'request_id':str(uuid.uuid4()),'expected_revision':revision,'video_ids':ids})
        service.create({'id':collection,'name':'Bound on write'})
        change('add',[video,'a0000000001'])
        try:
            bound_at_insert=stored('a0000000001')
            self.assertEqual((stored(),bound_at_insert),((None,None),(work(owner),None)))
            now=self.admin.execute('SELECT clock_timestamp()').fetchone()[0]
            self.admin.execute("INSERT INTO public.video_inventory(video_id,channel_id,video_type) VALUES(%s,%s,'long')",(video,owner))
            self.admin.execute('''INSERT INTO public.video_inventory_memberships
                (video_id,channel_id,video_type,playlist_id,source_published_at,first_seen_at,last_seen_at,last_present_at,evidence_source)
                VALUES(%s,%s,'long',%s,%s,%s,%s,%s,'legacy_inventory_backfill')''',
                (video,owner,'UULF'+owner[2:],now-timedelta(days=10),now-timedelta(hours=1),now-timedelta(hours=1),now-timedelta(hours=1)))
            self.admin.execute('''INSERT INTO public.videos(video_id,channel_id,title,views,checked_at,metadata_checked_at,published_at,duration,
                availability_status,availability_checked_at,stream_actual_end_at,publication_status)
                VALUES(%s,%s,'Tracked later',42,%s,%s,%s,'PT61S','public',%s,NULL,NULL)''',
                (video,owner,now-timedelta(hours=1),now-timedelta(hours=1),now-timedelta(days=10),now-timedelta(hours=1)))
            # Before any write the owner is derived on every read and follows the inventory (the risk of using a stale owner).
            self.assertEqual((member(),stored()),(('available',None),(None,None)))
            move(other)
            self.assertEqual((member(),stored()),(('available',None),(None,None)))
            move(owner)
            # A no-op write (the member is already there) stores the derived owner; the revision does not move.
            revision=service.get(collection)['collection']['revision']
            self.assertEqual(change('add',[video])['mutation']['changed_count'],0)
            self.assertEqual(service.get(collection)['collection']['revision'],revision)
            self.assertEqual((stored(),stored('a0000000001')),((work(owner),None),bound_at_insert))
            self.assertEqual(member(),('available',None))
            # Stored: moving the video does not move the member.
            move(other)
            self.assertEqual(stored(),(work(owner),None))
            self.assertEqual(member()[0],'unavailable')
            move(owner)
            self.assertEqual(member(),('available',None))
            # A removal writes too: a member still outside the inventory stays unbound.
            late='n0000000003'
            change('add',[late])
            change('remove',['a0000000001'])
            self.assertEqual((stored(late),stored()),((None,None),(work(owner),None)))
            self.assertEqual(member(late),('unavailable','inventory_missing'))
        finally:
            for table in ('videos','video_inventory_memberships','video_inventory'):
                self.admin.execute(f'DELETE FROM public.{table} WHERE video_id=%s',(video,))
            self.admin.execute('DELETE FROM public.saved_collection_members WHERE collection_id=%s',(collection,))
            self.admin.execute('DELETE FROM public.saved_collection_commands WHERE collection_id=%s',(collection,))
            self.admin.execute("DELETE FROM public.saved_objects WHERE kind='collection' AND id=%s",(collection,))
    def test_12_an_id_of_another_uuid_version_is_named_and_the_dialog_still_opens(self):
        # A channel and a folder written by hand with version-1 IDs.
        from db.user_work.user_work_backup import UserWorkBackupRepository
        from user_work_backup import UNSUPPORTED_IDENTITY,UserWorkError
        from user_work_service import UserWorkService
        channel,identity,folder='UC'+'x'*22,'6ba7b810-9dad-11d1-80b4-00c04fd430c8','6ba7b810-9dad-11d1-80b4-00c04fd430c9'
        self.admin.execute("INSERT INTO public.folders(id,name) VALUES(%s,'Hand-made folder')",(folder,))
        self.admin.execute('INSERT INTO public.channels(channel_id,user_work_id,folder_id,added_at) VALUES(%s,%s,%s,clock_timestamp())',
                           (channel,identity,folder))
        self.admin.execute("INSERT INTO public.channel_collection_state(channel_id,user_work_id,owner_permission,is_hidden,control_epoch) "
                           "VALUES(%s,%s,'accepted',false,1)",(channel,identity))
        try:
            with self.assertRaises(UserWorkError) as raised:build_backup(export_snapshot(self.db))
            error=raised.exception
            self.assertEqual((error.code,error.status,error.details['count']),(UNSUPPORTED_IDENTITY,422,2))
            self.assertEqual(error.details['items'],[{'kind':'channel','id':identity,'channel_id':channel},
                                                     {'kind':'folder','id':folder,'name':'Hand-made folder'}])
            self.assertIn('the channel '+channel+' has an internal ID',error.message)
            status=UserWorkService(UserWorkBackupRepository(self.db)).status()
            self.assertNotIn(identity,[row['id'] for row in status['channel_mappings']]+status['user_channel_refs'])
            self.assertNotIn(folder,status['folder_ids'])
            self.assertFalse(status['can_restore'])
        finally:
            self.admin.execute('DELETE FROM public.channels WHERE channel_id=%s',(channel,))
            self.admin.execute('DELETE FROM public.folders WHERE id=%s',(folder,))
        self.assertNotIn(channel,[row['channel_id'] for row in export_snapshot(self.db)['channels']])
    def test_13_a_removed_channel_with_another_uuid_version_stays_in_deleted_channels(self):
        # One such channel failed the whole Deleted channels list (422 invalid_request).
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from channel_trash import create_channel_trash_router
        from db.channel.channel_trash import ChannelTrashRepository
        channel,identity='UC'+'y'*22,'6ba7b810-9dad-11d1-80b4-00c04fd430ca'
        self.admin.execute('INSERT INTO public.channels(channel_id,user_work_id,added_at) VALUES(%s,%s,clock_timestamp())',(channel,identity))
        self.admin.execute("INSERT INTO public.channel_collection_state(channel_id,user_work_id,owner_permission,is_hidden,control_epoch) "
                           "VALUES(%s,%s,'accepted',false,1)",(channel,identity))
        app=FastAPI();app.include_router(create_channel_trash_router(ChannelTrashRepository(self.db)))
        headers={'x-saved-work':'1'}
        try:
            with TestClient(app) as client:
                # As the page does: remove by channel ID, read every page of the list, names, restore.
                removed=client.post('/user-work/deleted-channels/remove/'+channel,headers=headers,json={})
                self.assertEqual((removed.status_code,removed.json()),(200,{'status':'removed'}))
                listed,after=[],None
                while True:
                    page=client.get('/user-work/deleted-channels',params={'after':after} if after else {})
                    self.assertEqual(page.status_code,200,page.text)
                    listed+=page.json()['items'];after=page.json()['next_cursor']
                    if after is None:break
                [item]=[row for row in listed if row['id']==identity]
                self.assertTrue(item['can_restore'])
                names=client.get('/user-work/deleted-channels/titles',params={'ids':identity})
                self.assertEqual(names.status_code,200,names.text)
                restored=client.post('/user-work/deleted-channels/'+identity+'/restore',headers=headers,json={'removed_at':item['removed_at']})
                self.assertEqual((restored.status_code,restored.json()),(200,{'status':'restored'}),restored.text)
            state=self.admin.execute('SELECT is_hidden,hidden_at FROM public.channel_collection_state WHERE channel_id=%s',(channel,)).fetchone()
            self.assertEqual(state,(False,None))
        finally:
            self.admin.execute('DELETE FROM public.channels WHERE channel_id=%s',(channel,))

if __name__=='__main__':unittest.main()
