"""Five synthetic videos: real PG17 handoff, no YouTube or live installation."""
from datetime import datetime, timezone
from pathlib import Path
import json, sys, time
from uuid import uuid4
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'application'),str(ROOT/'tests')]
from schema25_regressions.fixtures import Fixture, admission, dsns
from db.videos.video_collection import VideoCollectionRepository
from video_collection_service import VideoCollectionService


def main():
    with admission() as gate:
        f=Fixture(*dsns(),gate)
        try:
            channel='UC0000000000000000000001'
            f.channel(channel)
            for n in range(5):
                video_id='q'+str(n).zfill(10)
                f.rows("INSERT INTO public.video_inventory(video_id,channel_id,video_type,published_at,published_acquired_at) VALUES(%s,%s,'long',now()-interval '365 days',now()-interval '2 days')",(video_id,channel))
                f.rows("INSERT INTO public.videos(video_id,channel_id,published_at,checked_at,metadata_checked_at,availability_checked_at,availability_status,privacy_status,upload_status,views) VALUES(%s,%s,now()-interval '365 days',now()-interval '2 days',now()-interval '2 days',now()-interval '2 days','public','public','processed',1)",(video_id,channel))
                f.rows("INSERT INTO public.video_collection_state(video_id,channel_id,video_type,hydration_needed,profile_metadata_version,stats_due_at,metadata_due_at,next_due_at,stats_source_checked_at,metadata_source_checked_at) VALUES(%s,%s,'long',false,1,now()-interval '1 hour',now()+interval '2 days',now()-interval '1 hour',now()-interval '2 days',now()-interval '2 days')",(video_id,channel))
            f.rows('UPDATE public.video_collection_state SET consecutive_failures=12')
            sources='SELECT video_id,checked_at,metadata_checked_at,availability_checked_at FROM public.videos ORDER BY video_id'
            before=f.rows(sources)
            repo=VideoCollectionRepository(f.database)
            calls=[]
            def forbidden(*args,**kwargs): raise AssertionError('unexpected path')
            def stats(ids,reason):
                calls.append('stats')
                return {'items':[],'summary':{'failed_ids':[]}}
            def general(ids,reason):
                calls.append('general')
                return []
            collector=VideoCollectionService(repo,fetch_full=forbidden,fetch_metadata=general,
                fetch_counters=stats,build_full_payload=forbidden,build_counter_payload=forbidden)
            def claim(): return repo.claim_due_batch(limit=50,worker_id='handoff-probe',run_id=str(uuid4()))['rows']
            first=claim()
            f.check(len(first)==5 and all(row.collection_mode=='counters' for row in first),'first_stats')
            result=collector._process_common_claims(first)
            f.check(not result['settlement_unknown'] and result['metadata_deferred']==5 and result['unavailable_marked']==0,'stats_not_unavailable')
            state=f.rows('SELECT metadata_due_at,next_due_at,lease_token,consecutive_failures,last_error_type FROM public.video_collection_state')
            f.check(all(row['metadata_due_at']<=datetime.now(timezone.utc)<row['next_due_at'] and row['lease_token'] is None and row['consecutive_failures']==12 and row['last_error_type']=='youtube_stats_counters_unavailable' for row in state),'atomic_general_handoff')
            f.check(before==f.rows(sources),'sources_unchanged')
            print(json.dumps({'stage':'handoff','status':'passed','videos':5}),flush=True)
            delay=(max(row['next_due_at'] for row in state)-datetime.now(timezone.utc)).total_seconds()+0.05
            f.check(0<delay<31,'real_due_wait_bound')
            time.sleep(delay)
            second=claim()
            f.check(len(second)==5 and all(row.collection_mode=='metadata' for row in second),'next_pass_general')
            result=collector._process_common_claims(second)
            f.check(not result['settlement_unknown'] and result['unavailable_marked']==5,'general_confirms_missing')
            f.check(calls==['stats','general'],'no_second_stats')
            f.check(f.scalar("SELECT count(*) FROM public.video_collection_state WHERE paused_reason='unavailable_inventory' AND next_due_at IS NULL AND lease_token IS NULL")==5,'no_ordinary_stats_retry')
            f.observe()
            print(json.dumps({'stage':'complete','status':'passed','checks':f.checks,'youtube_requests':0,'videos':5}),flush=True)
        finally: f.close()

if __name__=='__main__': main()
