"""Bounded validation of copied chain evidence; no dispatch or acknowledgement.

Initializer owns channel/parent and takes source locks before copying. Replay
uses current committed copies and validates native source clocks without requiring
the historical parent lease to remain active. Both paths remain source readers.
"""
from psycopg.types.json import Jsonb
from ..base.postgres import DatabaseServerError
from ..inventory.inventory_scan_clock import SCAN_IS_HOT_SQL
from ..inventory.inventory_scan_steps import _hot
from .operator_job_inventory_evidence import _NativeObject
from .operator_job_scope import assert_full_initial_scope

_JOB = "SELECT to_jsonb(j)::text AS value FROM public.operator_jobs j WHERE id=%s::uuid"
_CHAINS = """SELECT to_jsonb(c)::text AS value FROM public.operator_job_inventory_chains c
 WHERE job_id=%s::uuid ORDER BY video_type"""
_PLAYLIST_LOCKS = """SELECT video_type FROM public.channel_playlist_state
 WHERE channel_id=%s::text AND video_type=ANY(ARRAY['long','short','stream']::text[])
 ORDER BY video_type FOR UPDATE"""
_SCAN_LOCKS = """SELECT scan_id FROM public.channel_video_inventory_scans
 WHERE scan_id=ANY(%s::uuid[]) ORDER BY scan_id FOR UPDATE"""
_RECEIPT_LOCKS = """SELECT r.receipt_scope,r.transition_id FROM public.typed_inventory_receipts r
 JOIN jsonb_to_recordset(%s::jsonb) k(scope text,transition uuid)
 ON r.receipt_scope=k.scope AND r.transition_id=k.transition
 ORDER BY r.receipt_scope,r.transition_id FOR UPDATE OF r"""
_SCAN = """SELECT scan.channel_id=%(channel)s::text AND scan.video_type=%(kind)s::text
 AND scan.scan_kind IN('initial_inventory','full_reconciliation')
 AND scan.started_at>=%(requested_at)s::timestamptz
 AND scan.scan_boundary_at>=%(requested_at)s::timestamptz
 AND """ + SCAN_IS_HOT_SQL.replace('%(scan_at)s::timestamptz','native.at') + """ AS valid
 FROM public.channel_video_inventory_scans scan
 CROSS JOIN (SELECT clock_timestamp() AS at OFFSET 0) native WHERE scan.scan_id=%(scan)s::uuid"""
_RECEIPT = """SELECT to_jsonb(r)::text AS value FROM public.typed_inventory_receipts r
 WHERE receipt_scope=%s::text AND transition_id=%s::uuid"""
_RECEIPT_VALID = """SELECT
 NOT(r.result ? 'source_expired') AND isfinite(r.source_checked_at)
 AND r.source_checked_at BETWEEN clock_timestamp()-interval '696 hours' AND clock_timestamp()
 AND CASE WHEN r.receipt_scope='quota_defer' THEN
  r.scan_id IS NULL AND r.transition_kind IS NULL
  AND r.claims=jsonb_build_array(jsonb_build_object('channel_id',%(channel)s::text,
   'video_type',%(kind)s::text,'lease_token',%(playlist_lease)s::text))
  AND r.result->>'released'='1' AND r.result->>'rejected'='0'
  AND (%(outcome)s::text IS NULL OR %(outcome)s::text IN('quota','unknown'))
 ELSE r.scan_id=%(scan)s::uuid AND r.result->>'scan_id'=%(scan)s::text
  AND r.result->>'channel_id'=%(channel)s::text AND r.result->>'video_type'=%(kind)s::text
  AND r.result->>'lease_token'=%(playlist_lease)s::text
  AND r.result->>'transition_id'=%(transition)s::text
  AND r.transition_kind IN('page','failure')
  AND (%(outcome)s::text IS NULL OR %(outcome)s::text='unknown'
   OR (%(outcome)s::text='page' AND r.transition_kind='page')
   OR (%(outcome)s::text IN('error','playlist_not_found') AND r.transition_kind='failure'
    AND r.result->>'outcome'=%(outcome)s::text AND r.result->>'error_code'=%(error_code)s::text))
 END AS valid
 FROM jsonb_populate_record(NULL::public.typed_inventory_receipts,%(receipt)s::jsonb) r"""


def _fail(code='initial_inventory_outcome_unresolved'):
    raise DatabaseServerError(code, code='P0001')


def _copies(tx, job, scalar, *, lock=False):
    if not scalar:
        rows = tx.fetch_all(_CHAINS + (' FOR UPDATE' if lock else ''), (job['id'],), max_rows=3)
        copies = [_NativeObject(row['value']) for row in rows]
        if len(copies) != 3 or {row['video_type'] for row in copies} != {'long','short','stream'}:
            _fail('initial_inventory_chain_state_invalid')
        return copies
    sources = job['summary_json'].get('full_inventory_sources', {})
    ack = job['summary_json'].get('inventory_ack', {})
    return [dict(channel_id=job['channel_id'], video_type=kind,
        scan_id=(job['inventory_scan_id'] or sources.get(kind, {}).get('scan_id'))
            if kind == job['inventory_video_type'] else sources.get(kind, {}).get('scan_id'),
        checkpoint=job['inventory_checkpoint'] if kind == job['inventory_video_type'] else {},
        last_ack=ack if ack.get('video_type') == kind else {},
        terminal_outcome=sources.get(kind, {}).get('terminal_outcome'))
        for kind in ('long','short','stream')]


