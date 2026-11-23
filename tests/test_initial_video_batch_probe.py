"""Offline guards for the explicitly selected, bounded native initial probe."""
from contextlib import nullcontext
from datetime import datetime,timezone
import importlib.util
import io
import re
import ast
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock,patch
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'application'))
SPEC=importlib.util.spec_from_file_location('initial_batch_probe_test',ROOT/'tools/check_initial_video_batch.py')
probe=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(probe)


class InitialBatchProbeTests(unittest.TestCase):
    def test_explicit_selection_and_listing_never_open_a_connection(self):
        with patch.object(probe.safety,'connect') as connect,patch('sys.stdout',new_callable=io.StringIO):
            self.assertEqual(probe.main(['--list-cases']),0)
            for args in ([],['--case','atomic','--case','atomic']):
                with patch('sys.stderr',new_callable=io.StringIO),self.assertRaises(SystemExit):probe.main(args)
            connect.assert_not_called()

    def test_chunk_fixture_is_at_most_400_and_does_not_create_queue_rows(self):
        fixture=Mock();fixture.now=datetime(2026,10,7,tzinfo=timezone.utc)
        fixture.admin.transaction.side_effect=lambda:nullcontext()
        with patch.object(probe,'seed_channel'):
            for count in (100,400):
                fixture.admin.execute.reset_mock();ids=probe.seed_chunk(fixture,count)
                self.assertEqual(len(ids),count)
                for call in fixture.admin.execute.call_args_list:
                    sql,params=call.args
                    PostgresQuery(Transformer()).convert(sql,params)
                    self.assertNotIn('INSERT INTO public.video_collection_state',sql)
                    self.assertNotIn('GRANT',sql)
                    self.assertNotIn('ALTER',sql)
                job=next(call.args for call in fixture.admin.execute.call_args_list if 'UPDATE public.operator_jobs' in call.args[0])
                self.assertEqual(job[1][2],count)
                self.assertTrue(all(item['inventory_ready'] for item in job[1][4].obj['inventory_results'].values()))
                inventory=next(call.args for call in fixture.admin.execute.call_args_list
                               if 'INSERT INTO public.video_inventory(' in call.args[0])
                self.assertIn('published_at,published_acquired_at',inventory[0])
                # Publication date and the observation clock are distinct. The
                # fixture uses the same acquired clock as its recorded scan.
                self.assertEqual(inventory[1][2].isoformat(),job[1][4].obj['full_inventory_sources']['long']['source_checked_at'])
                self.assertLess(inventory[1][1],inventory[1][2])
            for count in (0,401,True):
                with self.assertRaises(probe.fixtures.ProbeFailure):probe.seed_chunk(fixture,count)

    def test_channel_fixture_uses_python_owner_and_safe_diagnostics_hide_values(self):
        fixture=Mock();fixture.now=datetime(2026,10,7,tzinfo=timezone.utc)
        with patch('db.channel.channel_control.create_channel_for_collection') as create:
            probe.seed_channel(fixture)
        create.assert_called_once()
        self.assertIs(create.call_args.args[0],fixture.database)
        self.assertEqual(create.call_args.args[2],'accepted')
        self.assertNotIn('SELECT public.',fixture.admin.execute.call_args.args[0])
        class Error(Exception):
            sqlstate='42883'
            diag=type('Diag',(),{'message_primary':'function public.old_helper(secret_private_value) does not exist'})()
        result=probe.safe_failure(Error('secret_private_value'))
        self.assertEqual(result['missing_function'],'public.old_helper')
        self.assertNotIn('secret_private_value',str(result))

    def test_all_seed_sql_relations_columns_and_bindings_match_current_schema(self):
        fixture=Mock();fixture.now=datetime(2026,10,7,tzinfo=timezone.utc)
        fixture.admin.transaction.side_effect=lambda:nullcontext()
        with patch.object(probe,'seed_channel'):
            for terminal,fail in ((False,False),(False,True),(True,True)):
                probe.seed_mixed(fixture,terminal_stream=terminal,include_failure=fail)
            probe.seed_chunk(fixture,100)
            probe.seed_chunk(fixture,400)
        schema=(ROOT/'schema/schema.sql').read_text(encoding='utf-8')
        tables={match.group(1):set(re.findall(r'^    ([a-z_]+) ',match.group(2),re.M))
            for match in re.finditer(r'CREATE TABLE (public\.[a-z_]+) \((.*?)\n\);',schema,re.S)}
        for call in fixture.admin.execute.call_args_list:
            sql,params=call.args
            PostgresQuery(Transformer()).convert(sql,params)
            for relation in re.findall(r'(?:INTO|UPDATE|FROM|JOIN) (public\.[a-z_]+)',sql):
                self.assertIn(relation,tables)
            insert=re.search(r'INSERT INTO (public\.[a-z_]+)\(([^)]+)\)',sql,re.S)
            if insert:
                self.assertTrue({value.strip() for value in insert.group(2).split(',')}<=tables[insert.group(1)])
            self.assertNotIn('SELECT public.',sql)
        tree=ast.parse(Path(probe.__file__).read_text())
        checks={node.names[0].name for node in tree.body if isinstance(node,ast.Import)
            and node.names[0].name.startswith('check_')}
        self.assertEqual(checks,{'check_direct_pg_repositories'})
        fixture.admin.scalar.reset_mock();probe.state(fixture)
        sql=fixture.admin.scalar.call_args.args[0]
        for relation in re.findall(r'FROM (public\.[a-z_]+)',sql):self.assertIn(relation,tables)

    def test_probe_uses_real_runtime_pool_and_no_legacy_oracle_or_live_api(self):
        source=Path(probe.__file__).read_text()
        self.assertIn('cadence_snapshot=CollectionSchedules().cadence_snapshot()',source)
        self.assertIn('max_size=1',source)
        self.assertIn('VideoCollectionRepository(boundary).complete_initial_batch',source)
        self.assertIn('run_video_collection_queue_batch',source)
        self.assertIn('Barrier(2,timeout=10)',source)
        self.assertIn('repository.verify_initial_hydration',source)
        self.assertNotIn('import requests',source)
        self.assertNotIn('import urllib',source)
        self.assertNotIn('LegacyOracleDatabase',source)
        self.assertNotIn('subprocess',source)


if __name__=='__main__':unittest.main()
