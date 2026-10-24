"""Offline guards for the exact U2 snapshot and real disposable harness wiring."""
from __future__ import annotations
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import schema_catalog_snapshot as catalog
from schema_upgrades import run as native


def empty():
    return {name: [] for name in (*catalog.QUERIES, "seeds")}


class CatalogSnapshotTests(unittest.TestCase):
    def test_row_order_and_only_function_line_endings_are_normalized(self):
        first = empty()
        first["functions"] = [{"name": "b", "definition": "one\r\ntwo\r"}, {"name": "a", "definition": "body"}]
        second = copy.deepcopy(first)
        second["functions"].reverse()
        second["functions"][1]["definition"] = "one\ntwo\n"
        self.assertEqual(catalog.serialize(first), catalog.serialize(second))
        second["functions"][1]["definition"] = "one  \ntwo\n"
        self.assertEqual(catalog.differences(first, second), ["functions"])
        first["views"] = [{"name": "v", "definition": "a\r\nb"}]
        second = copy.deepcopy(first)
        second["views"][0]["definition"] = "a\nb"
        self.assertEqual(catalog.differences(first, second), ["views"])

    def test_acl_raw_default_role_options_and_seed_changes_remain_visible(self):
        first = empty()
        for section, field in (("relations", "acl_default"), ("memberships", "inherit_option"),
                               ("policies", "using_expression"), ("default_privileges", "acl"),
                               ("functions", "config"), ("roles", "bypass_rls")):
            first[section] = [{"name": "x", field: False}]
            changed = copy.deepcopy(first)
            changed[section][0][field] = True
            self.assertEqual(catalog.differences(first, changed), [section])
        first["seeds"] = [{"relation": "public.settings", "values": {"value": 1}, "clock_valid": True}]
        changed = copy.deepcopy(first)
        changed["seeds"][0]["values"]["value"] = 2
        self.assertEqual(catalog.differences(first, changed), ["seeds"])

    def test_seed_clock_exceptions_are_explicit_and_bad_clocks_refused(self):
        self.assertEqual(catalog.SEED_CLOCKS, {
            "public.channel_labels": ("created_at",),
            "public.typed_video_collection_runtime": ("created_at", "updated_at"),
            "public.video_collection_cadence": ("created_at", "updated_at"),
            "public.video_collection_control": ("invalidated_at", "updated_at"),
            "public.channel_discovery_queue_control": ("updated_at",),
        })
        value = empty()
        value["seeds"] = [{"clock_valid": False}]
        with self.assertRaisesRegex(ValueError, "snapshot_seed_clock_invalid"):
            catalog.serialize(value)
        sql = catalog.snapshot_sql()
        self.assertEqual(catalog.BOOTSTRAP_RECONCILIATION_TABLES,
                         {"public.video_collection_control", "public.channel_discovery_queue_control"})
        self.assertIn("t.last_reconciled_at=t.updated_at", sql)
        self.assertIn("t.reconciliation_required IS FALSE", sql)
        self.assertIn("isfinite(t.last_reconciled_at)", sql)
        self.assertIn("initial-bootstrap-clock", sql)
        # It remains a compared field; it is not in the timestamp exclusions.
        self.assertNotIn("ARRAY['invalidated_at','updated_at','last_reconciled_at']", sql)
        self.assertIn("isfinite", sql)

    def test_seed_diagnostic_reports_only_changed_field_names(self):
        first = empty()
        first["seeds"] = [{"relation": "public.settings", "values": {"name": "synthetic", "value": 1}, "clock_valid": True}]
        second = copy.deepcopy(first)
        second["seeds"][0]["values"]["value"] = 2
        self.assertEqual(catalog.seed_difference_fields(first, second), ["public.settings.value"])

    def test_names_and_signatures_not_oid_values_are_comparison_identity(self):
        sql = catalog.snapshot_sql()
        self.assertIn("pg_get_function_identity_arguments", sql)
        self.assertIn("aclexplode", sql)
        self.assertIn("m.inherit_option", sql)
        self.assertIn("m.set_option", sql)
        self.assertIn("d.datlocale", sql)
        self.assertNotIn("d.daticulocale", sql)
        self.assertIn("NOT a.attisdropped", sql)
        self.assertIn("jsonb_agg(a.attname ORDER BY x.position)", sql)
        self.assertIn("CASE WHEN NOT t.tgisinternal THEN t.tgname END", sql)
        self.assertNotIn("ORDER BY oid", sql)
        self.assertNotIn("rolpassword", sql)
        self.assertNotIn("relfilenode", sql)

    def test_unsupported_objects_fail_closed_instead_of_looking_equal(self):
        value = empty()
        value["unsupported_objects"] = [{"kind": "aggregate", "name": "public.unreviewed(integer)"}]
        with self.assertRaisesRegex(ValueError, "snapshot_unsupported_objects"):
            catalog.serialize(value)
        sql = catalog.snapshot_sql()
        for name in ("pg_event_trigger", "pg_foreign_server", "pg_publication", "pg_subscription", "pg_opclass"):
            self.assertIn(name, sql)

    def test_untrusted_seed_identifier_is_never_interpolated(self):
        with self.assertRaises(ValueError):
            catalog.snapshot_sql({"public.items; DROP DATABASE x": ()})
        with self.assertRaises(ValueError):
            catalog.snapshot_sql({"public.items": ("x); COMMIT",)})

    def test_synthetic_target_is_independent_and_real_runner_is_called(self):
        path = ROOT / "tests/schema_upgrades"
        baseline = (path / "synthetic_a.sql").read_bytes()
        target = (path / "synthetic_b.sql").read_bytes()
        step = (path / "0026-synthetic.sql").read_bytes()
        self.assertNotEqual(target, baseline + step)
        self.assertIn(b"CREATE TABLE public.items", target)
        self.assertNotIn(b"ALTER TABLE public.items ADD COLUMN", target)
        plan = native.synthetic_plan(step)
        self.assertEqual(plan.steps[0].sql, step)
        self.assertEqual(plan.steps[0].sha256, hashlib.sha256(step).hexdigest())
        self.assertEqual(plan.schema_version, 26)
        source = (path / "run.py").read_text()
        self.assertIn("engine.run_schema_upgrade", source)
        self.assertIn("engine.reconcile_schema_upgrade", source)
        self.assertIn("engine.PSQL_ARGUMENTS", source)
        self.assertNotIn("psycopg", source)
        self.assertNotIn("pg_connect", source)

    def test_native_owner_check_precedes_every_database_probe(self):
        calls = []
        with self.assertRaisesRegex(RuntimeError, "upgrade_fixture_ownership_required"):
            native.owned_identity({}, lambda *args, **kwargs: calls.append(args))
        self.assertFalse(calls)
        env = {"LIC_SCHEMA_CI_PG_ID": "c" * 64, "LIC_SCHEMA_CI_PG_TOKEN": "a" * 32,
               "LIC_SCHEMA_CI_PG_IMAGE": "sha256:" + "1" * 64,
               "LIC_SCHEMA_CI_PG_COMPONENT": "base", "LIC_SCHEMA_CI_PG_NAME": "leti-cook-schema-base-" + "a" * 32}
        def run(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, "wrong container", "")
        with self.assertRaisesRegex(RuntimeError, "upgrade_fixture_ownership_mismatch"):
            native.owned_identity(env, run)
        self.assertEqual(calls[0][:3], ["docker", "container", "inspect"])


