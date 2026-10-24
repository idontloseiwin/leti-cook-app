"""Focused schedule injection contracts; fake transport, no PostgreSQL/provider."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import timedelta
import ast
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'application'), str(ROOT/'tests')]
from collection_schedule import CollectionSchedules, TypedMetadata
import collection_policy as pure
from db.videos import collection_policy as cadence
from db.readiness import capabilities as caps
from db.videos import video_counter_completion as counter, video_metadata_completion as metadata
from db.videos import video_queue_initialization as initial, video_stream_completion as stream
from db.videos import video_typed_metadata_completion as typed, video_missing_completion as missing
from db.videos import typed_reconciliation_schedule as reconcile, video_queue_maintenance as maintenance
from db.videos import video_collection
from db.inventory import typed_inventory
from db.base.postgres import DatabaseError, DatabaseServerError, DatabaseUnavailable, Operation, PostgresDatabase, _reviewed_statement
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
from psycopg.types.json import Jsonb
import psycopg
import test_video_counter_completion as counter_fixture
import test_video_metadata_completion as metadata_fixture
import test_video_queue_initialization as initial_fixture
import test_video_stream_completion as stream_fixture
import test_video_typed_metadata_completion as typed_fixture
import test_video_missing_completion as missing_fixture
import test_video_queue_maintenance as maintenance_fixture
import test_capabilities as capability_fixture
from test_collection_policy import NOW, rows as legacy_rows, seed_snapshot
from test_direct_pg_transactions import Result, ServerFailure

DEFAULT = CollectionSchedules().cadence_snapshot()
CUSTOM = replace(CollectionSchedules(),
    long_counters=replace(CollectionSchedules().long_counters, age_0_1d_seconds=43, age_1_7d_seconds=7011),
    long_metadata=replace(CollectionSchedules().long_metadata, age_0_7d_seconds=8113),
    short_metadata=TypedMetadata(17, 29, 0),
    stream_metadata=TypedMetadata(101, 211, 7)).cadence_snapshot()

def rows(snapshot):
    return [dict(asdict(rule), captured_at=NOW.isoformat(), clocks_present=True) for rule in snapshot.rules]

class LegacyDatabase:
    def __init__(self, snapshot=CUSTOM, stored=None):
        self.cadence_snapshot=snapshot; self.stored=legacy_rows() if stored is None else stored; self.calls=[]
    def read_row(self, name, sql, parameters=()):
        assert name=='collection_cadence_legacy_compatibility' and sql==cadence.LEGACY_COMPATIBILITY_SQL
        assert parameters==()
        self.calls.append(name)
        # A finite transport fixture, not a substitute for the pending native SQL proof.
        default=legacy_rows();fields=tuple(pure.CadenceRule.__dataclass_fields__)
        valid=(len(self.stored)==16 and all(row.get('clocks_present') is True for row in self.stored)
               and {tuple(row.get(k) for k in fields) for row in self.stored}=={tuple(row[k] for k in fields) for row in default})
        return {'legacy_cadence_defaults':valid}

class SnapshotTests(unittest.TestCase):
    def test_invalid_inventory_snapshot_is_rejected_without_database_io(self):
        for corrupt in ('wrapper','type','interval'):
            snapshot=deepcopy(DEFAULT)
            if corrupt=='wrapper':object.__setattr__(snapshot,'inventory_by_type',{})
            elif corrupt=='type':object.__setattr__(snapshot.inventory_by_type,'short',{})
            else:object.__setattr__(snapshot.inventory_by_type.short,'missing_playlist_seconds',-1)
            owner=LegacyDatabase(snapshot)
            with self.assertRaisesRegex(DatabaseError,'collection_schedule_snapshot_invalid'):
                cadence.load_cadence_snapshot(owner)
            self.assertEqual(owner.calls,[])

    def test_default_parameter_preserves_schema16_and_adds_per_type_counters(self):
        legacy=set(seed_snapshot().rules)
        additions={replace(rule,video_type=kind) for kind in ('short','stream')
                   for rule in legacy if rule.video_type=='long' and rule.collection_kind=='counters'}
        self.assertEqual(set(DEFAULT.rules),legacy|additions)
        parameter=cadence.cadence_parameter(SimpleNamespace(cadence_snapshot=DEFAULT))
        self.assertEqual(parameter.obj, [asdict(r) for r in DEFAULT.rules])
        self.assertEqual(len(parameter.obj),30)
        self.assertTrue(all(set(row)==set(pure.CadenceRule.__dataclass_fields__) for row in parameter.obj))

    def test_snapshot_reader_returns_same_object_without_database_io(self):
        owner=LegacyDatabase()
        self.assertIs(cadence.load_cadence_snapshot(owner),CUSTOM)
        self.assertEqual(owner.calls,[])
        class Tx:
            cadence_snapshot=CUSTOM
            borrow=staticmethod(contextmanager(lambda: iter([None])))
        self.assertIs(cadence.load_cadence_snapshot_in_transaction(Tx()),CUSTOM)

    def test_missing_or_partial_snapshot_has_no_default_fallback(self):
        for value in (None,{},pure.CadenceSnapshot(DEFAULT.rules[:-1])):
            with self.subTest(value=type(value).__name__), self.assertRaises(DatabaseError):
                cadence.cadence_parameter(SimpleNamespace(cadence_snapshot=value))
        with self.assertRaises(DatabaseError):cadence.cadence_parameter(object())

    def test_algorithm_revision_clock_and_rule_shape_are_not_configurable(self):
        for snapshot in (replace(DEFAULT,captured_at=NOW),
                         replace(DEFAULT,rules=(replace(DEFAULT.rules[0],policy_version=2),)+DEFAULT.rules[1:]),
                         replace(DEFAULT,rules=(replace(DEFAULT.rules[0],enabled=False),)+DEFAULT.rules[1:])):
            with self.assertRaises(DatabaseError):cadence.cadence_parameter(SimpleNamespace(cadence_snapshot=snapshot))
        inconsistent=list(DEFAULT.rules)
        index=next(i for i,r in enumerate(inconsistent) if r.video_type=='short' and r.collection_kind=='metadata')
        inconsistent[index]=replace(inconsistent[index],jitter_window_seconds=0)
        with self.assertRaises(DatabaseError):cadence.cadence_parameter(SimpleNamespace(cadence_snapshot=pure.CadenceSnapshot(tuple(inconsistent))))

    def test_malformed_internal_rule_and_nonzero_long_jitter_fail_closed(self):
        malformed=[]
        for value in (None, [DEFAULT.rules[0]]*16, (object(),)+DEFAULT.rules[1:]):
            snapshot=object.__new__(pure.CadenceSnapshot)
            object.__setattr__(snapshot,'rules',value);object.__setattr__(snapshot,'captured_at',None)
            malformed.append(snapshot)
        wrong=object.__new__(pure.CadenceRule)
        for key,value in asdict(DEFAULT.rules[0]).items():object.__setattr__(wrong,key,value)
        object.__setattr__(wrong,'jitter_window_seconds',1)
        malformed.append(replace(DEFAULT,rules=(wrong,)+DEFAULT.rules[1:]))
        for snapshot in malformed:
            with self.subTest(value=repr(snapshot.rules)[:40]),self.assertRaises(DatabaseError):
                cadence.cadence_parameter(SimpleNamespace(cadence_snapshot=snapshot))

    def test_recheck_snapshot_is_required_and_revalidated_before_sql(self):
        invalids=[None, {}, object()]
        for key in ('unavailable_long_max_seconds','identity_seconds'):
            for value in (0,2419201,True,1.5):
                bad=object.__new__(pure.RecheckSchedules)
                object.__setattr__(bad,'unavailable_long_max_seconds',2419200)
                object.__setattr__(bad,'identity_seconds',2419200)
                object.__setattr__(bad,key,value)
                invalids.append(bad)
        for invalid in invalids:
            malformed=object.__new__(pure.CadenceSnapshot)
            object.__setattr__(malformed,'rules',DEFAULT.rules)
            object.__setattr__(malformed,'captured_at',None)
            object.__setattr__(malformed,'rechecks',invalid)
            with self.subTest(value=repr(invalid)),self.assertRaises(DatabaseError):
                cadence.require_cadence_snapshot(SimpleNamespace(cadence_snapshot=malformed))
        custom=replace(DEFAULT,rechecks=pure.RecheckSchedules(21600,1))
        self.assertIs(cadence.require_cadence_snapshot(SimpleNamespace(cadence_snapshot=custom)),custom)

    def test_custom_snapshot_accepts_historical_default_table(self):
        db=LegacyDatabase();self.assertEqual(cadence.assert_legacy_cadence_defaults(db),{'legacy_cadence_defaults':True})
        self.assertEqual(db.calls,['collection_cadence_legacy_compatibility'])
        self.assertEqual(cadence.cadence_policy_version(db),1)

    def test_stored_custom_missing_extra_or_clock_invalid_is_conflict(self):
        original=legacy_rows();changed=deepcopy(original);changed[0]['cadence_seconds']=43
        bad_clock=deepcopy(original);bad_clock[0]['clocks_present']=False
        for values in (changed,original[:-1],original+[original[0]],bad_clock,[]):
            with self.subTest(count=len(values)),self.assertRaisesRegex(DatabaseError,'legacy_cadence_conflict'):
                cadence.assert_legacy_cadence_defaults(LegacyDatabase(stored=values))

    def test_configuration_change_is_not_a_reconciliation_version_bump(self):
        self.assertEqual(cadence.cadence_policy_version(SimpleNamespace(cadence_snapshot=DEFAULT)),1)
        self.assertEqual(cadence.cadence_policy_version(SimpleNamespace(cadence_snapshot=CUSTOM)),1)
        self.assertNotIn('video_collection_cadence',maintenance._GAPS)
        self.assertIn('state.policy_version<%s::integer',maintenance._GAPS)

    def test_old_deadline_is_retained_until_next_completion_then_custom_interval(self):
        checked=NOW-timedelta(hours=2);anchor=NOW-timedelta(hours=12)
        assigned=pure.long_due_at(DEFAULT,'counters','public',anchor,checked,NOW,db_now=NOW)
        persisted={'stats_due_at':assigned,'metadata_due_at':NOW+timedelta(hours=5)}
        before=deepcopy(persisted)
        cadence.load_cadence_snapshot(SimpleNamespace(cadence_snapshot=CUSTOM))
        self.assertEqual(persisted,before)
        later=pure.long_due_at(CUSTOM,'counters','public',anchor,NOW,NOW,db_now=NOW)
        self.assertEqual(later,NOW+timedelta(seconds=43))
        self.assertEqual(pure.typed_due_at(CUSTOM,'short','aaaaaaaaaaa',NOW,NOW,db_now=NOW),NOW+timedelta(seconds=17))
        self.assertEqual(pure.typed_due_at(CUSTOM,'short','aaaaaaaaaaa',NOW-timedelta(days=91),NOW,db_now=NOW),NOW+timedelta(seconds=29))

class QueryBindingTests(unittest.TestCase):
    def test_native_cadence_fact_readers_bind_fixed30_without_table_fallback(self):
        value=cadence.cadence_parameter(SimpleNamespace(cadence_snapshot=CUSTOM))
        named=dict(rows=Jsonb([]),indices=[],owned_ids=[],cadence=value,identity_seconds=CUSTOM.rechecks.identity_seconds)
        cases=[(cadence._SQL,(value,)),(counter._POLICY_INPUT,(value,Jsonb([]))),
               (metadata._POLICY_INPUT,(value,Jsonb([]))),(initial._LONG_INPUT,(value,[])),
               (stream._POLICY,named),(typed._POLICY,named)]
        for statement,params in cases:
            with self.subTest(statement=statement[:45]):
                self.assertNotIn('public.video_collection_cadence',statement)
                self.assertIn('statement_timestamp()',statement)
                _reviewed_statement(statement,read=True,max_rows=1)
                PostgresQuery(Transformer()).convert(statement,params)
        for statement,params in ((maintenance._SELECT,(1,10)),(maintenance._REMAINING,(1,))):
            _reviewed_statement(statement,read=True,max_rows=1000);PostgresQuery(Transformer()).convert(statement,params)

    def test_closed_acquire_adds_only_internal_snapshot_and_preserves_conflict_sql(self):
        original=dict(video_type='short',inventory_ids=['aaaaaaaaaaa'],scopes=Jsonb([]));copy=dict(original)
        bound=reconcile.acquisition_parameters(SimpleNamespace(cadence_snapshot=CUSTOM),original)
        self.assertEqual(original,copy);self.assertEqual(set(bound),set(original)|{'cadence','max_interval_seconds'})
        self.assertEqual(bound['cadence'].obj,[asdict(r) for r in CUSTOM.rules])
        self.assertEqual(bound['max_interval_seconds'],2419200)
        PostgresQuery(Transformer()).convert(reconcile._ACQUIRE,bound)
        self.assertNotIn('public.video_collection_cadence',reconcile._ACQUIRE)
        self.assertIn('RETURNING state.video_id,state AS previous',reconcile._ACQUIRE)
        self.assertIn('ON CONFLICT(video_id) DO UPDATE SET updated_at=state.updated_at',reconcile._ACQUIRE)
        with self.assertRaises(DatabaseError):reconcile.acquisition_parameters(SimpleNamespace(cadence_snapshot=CUSTOM),dict(original,cadence='override'))
        with self.assertRaises(DatabaseError):reconcile.acquisition_parameters(object(),original)

    def test_native_writer_and_receipt_expressions_unchanged(self):
        # Source inverse is intentionally limited to expressions outside cadence inputs.
        names=['video_counter_completion','video_metadata_completion','video_queue_initialization',
               'video_stream_completion','video_typed_metadata_completion','video_missing_completion',
               'typed_reconciliation_schedule','video_queue_maintenance']
        def assignments(path):
            # R01 moved source retention from 28 to 29 days; test_retention_window pins it.
            source=path.read_bytes().replace(b"interval '696 hours'",b"interval '672 hours'")
            return {node.targets[0].id:ast.dump(node.value) for node in ast.parse(source).body
                if isinstance(node,ast.Assign) and len(node.targets)==1 and isinstance(node.targets[0],ast.Name)}
        # Canonical AST fingerprints from exact historical108 owner bytes,
        # freeze SHA8d2a10e29bb46bce9c054b006c82278d3cf3e82f5ea1c5a77f75de02dda2ccac.
        # Queue initialization also includes the accepted 2026-10-07 per-video
        # initial-to-regular handoff in _LONG_INPUT and _LONG_UPSERT. Its new
        # pin retains the full expression guard; runtime recovery is covered by
        # tests/test_initial_regular_handoff.py and the native recovery group in
        # tools/check_initial_regular_handoff.py.
        # P3 updates only the five availability ceilings to bound MAX_INTERVAL_SECONDS.
        # Their pins include the new placeholders; actual owner tests assert the bound value.
        # F6's reviewed General/Stats split updates only metadata and typed metadata
        # pins: explicit metadata_observed_at, source bounds/regression fences and
        # each source's existing cadence anchor. test_video_source_clocks covers
        # this accepted delta; native split-clock acceptance remains separate.
        # DATA-LIFECYCLE-01 adds acquisition pairs and clears unconfirmed missing
        # availability details in the reviewed owners below. Native acquisition
        # proves the same due policy, receipt replay and field-specific origin;
        # counter/General/live owner expressions and cadence exclusions stay pinned.
        # Unified scheduling extends the counter writer to all types and gives
        # absence episodes their own shared-owner deadline. Successful General
        # clears the episode; inventory reconciliation preserves its deadline.
        # Focused service/owner tests and the native all-format proof cover this delta.
        # Review correction: source-time return evidence replaces the boolean;
        # accepted terminal empty-Long channels share the ordinary mode. Native04
        # covers all completion owners, both event orders and legacy admission.
        # Schema29 also admits a Long/Short conflict for General bootstrap:
        # exact two fresh same-channel memberships, no owner decision/foreign
        # identity, and no duration guess. It adds LONG_SHORT_CONFLICT_SQL,
        # _CONFLICT_INVENTORY/_TYPED_BOOTSTRAP and extends _TYPED_INSERT only.
        # test_automatic_initial_bootstrap covers the metadata-only admission;
        # all eight complete AST-expression sets remain pinned below.
        # Schema31: waiting state is type-independent; stream_probe persists only
        # metadata and schedules ordinary General, counter completion refuses waiting,
        # and full General clears lifecycle atomically. Native publication CI covers
        # receipts, deadlines, handoff, masking and the same transaction fences.
        held = {'video_counter_completion': 'c0e82d5a379d8b1e365b25c73537d79d18337d37c1dece423f85be13f096c522', 'video_metadata_completion': 'e34f7ce4345725324addfcad5f2617167e5cd0cac3d70a9941ee126ebd121333', 'video_queue_initialization': '3e62a7a82c5095aee0fcd0623dcece68e63050c41bfecbb88c2a74de2bf8d23e', 'video_stream_completion': '399c29927e5778bf45e604181b641fb2b5c2c3c2cbde9e2a12e7f147fbad1027', 'video_typed_metadata_completion': 'b2425edd65c319dfdd187302e44cd31bcfdd78e50e112334d6d93cc4c3172fc0', 'video_missing_completion': '94d271581b9167725aa1c0ca291df917d2f6f23e242e8c2a0e2edf13c4ba05fe', 'typed_reconciliation_schedule': '9d8337fa45a4a539f5bfa31468d50e90632928227eea8bbdb8a0619f64792d41', 'video_queue_maintenance': 'fecba7c894be985cc865cd0b072cbf1f607e6beb4c93419bb1838077383ef97c'}
        for name in names:
            new=assignments(ROOT/'application/db/videos'/f'{name}.py')
            allowed={'_CADENCE'} if name=='typed_reconciliation_schedule' else ({'_GAPS'} if name=='video_queue_maintenance' else set())
            digest=hashlib.sha256(json.dumps({k:v for k,v in new.items() if k not in allowed},sort_keys=True).encode()).hexdigest()
            self.assertEqual(digest,held[name],name)

class ActualOwnerTests(unittest.TestCase):
    def fixture(self,kind,snapshot=CUSTOM):
        module,cls={'counter':(counter_fixture,counter_fixture.CounterCompletionTests),
             'metadata':(metadata_fixture,metadata_fixture.MetadataCompletionTests),
             'initial':(initial_fixture,initial_fixture.QueueInitializationTests),
             'stream':(stream_fixture,stream_fixture.StreamCompletionTests),
             'typed':(typed_fixture,typed_fixture.TypedMetadataTests),
             'missing':(missing_fixture,missing_fixture.MissingCompletionTests),
             'maintenance':(maintenance_fixture,maintenance_fixture.QueueMaintenanceTests)}[kind]
        def database(config,**kwargs):
            kwargs['cadence_snapshot']=snapshot
            return PostgresDatabase(config,**kwargs)
        value=cls()
        with patch.object(module,'PostgresDatabase',database):value.setUp()
        self.addCleanup(value.doCleanups);return value
    def invoke(self,fixture,snapshot,callback):
        self.assertIs(fixture.database.cadence_snapshot,snapshot)
        with fixture.database.transaction(Operation('schedule_injection_test')) as tx:
            self.assertIs(tx.cadence_snapshot,snapshot)
            return callback(tx)
    def assert_parameter(self,fixture,query,snapshot):
        values=[params for sql,params in fixture.connection.queries if query in sql]
        self.assertEqual(len(values),1)
        parameter=values[0]['cadence'] if isinstance(values[0],dict) else values[0][0]
        self.assertEqual(parameter.obj,[asdict(r) for r in snapshot.rules])
        self.assertEqual((fixture.connection.commits,fixture.pool.borrowed),(1,1))

    def test_counter_default_real_owner_keeps_original_intervals(self):
        f=self.fixture('counter',DEFAULT);data=f.setup_success(cadence=rows(DEFAULT))
        self.assertEqual(self.invoke(f,DEFAULT,lambda tx:counter.complete_video_counter_collection(None,data,transaction=tx)),1)
        self.assert_parameter(f,counter._POLICY_INPUT,DEFAULT)
        settlement=next(params[0].obj[0] for sql,params in f.connection.queries if sql==counter._COMPLETE)
        self.assertEqual(settlement['stats_seconds'],7200)

    def test_counter_one_and1000_keep_fixed_command_and_policy_parameter_bounds(self):
        for count in (1,1000):
            with self.subTest(count=count):
                f=self.fixture('counter');data=f.setup_success(count,cadence=rows(CUSTOM))
                self.assertEqual(self.invoke(f,CUSTOM,lambda tx:counter.complete_video_counter_collection(None,data,transaction=tx)),count)
                self.assert_parameter(f,counter._POLICY_INPUT,CUSTOM)
                self.assertEqual(len(f.connection.queries),15)
                settlement=next(params[0].obj for sql,params in f.connection.queries if sql==counter._COMPLETE)
                self.assertEqual(len(settlement),count)
                self.assertTrue(all(row['stats_seconds']==7011 for row in settlement))

    def test_counter_actual_driver_custom_policy_preserves_existing_metadata_deadline(self):
        f=self.fixture('counter');deadline='2026-10-06T00:00:00+00:00'
        data=f.setup_success(cadence=rows(CUSTOM),source_changes={'metadata_due_at':deadline})
        self.assertEqual(self.invoke(f,CUSTOM,lambda tx:counter.complete_video_counter_collection(None,data,transaction=tx)),1)
        self.assert_parameter(f,counter._POLICY_INPUT,CUSTOM)
        settlement=next(params[0].obj[0] for sql,params in f.connection.queries if sql==counter._COMPLETE)
        self.assertEqual(settlement['stats_seconds'],7011);self.assertEqual(settlement['metadata_due_at'],deadline)
        self.assertEqual(len(f.connection.queries),15)

    def test_metadata_actual_driver_uses_new_counter_and_metadata_intervals(self):
        f=self.fixture('metadata');data=f.setup_success(cadence=rows(CUSTOM))
        self.assertEqual(self.invoke(f,CUSTOM,lambda tx:metadata.complete_video_metadata_collection(None,data,transaction=tx)),1)
        self.assert_parameter(f,metadata._POLICY_INPUT,CUSTOM)
        settlement=next(params[0].obj[0] for sql,params in f.connection.queries if sql==metadata._COMPLETE)
        self.assertEqual((settlement['stats_seconds'],settlement['metadata_seconds']),(43,8113))

    def test_initialization_actual_driver_extended_clocks_and_four_commands(self):
        f=self.fixture('initial');f.header(cadence=rows(CUSTOM));f.queue(initial_fixture.one(video_id=initial_fixture.VIDEO),Result())
        self.assertEqual(self.invoke(f,CUSTOM,lambda tx:initial.ensure_video_collection_state_in_transaction(tx,[initial_fixture.VIDEO])),1)
        self.assert_parameter(f,initial._LONG_INPUT,CUSTOM)
        settlement=f.connection.queries[2][1][0].obj[0]
        self.assertEqual((settlement['stats_seconds'],settlement['metadata_seconds']),(43,8113))
        self.assertEqual(settlement['published_at'],'10000-01-01T00:00:00+00:00');self.assertEqual(len(f.connection.queries),4)

    def test_stream_actual_driver_uses_independent_interval_and_jitter(self):
        f=self.fixture('stream');data=f.setup_success(cadence=rows(CUSTOM))
        self.assertEqual(self.invoke(f,CUSTOM,lambda tx:stream.complete_stream_lifecycle_probe(None,data,transaction=tx)),1)
        self.assert_parameter(f,stream._POLICY,CUSTOM)
        plan=next(params['schedules'].obj[0] for sql,params in f.connection.queries if sql==stream._COMPLETE)
        self.assertEqual(plan['seconds'],pure.typed_jittered_seconds(data[0]['video_id'],101,7));self.assertEqual(len(f.connection.queries),21)

    def test_typed_metadata_actual_owner_uses_short_interval(self):
        f=self.fixture('typed')
        with patch.object(typed_fixture,'cadence_rows',return_value=rows(CUSTOM)):data=f.setup_success()
        self.assertEqual(self.invoke(f,CUSTOM,lambda tx:typed.complete_typed_video_metadata_collection(None,data,transaction=tx)),1)
        self.assert_parameter(f,typed._POLICY,CUSTOM)
        plan=next(params['schedules'].obj[0] for sql,params in f.connection.queries if sql==typed._COMPLETE)
        self.assertEqual(plan['seconds'],17);self.assertEqual(len(f.connection.queries),22)

    def test_missing_actual_owner_uses_unavailable_schedule_after_incident_effects(self):
        f=self.fixture('missing');data=f.success(cadence=rows(CUSTOM))
        self.assertEqual(self.invoke(f,CUSTOM,lambda tx:missing.complete_typed_video_missing(None,data,transaction=tx)),1)
        self.assertNotIn('cadence',next(params for sql,params in f.connection.queries if missing._POLICY in sql))
        plan=next(params['schedules'].obj[0] for sql,params in f.connection.queries if sql==missing._COMPLETE)
        self.assertEqual(plan['next_due_at'],(NOW+timedelta(days=1)).isoformat())
        self.assertEqual(plan['first_missing_at'],NOW.isoformat());self.assertEqual(len(f.connection.queries),23)

    def test_existing_queue_repair_uses_revision1_and_same_bounds(self):
        f=self.fixture('maintenance');f.backfill()
        result=self.invoke(f,CUSTOM,lambda tx:maintenance.backfill_video_collection_state(None,10,transaction=tx))
        self.assertEqual(result,[{'selected_count':1,'affected_count':3,'remaining_count':0}])
        bound=[p for sql,p in f.connection.queries if maintenance._SELECT in sql][0]
        self.assertEqual(bound,(1,10));self.assertEqual(len(f.connection.queries),7)

    def test_missing_snapshot_rolls_back_prior_counter_writes(self):
        f=self.fixture('counter',None);data=f.setup_success()
        with self.assertRaisesRegex(DatabaseError,'snapshot_required'):
            self.invoke(f,None,lambda tx:counter.complete_video_counter_collection(None,data,transaction=tx))
        self.assertTrue(any(sql==counter._INSERT_SNAPSHOTS for sql,_ in f.connection.queries))
        self.assertFalse(any(sql==counter._COMPLETE for sql,_ in f.connection.queries))
        self.assertEqual((f.connection.commits,f.connection.rollbacks),(0,1))

    def test_empty_and_native_input_errors_still_precede_schedule_requirement(self):
        f=self.fixture('counter');f.test_empty_returns_after_native_cast_count_and_nonarrays_do_not_dispatch()
        f=self.fixture('stream');f.queue(Result(execute_error=ServerFailure('22P02')))
        with self.assertRaises(DatabaseError) as caught:stream.complete_stream_lifecycle_probe(f.database,[{}])
        self.assertEqual(caught.exception.code,'22P02');self.assertEqual(len(f.connection.queries),1)

    def test_exact_metadata_receipt_replay_does_not_need_policy_or_mutate(self):
        f=self.fixture('metadata');data=f.setup_success(owned_count=0)
        self.assertEqual(metadata.complete_video_metadata_collection(f.database,data),1)
        self.assertFalse(any(metadata._POLICY_INPUT in sql or sql==metadata._UPSERT for sql,_ in f.connection.queries))
        self.assertEqual(f.connection.commits,1)

    def test_unknown_commit_has_one_attempt_and_no_schedule_retry(self):
        f=self.fixture('counter');data=f.setup_success(cadence=rows(CUSTOM));f.connection.commit_error=psycopg.OperationalError('private')
        with self.assertRaises(DatabaseUnavailable) as caught:
            self.invoke(f,CUSTOM,lambda tx:counter.complete_video_counter_collection(None,data,transaction=tx))
        self.assertTrue(caught.exception.outcome_unknown);self.assertEqual(f.pool.borrowed,1);self.assertEqual(len(f.connection.queries),15)

class ReadinessTests(unittest.TestCase):
    def setUp(self):capability_fixture.reviewed_chain_facts(self)
    def database(self):
        db=capability_fixture.FactsDatabase();db.cadence_snapshot=CUSTOM;db.legacy_calls=[]
        original=db.read_row
        def read_row(name,sql,parameters=()):
            if sql==cadence.LEGACY_COMPATIBILITY_SQL:
                self.assertEqual(parameters,());db.legacy_calls.append(name);return {'legacy_cadence_defaults':True}
            return original(name,sql,parameters)
        db.read_row=read_row;return db
    def strict_database(self, snapshot):
        db=self.database();db.cadence_snapshot=snapshot
        overrides={
            'assert_typed_inventory_quota_defer_schema':dict(schema_version=1,claim_limit=500,retry_floor_seconds=3600,
                cadence_jitter_seconds=21600,lost_ack_receipts=True,active_scan_state_preserved=True,
                quota_claim_floor_guard=True,retry_until='youtube_quota_reset'),
            'assert_typed_video_collection_schema':dict(schema_version=1,runtime_video_types=['short','stream'],
                max_request_batch=50,full_metadata_each_refresh=True,completed_streams_only=True,
                product_exposure_changed=False,exact_receipt_payload_fencing=True,repair_version=2,bounded_claim_batch=True),
            'assert_typed_inventory_runtime_schema':dict(schema_version=2,runtime_video_types=['long','short','stream'],
                durable_fairness=True,product_exposure_independent_of_active_scan=True,initial_chunk_pages=1)}
        for name,fields in overrides.items():
            fact=capability_fixture.valid_facts(name);fact['value'].update(fields);db.overrides[name]=fact
        return db

    def test_actual_strict_readiness_consumers_default_and_custom(self):
        for snapshot in (DEFAULT,CUSTOM):
            with self.subTest(snapshot=snapshot),patch.object(caps.operational_read_queries,'get_typed_video_collection_health',return_value={}):
                db=self.strict_database(snapshot);video=video_collection.VideoCollectionRepository(db)
                self.assertEqual(video.schema_status()['schedule_source'],'process_snapshot')
                result=video.typed_schema_status()
                expected=cadence.cadence_report(db)['typed_jitter_seconds']
                self.assertEqual(result['typed_inventory_quota_defer']['typed_jitter_seconds'],expected)
                self.assertEqual(video.preflight_typed_collection_ready()['schedule_source'],'process_snapshot')
                inventory=typed_inventory.TypedInventoryRepository(db)
                self.assertEqual(inventory.assert_quota_defer_ready()['typed_jitter_seconds'],expected)
                self.assertEqual(inventory.assert_ready()['status'],'ok')

    def test_strict_consumers_reject_legacy_scalar_bool_missing_type_or_mismatch(self):
        db=self.strict_database(CUSTOM);video=video_collection.VideoCollectionRepository(db)
        good=caps.assert_typed_inventory_quota_defer_schema(db)
        malformed=[dict(good,typed_jitter_seconds=value) for value in
            (21600,{'short':0},{'short':False,'stream':7},{'short':0,'stream':21600},
             {'short':0,'stream':7,'long':0})]
        malformed.append(dict(good,schedule_source='legacy_table'))
        legacy=dict(good);legacy.pop('typed_jitter_seconds');legacy['cadence_jitter_seconds']=21600;malformed.append(legacy)
        for result in malformed:
            with self.subTest(result=result):
                with self.assertRaises(RuntimeError):video._assert_typed_quota_defer_contract(result)
                with patch.object(caps,'assert_typed_inventory_quota_defer_schema',return_value=result):
                    with self.assertRaises(RuntimeError):typed_inventory.TypedInventoryRepository(db).assert_quota_defer_ready()

    def test_custom_reports_are_per_type_and_legacy_validation_once(self):
        db=self.database();read=caps._Read(db)
        value=read.facts('assert_video_collection_cadence_schema')
        self.assertEqual(value['schedule_source'],'process_snapshot')
        self.assertEqual(value['typed_jitter_seconds'],{'short':0,'stream':7})
        self.assertEqual(value['metadata_cadence_seconds'],{'short':{'fresh':17,'old':29},'stream':{'fresh':101,'old':211}})
        typed_report=read.facts('assert_typed_video_collection_schema')
        self.assertNotIn('fresh_cadence_days',typed_report);self.assertNotIn('old_cadence_days',typed_report)
        self.assertEqual(typed_report['metadata_cadence_seconds'],value['metadata_cadence_seconds'])
        quota=read.facts('assert_typed_inventory_quota_defer_schema')
        self.assertNotIn('cadence_jitter_seconds',quota);self.assertEqual(quota['typed_jitter_seconds'],value['typed_jitter_seconds'])
        self.assertEqual(len(db.legacy_calls),1)
    def test_intrinsic_catalog_and_structural_denials_still_precede_reports(self):
        db=self.database();db.overrides['final_schema']={'roles_valid':True,'functions_valid':False,'triggers_valid':True}
        with self.assertRaises(DatabaseError):caps.assert_video_collection_cadence_schema(db)
        self.assertEqual(db.legacy_calls,[])
        db=self.database();native=capability_fixture.valid_facts('assert_typed_video_collection_schema');native['violations'][1]=True
        db.overrides['assert_typed_video_collection_schema']=native
        with self.assertRaises(DatabaseError):caps._Read(db).facts('assert_typed_video_collection_schema')
        self.assertEqual(db.legacy_calls,[])
    def test_no_snapshot_and_custom_legacy_table_cannot_report_ready(self):
        db=self.database();db.cadence_snapshot=None
        with self.assertRaisesRegex(DatabaseError,'snapshot_required'):caps.assert_video_collection_cadence_schema(db)
        db=self.database();original=db.read_row
        db.read_row=lambda n,s,p=():{'legacy_cadence_defaults':False} if s==cadence.LEGACY_COMPATIBILITY_SQL else original(n,s,p)
        with self.assertRaisesRegex(DatabaseError,'legacy_cadence_conflict'):caps.assert_video_collection_cadence_schema(db)

    def test_hot_startup_uses_only_closed_guard_before_ordinary_readiness(self):
        db=capability_fixture.FactsDatabase();original=db.read_maintenance_capability
        def maintenance(name):
            if name=='collection_cadence':db.calls.append(('maintenance',name));return {'legacy_cadence_defaults':True}
            return original(name)
        db.read_maintenance_capability=maintenance
        self.assertTrue(caps.get_hot_runtime_contract(db)['schema_ready'])
        self.assertEqual(db.calls[1:3],[('maintenance','schema_release'),('maintenance','collection_cadence')])
        db.read_maintenance_capability=lambda name: {'legacy_cadence_defaults':False} if name=='collection_cadence' else original(name)
        with self.assertRaisesRegex(DatabaseError,'legacy_cadence_conflict'):caps.get_hot_runtime_contract(db)
        for bad in ({},{'legacy_cadence_defaults':1},{'legacy_cadence_defaults':True,'extra':False}):
            with self.assertRaisesRegex(DatabaseError,'result_shape_invalid'):cadence.validate_legacy_cadence_defaults(bad)

    def test_fixed_startup_guard_exact_default16_bidirectional_comparison(self):
        sql=cadence.LEGACY_COMPATIBILITY_SQL
        self.assertIs(caps.MAINTENANCE_CAPABILITY_QUERIES['collection_cadence'],sql)
        _reviewed_statement(sql,read=True,max_rows=1);PostgresQuery(Transformer()).convert(sql,())
        self.assertEqual(sql.count(' EXCEPT '),2);self.assertIn('created_at IS NULL OR updated_at IS NULL',sql)
        schema=(ROOT/'schema/schema.sql').read_text()
        import re
        held=re.search(r'INSERT INTO public\.video_collection_cadence\([^;]+? VALUES\s*(.*?);',schema,re.S).group(1)
        self.assertIn(held,sql);self.assertIn(')=16',sql)

if __name__=='__main__':unittest.main(verbosity=2)
