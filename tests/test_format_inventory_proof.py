"""List evidence of the header's unlisted note, separate from view totals. Offline only.

The proof is its own thin read (GET /channels/{id}/explorer/list-inventory); header
totals come from channel.format_totals.v1 only.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'application'))
from channel_explorer import LIST_SCAN_LIMIT, ChannelExplorerNotFoundError, build_channel_list_inventory
from db.reads.channel_explorer import ChannelExplorerRepository, _LIST_INVENTORY
from db.base.postgres import PostgresConfig, PostgresDatabase, _reviewed_statement
from hot_storage import HotSourceReadExpired
from test_direct_pg_transactions import Connection, Pool, Result, DSN

NOW=datetime(2026,10,9,12,tzinfo=timezone.utc)


def iso(value):
    return value.isoformat().replace('+00:00','Z')


EXPIRY=iso(NOW+timedelta(days=29))


def proof(*,complete=True,unique=302):
    return dict(complete=complete,unique_video_count=unique if complete else None,source_expires_at=EXPIRY,
                library_formats={kind:dict(status='complete' if complete else 'partial',
                    full_completed_at=iso(NOW) if complete else None,absent_checked_at=None,source_expires_at=EXPIRY)
                    for kind in ('long','short','stream')},
                formats={kind:dict(status='complete' if complete else 'incomplete',
                                   listed_video_count=count if complete else None,source_expires_at=EXPIRY)
                         for kind,count in (('long',200),('short',100),('stream',10))})


class Lists:
    """One channel: its profile row and its list proof; nothing else may be read."""
    def __init__(self):
        self.channel_age=timedelta(0)
        self.channel=dict(channel_id='c',title='Channel',channel_metadata_status='ok',video_count=314)
        self.proof=proof()
        self.limits=[]

    def load_channel(self,*,channel_id,columns):
        return dict(self.channel,channel_metadata_checked_at=iso(NOW-self.channel_age))

    def load_list_inventory(self,channel_id,*,limit):
        self.limits.append(limit)
        return deepcopy(self.proof)


class InventoryAssemblyTests(unittest.TestCase):
    def test_fresh_profile_count_and_complete_lists_are_the_whole_proof(self):
        repo=Lists()
        result=build_channel_list_inventory(repo,'c',now=NOW)
        self.assertEqual(set(result),{'contract_version','channel_id','generated_at','inventory','library','source'})
        self.assertTrue(result['inventory']['complete'])
        self.assertEqual((result['inventory']['youtube_video_count'],result['inventory']['unique_video_count']),(314,302))
        self.assertEqual(result['inventory']['source_expires_at'],EXPIRY)
        self.assertEqual(result['source'],dict(kind='stored_channel_list_inventory',youtube_requests=False,database_writes=False))
        self.assertEqual(repo.limits,[LIST_SCAN_LIMIT])
        self.assertGreaterEqual(LIST_SCAN_LIMIT,1_000_000)

    def test_stale_channel_count_cannot_be_used_with_fresh_list_proof(self):
        repo=Lists();repo.channel_age=timedelta(days=30)
        result=build_channel_list_inventory(repo,'c',now=NOW)
        self.assertTrue(result['inventory']['complete'])
        self.assertIsNone(result['inventory']['youtube_video_count'])
        self.assertEqual(result['inventory']['source_expires_at'],EXPIRY)
        repo=Lists();repo.channel['channel_metadata_status']='error'
        self.assertIsNone(build_channel_list_inventory(repo,'c',now=NOW)['inventory']['youtube_video_count'])

    def test_inventory_deadline_cannot_outlive_its_channel_count(self):
        repo=Lists();repo.channel_age=timedelta(days=28,hours=23)
        result=build_channel_list_inventory(repo,'c',now=NOW)
        self.assertEqual(result['inventory']['source_expires_at'],iso(NOW+timedelta(hours=1)))

    def test_partial_list_proof_keeps_count_unknown(self):
        repo=Lists();repo.proof=proof(complete=False)
        result=build_channel_list_inventory(repo,'c',now=NOW)
        self.assertFalse(result['inventory']['complete'])
        self.assertIsNone(result['inventory']['unique_video_count'])

    def test_hidden_or_unknown_channel_is_not_found_before_any_list_read(self):
        for channel in (None,dict(channel_id='c',is_hidden=True)):
            repo=Lists();repo.load_channel=lambda **kwargs:channel
            with self.assertRaises(ChannelExplorerNotFoundError):
                build_channel_list_inventory(repo,'c',now=NOW)
            self.assertEqual(repo.limits,[])


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
        for view in ('library', 'manual'):
            self.assertIn(f'{view}.channel_id=(SELECT channel_id FROM args)', sql)
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

    def test_expired_format_deadline_refuses_a_late_read_even_if_the_count_proof_is_current(self):
        value = proof()
        value['library_formats']['short']['source_expires_at'] = iso(NOW)
        with self.assertRaises(HotSourceReadExpired):
            self.run_proof(value)

    def test_bad_limits_dispatch_nothing(self):
        for limit in (True,0,1_000_001):
            with self.assertRaises(ValueError):self.repository.load_list_inventory('c',limit=limit)
        self.assertEqual(self.connection.queries,[])

    def test_missing_proof_is_not_zero(self):
        for value in (None,dict(complete=True),dict(complete=False,unique_video_count=0,formats={}),
                      {**proof(), 'source_expires_at':None}):
            with self.assertRaises(RuntimeError):self.run_proof(value)


if __name__=='__main__':unittest.main()
