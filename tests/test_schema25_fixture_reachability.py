"""Offline guards for reachable final-schema fixtures and exact CI cleanup."""
from pathlib import Path
import ast,re,sys,unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'application'),str(ROOT/'tests')]
from schema25_regressions import cases_policy,fixtures

class Schema25ReachabilityTests(unittest.TestCase):
    def test_acl_fault_removes_effective_table_grant_without_new_column_acl(self):
        schema=(ROOT/'schema/schema.sql').read_text()
        self.assertIn('GRANT SELECT ON TABLE public.channel_collection_state TO service_role;',schema)
        tree=ast.parse((ROOT/'tests/schema25_regressions/cases_readiness.py').read_text())
        faults=[n for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='fault']
        pair=[(n.args[1].value,n.args[2].value) for n in faults if isinstance(n.args[1],ast.Constant)]
        self.assertIn(('REVOKE SELECT ON public.channel_collection_state FROM service_role','GRANT SELECT ON public.channel_collection_state TO service_role'),pair)
        self.assertFalse(any('SELECT(is_hidden)' in s for p in pair for s in p))
        text=(ROOT/'tests/schema25_regressions/cases_readiness.py').read_text()
        self.assertIn("(f.catalog(),f.snapshot(),f.sequences())==before",text)

    def test_detached_fixture_matches_retained_lifecycle_and_needs_no_inventory_fk(self):
        source=ast.parse((ROOT/'tests/schema25_regressions/cases_retention.py').read_text())
        fn=next(n for n in source.body if isinstance(n,ast.FunctionDef) and n.name=='detached_reviews')
        sql=next(n.args[0].value for n in ast.walk(fn) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='rows')
        self.assertEqual(sql,"INSERT INTO public.video_type_review_cases(video_id,channel_id,observed_canonical_type,observed_types,observed_channels,state,source_detached_at) VALUES(NULL,NULL,NULL,ARRAY[]::text[],ARRAY[]::text[],'open',now())")
        schema=(ROOT/'schema/schema.sql').read_text()
        check=next(s for s in schema.splitlines() if 'CONSTRAINT video_type_review_source_lifecycle CHECK' in s)
        for term in ('video_id IS NULL','channel_id IS NULL','source_detached_at IS NOT NULL','observed_canonical_type IS NULL','cardinality(observed_types) = 0','cardinality(observed_channels) = 0','blocked_reason IS NULL'):
            self.assertIn(term,check)
        self.assertIn('DEFAULT ARRAY[]::text[] NOT NULL',schema)

    def test_actual_schedule_seed_has_completed_scan_playlist_and_present_member(self):
        class Capture:
            def __init__(self):self.calls=[];self.channel_calls=0
            def channel(self):self.channel_calls+=1
            def rows(self,sql,args=()):self.calls.append((sql,args));return []
        f=Capture();cases_policy._seed_typed_schedule(f)
        self.assertEqual(f.channel_calls,1);self.assertEqual(len(f.calls),5)
        inventory,scan,playlist,member,runtime=f.calls
        self.assertIn("'short',now()-interval '1 day'",inventory[0])
        self.assertIn("'full_reconciliation','completed','complete',now(),now(),1,1",scan[0])
        self.assertIn("true,'ready',now(),'completed'",playlist[0])
        self.assertIn('last_completed_scan_id=excluded.last_completed_scan_id',playlist[0])
        self.assertIn("first_seen_scan_id,last_seen_scan_id,membership_status",member[0])
        self.assertIn("%s,%s,'present'",member[0])
        identity,channel,playlist_id=scan[1]
        self.assertEqual(channel,fixtures.CHANNEL)
        self.assertEqual(playlist_id,'UUSH'+channel[2:])
        self.assertEqual(playlist[1],(channel,playlist_id,identity))
        self.assertEqual(member[1],(channel,playlist_id,identity,identity))
        self.assertIn("video_type='short'",runtime[0])
        policy=(ROOT/'application/db/videos/typed_video_read_policy.py').read_text()
        for term in ('JOIN public.channel_playlist_state','JOIN public.channel_video_inventory_scans',"scan.status='completed' AND scan.coverage='complete'",'s.last_completed_scan_id'):
            self.assertIn(term,policy)
        source=(ROOT/'tests/schema25_regressions/cases_policy.py').read_text()
        self.assertIn("result['inventory_examined_count']==1 and result['affected_count']==0",source)
        self.assertIn("next_due_at IS NOT NULL AND hydration_needed AND paused_reason IS NULL",source)
        for label in ('actual_queue_absent','actual_schedule_no_rewrite','actual_schedule_idempotent'):
            self.assertIn(label,source)

    def test_every_container_uses_verified_shared_volume_cleanup(self):
        for name in ('check_schema_postgres.sh','check_schema_ci_suite.sh','prepare_schema_ci_python.sh'):
            source=(ROOT/'tools'/name).read_text()
            self.assertIn('source "$root/tools/schema_ci_resources.sh"',source)
            self.assertIn('schema_ci_create ',source)
            self.assertNotIn('docker rm ',source)
            self.assertNotIn('docker run ',source)
        source=(ROOT/'tools/schema_ci_resources.sh').read_text()
        for term in ('schema_ci_verify "$index" "$identity"','docker rm -fv "$identity"','--pull=never','org.leti-cook.schema-ci.component','Unexpected named volume','anonymous volume cleanup failed'):
            self.assertIn(term,source)
        self.assertNotIn('volume prune',source)
        self.assertNotIn('|| true',source)

    def test_runner_bounds_and_final_runtime_pins_remain_explicit(self):
        import hashlib
        # Approved same-transaction chain proof; no native acceptance claimed by this source pin.
        self.assertEqual(hashlib.sha256((ROOT/'application/db/readiness/collection_preflight.py').read_bytes()).hexdigest(),'6ba7fb860be3c1d1ef833f3f3ffae09efce2cdc3aa170667c168f99aaaa16e1a')
        runner=(ROOT/'tools/check_schema_ci_suite.sh').read_text()
        for term in ('[[ $# == 1 ]]','--kill-after=10s 450s','--memory 768m --memory-swap 768m --cpus 1.5','--network "container:$LIC_SCHEMA_CI_PG_ID"'):
            self.assertIn(term,runner)
        fixture=(ROOT/'tests/schema25_regressions/fixtures.py').read_text()
        self.assertIn('max_size=1',fixture);self.assertIn('time.monotonic()+420',fixture)

if __name__=='__main__':unittest.main()
