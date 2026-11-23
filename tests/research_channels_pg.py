"""Channel sources on disposable PG17; no YouTube, real installation or API key."""
import sys,unittest,uuid
from pathlib import Path
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'application'),str(Path(__file__).resolve().parent)]
import research_query_pg as native
from test_research_channels import channel_query
from research_query.validation import canonical,QueryRejected

class ChannelPostgres(unittest.TestCase):
    setUpClass=classmethod(native.NativeQueryTests.setUpClass.__func__)
    tearDownClass=classmethod(native.NativeQueryTests.tearDownClass.__func__)
    refresh=classmethod(native.NativeQueryTests.refresh.__func__)
    def test_01_channel_without_videos_profile_unknown_and_precise_paging(self):
        folder=native.FIXTURE['folders'][0];identity='UC'+'Z'*22;owner=str(uuid.uuid4())
        self.admin.execute("INSERT INTO public.channels(channel_id,user_work_id,folder_id) VALUES(%s,%s,%s)",(identity,owner,folder))
        self.admin.execute("INSERT INTO public.channel_collection_state(channel_id,user_work_id,owner_permission) VALUES(%s,%s,'accepted')",(identity,owner))
        try:
            q={**channel_query(),'scope':{'kind':'folder','id':folder},'page':{'size':2}}
            result=self.service.execute(canonical(q));rows=list(result['rows'])
            self.assertEqual(result['coverage']['basis'],'channel_profiles');self.assertEqual(result['coverage']['status'],'partial')
            while result['page']['has_more']:
                result=self.service.execute(canonical({**q,'page':{'size':2,'after':result['page']['next_cursor']}}));rows+=result['rows']
            self.assertIn(identity,[r['id'] for r in rows]);self.assertEqual(len(rows),len({r['id'] for r in rows}))
            row=next(r for r in rows if r['id']==identity)
            self.assertEqual(row['cells']['channel.id']['value'],identity);self.assertEqual(row['cells']['channel.title']['state'],'unknown')
            with self.assertRaisesRegex(QueryRejected,'incomplete_source'):
                self.service.execute(canonical({**q,'coverage':{'mode':'require_complete'}}))
            self.admin.execute("UPDATE public.channels SET title='Stra\u00dfe',subscribers=9007199254740993,views=0,channel_metadata_status='ok',channel_metadata_checked_at=clock_timestamp(),country_code='UA',country_collected_at=clock_timestamp()-interval '700 hours' WHERE channel_id=%s",(identity,))
            q={**q,'scope':{'kind':'channel','id':identity},'select':['channel.id','channel.title','channel.subscribers','channel.views','channel.country_code'],
                'where':{'field':'channel.title','op':'contains_casefold_v1','value':'STRASSE'}}
            result=self.service.execute(canonical(q));self.assertEqual(result['coverage']['status'],'complete')
            self.assertEqual(result['rows'][0]['cells']['channel.title']['value'],'Stra\u00dfe')
            self.assertEqual(result['rows'][0]['cells']['channel.subscribers']['value'],'9007199254740993')
            self.assertEqual(result['rows'][0]['cells']['channel.views']['value'],'0')
            self.assertEqual(result['rows'][0]['cells']['channel.country_code']['state'],'expired')
            self.admin.execute("UPDATE public.channel_collection_state SET is_hidden=true WHERE channel_id=%s",(identity,))
            with self.assertRaisesRegex(QueryRejected,'source_unavailable'):self.service.execute(canonical(q))
        finally:self.admin.execute('DELETE FROM public.channels WHERE channel_id=%s',(identity,))

    def test_02_thousand_channel_profiles_whole_scope_order(self):
        import time,json
        folder=str(uuid.uuid4())
        self.admin.execute("INSERT INTO public.folders(id,name) VALUES(%s,'Channel scale fixture')",(folder,))
        self.admin.execute("INSERT INTO public.channels(channel_id,user_work_id,folder_id,title,subscribers,views,video_count,channel_metadata_status,channel_metadata_checked_at) SELECT 'UCq'||lpad(n::text,21,'0'),gen_random_uuid(),%s,'Profile '||n,n,n*10,0,'ok',clock_timestamp() FROM generate_series(1,1000)n",(folder,))
        self.admin.execute("INSERT INTO public.channel_collection_state(channel_id,user_work_id,owner_permission) SELECT channel_id,user_work_id,'accepted' FROM public.channels WHERE folder_id=%s",(folder,))
        try:
            q={**channel_query(),'scope':{'kind':'folder','id':folder},'select':['channel.id','channel.title','channel.avatar_url','channel.subscribers','channel.views','channel.video_count','channel.country_code','channel.created_at'],'page':{'size':50}}
            timings=[]
            for _ in range(5):
                start=time.perf_counter();result=self.service.execute(canonical(q));timings.append((time.perf_counter()-start)*1000)
                self.assertEqual(result['rows'][0]['cells']['channel.subscribers']['value'],'1000')
                self.assertEqual(result['coverage']['channels_in_scope'],1000);self.assertEqual(result['coverage']['status'],'complete')
            print('CHANNEL_SCALE '+json.dumps({'channels':1000,'whole_request_ms':timings,'max_ms':max(timings)}),flush=True)
            self.assertLess(max(timings),750)
        finally:
            self.admin.execute('DELETE FROM public.channels WHERE folder_id=%s',(folder,));self.admin.execute('DELETE FROM public.folders WHERE id=%s',(folder,))

if __name__=='__main__':unittest.main(verbosity=2)
