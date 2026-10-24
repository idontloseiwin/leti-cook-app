"""Current-source cleanup lookahead, ownership and typed estimate clocks."""
from pathlib import Path
import sys
import json
import unittest
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.retention.current_source_retention import _TARGETS,_page
from db.retention import channel_estimate_retention as retention
from db.channel import channel_inventory_estimate_normalization as normalization
from db.base.postgres import DatabaseServerError,PostgresConfig,PostgresDatabase
from test_direct_pg_transactions import Connection,Pool,Result,ServerFailure,DSN
AT='2026-10-05T12:00:00+00:00';CUTOFF='2026-09-07T12:00:00+00:00';OLD='2026-09-01T12:00:00+00:00';FRESH='2026-10-04T12:00:00+00:00'

def one(**values):
    if isinstance(values.get('value'),dict):
        row=values['value'];values['value']=json.dumps(row)
        if 'initial_inventory_estimate' in row:
            values['estimate_json']=json.dumps(row['initial_inventory_estimate']) if row['initial_inventory_estimate'] is not None else None
    return Result(rows=(values,))
def count(value):return Result(has_columns=False,rowcount=value)
def item(identity='aaaaaaaaaaa',**changes):return dict(dict(id=identity,source_key=OLD,invalid_clock=False,cursor='native:'+identity),**changes)

