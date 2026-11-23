"""Typed identity admission: native SQL NULL semantics and strict result boundary."""
import itertools
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'application'))
from typed_video_identity_policy import typed_identity_from_native_facts
from db.videos import typed_video_read_policy as adapter
from db.base.postgres import DatabaseError

def positive():
    return dict(typed_type=True,channel_runtime=True,playlist_enabled=True,inventory_ready=True,
        scan_complete=True,foreign_present=False,manual_valid=None,automatic_valid=False,exact_present=True,
        conflicting_present_type=False)

def sql_and(*values):
    return False if False in values else None if None in values else True

def sql_or(*values):
    return True if True in values else None if None in values else False

def sql_not(value):
    return None if value is None else not value

class TypedPolicyTests(unittest.TestCase):
    def test_exhaustive_native_three_valued_admission(self):
        # Independently reproduce the retained WHERE/ON Boolean shape, including UNKNOWN.
        names=tuple(positive())
        for values in itertools.product((False,True,None),repeat=len(names)):
            f=dict(zip(names,values))
            native=sql_and(f['typed_type'],f['channel_runtime'],f['playlist_enabled'],f['inventory_ready'],
                f['scan_complete'],sql_not(f['foreign_present']),sql_or(f['manual_valid'],f['automatic_valid'],
                sql_and(f['exact_present'],sql_not(f['conflicting_present_type']))))
            self.assertIs(typed_identity_from_native_facts(f),native is True)

    def test_manual_override_never_overrides_foreign_channel_evidence(self):
        facts=dict(positive(),manual_valid=True,exact_present=False,conflicting_present_type=True)
        self.assertTrue(typed_identity_from_native_facts(facts))
        for foreign in (True,None):
            self.assertFalse(typed_identity_from_native_facts(dict(facts,foreign_present=foreign)))
        self.assertFalse(typed_identity_from_native_facts(dict(facts,manual_valid=None)))

    def test_automatic_resolution_keeps_common_guards_and_never_changes_manual_source(self):
        facts=dict(positive(),automatic_valid=True,conflicting_present_type=True)
        self.assertTrue(typed_identity_from_native_facts(facts))
        for name in ('typed_type','channel_runtime','playlist_enabled','inventory_ready','scan_complete'):
            for value in (False,None):
                self.assertFalse(typed_identity_from_native_facts(dict(facts,**{name:value})))
        for foreign in (True,None):
            self.assertFalse(typed_identity_from_native_facts(dict(facts,foreign_present=foreign)))
        for automatic in (False,None):
            self.assertFalse(typed_identity_from_native_facts(dict(facts,automatic_valid=automatic)))
        self.assertNotIn('automatic_valid',adapter._MANUAL_SOURCE_SQL)
        query=adapter._IDENTITY_FACT_SQL['automatic_valid']
        for addressed in ('automatic.video_id=i.video_id','automatic.channel_id=i.channel_id',
                          'automatic.automatic_type=i.video_type'):
            self.assertIn(addressed,query)

    def test_every_common_gate_fails_closed_for_false_or_unknown(self):
        for name in ('typed_type','channel_runtime','playlist_enabled','inventory_ready','scan_complete'):
            for value in (False,None):
                self.assertFalse(typed_identity_from_native_facts(dict(positive(),**{name:value})))

    def test_native_transport_missing_identity_is_false_and_bad_shape_is_rejected(self):
        self.assertEqual(adapter.typed_identity_row({'identity_facts':None}),{'eligible':False})
        self.assertEqual(adapter.typed_identity_row({'identity_facts':positive()}),{'eligible':True})
        for bad in (None,{},[],{'eligible':True},{'identity_facts':None,'extra':0},
                    {'identity_facts':{}},{'identity_facts':dict(positive(),extra=False)},
                    {'identity_facts':dict(positive(),typed_type=1)},
                    {'identity_facts':dict(positive(),manual_valid='true')}):
            with self.assertRaisesRegex(DatabaseError,'database_result_shape_invalid'):
                adapter.typed_identity_row(bad)

    def test_projection_does_not_mutate_native_facts(self):
        row={'identity_facts':positive()};held=dict(row['identity_facts'])
        adapter.typed_identity_row(row)
        self.assertEqual(row,{'identity_facts':held})

if __name__=="__main__": unittest.main()
