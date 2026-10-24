"""Schema hardening: error codes, search paths, grants and bootstrap limits. Source only: no database."""
import hashlib
import json
from pathlib import Path
import re
import unittest
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'application'))
SCHEMA_BYTES=(ROOT/'schema/schema.sql').read_bytes()
SCHEMA=SCHEMA_BYTES.decode('utf-8').replace('\r\n','\n')
BOOTSTRAP=(ROOT/'database/bootstrap.sh').read_text(encoding='utf-8').replace('\r\n','\n')
PG_TEMP_LAST="SET search_path TO 'pg_catalog', 'public', 'pg_temp'"

def function_header(name):
    match=re.search(r'CREATE FUNCTION public\.'+name+r'\(.*?\n    AS ',SCHEMA,re.S)
    if match is None:raise AssertionError(name+' missing')
    return match.group(0)

def function_body(name):
    start=SCHEMA.index('CREATE FUNCTION public.'+name+'(')
    opening=re.search(r'\bAS (\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$)',SCHEMA[start:])
    if opening is None:raise AssertionError(name+' body missing')
    body_start=start+opening.end()
    return SCHEMA[start:SCHEMA.index(opening.group(1)+';',body_start)]

class ErrorColumnTests(unittest.TestCase):
    def test_channel_error_columns_accept_only_codes(self):
        for column in ('channel_metadata_error','deep_inventory_error','deep_bypass_reason'):
            self.assertIn(f"CHECK ((({column} IS NULL) OR ({column} ~ '^[a-z0-9_]{{1,64}}$'::text)))",SCHEMA)

    def test_discovery_error_accepts_only_a_class_or_code(self):
        self.assertIn("CHECK (((discovery_error IS NULL) OR (discovery_error ~ '^[A-Za-z0-9_]{1,120}$'::text)))",SCHEMA)
        from db.inventory import inventory_scan_steps as owner
        for valid in ('error','a'*64):self.assertIsNotNone(owner._ERROR.fullmatch(valid))
        for invalid in ('','a'*65,'private message','Error','error\n'):self.assertIsNone(owner._ERROR.fullmatch(invalid))
        source=Path(owner.__file__).read_text(encoding='utf-8')
        self.assertIn('discovery_error=%(error_code)s',source)
        self.assertIn('_fail("inventory_quota_defer_required")',source)
        self.assertNotIn('left(',source)

    def test_other_python_written_error_columns_accept_only_codes(self):
        for constraint,pattern in (('operator_jobs_error_code_check',"last_error_code ~ '^[a-z0-9_]{1,120}$'"),
                                   ('video_collection_state_error_type_check',"last_error_type ~ '^[a-z0-9_]{1,120}$'"),
                                   ('channel_video_discovery_state_error_code_check',"last_error_code ~ '^[A-Za-z0-9_]{1,120}$'"),
                                   ('youtube_api_request_attempts_error_type_check',"error_type ~ '^[A-Za-z][A-Za-z0-9_.:-]{0,119}$'")):
            line=next(l for l in SCHEMA.splitlines() if f'CONSTRAINT {constraint} ' in l)
            self.assertIn(pattern,line,constraint)

class SearchPathTests(unittest.TestCase):
    def test_temp_schema_is_searched_last(self):
        names=re.findall(r'CREATE FUNCTION public\.([a-z_]+)\(',SCHEMA)
        # Schema26 adds only the three native-reviewed inventory-chain guards.
        self.assertEqual(set(names), {
            'bind_channel_user_work_owner', 'enforce_user_work_backup_limits',
            'enforce_video_inventory_membership_scan', 'guard_channel_user_work_identity',
            'guard_hot_operator_scope', 'guard_hot_operator_target_scope',
            'guard_hot_storage_collection_delete', 'guard_manual_video_type_decision',
            'guard_inventory_chain_identity', 'guard_inventory_chain_parent_version',
            'guard_inventory_chain_consistency',
        })
        self.assertEqual(len(names), len(set(names)))
        for name in names:
            header=function_header(name)
            path=re.search(r"SET search_path TO ([^\n]+)",header).group(1)
            self.assertTrue(path.startswith("'pg_catalog'"),name)
            self.assertTrue(path.endswith("'pg_temp'"),name)

    def test_no_definer_function_searches_temp_first(self):
        self.assertNotRegex(SCHEMA,r"SET search_path TO 'pg_temp'")
        self.assertNotIn("search_path=pg_temp, public",SCHEMA)

    def test_intrinsic_contract_pins_the_new_search_path(self):
        from db.readiness.intrinsic_capabilities import READINESS_SQL
        self.assertIn('p.proconfig IS DISTINCT FROM ARRAY[expected.search_path]',READINESS_SQL)
        for name in re.findall(r'CREATE FUNCTION public\.([a-z_]+)\(',SCHEMA):
            path=re.search(r"SET search_path TO ([^\n]+)",function_header(name)).group(1).replace("'",'')
            # PostgreSQL proconfig canonicalizes separators to comma + space.
            path=', '.join(part.strip() for part in path.split(','))
            self.assertIn('search_path='+path,READINESS_SQL)

