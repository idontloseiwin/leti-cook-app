"""Exact current SQL/Python dispatch; missing cases or zero assertions fail closed."""
from . import cases_retention as r,cases_jobs as j,cases_user_work as u,cases_policy as p,cases_readiness as c
CASES={
'availability_recheck':r.availability_recheck,'restore_merge':u.restore_merge,'cadence_policy':p.cadence_policy,
'playlist_absence':r.playlist_absence,'quota_phase_labels':r.quota_phase_labels,'removal_stops_job':j.removal_stops_job,
'removed_job_scan_cleanup':j.removed_job_scan_cleanup,'failed_job_backoff':j.failed_job_backoff,'retention_scrub':r.retention_scrub,
'detached_reviews':r.detached_reviews,'operational_retention':r.operational_retention,'inventory_window':r.inventory_window,
'saved_noop_revision':u.saved_noop_revision,'folder_label_management':u.folder_label_management,
'runtime_readiness':c.runtime_readiness,'negative_intrinsic_guard':c.negative_intrinsic_guard,
'trashed_channel_removal':r.trashed_channel_removal,'inventory_queue_seek':r.inventory_queue_seek,
'receipt_scan_index':r.receipt_scan_index,
'growth_window_from_last_snapshot':r.growth_window_from_last_snapshot,
'claim_window_skips_leases':j.claim_window_skips_leases,
'user_work_limit_race':u.user_work_limit_race,
'evidence_page_locks':r.evidence_page_locks,
'current_source_page_locks':r.current_source_page_locks}
