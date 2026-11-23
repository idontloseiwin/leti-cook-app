"""Disposable PG17 field settings: physical costs, protected masks and fresh schema."""
from pathlib import Path
import sys,json,unittest,os,uuid
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'application'),str(Path(__file__).resolve().parent)]
import research_query_pg as native
FIXTURE,ROOT=native.FIXTURE,native.ROOT
from research_field_policy import defaults
from db.user_work.research_preferences import ResearchPreferencesRepository
from db.retention.current_source_retention import _page,_TARGETS

class FieldsPostgres(unittest.TestCase):
    setUpClass=classmethod(native.NativeQueryTests.setUpClass.__func__)
    tearDownClass=classmethod(native.NativeQueryTests.tearDownClass.__func__)
    refresh=classmethod(native.NativeQueryTests.refresh.__func__)
    def test_01_physical_description_cost_and_indexed_cleanup(self):
        c=self.admin; channel=FIXTURE['channels'][0]['id']
        c.execute("INSERT INTO public.videos(video_id,channel_id) SELECT 'z'||lpad(i::text,10,'0'),%s FROM generate_series(1,10000) i",(channel,))
        before=c.execute('SELECT pg_current_wal_insert_lsn()').fetchone()[0]
        c.execute("""INSERT INTO public.video_optional_metadata(video_id,description,description_folded,description_checked_at,description_policy_revision,paid_promotion,paid_promotion_checked_at,paid_promotion_policy_revision)
        SELECT 'z'||lpad(i::text,10,'0'),txt,txt,clock_timestamp()-interval '700 hours',1,false,clock_timestamp(),1
        FROM generate_series(1,10000) i CROSS JOIN LATERAL
        (SELECT string_agg(md5(i::text||':'||j::text),'') txt FROM generate_series(1,64) j) d""")
        c.execute('ANALYZE public.video_optional_metadata')
        size=c.execute("SELECT pg_table_size('public.video_optional_metadata'),pg_indexes_size('public.video_optional_metadata'),pg_wal_lsn_diff(pg_current_wal_insert_lsn(),%s)::bigint",(before,)).fetchone()
        params=dict(target='video_description',after_clock='-infinity',after_id='',size=100,
                    cutoff='2026-01-01T00:00:00Z',at='2027-01-01T00:00:00Z')
        # Use real current clocks, not a date that would accidentally match all rows.
        params.update(cutoff=c.execute("SELECT clock_timestamp()-interval '696 hours'").fetchone()[0],at=c.execute('SELECT clock_timestamp()').fetchone()[0])
        plan=c.execute('EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) '+_page(_TARGETS['video_description'],False),params).fetchone()[0][0]
        self.assertIn('video_optional_description_expiry',json.dumps(plan))
        measurements=dict(rows=10000,description_bytes_per_row=2048,folded_bytes_per_row=2048,
            table_bytes=size[0],index_bytes=size[1],wal_bytes=size[2],cleanup_100_execution_ms=plan['Execution Time'])
        print('FIELD_STORAGE_MEASUREMENT '+json.dumps(measurements),flush=True)
        self.assertLess(plan['Execution Time'],250)
        # Masking is enforced even when using the service role/readable view directly.
        self.assertEqual(c.execute("SELECT count(*) FROM readable.video_optional_metadata").fetchone()[0],0)
        c.execute("DELETE FROM public.videos WHERE video_id LIKE 'z%%'")

    def test_02_fresh_schema_seeds_and_profile_guard(self):
        import psycopg,importlib.util
        sys.path.insert(0,str(ROOT))
        self.admin.execute('CREATE DATABASE "lic_fields_fresh"')
        try:
            with psycopg.connect(host='127.0.0.1',port=int(os.environ['LIC_QUERY_TEST_PORT']),dbname='lic_fields_fresh',user='postgres',password=os.environ['LIC_QUERY_TEST_PASSWORD'],autocommit=True) as fresh:
                fresh.execute('CREATE SCHEMA extensions; CREATE EXTENSION pgcrypto WITH SCHEMA extensions',prepare=False)
                fresh.execute((ROOT/'schema/schema.sql').read_text(encoding='utf-8'),prepare=False)
                target=json.loads((ROOT/'schema/manifest.json').read_text(encoding='utf-8'))['schema_version']
                self.assertEqual(fresh.execute('SELECT initial_version,current_version FROM operator_private.schema_state').fetchone(),(target,target))
                self.assertEqual(fresh.execute("SELECT document->>'lifecycle_profile',revision FROM public.research_preferences").fetchone(),('public-696h-v1',1))
        finally:self.admin.execute('DROP DATABASE "lic_fields_fresh"')
        spec=importlib.util.spec_from_file_location('fields_schema_upgrade',ROOT/'ops/schema_upgrade.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        self.admin.execute(module._session(lock=False),prepare=False)
        try:
            self.admin.execute("UPDATE public.research_preferences SET document=jsonb_set(document,'{lifecycle_profile}','\"future-profile\"')")
            with self.assertRaisesRegex(psycopg.Error,'schema_lifecycle_profile_unsupported'):
                self.admin.execute(module._session(lock=False),prepare=False)
        finally:self.admin.execute("UPDATE public.research_preferences SET document=jsonb_set(document,'{lifecycle_profile}','\"public-696h-v1\"')")

if __name__=='__main__':unittest.main(verbosity=2)
