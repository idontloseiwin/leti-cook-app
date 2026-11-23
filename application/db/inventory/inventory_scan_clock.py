"""Native scan freshness predicate shared by finite retention protocols.

The caller aliases its module-owned scan row/record as ``scan`` and binds the
native comparison clock as ``scan_at``. No function call, Python clock cast,
identifier interpolation or user-supplied SQL enters this fixed expression.
"""

SCAN_IS_HOT_SQL = """coalesce(
 isfinite(%(scan_at)s::timestamptz)
 AND isfinite(scan.started_at)
 AND scan.started_at BETWEEN %(scan_at)s::timestamptz-interval '696 hours' AND %(scan_at)s::timestamptz
 AND isfinite(scan.scan_boundary_at)
 AND scan.scan_boundary_at BETWEEN %(scan_at)s::timestamptz-interval '696 hours' AND %(scan_at)s::timestamptz
 AND ((isfinite(scan.source_checked_at)
       AND scan.source_checked_at BETWEEN scan.scan_boundary_at AND %(scan_at)s::timestamptz)
   OR (scan.source_checked_at IS NULL AND scan.status='running' AND scan.coverage='partial'
       AND scan.pages_completed=0 AND scan.items_seen=0 AND scan.new_memberships_found=0
       AND scan.resume_page_token IS NULL AND scan.last_transition_id IS NULL AND scan.completed_at IS NULL))
 AND (scan.status<>'completed' OR (isfinite(scan.completed_at)
       AND scan.completed_at BETWEEN %(scan_at)s::timestamptz-interval '696 hours' AND %(scan_at)s::timestamptz)),false)"""
