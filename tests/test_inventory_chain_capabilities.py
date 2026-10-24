"""Release catalog pins and fail-closed behavior when a contract is incomplete."""
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock,patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'application'))
from db.readiness import inventory_chain_capabilities as capability
from db.base.postgres import DatabaseError,_reviewed_statement
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer

def pins():
    return {prefix+name:'0'*32 for prefix,names in (
        ('constraint:',capability.CONSTRAINT_NAMES+capability.PARENT_CONSTRAINT_NAMES),
        ('index:',capability.INDEX_NAMES)) for name in names}

class ChainReadinessTests(unittest.TestCase):
    def test_release_pins_are_present_in_the_actual_hot_readiness_statement(self):
        from db.readiness import capabilities
        statement=capability.definition_sql()
        self.assertEqual(len(capability.DEFINITION_PINS),18)
        self.assertEqual(capability.HOT_READINESS_SQL,capability.hot_readiness_sql())
        self.assertIn(statement,capability.HOT_READINESS_SQL)
        self.assertIn(capability.HOT_READINESS_SQL,capabilities._HOT_RUNTIME_SQL)
        for value in capability.DEFINITION_PINS.values():
            self.assertNotEqual(value,'0'*32)
            self.assertIn(value,capabilities._HOT_RUNTIME_SQL)

    def test_unreviewed_release_fails_before_catalog_or_domain_access(self):
        database=Mock()
        with patch.object(capability,'DEFINITION_PINS',{}):
            with self.assertRaisesRegex(DatabaseError,'definition_pins_missing'):
                capability.assert_inventory_chains_ready(database)
        database.read_row.assert_not_called()

    def test_partial_extra_or_nonliteral_pin_set_fails_closed(self):
        for mutate in (lambda p:p.pop(next(iter(p))),
                       lambda p:p.update(extra='0'*32),
                       lambda p:p.update({next(iter(p)):"unreviewed' OR true"})):
            values=pins();mutate(values)
            with self.subTest(size=len(values)),patch.object(capability,'DEFINITION_PINS',values):
                with self.assertRaises(DatabaseError):capability.definition_sql()

    def test_readiness_requires_acl_shape_and_definitions(self):
        facts=[dict(roles_valid=True,relations_valid=True,privileges_valid=True),
               dict(table_valid=True,columns_valid=True,readers_denied=True),
               dict(definitions_valid=True,constraints_valid=True,indexes_valid=True)]
        with patch.object(capability,'DEFINITION_PINS',pins()):
            for index in range(3):
                bad=[dict(value) for value in facts]
                bad[index][next(iter(bad[index]))]=False
                database=Mock();database.read_row.side_effect=bad
                with self.subTest(stage=index),self.assertRaises(DatabaseError):
                    capability.assert_inventory_chains_ready(database)
                self.assertEqual(database.read_row.call_count,index+1)
            database=Mock();database.read_row.side_effect=facts
            self.assertEqual(capability.assert_inventory_chains_ready(database),{'ready':True,'chain_version':1})

    def test_truthy_or_extra_catalog_fact_never_counts_as_ready(self):
        with patch.object(capability,'DEFINITION_PINS',pins()):
            for row in (dict(roles_valid=1,relations_valid=True,privileges_valid=True),
                        dict(roles_valid=True,relations_valid=True,privileges_valid=True,extra=True)):
                database=Mock();database.read_row.return_value=row
                with self.assertRaisesRegex(DatabaseError,'result_shape_invalid'):
                    capability.assert_inventory_chains_ready(database)

    def test_exact_storage_grants_and_native_definition_statements(self):
        self.assertNotIn('updated_at',capability.INSERT_COLUMNS)
        self.assertNotIn('job_id',capability.UPDATE_COLUMNS)
        self.assertNotIn('channel_id',capability.UPDATE_COLUMNS)
        self.assertNotIn('video_type',capability.UPDATE_COLUMNS)
        with patch.object(capability,'DEFINITION_PINS',pins()):
            statements=(capability.ACL_SQL,capability.CATALOG_SQL,capability.CAPTURE_SQL,capability.definition_sql())
        for sql in statements:
            with self.subTest(sql=sql[:60]):
                PostgresQuery(Transformer()).convert(sql,())
                _reviewed_statement(sql,read=True,max_rows=32)
        self.assertIn("contype IN('p','f','u','c')",statements[-1])
        self.assertIn('indisvalid AND indisready AND indislive',statements[-1])
        self.assertIn("confdeltype<>'a'",statements[-1])
        self.assertIn("confdeltype<>'c'",statements[-1])
        self.assertIn('pg_catalog.pg_get_constraintdef',capability.CAPTURE_SQL)
        self.assertIn('lic_reader',capability.CATALOG_SQL)

    def test_pending_pins_close_full_readiness_before_domain_queries(self):
        from test_capabilities import FactsDatabase
        from db.readiness import capabilities
        database=FactsDatabase()
        with patch.object(capability,'DEFINITION_PINS',{}):
            with self.assertRaisesRegex(DatabaseError,'definition_pins_missing'):
                capabilities.assert_operator_jobs_ready(database)
        self.assertEqual([call[0] for call in database.calls],
                         ['capability_schema_release','capability_final_schema'])

    def test_hot_uses_same_chain_catalog_without_new_maintenance_authority(self):
        from db.readiness import capabilities
        with patch.object(capability,'DEFINITION_PINS',{}):
            self.assertEqual(capability.hot_readiness_sql(),'SELECT false AS inventory_chains_valid')
        self.assertIn(capability.HOT_READINESS_SQL,capabilities._HOT_RUNTIME_SQL)
        self.assertIn('AND inventory_chains_valid',capabilities._HOT_RUNTIME_SQL)
        with patch.object(capability,'DEFINITION_PINS',pins()):
            statement=capability.hot_readiness_sql()
            for part in (capability.ACL_SQL,capability.CATALOG_SQL,capability.definition_sql()):
                self.assertIn(part,statement)
        _reviewed_statement(statement,read=True,max_rows=1)
        PostgresQuery(Transformer()).convert(statement,())


if __name__=='__main__':unittest.main()
