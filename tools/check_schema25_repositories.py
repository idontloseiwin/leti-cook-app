#!/usr/bin/env python3
"""Run one fail-closed schema25 CI suite in an explicitly disposable database.

Not a production tool. Requires the suite-owned loopback DSNs and explicit
15000-row authorization, covering unchanged 13002-row and 3001-row fixtures.
There is deliberately no single-case/skip switch in this CI acceptance runner.
"""
from pathlib import Path
import json,os,sys,traceback
ROOT=Path(__file__).resolve().parents[1]
sys.dont_write_bytecode=True
sys.path[:0]=[str(ROOT/'application'),str(ROOT/'tests'),str(ROOT)]
from schema25_regressions.manifest import load,require,validate_dispatch
from schema25_regressions.fixtures import Fixture,admission,dsns
from schema25_regressions.suite import CASES
from psycopg.pq import TransactionStatus


# Static assertion labels extracted from this frozen suite; never echo an
# arbitrary exception string, source line, payload, connection string or URL.
_STATIC_CHECK_CODES=frozenset(('absent_lists_retained', 'absent_remove_noop', 'actual_empty_preflights', 'actual_schedule_owner', 'actual_schedule_row', 'add_two', 'admin_role', 'attached_visibility', 'availability_all_order', 'availability_class', 'availability_stale_order', 'backoff_chunk', 'backoff_claim', 'bind_replay', 'blocked_gate_no_writes', 'body_fault_was_applied', 'cadence_count', 'case_binding', 'case_contract', 'case_fixture_duplicate', 'case_fixture_missing', 'case_name', 'channel_catalog_clone_cleanup', 'channel_catalog_clone_matches', 'channel_catalog_live_order_rejected', 'channel_catalog_live_order_restored', 'channel_catalog_live_type_rejected', 'channel_catalog_live_type_restored', 'channel_removed', 'ci_owned_required', 'closed_hot_control_when_blocked', 'conflict_no_writes', 'created_channel', 'custom_old_name', 'cycle_avoided', 'cycle_import', 'cycle_label_preserved', 'cycle_merge', 'deleted_preset_import', 'detached_visibility', 'dsn_identity', 'dsn_options', 'duplicate_noop', 'early_claim_denied', 'empty_live_state_count', 'empty_python_case', 'error_code', 'error_rows_unchanged', 'exact_dispatch', 'exact_readiness_inventory', 'exact_receipts', 'exhausted_not_retried', 'expired_removed', 'fault_restored_exactly', 'finite_pages', 'first_window_incomplete', 'fixture_consumers', 'fixture_digest', 'fixture_escape', 'fixture_json_only', 'fixture_kind', 'fixture_manifest_coverage', 'fixture_missing', 'fixture_name', 'fixture_path', 'fixture_restore_exact', 'fixture_row_budget', 'fresh_seed_bound', 'hot_ceiling', 'hot_contract', 'initial_claim', 'intrinsic_bodies', 'job_completed', 'job_scan_detached', 'job_suspended', 'large_count_guard', 'large_fixture_exact', 'late_release_cannot_reopen', 'legacy_seed_call', 'linked_scan_deferred', 'long_due', 'long_invalid_clock', 'long_native_fractional_boundary', 'long_selected_row', 'manifest_version', 'merge_owned_state', 'native_dst_boundary', 'no_active_removed_job', 'no_status_copy', 'nonempty_readiness_report', 'null_youngest', 'one_member', 'one_scrub', 'operational_receipt', 'page_work_bound', 'pending_removed', 'pg17_required', 'playlist_released', 'preflights_restored', 'preset_stays_deleted', 'python_manifest_count', 'python_row_bound', 'quota_finish', 'quota_label', 'quota_persisted_labels', 'quota_start', 'raw_fixture_only', 'readiness_did_not_write', 'readiness_fields', 'ready_flag', 'real_remove', 'receipt_replay', 'recent_preserved', 'renamed_import', 'renamed_preset', 'restore_import', 'retry_delay', 'review_scrub_rows', 'runtime_role', 'scalar_shape', 'scan_eventually_removed', 'scan_has_no_source_clock', 'schema_digest', 'scrub_idempotent', 'sequence_inventory', 'small_estimate', 'sql_digest', 'sql_escape', 'sql_manifest_count', 'sql_manifest_coverage', 'sql_missing', 'sql_path', 'sql_row_bound', 'stale_conflict', 'stream_end_anchor', 'structural_due_index_accepted', 'structural_error_code', 'structural_membership_table_accepted', 'structural_operator_scan_fk_accepted', 'suite_deadline', 'table_inventory', 'target_attempt_bound', 'tracked_merge', 'twelve_by_nine', 'typed_age_at_observation', 'typed_anchor_edges', 'typed_bad_source_renews_now', 'typed_native_jitter', 'unavailable_ineligible_drift', 'unknown_size_guard', 'window_pages_and_deletions', 'window_survivors'))
def safe_failure(error):
    code=error.args[0] if isinstance(error,AssertionError) and len(error.args)==1 and isinstance(error.args[0],str) and error.args[0] in _STATIC_CHECK_CODES else None
    frames=[]
    for frame in traceback.extract_tb(error.__traceback__):
        try:relative=Path(frame.filename).resolve().relative_to(ROOT).as_posix()
        except ValueError:continue
        frames.append((relative,frame.lineno))
    source=next((f for f in reversed(frames) if f[0].startswith('tests/schema25_regressions/cases_')),frames[-1] if frames else (None,None))
    return {'check_code':code,'source':source[0],'line':source[1]}

