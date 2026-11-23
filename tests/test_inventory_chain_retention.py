"""Bounded chain expiry and physical pins in existing retention owners."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'application'),str(ROOT/'tests')]
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
from db.base.postgres import DatabaseError,DatabaseServerError,_reviewed_statement
from db.retention import inventory_chain_retention as chains
from db.retention import operational_retention as operational
from db.retention import inventory_copies_retention as inventory
from test_inventory_chain_proof import Database,copies,ack,checkpoint
from test_operator_job_inventory_chains import JOB,CHANNEL,SCAN,TRANSITION,PLAYLIST_LEASE

P=dict(id=JOB,cutoff='2026-09-11T00:00:00Z')
def job(**changes):
    value=dict(id=JOB,channel_id=CHANNEL,summary='{"inventory_chain_version":1,"full_inventory_sources":{}}',
        inventory_scan_id=None,initial_scope_version=1,source_at='2026-09-01T00:00:00Z',
        has_lease=False,lease_expires_at=None,last_error_code=None)
    value.update(changes);return value

class CopyTests(unittest.TestCase):
    def test_scalar_path_never_reads_child_table(self):
        tx=Database()
        self.assertEqual(chains.inspect_copies(tx,job(summary='{}'),P),
            dict(linked=False,clear_ack_types=[],busy=False))
        self.assertFalse(tx.calls)

    def test_child_recent_updated_at_does_not_renew_expired_scan(self):
        rows=copies(source=True)
        value=json.loads(rows[0]['value']);value['updated_at']='2099-01-01T00:00:00Z'
        rows[0]['value']=json.dumps(value)
        tx=Database(rows,{'hot':False})
        result=chains.inspect_copies(tx,job(),P)
        self.assertTrue(result['linked'])
        self.assertNotIn('updated_at',chains._SCAN_HOT)
        self.assertIn('clock_timestamp()',chains._SCAN_HOT)

    def test_historical_independent_ack_is_cleared_without_current_source_scrub(self):
        rows=copies(ack())
        tx=Database(rows,{'hot':False},{'hot':True})
        facts=chains.inspect_copies(tx,job(),P)
        self.assertFalse(facts['linked'])
        self.assertEqual(facts['clear_ack_types'],['long'])

    def test_missing_receipt_invalidates_owning_current_scan(self):
        tx=Database(copies(ack(),source=True),{'hot':True},None)
        facts=chains.inspect_copies(tx,job(),P)
        self.assertTrue(facts['linked'])
        self.assertFalse(facts['clear_ack_types'])

    def test_unknown_ack_needs_no_fabricated_receipt(self):
        tx=Database(copies(ack('unknown'),source=True),{'hot':True})
        facts=chains.inspect_copies(tx,job(),P)
        self.assertFalse(facts['linked'])
        self.assertFalse(any(sql==chains._RECEIPT_HOT for _,sql,_ in tx.calls))

    def test_expired_parent_with_live_original_playlist_claim_defers(self):
        tx=Database(copies(cp=checkpoint('bound'),source=True),{'hot':False},
            {'busy':True},None,None)
        facts=chains.inspect_copies(tx,job(),P)
        self.assertTrue(facts['linked'])
        self.assertTrue(facts['busy'])
        self.assertIn('lease_token=%(token)s::uuid',chains._BOUND_BUSY)
        self.assertIn('active_scan_id=%(scan)s::uuid',chains._BOUND_BUSY)

    def test_parent_mutation_takes_children_playlist_scans_receipts_then_rereads(self):
        tx=Database(copies(),[],[],[],copies())
        chains.inspect_copies(tx,job(),P,lock=True)
        self.assertEqual([sql for _,sql,_ in tx.calls],
            [chains._READ+' FOR UPDATE NOWAIT',chains._PLAYLIST_LOCKS,
             chains._SCAN_LOCKS,chains._RECEIPT_LOCKS,chains._READ])
        self.assertTrue(all('NOWAIT' in sql for _,sql,_ in tx.calls[:-1]))

    def test_partial_child_set_fails_closed(self):
        tx=Database(copies()[:2])
        with self.assertRaisesRegex(DatabaseError,'chain_state_invalid'):
            chains.inspect_copies(tx,job(),P)

    def test_pins_are_read_only_and_do_not_acquire_parent_after_scan_lock(self):
        tx=Database({'pinned':True},{'pinned':True})
        self.assertTrue(chains.pinned_scan(tx,SCAN))
        self.assertTrue(chains.pinned_receipt(tx,None,TRANSITION))
        self.assertTrue(all('FOR UPDATE' not in sql for _,sql,_ in tx.calls))
        self.assertTrue(all('operator_jobs ' not in sql for _,sql,_ in tx.calls))

class OwnerTests(unittest.TestCase):
    def facts(self,*,busy=False):
        return dict(linked=True,clear_ack_types=[],busy=busy)
    def transaction(self,*tail):
        return Database(job(),{'expired':False},{'present':False},{'hot':False},
            [],job(),{'stale':True},{'expired':False},{'present':False},{'busy':False},*tail)
    def test_atomic_parent_scrub_clears_all_children_first_and_preserves_marker(self):
        tx=self.transaction(3,1)
        with patch.object(chains,'inspect_copies',return_value=self.facts()) as inspect:
            result=operational._jobs(tx,'operator_jobs',P,{})
        self.assertEqual(result,(0,4,0))
        self.assertEqual([sql for kind,sql,_ in tx.calls if kind=='execute'],
                         [chains._SCRUB,operational._SCRUB_JOB])
        self.assertEqual(inspect.call_args.kwargs,{'lock':True})
        self.assertIn('inventory_chain_version',operational._SCRUB_JOB)
        self.assertIn("summary_json->'inventory_chain_version'='1'::jsonb",operational._SCRUB_JOB)
        self.assertNotIn('requested_at=',operational._SCRUB_JOB)

    def test_live_bound_lease_prevents_both_child_and_parent_mutation(self):
        tx=self.transaction()
        step={}
        with patch.object(chains,'inspect_copies',return_value=self.facts(busy=True)):
            self.assertEqual(operational._jobs(tx,'operator_jobs',P,step),(0,0,1))
        self.assertEqual(step['reason'],'lease')
        self.assertFalse(any(kind=='execute' for kind,_,_ in tx.calls))

    def test_parent_write_failure_propagates_without_ack_or_second_pass(self):
        error=DatabaseServerError('operator_job_source_scope_expired',code='P0001')
        tx=self.transaction(3,error)
        with patch.object(chains,'inspect_copies',return_value=self.facts()):
            with self.assertRaises(DatabaseError):operational._jobs(tx,'operator_jobs',P,{})
        self.assertEqual(sum(sql==chains._SCRUB for _,sql,_ in tx.calls),1)
        self.assertEqual(tx.calls[-1][1],operational._SCRUB_JOB)

    def test_pinned_scan_prevents_fail_scan_payload_scrub(self):
        tx=Database({'hot':False,'channel_id':CHANNEL,'video_type':'long','value_clock':'old'},None,
                    {'hot':False,'value_clock':'old'},{'pinned':True})
        self.assertEqual(inventory._scans(tx,dict(a=SCAN),{}),(0,0,1))
        self.assertEqual(tx.calls[-1][1],chains.PIN_SCAN)
        self.assertFalse(any(kind=='execute' for kind,_,_ in tx.calls))

    def test_pinned_quota_receipt_prevents_delete_without_scan_fk(self):
        receipt=dict(scan_id=None,expired_marker=False,has_hash=False,created_hot=False,source_hot=False,value_clock='old')
        tx=Database(receipt,{'acquired':True},receipt,{'pinned':True})
        self.assertEqual(inventory._receipts(tx,dict(a='quota_defer',b=TRANSITION),{}),(0,0,1))
        self.assertFalse(any(kind=='execute' for kind,_,_ in tx.calls))

    def test_pinned_playlist_indirect_scan_is_not_mutated(self):
        tx=Database(dict(value='{}',active_scan_id=SCAN,last_completed_scan_id=None),[],
            dict(noop=False,value_clock='old',active_valid=False),{'busy':False},{'pinned':True})
        self.assertEqual(inventory._playlist(tx,dict(a=CHANNEL,b='long'),{}),(0,0,1))
        self.assertFalse(any(kind=='execute' for kind,_,_ in tx.calls))

    def test_fixed_sql_parameters_and_native_clocks(self):
        params=dict(P,channel_id=CHANNEL,scan=SCAN,chain_scan=SCAN,chain_transition=TRANSITION,
            kind='long',channel=CHANNEL,token=PLAYLIST_LEASE,clear_ack_types=['long'])
        cases=((chains._READ,params,3),(chains._PLAYLIST_LOCKS,params,3),
            (chains._SCAN_LOCKS,([SCAN],),6),(chains._RECEIPT_LOCKS,('[]',),12),
            (chains._SCAN_HOT,params,1),(chains._RECEIPT_HOT,('quota_defer',TRANSITION),1),
            (chains._BOUND_BUSY,params,1),(chains.PIN_SCAN,params,1),(chains.PIN_RECEIPT,params,1),
            (chains._CLEAR_ACK,params,0),(chains._SCRUB,params,0))
        for sql,values,limit in cases:
            with self.subTest(sql=sql[:45]):
                PostgresQuery(Transformer()).convert(sql,values)
                _reviewed_statement(sql,read=bool(limit),max_rows=limit)

if __name__=='__main__':unittest.main()