class GrantTests(unittest.TestCase):
    def test_service_role_gets_no_table_ddl_privileges(self):
        self.assertNotRegex(SCHEMA,r'GRANT ALL ON TABLE [^\n]* TO service_role;')

    def test_public_schema_is_closed_to_other_roles(self):
        self.assertIn('REVOKE USAGE ON SCHEMA public FROM PUBLIC;',SCHEMA)
        self.assertEqual(re.findall(r'GRANT USAGE ON SCHEMA public TO (\w+);',SCHEMA),['postgres','service_role','lic_reader'])

    def test_read_only_view_is_granted_select_only(self):
        self.assertIn('GRANT SELECT ON TABLE public.hot_video_inventory TO service_role;',SCHEMA)

    def test_unused_orphan_reader_is_gone(self):
        self.assertNotIn('get_orphan_visible_videos_without_long_inventory',SCHEMA)

class DigestTests(unittest.TestCase):
    def test_digest_matches_sha_file_and_manifest(self):
        digest=hashlib.sha256(SCHEMA_BYTES).hexdigest()
        self.assertEqual((ROOT/'schema/schema.sql.sha256').read_text(encoding='utf-8').split(),[digest,'schema.sql'])
        self.assertEqual(json.loads((ROOT/'schema/manifest.json').read_text(encoding='utf-8'))['files']['schema.sql'],digest)

class BootstrapTests(unittest.TestCase):
    def test_authenticator_and_direct_driver_bound_statements(self):
        from db.base.postgres import Operation
        self.assertIn("alter role authenticator set statement_timeout = '60s';",BOOTSTRAP)
        self.assertEqual(Operation('schema_probe').statement_timeout_ms,60000)

    def test_only_the_authenticator_login_connects_and_no_role_creates_temporary_objects(self):
        self.assertIn('revoke connect, temporary on database "leti-cook" from public;',BOOTSTRAP)
        self.assertIn('grant connect on database "leti-cook" to authenticator;',BOOTSTRAP)

    def test_database_name_and_schema_mount_agree_across_fresh_bootstraps(self):
        compose=(ROOT/'database/compose.yaml').read_text(encoding='utf-8')
        self.assertIn('POSTGRES_DB: leti-cook',compose)
        self.assertIn('target: /opt/leti-cook/schema.sql',compose)
        self.assertIn('--dbname=leti-cook',BOOTSTRAP)
        self.assertIn('--file=/opt/leti-cook/schema.sql',BOOTSTRAP)
        for path in ('tools/check_schema_postgres.sh','tools/retention_benchmark.sh'):
            source=(ROOT/path).read_text(encoding='utf-8')
            self.assertIn('POSTGRES_DB=leti-cook',source)
            self.assertIn('/opt/leti-cook/schema.sql:ro',source)
            self.assertIn('-U postgres -d leti-cook',source)
        benchmark=(ROOT/'tools/retention_benchmark.sh').read_text(encoding='utf-8')
        self.assertIn('ALTER DATABASE "leti-cook" RENAME TO lic_direct_pg_probe_bench',benchmark)
        self.assertNotIn('ALTER DATABASE leti-cook ',benchmark)
        # Libpq accepts the hyphen verbatim in both DSN forms; no connection.
        from psycopg.conninfo import conninfo_to_dict
        self.assertEqual(conninfo_to_dict('host=db dbname=leti-cook user=authenticator')['dbname'],'leti-cook')
        self.assertEqual(conninfo_to_dict('postgresql://authenticator@db/leti-cook')['dbname'],'leti-cook')

    def test_no_unused_platform_role(self):
        self.assertNotIn('supabase_admin',BOOTSTRAP)
        self.assertNotIn('supabase_admin',SCHEMA)

if __name__=='__main__':unittest.main()