def execute_python_case(fixture,case):
    before=fixture.checks
    case(fixture)
    # The budget observation is also an assertion; it must not make an empty
    # domain case appear to have verified application behavior.
    require(fixture.checks>before,'empty_python_case')
    fixture.observe()


def main():
    manifest=load();validate_dispatch(manifest,CASES)
    require(os.getenv('LIC_SCHEMA_CI_MAX_ROWS')=='15000','large_fixture_authorization_required')
    names=[];stage='identity';fixture=None
    try:
        with admission() as gate:
            fixture=Fixture(*dsns(),gate)
            try:
                for name,entry in manifest['sql'].items():
                    stage='sql:'+name;fixture.budget=entry['row_budget']
                    try:
                        fixture.rows((ROOT/'tests/sql'/name).read_text())
                        require(fixture.admin.info.transaction_status==TransactionStatus.IDLE,'sql_fixture_must_rollback')
                        require(fixture.snapshot()==fixture.baseline,'sql_rollback_rows')
                    finally:
                        fixture.admin.rollback();fixture.reset()
                    names.append(stage);print(json.dumps({'case':stage,'status':'pass','row_budget':entry['row_budget']}),flush=True)
                for name,entry in manifest['python'].items():
                    stage='python:'+name;fixture.budget=entry['row_budget'];before=fixture.checks
                    try:
                        execute_python_case(fixture,CASES[name])
                    finally:fixture.reset()
                    names.append(stage);print(json.dumps({'case':stage,'status':'pass','checks':fixture.checks-before,'row_budget':entry['row_budget']}),flush=True)
                stage='final_identity';fixture.identity()
                require(len(names)==26 and len(set(names))==26,'complete_suite_required')
            finally:fixture.close()
    except Exception as error:
        # No raw server messages, source payloads or DSNs reach CI output.
        code=getattr(error,'code',None)
        safe=code if isinstance(code,str) and code.isalnum() and len(code)<=5 else None
        print(json.dumps({'stage':stage,'status':'fail','error_type':type(error).__name__,'sqlstate':safe,**safe_failure(error)}),flush=True)
        return 1
    print(json.dumps({'status':'pass','sql':10,'python':16,'schema_version':manifest['schema_version'],'cleanup':'verified'}));return 0


if __name__=='__main__':raise SystemExit(main())
