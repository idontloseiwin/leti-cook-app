"""Real service-role capabilities and public preflights, including denied schemas."""
from contextlib import contextmanager
from datetime import datetime,timedelta
from db.readiness import capabilities,intrinsic_capabilities
from db.base.postgres import DatabaseError
from db.hot.hot_collection_control import verify_control_contract
from db.channel.channel_sync import ChannelSyncRepository
from db.videos.video_collection import VideoCollectionRepository
import hot_runtime
from .fixtures import expect_error
from .manifest import require

EXPECTED_CHECKS=frozenset(('assert_channel_discovery_paging_schema','assert_channel_discovery_queue_schema',
'assert_channel_sync_control_ready','assert_full_initial_schema','assert_inventory_receipts_schema',
'assert_inventory_switch_schema','assert_operator_jobs_ready','assert_typed_inventory_canonical_repair_schema',
'assert_typed_inventory_foundation_schema','assert_typed_inventory_quota_defer_schema','assert_typed_inventory_runtime_schema',
'assert_typed_video_collection_schema','assert_video_collection_cadence_schema','assert_video_collection_common_pool_schema',
'assert_video_collection_queue_schema','assert_channel_discovery_queue_ready','assert_video_collection_queue_ready'))


def preflights(f):
    return (lambda:ChannelSyncRepository(f.database).assert_metadata_batch_ready(),
      lambda:VideoCollectionRepository(f.database).assert_forced_metadata_claim_ready(worker_id='schema-ci',run_id='10000000-0000-4000-8000-000000000100'))


def runtime_readiness(f):
    f.check(set(capabilities.CHECKS)==EXPECTED_CHECKS and len(EXPECTED_CHECKS)==17,'exact_readiness_inventory')
    before=f.snapshot()
    for name in sorted(EXPECTED_CHECKS):
        value=capabilities.CHECKS[name](f.database)
        f.check(isinstance(value,dict) and bool(value),'nonempty_readiness_report')
        if name in capabilities._FIELDS:
            expected_fields=set(capabilities._FIELDS[name])
            if name in ('assert_video_collection_cadence_schema','assert_typed_inventory_quota_defer_schema',
                        'assert_typed_video_collection_schema','assert_video_collection_common_pool_schema',
                        'assert_video_collection_queue_schema'):
                expected_fields.add('schedule_source')
            if name=='assert_video_collection_cadence_schema':
                expected_fields.update(('metadata_cadence_seconds','typed_jitter_seconds'))
            if name=='assert_typed_inventory_quota_defer_schema':
                expected_fields.remove('cadence_jitter_seconds');expected_fields.add('typed_jitter_seconds')
            if name=='assert_typed_video_collection_schema':
                expected_fields.difference_update(('fresh_cadence_days','old_cadence_days'))
                expected_fields.add('metadata_cadence_seconds')
            f.check(set(value)==expected_fields,'readiness_fields')
        for field in ('runtime_enabled_rows','product_exposure_ready_rows','typed_nonlong_runtime_enabled_rows','long_typed_drift','typed_long_clock_drift','identity_drift','disabled_active_drift'):
            if field in value:f.check(type(value[field]) is int and value[field]==0,'empty_live_state_count')
        if 'ineligible_active_drift' in value:
            f.check(value['ineligible_active_drift'] is None,'unavailable_ineligible_drift')
        if 'ready' in value:f.check(value['ready'] is True,'ready_flag')
    value=capabilities.get_hot_runtime_contract(f.database)
    f.check(value['schema_ready'] is True and value['version']==1 and datetime.fromisoformat(value['as_of']).tzinfo is not None,'hot_contract')
    f.check(set(value)=={'version','as_of','schema_ready'},'hot_contract_has_no_age_bypass')
    f.check(f.scalar("SELECT NOT EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='operator_private' AND c.relname='database_birth')") is True,'database_birth_absent')
    metadata,forced=preflights(f)
    f.check(metadata() is None and forced()==[],'actual_empty_preflights')
    f.check(f.snapshot()==before,'readiness_did_not_write')
    expect_error(f,lambda:VideoCollectionRepository(f.database).assert_forced_metadata_claim_ready(worker_id='schema-ci',run_id='not-a-uuid'),DatabaseError,code='22P02')
    f.gate.ready=False
    try:
        f.check(verify_control_contract(f.database)=={'status':'ready','abort_contract':True,'interrupt_contract':True},'closed_hot_control_when_blocked')
        expect_error(f,lambda:f.database.read_row('schema_ci_blocked','SELECT 1 AS value'),hot_runtime.HotRuntimeUnavailable)
    finally:f.gate.ready=True
    f.check(f.snapshot()==before,'blocked_gate_no_writes')
    from .cases_channel_reads import channel_readers
    channel_readers(f)


@contextmanager
def fault(f,apply,undo):
    # Genuine separate runtime connection must see committed DDL. This runner
    # only accepts the explicit disposable CI marker and fixed loopback DB.
    before=(f.catalog(),f.snapshot(),f.sequences())
    try:
        f.rows(apply)
        yield
    finally:
        f.rows(undo)
        f.check((f.catalog(),f.snapshot(),f.sequences())==before,'fault_restored_exactly')