FAKE = r'''
import json,os,sys
from pathlib import Path
p=Path(os.environ['FAKE_STATE']); s=json.loads(p.read_text()); a=sys.argv[2:]; kind=sys.argv[1]
s['calls'].append([kind,*a])
def finish(value='',code=0):
 p.write_text(json.dumps(s)); print(value); raise SystemExit(code)
def val(flag):return a[a.index(flag)+1]
if kind=='python':
 phase=a[2]
 if phase in ('upgraded','fresh'):
  if os.environ.get('FAKE_FAILURE')==phase:finish(code=1)
  output=Path(val('--output')); (Path(os.environ['FAKE_TEMP'])/output.parent.name/output.name).write_text('{}')
 finish()
if kind=='timeout':finish()
if a[:2]==['image','inspect']:finish('sha256:'+'1'*64+'|'+os.environ['FAKE_IMAGE_SHA'] if '.Config.Labels' in val('--format') else 'sha256:'+'1'*64)
if a[:2]==['container','ls']:
 f=val('--filter'); c=s.get('container');finish(c['id'] if c and (f=='id='+c['id'] or f=='name=^/'+c['name']+'$') else '')
if a[0]=='create':
 if s.get('container'):finish(code=91)
 s['created']+=1;cid=str(s['created'])*64; labels=[a[i+1] for i,x in enumerate(a) if x=='--label']
 c={'id':cid,'name':val('--name'),'image':'sha256:'+'1'*64,'token':labels[0].split('=',1)[1],'component':labels[1].split('=',1)[1]}
 s['container']=c;s['volumes']=['a'*64];finish(cid)
if a[:2]==['container','inspect']:
 c=s.get('container')
 if not c or a[-1]!=c['id']:finish(code=92)
 fmt=val('--format')
 if '.Mounts' in fmt:finish('\n'.join(s['volumes']))
 finish('|'.join((c['id'],'/'+c['name'],c['image'],c['token'],c['component']))+('|none' if '.HostConfig.NetworkMode' in fmt else ''))
if a[0]=='start':finish(a[-1])
if a[0]=='exec':finish()
if a[0]=='rm':
 s['removed'].append(a[-1]);s['container']=None;s['volumes']=[];finish(a[-1])
if a[:2]==['volume','ls']:finish('\n'.join(s['volumes']))
finish(code=99)
'''


class SchemaUpgradeHarnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bash = 'C:/Program Files/Git/bin/bash.exe' if os.name == 'nt' else shutil.which('bash')
        if not cls.bash or not Path(cls.bash).exists():
            raise RuntimeError('Bash required for native harness guards')

    def harness(self, failure=''):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            fake, state = directory/'fake.py', directory/'state.json'
            fake.write_text(FAKE)
            state.write_text(json.dumps({'calls': [], 'created': 0, 'removed': [], 'container': None, 'volumes': []}))
            env = dict(os.environ, MSYS_NO_PATHCONV='1', MSYS2_ARG_CONV_EXCL='*',
                       FAKE_STATE=str(state), FAKE_FAILURE=failure, FAKE_TEMP=str(directory), FAKE_PYTHON=Path(sys.executable).as_posix(),
                       FAKE_PROGRAM=fake.as_posix(), FAKE_IMAGE_SHA=hashlib.sha256((ROOT/'database/Dockerfile').read_bytes()).hexdigest(),
                       HARNESS=(ROOT/'tools/check_schema_upgrades.sh').as_posix(), LIC_SCHEMA_UPGRADES_MAX_ROWS='1000', TMPDIR=directory.as_posix())
            script = '''set -euo pipefail
 docker() { "$FAKE_PYTHON" "$FAKE_PROGRAM" docker "$@"; }
 python3() { "$FAKE_PYTHON" "$FAKE_PROGRAM" python "$@"; }
 timeout() { "$FAKE_PYTHON" "$FAKE_PROGRAM" timeout "$@"; shift 3; "$@"; }
 source "$HARNESS"
'''
            result = subprocess.run([self.bash,'-c',script],env=env,capture_output=True,text=True,timeout=25)
            return result,json.loads(state.read_text())

    def test_actual_shell_runs_independent_clusters_sequentially_and_cleans(self):
        result,state = self.harness()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(state['created'],2)
        self.assertEqual(state['removed'],['1'*64,'2'*64])
        self.assertIsNone(state['container'])
        creates = [row for row in state['calls'] if row[:2]==['docker','create']]
        for row in creates:
            self.assertIn('--network',row);self.assertEqual(row[row.index('--network')+1],'none')
            self.assertIn('--memory',row);self.assertIn('768m',row);self.assertIn('1.5',row)
            self.assertNotIn('-p',row);self.assertNotIn('--publish',row);self.assertIn('--pull=never',row)
        phases=[row[3] for row in state['calls'] if row[0]=='python']
        self.assertEqual(phases,['upgraded','fresh','compare'])
        timeouts=[row[3] for row in state['calls'] if row[0]=='timeout']
        self.assertEqual(timeouts,['270s','90s'])

    def test_failed_native_phase_never_skips_cleanup_or_starts_another_cluster(self):
        for phase,expected in (('upgraded',1),('fresh',2)):
            result,state=self.harness(phase)
            self.assertNotEqual(result.returncode,0)
            self.assertEqual(state['created'],expected)
            self.assertEqual(len(state['removed']),expected)
            self.assertIsNone(state['container'])
            self.assertEqual(state['volumes'],[])


if __name__ == '__main__':
    unittest.main()