def _references(copies, job):
    scans, receipts = set(), set()
    for copy in copies:
        ack, checkpoint = copy['last_ack'], copy['checkpoint']
        scans.update(value for value in (copy['scan_id'], ack.get('scan_id')) if value is not None)
        if checkpoint.get('phase') == 'bound':
            receipts.update((scope,checkpoint['transition_id']) for scope in ('scan_transition','quota_defer'))
        if ack and ack.get('outcome') not in ('none','deferred'):
            receipts.add(('quota_defer' if ack.get('outcome') == 'quota' else 'scan_transition',ack['transition_id']))
        proof = job['summary_json'].get('full_inventory_sources', {}).get(copy['video_type'], {})
        if proof.get('last_transition_id'):
            receipts.add(('scan_transition',proof['last_transition_id']))
    if len(scans)>6 or len(receipts)>12:
        _fail('initial_inventory_chain_state_invalid')
    return sorted(scans), [dict(scope=scope,transition=transition) for scope,transition in sorted(receipts)]


def _validate_copy(tx, job, copy, checked_scans):
    kind = copy['video_type']
    if copy['channel_id'] != job['channel_id']:
        _fail('initial_inventory_chain_state_invalid')
    proof = job['summary_json'].get('full_inventory_sources', {}).get(kind, {})
    if copy['terminal_outcome'] != proof.get('terminal_outcome'):
        _fail('initial_inventory_chain_state_invalid')
    if copy['scan_id'] != proof.get('scan_id'):
        _fail('initial_inventory_chain_state_invalid')
    for scan in (copy['scan_id'],copy['last_ack'].get('scan_id')):
        if scan is None or (scan,kind) in checked_scans:
            continue
        checked_scans.add((scan,kind))
        facts = tx.fetch_one(_SCAN, dict(scan=scan,kind=kind,channel=job['channel_id'],
                                       requested_at=job['requested_at']))
        if facts is None or facts['valid'] is not True:
            _fail('initial_inventory_receipt_expired')
    contexts = []
    checkpoint, ack = copy['checkpoint'], copy['last_ack']
    if checkpoint.get('phase') == 'bound':
        contexts.append((checkpoint,copy['scan_id'],None,False))
    if ack and ack.get('outcome') not in ('none','deferred'):
        contexts.append((ack,ack['scan_id'],ack['outcome'],ack['outcome'] != 'unknown'))
    for context, scan, outcome, required in contexts:
        found = 0
        for scope in ('scan_transition','quota_defer'):
            row = tx.fetch_one(_RECEIPT,(scope,context['transition_id']))
            if row is None:
                continue
            found += 1
            values = dict(receipt=row['value'],channel=job['channel_id'],kind=kind,scan=scan,
                transition=context['transition_id'],playlist_lease=context.get('playlist_lease_token'),
                outcome=outcome,error_code=context.get('error_code'))
            if tx.fetch_one(_RECEIPT_VALID,values)['valid'] is not True:
                _fail('initial_inventory_receipt_expired')
        if found>1 or (required and found!=1):
            _fail()


def validate_inventory_chain_copies(tx, job_id, *, scalar=False, lock_sources=False):
    """Return current proved parent/children; caller owns any mutation locks."""
    with tx.borrow():
        row = tx.fetch_one(_JOB,(job_id,))
        if row is None:
            _fail('initial_inventory_chain_state_invalid')
        job = _NativeObject(row['value'])
        if job.get('initial_scope_version') != 1:
            _fail('full_initial_scope_invalid')
        marker = job['summary_json'].get('inventory_chain_version')
        if (scalar and marker is not None) or (not scalar and (type(marker) is not int or marker!=1)):
            _fail('initial_inventory_chain_state_invalid')
        assert_full_initial_scope(tx,job,False)
        copies = _copies(tx,job,scalar,lock=lock_sources)
        scans,receipts = _references(copies,job)
        if lock_sources:
            # Parent (and any existing children) are already locked by caller.
            tx.fetch_all(_PLAYLIST_LOCKS,(job['channel_id'],),max_rows=3)
            tx.fetch_all(_SCAN_LOCKS,(scans,),max_rows=6)
            tx.fetch_all(_RECEIPT_LOCKS,(Jsonb(receipts),),max_rows=12)
            row = tx.fetch_one(_JOB,(job_id,))
            if row is None:
                _fail('initial_inventory_chain_state_invalid')
            job = _NativeObject(row['value'])
            assert_full_initial_scope(tx,job,False)
            copies = _copies(tx,job,scalar)
            if _references(copies,job) != (scans,receipts):
                _fail('initial_inventory_chain_state_invalid')
        checked_scans = set()
        for copy in copies:
            _validate_copy(tx,job,copy,checked_scans)
        # Check the immutable parent copy clock at the last reader boundary.
        _hot(tx,job['requested_at'])
        return job,copies


def validate_inventory_chain_replay(tx, args, *, ack):
    """Exact historical identity is evidence only, never fresh dispatch."""
    job,copies = validate_inventory_chain_copies(tx,args['job_id'])
    copy = next((value for value in copies if value['video_type']==args['video_type']),None)
    if copy is None:
        _fail('initial_inventory_chain_state_invalid')
    if ack:
        value = copy['last_ack']
        if value.get('transition_id')!=args['transition'] or value.get('job_lease_token')!=args['lease_token']:
            _fail()
        return value
    expected = dict(phase='bound',job_lease_token=args['lease_token'],transition_id=args['transition'],
        reserved_units=1,playlist_lease_token=args['playlist_lease_token'])
    if copy['scan_id']!=args['scan_id'] or copy['checkpoint']!=expected:
        _fail()
    return None