class CurrentCleanupTests(unittest.TestCase):
    def setUp(self):
        self.trace=[];self.connection=Connection(self.trace);self.pool=Pool(self.connection,self.trace);self.gates=[]
        self.database=PostgresDatabase(PostgresConfig(DSN),admission=self.gates.append,pool_factory=lambda config:self.pool).open()
        self.addCleanup(self.database.close)
    def queue(self,*results):self.connection.results.extend(results)
    def header(self,target='video_counters',after_id=None,limit=100):self.queue(one(target=target,after_id=after_id,limit=limit,at=AT,cutoff=CUTOFF))
    def run_page(self,target='video_counters',**kwargs):return self.database.cleanup_hot_current_source(target=target,**kwargs)
    def test_empty_page_has_operation_start_clock_and_one_commit(self):
        self.header();self.queue(Result(rows=()))
        result=self.run_page()
        self.assertEqual((result['as_of'],result['cutoff'],result['scanned'],result['complete'],result['next_cursor']),(AT,CUTOFF,0,True,None))
        self.assertIn('statement_timestamp()',self.connection.queries[0][0]);self.assertEqual(self.connection.commits,1)
        self.assertEqual(set(self.gates),{'cleanup_hot_current_source'})
    def test_limit_then_target_precede_cursor_body_validation(self):
        for target,limit,code in (('bad',None,'hot_current_cleanup_invalid_limit'),('bad',100,'hot_current_cleanup_invalid_target')):
            self.header(target,after_id='bad-json',limit=limit)
            with self.assertRaises(DatabaseServerError) as error:self.run_page(target,after_id='bad-json',limit=limit)
            self.assertEqual(error.exception.message,code)
        self.assertEqual(len(self.connection.queries),2)
    def test_cursor_errors_are_domain_errors_and_cancel_is_not(self):
        for sqlstate in ('22007','57014'):
            self.header(after_id='{}');self.queue(Result(execute_error=ServerFailure(sqlstate)))
            with self.assertRaises(DatabaseServerError) as error:self.run_page(after_id='{}')
            self.assertEqual(error.exception.code,'57014' if sqlstate=='57014' else 'P0001')
    def test_lookahead_excludes_extra_candidate_and_retains_nonempty_cursor(self):
        first=item();self.header(limit=1);self.queue(Result(rows=(first,item('bbbbbbbbbbb'))),Result(rows=()))
        result=self.run_page(limit=1)
        self.assertEqual((result['scanned'],result['complete'],result['next_cursor']),(1,False,first['cursor']))
        self.assertEqual(self.connection.queries[1][1]['size'],2)
        self.header(limit=1);self.queue(Result(rows=(first,)),Result(rows=()))
        result=self.run_page(limit=1)
        self.assertTrue(result['complete']);self.assertEqual(result['next_cursor'],first['cursor'])
    def test_noop_proof_precedes_owner_locks(self):
        self.header();self.queue(Result(rows=(item(),)),one(value={'views':0}),one(expired=False))
        self.assertEqual(self.run_page()['deferred'],0)
        self.assertFalse(any('FOR UPDATE' in query for query,params in self.connection.queries))
    def test_busy_queue_defers_without_touching_source_or_user_work(self):
        self.header();self.queue(Result(rows=(item(),)),one(value={'views':1}),one(expired=True),Result(rows=()),one(present=True),one(value=OLD))
        result=self.run_page();self.assertEqual((result['deferred'],result['scrubbed'],result['oldest_remaining_expired_source_at']),(1,0,OLD))
        self.assertFalse(any(query.startswith('UPDATE') for query,params in self.connection.queries))
    def test_post_lock_refresh_wins(self):
        self.header();self.queue(Result(rows=(item(),)),one(value={'views':1}),one(expired=True),one(has_token=False,expires_at=None),one(value={'views':2}),one(expired=False))
        self.assertEqual(self.run_page()['scrubbed'],0)
        self.assertFalse(any(query.startswith('UPDATE') for query,params in self.connection.queries))
    def test_scrub_payload_only_no_clocks_or_inventory_delete(self):
        self.header();self.queue(Result(rows=(item(),)),one(value={'views':1}),one(expired=True),one(has_token=False,expires_at=None),one(value={'views':1}),one(expired=True),count(1))
        self.assertEqual(self.run_page()['scrubbed'],1)
        query=self.connection.queries[-1][0]
        self.assertIn('views=NULL,like_count=NULL,comment_count=NULL',query)
        for field in ('checked_at','channel_id','published_at','lease_token'):self.assertNotIn(field,query)
        self.assertEqual(self.connection.commits,1)
    def test_live_lease_comparison_is_after_skip_locked_fetch(self):
        self.header();self.queue(Result(rows=(item(invalid_clock=True,source_key='infinity'),)),one(value={'views':1}),one(expired=True),one(has_token=True,expires_at=AT),one(active=True))
        result=self.run_page();self.assertEqual((result['deferred'],result['invalid_clock_remaining']),(1,1))
        locking=[i for i,(query,params) in enumerate(self.connection.queries)
                 if 'AS has_token' in query and 'FOR UPDATE SKIP LOCKED' in query][0]
        self.assertIn('SKIP LOCKED',self.connection.queries[locking][0]);self.assertNotIn('clock_timestamp',self.connection.queries[locking][0])
        self.assertIn('clock_timestamp()',self.connection.queries[locking+1][0])
    def test_estimate_none_needs_no_lock_or_native_cast(self):
        self.header('channel_inventory_estimate');self.queue(Result(rows=(item('channel'),)),one(value={'initial_inventory_estimate':None}))
        result=self.run_page('channel_inventory_estimate');self.assertEqual(result['scrubbed'],0)
        self.assertEqual(len(self.connection.queries),3)
    def test_malformed_estimate_is_rechecked_and_atomically_cleared(self):
        self.header('channel_inventory_estimate');self.queue(Result(rows=(item('channel'),)),one(value={'initial_inventory_estimate':[]}))
        for locked in (False,True):
            if locked:self.queue(one(value={'initial_inventory_estimate':[]}))
            self.queue(one(at=None),one(keep=False,count=None,status=None,changed=True))
        self.queue(count(1))
        result=self.run_page('channel_inventory_estimate');self.assertEqual(result['scrubbed'],1)
        self.assertTrue(self.connection.queries[-1][0].startswith('UPDATE public.channels SET initial_inventory_estimate=NULL'))
    def test_mixed_estimate_preserves_only_fresh_types_and_original_clock(self):
        estimate=dict(scope='all',counts={'long':3,'short':4,'stream':None},statuses={'long':'ok','short':'ok','stream':'error'},checked_at_by_type={'long':FRESH,'short':OLD,'stream':None})
        self.header('channel_inventory_estimate');self.queue(Result(rows=(item('channel'),)),one(value={'initial_inventory_estimate':estimate}))
        for locked in (False,True):
            if locked:self.queue(one(value={'initial_inventory_estimate':estimate}))
            self.queue(one(at=FRESH),one(keep=True,count=3,status='ok',changed=True),one(at=OLD),one(keep=False,count=4,status='ok',changed=True),one(at=None),one(keep=False,count=None,status='error',changed=False))
        proposed=dict(scope='all',video_types=['long','short','stream'],
            counts=dict(long=3,short=None,stream=None),statuses=dict(long='ok',short='error',stream='error'),
            checked_at_by_type=dict(long=FRESH,short=None,stream=None))
        raw=json.dumps(proposed);normalized=json.dumps(dict(proposed,checked_at=None))
        # Actual76 clears first, then re-enters the native-text normalizer with OLD SQL NULL.
        self.queue(count(1),one(estimate_text=raw),one(same=False),one(invalid=False),
            one(invalid=False,kinds=['long','short','stream']),
            one(kinds=['long','short','stream'],counts='{}',statuses='{}',clocks='{}',common_input=None))
        counts,statuses,clocks={},{},{}
        def state():return one(counts=json.dumps(counts),statuses=json.dumps(statuses),clocks=json.dumps(clocks))
        for kind in ('long','short','stream'):
            counts[kind]=None;statuses[kind]='unknown';clocks[kind]=None;self.queue(state())
        for kind in ('long','short','stream'):
            at=proposed['checked_at_by_type'][kind]
            self.queue(one(invalid=False,overflow=False,count=json.dumps(proposed['counts'][kind]),status=proposed['statuses'][kind]),one(clock=at))
            if at is not None:self.queue(one(invalid=False))
            self.queue(one(raw=None,error=None,clock=None),one(keep=False,conflict=None))
            counts[kind]=proposed['counts'][kind];statuses[kind]=proposed['statuses'][kind];clocks[kind]=at
            self.queue(state())
        self.queue(one(raw=FRESH,error=None,clock=FRESH),one(unknown=False,common=FRESH),
            one(raw=None,error=None,clock=None),one(raw=None,error=None,clock=None),one(value=normalized),count(1))
        self.assertEqual(self.run_page('channel_inventory_estimate')['scrubbed'],1)
        writes=[(query,params) for query,params in self.connection.queries if query.startswith('UPDATE')]
        self.assertEqual(writes,[(retention._CLEAR,('channel',)),(retention._WRITE,(normalized,'channel'))])
        typed=next(params[0].obj for query,params in self.connection.queries if retention._TYPED in query)
        self.assertEqual(typed,proposed);self.assertNotIn('checked_at',typed)
        self.assertTrue(any(normalization._EQUAL in query and params==(raw,None) for query,params in self.connection.queries))
        saved=json.loads(writes[1][1][0])
        self.assertEqual(saved['counts'],dict(long=3,short=None,stream=None))
        self.assertEqual(saved['checked_at_by_type'],dict(long=FRESH,short=None,stream=None))
        self.assertIsNone(saved['checked_at']);self.assertFalse(self.connection.results)
        # This fake counts six native-cast savepoint releases as commits too.
        self.assertEqual((self.connection.commits,self.connection.rollbacks,self.pool.borrowed),(7,0,1))
    def test_invalid_estimate_clock_rollback_keeps_page_usable(self):
        self.header('channel_inventory_estimate');self.queue(Result(rows=(item('channel'),)),one(value={'initial_inventory_estimate':{'checked_at':'bad'}}))
        self.queue(Result(execute_error=ServerFailure('22007')),one(keep=False,count=None,status=None,changed=True),Result(rows=()))
        result=self.run_page('channel_inventory_estimate');self.assertEqual(result['deferred'],1);self.assertEqual(result['invalid_clock_remaining'],0)
        self.assertEqual(self.connection.commits,1)
    def test_late_native_failure_rolls_back_page_without_retry(self):
        self.header('channel_country');self.queue(Result(rows=(item('channel'),)),one(value={'country_code':'US'}),one(expired=True),one(value={'country_code':'US'}),one(expired=True),Result(execute_error=ServerFailure('23514')))
        with self.assertRaises(DatabaseServerError):self.run_page('channel_country')
        self.assertEqual(self.connection.commits,0);self.assertEqual(self.pool.borrowed,1)
    def test_native_estimate_numeric_text_survives_both_candidate_and_locked_checks(self):
        for numeric in ('0.'+'0'*399+'1','1'+'0'*5000):
            raw='{"scope":"long","counts":{"long":'+numeric+'},"statuses":{"long":"playlist_not_found"},"checked_at":"'+FRESH+'"}'
            self.connection.queries.clear()
            self.header('channel_inventory_estimate')
            self.queue(Result(rows=(item('channel'),)))
            for locked in (False,True):
                self.queue(one(value='{"initial_inventory_estimate":'+raw+'}',estimate_json=raw),one(at=FRESH),one(keep=False,count=None,status='playlist_not_found',changed=True))
            self.queue(count(1))
            self.assertEqual(self.run_page('channel_inventory_estimate')['scrubbed'],1)
            native=[params[0] for query,params in self.connection.queries if 'WITH input AS(SELECT %s::jsonb AS estimate' in query]
            self.assertEqual(native,[raw]*4)
    def test_json_null_estimate_is_noop_like_sql_null(self):
        self.header('channel_inventory_estimate');self.queue(Result(rows=(item('channel'),)),one(value='{"initial_inventory_estimate":null}',estimate_json='null'))
        self.assertEqual(self.run_page('channel_inventory_estimate')['scrubbed'],0)
        self.assertEqual(len(self.connection.queries),3)
    def test_rejected_estimate_numbers_and_status_are_not_decoded(self):
        from db.retention import current_source_retention
        import inspect
        sql=inspect.getsource(current_source_retention._estimate)
        self.assertIn("CASE WHEN keep THEN estimate->'counts'->kind END AS count",sql)
        self.assertIn("CASE WHEN keep THEN estimate->'statuses'->kind END AS status",sql)

    def test_profile_predicate_preserves_unrelated_native_json_numbers(self):
        raw='{"channel_metadata_checked_at":null,"initial_inventory_estimate":{"counts":{"long":1e1000}}}'
        self.header('channel_profile');self.queue(Result(rows=(item('channel'),)),one(value=raw,estimate_json='{"counts":{"long":1e1000}}'),one(expired=False))
        self.assertEqual(self.run_page('channel_profile')['scrubbed'],0)
        predicate=self.connection.queries[-1]
        self.assertEqual(predicate[1]['row'],raw)
        self.assertIn('jsonb_populate_record',predicate[0])

    def test_page_is_native_ordered_bounded_with_pk_estimate_exception(self):
        for name,spec in _TARGETS.items():
            query=_page(spec,name=='channel_inventory_estimate')
            self.assertIn('ORDER BY page.source_key,page.id',query)
            self.assertIn('LIMIT %(size)s',query)
            self.assertEqual('UNION ALL' in query,name!='channel_inventory_estimate')

if __name__=='__main__':unittest.main()