def channel_insert_catalog_guards(f):
    # Exercise the actual production guard natively. Only its fixed relation
    # target changes; the temporary clone has the same defaults and owner.
    from db.channel.channel_initial_error_inserts import _SCHEMA_PIN,_SCHEMA_PARAMETERS
    before=(f.catalog(),f.snapshot(),f.sequences())
    guard=_SCHEMA_PIN.replace("'public.channels'::regclass","'pg_temp.schema_ci_channels'::regclass").replace('FROM public.channels WHERE false','FROM pg_temp.schema_ci_channels WHERE false')
    with f.admin.transaction():
        f.rows('CREATE TEMP TABLE schema_ci_channels (LIKE public.channels INCLUDING DEFAULTS INCLUDING CONSTRAINTS INCLUDING GENERATED INCLUDING IDENTITY) ON COMMIT DROP')
        f.rows('ALTER TABLE pg_temp.schema_ci_channels ENABLE ROW LEVEL SECURITY')
        f.check(f.scalar(guard,_SCHEMA_PARAMETERS) is True,'channel_catalog_clone_matches')
        f.rows('ALTER TABLE pg_temp.schema_ci_channels RENAME COLUMN title TO schema_ci_title; ALTER TABLE pg_temp.schema_ci_channels RENAME COLUMN avatar_url TO title; ALTER TABLE pg_temp.schema_ci_channels RENAME COLUMN schema_ci_title TO avatar_url')
        f.check(f.scalar(guard,_SCHEMA_PARAMETERS) is False,'channel_catalog_live_order_rejected')
        f.rows('ALTER TABLE pg_temp.schema_ci_channels RENAME COLUMN title TO schema_ci_avatar; ALTER TABLE pg_temp.schema_ci_channels RENAME COLUMN avatar_url TO title; ALTER TABLE pg_temp.schema_ci_channels RENAME COLUMN schema_ci_avatar TO avatar_url')
        f.check(f.scalar(guard,_SCHEMA_PARAMETERS) is True,'channel_catalog_live_order_restored')
        f.rows('ALTER TABLE pg_temp.schema_ci_channels ALTER COLUMN title TYPE varchar(500)')
        f.check(f.scalar(guard,_SCHEMA_PARAMETERS) is False,'channel_catalog_live_type_rejected')
        f.rows('ALTER TABLE pg_temp.schema_ci_channels ALTER COLUMN title TYPE text')
        f.check(f.scalar(guard,_SCHEMA_PARAMETERS) is True,'channel_catalog_live_type_restored')
    f.check((f.catalog(),f.snapshot(),f.sequences())==before,'channel_catalog_clone_cleanup')


def negative_intrinsic_guard(f):
    channel_insert_catalog_guards(f)
    definition=f.scalar("SELECT pg_get_functiondef('public.guard_channel_user_work_identity()'::regprocedure)")
    changed=definition.replace('BEGIN','BEGIN\n-- schema-ci body mismatch',1)
    require(changed!=definition,'body_fault_was_applied')
    faults=(
      ("CREATE FUNCTION public.schema_ci_unexpected() RETURNS integer LANGUAGE sql AS 'SELECT 1'","DROP FUNCTION public.schema_ci_unexpected()"),
      ("ALTER TABLE public.channels DISABLE TRIGGER channels_user_work_identity_immutable","ALTER TABLE public.channels ENABLE TRIGGER channels_user_work_identity_immutable"),
      ("CREATE TRIGGER schema_ci_unexpected BEFORE UPDATE OF user_work_id ON public.channels FOR EACH ROW EXECUTE FUNCTION public.guard_channel_user_work_identity()","DROP TRIGGER schema_ci_unexpected ON public.channels"),
      (changed,definition),
      ("GRANT EXECUTE ON FUNCTION public.guard_channel_user_work_identity() TO service_role","REVOKE EXECUTE ON FUNCTION public.guard_channel_user_work_identity() FROM service_role"))
    for apply,undo in faults:
        with fault(f,apply,undo):
            expect_error(f,lambda:intrinsic_capabilities.assert_final_schema_ready(f.database),DatabaseError,code='P0001')
            for action in preflights(f):expect_error(f,action,DatabaseError,code='P0001')
            # Empty native UUID casts still precede readiness even on bad schema.
            expect_error(f,lambda:VideoCollectionRepository(f.database).assert_forced_metadata_claim_ready(worker_id='schema-ci',run_id='not-a-uuid'),DatabaseError,code='22P02')
        intrinsic_capabilities.assert_final_schema_ready(f.database)
    for fault_name,apply,undo,check in (
      ("structural_membership_table_accepted","ALTER TABLE public.video_inventory_memberships RENAME TO schema_ci_memberships","ALTER TABLE public.schema_ci_memberships RENAME TO video_inventory_memberships",capabilities.assert_typed_inventory_foundation_schema),
      ("structural_due_index_accepted","ALTER INDEX public.video_collection_state_format_due_idx RENAME TO schema_ci_due_idx","ALTER INDEX public.schema_ci_due_idx RENAME TO video_collection_state_format_due_idx",capabilities.assert_typed_video_collection_schema),
      ("structural_operator_scan_fk_accepted","ALTER TABLE public.operator_jobs RENAME CONSTRAINT operator_jobs_inventory_scan_fkey TO schema_ci_scan_fk","ALTER TABLE public.operator_jobs RENAME CONSTRAINT schema_ci_scan_fk TO operator_jobs_inventory_scan_fkey",capabilities.assert_inventory_switch_schema)):
        with fault(f,apply,undo):
            try:check(f.database)
            except DatabaseError as error:f.check(error.code=='P0001','structural_error_code')
            else:raise AssertionError(fault_name)
        check(f.database)
    with fault(f,"REVOKE SELECT ON public.channel_collection_state FROM service_role","GRANT SELECT ON public.channel_collection_state TO service_role"):
        for action in preflights(f):expect_error(f,action,DatabaseError,code='P0001')
    f.check(all(action() in (None,[]) for action in preflights(f)),'preflights_restored')
