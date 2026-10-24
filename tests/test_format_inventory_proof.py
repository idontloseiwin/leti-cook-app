"""RR03/RR18 list evidence is separate from hydrated view totals. Offline only."""
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import sys
import unittest

from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'application'))
from channel_explorer import _build_typed_format_total, build_channel_format_totals
from db.reads.channel_explorer import ChannelExplorerRepository, _LIST_INVENTORY
from db.base.postgres import PostgresConfig, PostgresDatabase, _reviewed_statement
from hot_storage import HotSourceReadExpired
from test_direct_pg_transactions import Connection, Pool, Result, DSN
from test_format_totals import NOW, Repository, video, iso

EXPIRY=iso(NOW+timedelta(days=29))


def proof(*,complete=True,unique=302):
    return dict(complete=complete,unique_video_count=unique if complete else None,source_expires_at=EXPIRY,
                formats={kind:dict(status='complete' if complete else 'incomplete',
                                   listed_video_count=count if complete else None,source_expires_at=EXPIRY)
                         for kind,count in (('long',200),('short',100),('stream',10))})


class Lists(Repository):
    def __init__(self):
        stream=video('live',views=None)
        super().__init__([video('long'),*(video(f'long-{i}') for i in range(300))])
        self.lists['stream']=[stream]
        self.videos['live']=stream
        self.states={kind:dict(inventory_status='completed',source_checked_at=iso(NOW),product_exposure_ready=True)
                     for kind in ('long','short','stream')}
        self.states['short']['inventory_status']='playlist_not_found'
        self.channel_age=timedelta(0)
        self.proof=proof()
        self.proof['formats']={kind:dict(status=status,listed_video_count=count,source_expires_at=EXPIRY)
                               for kind,status,count in (('long','complete',301),('short','absent',0),('stream','complete',1))}

    def load_channel(self,*args,**kwargs):
        return dict(channel_id='c',title='Channel',channel_metadata_status='ok',
                    channel_metadata_checked_at=iso(NOW-self.channel_age),video_count=314)

    def load_inventory_state(self,channel_id,*,video_type):return deepcopy(self.states.get(video_type))
    def load_list_inventory(self,channel_id,*,limit):return deepcopy(self.proof)


class InventoryAssemblyTests(unittest.TestCase):
    def test_missing_stream_counter_does_not_remove_independent_inventory_evidence(self):
        result=build_channel_format_totals(Lists(),'c',include_typed=True,now=NOW)
        self.assertEqual(result['formats']['stream']['status'],'incomplete')
        self.assertIsNone(result['formats']['stream']['views'])
        self.assertTrue(result['inventory']['complete'])
        self.assertEqual((result['inventory']['youtube_video_count'],result['inventory']['unique_video_count']),(314,302))
        self.assertEqual(result['inventory']['source_expires_at'],EXPIRY)

    def test_fresh_absence_is_not_unread_and_never_claims_zero_total_videos(self):
        result=build_channel_format_totals(Lists(),'c',include_typed=True,now=NOW)['formats']['short']
        self.assertEqual(result,dict(status='absent',views=None,reason='playlist_not_found',source_expires_at=EXPIRY))
        self.assertNotIn('scanned_inventory_count',result)

    def test_unknown_partial_error_and_expired_absence_have_distinct_states(self):
        cases=[(None,'not_collected'),('pending','not_collected'),('running','incomplete'),('error','incomplete')]
        for state,expected in cases:
            with self.subTest(state=state):
                repo=Lists()
                if state is None:repo.states.pop('short')
                else:repo.states['short']['inventory_status']=state
                result=_build_typed_format_total(repo,'c',video_type='short',now=NOW)
                self.assertEqual(result['status'],expected)
                self.assertIsNone(result['views'])
        for source in (None,iso(NOW-timedelta(days=30)),iso(NOW+timedelta(seconds=1))):
            with self.subTest(source=source):
                repo=Lists();repo.states['short']['source_checked_at']=source
                self.assertEqual(_build_typed_format_total(repo,'c',video_type='short',now=NOW)['status'],'stale')

    def test_stale_channel_count_cannot_be_used_with_fresh_list_proof(self):
        repo=Lists();repo.channel_age=timedelta(days=30)
        result=build_channel_format_totals(repo,'c',include_typed=True,now=NOW)
        self.assertTrue(result['inventory']['complete'])
        self.assertIsNone(result['inventory']['youtube_video_count'])

    def test_inventory_deadline_is_not_inherited_from_an_earlier_counter_deadline(self):
        repo=Lists();repo.videos['long']['checked_at']=iso(NOW-timedelta(days=28,hours=23))
        result=build_channel_format_totals(repo,'c',include_typed=True,now=NOW)
        self.assertEqual(result['formats']['long']['source_expires_at'],iso(NOW+timedelta(hours=1)))
        self.assertEqual(result['inventory']['source_expires_at'],EXPIRY)

    def test_inventory_deadline_cannot_outlive_its_channel_count(self):
        repo=Lists();repo.channel_age=timedelta(days=28,hours=23)
        result=build_channel_format_totals(repo,'c',include_typed=True,now=NOW)
        self.assertEqual(result['inventory']['source_expires_at'],iso(NOW+timedelta(hours=1)))

    def test_partial_list_proof_keeps_count_unknown_even_if_views_ready(self):
        repo=Lists();repo.proof=proof(complete=False)
        result=build_channel_format_totals(repo,'c',include_typed=True,now=NOW)
        self.assertEqual(result['formats']['long']['status'],'ready')
        self.assertFalse(result['inventory']['complete'])
        self.assertIsNone(result['inventory']['unique_video_count'])


class ListInventoryRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.events=[];self.connection=Connection(self.events);self.pool=Pool(self.connection,self.events)
        self.database=PostgresDatabase(PostgresConfig(DSN),pool_factory=lambda config:self.pool,admission=lambda name:None).open()
        self.addCleanup(self.database.close)
        self.repository=ChannelExplorerRepository(self.database,read_clock=lambda:NOW)

    def run_proof(self,value):
        self.connection.results.append(Result(rows=(value,)))
        return self.repository.load_list_inventory('selected-channel',limit=1_000_000)

    def test_public_repository_uses_one_bounded_read_and_protected_channel_scoped_distinct(self):
        result=self.run_proof(proof())
        self.assertEqual(result['unique_video_count'],302)
        self.assertEqual(len(self.connection.queries),1)
        sql,params=self.connection.queries[0]
        self.assertEqual(params,('selected-channel',1_000_000))
        self.assertIn('count(DISTINCT video_id)',sql)
        self.assertIn('m.channel_id=args.channel_id',sql)
        self.assertIn('m.playlist_id=formats.playlist_id',sql)
        self.assertIn("m.membership_status='present'",sql)
        self.assertIn('readable.video_inventory_memberships',sql)
        self.assertNotIn('public.',sql)
        self.assertIn('LIMIT args.scan_limit+1',sql)
        self.assertIn(('SET TRANSACTION READ ONLY',()),self.connection.setup)
        settings=next(p for sql,p in self.connection.setup if "set_config('search_path'" in sql)
        self.assertEqual(settings,('5000ms','0ms'))
        self.assertEqual(self.pool.borrowed,1)

    def test_generation_identity_clock_and_full_scan_proofs_are_not_replaced_by_status_only(self):
        _reviewed_statement(_LIST_INVENTORY,read=True,max_rows=1)
        PostgresQuery(Transformer()).convert(_LIST_INVENTORY,('c',1_000_000))
        for clause in ('completed.scan_id=state.last_completed_scan_id',
                       'completed.channel_id=state.channel_id','completed.video_type=state.video_type',
                       'completed.playlist_id=state.playlist_id',"completed.coverage='complete'",
                       "scan.scan_kind IN('initial_inventory','full_reconciliation')",
                       'source_checked_at NOT BETWEEN',"interval '696 hours'",
                       "inventory_status='playlist_not_found'",'full_source_expires_at IS NOT NULL'):
            self.assertIn(clause,_LIST_INVENTORY)

    def test_expired_proof_is_rejected_after_read_without_returning_counts(self):
        value=proof();value['source_expires_at']=iso(NOW-timedelta(seconds=1))
        with self.assertRaises(HotSourceReadExpired):self.run_proof(value)
        self.assertEqual(len(self.connection.queries),1)

    def test_bad_limits_dispatch_nothing(self):
        for limit in (True,0,1_000_001):
            with self.assertRaises(ValueError):self.repository.load_list_inventory('c',limit=limit)
        self.assertEqual(self.connection.queries,[])

    def test_missing_proof_is_not_zero(self):
        for value in (None,dict(complete=True),dict(complete=False,unique_video_count=0,formats={}),
                      {**proof(), 'source_expires_at':None}):
            with self.assertRaises(RuntimeError):self.run_proof(value)


if __name__=='__main__':unittest.main()
