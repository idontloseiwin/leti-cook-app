"""Finite cleanup of playlist state, scan copies and inventory receipts.

Borrowed transaction only; the closed driver entry owns admission and deadlines.
Native PostgreSQL predicates/clocks/UUID ordering are preserved. Python owns
three fixed protocols and per-candidate savepoints; no retained business call,
provider work, second connection, arbitrary SQL or retry is used.
"""
from ..inventory.inventory_scan_clock import SCAN_IS_HOT_SQL
from ..base.postgres import DatabaseServerError
from psycopg.types.json import Jsonb
from . import inventory_chain_retention as chains

ERRORS=frozenset({'hot_inventory_copies_target_invalid','hot_inventory_copies_limit_invalid','hot_inventory_copies_cursor_invalid'})
TARGETS=frozenset({'playlist_state','inventory_scans','inventory_receipts'})
_HEADER="""WITH native AS(SELECT %s::text AS target,%s::text AS cursor,%s::integer AS limit),
 clock AS MATERIALIZED(SELECT clock_timestamp() AS at)
 SELECT native.*,length(cursor) AS cursor_length,to_jsonb(at)#>>'{}' AS at,
 to_jsonb(at-interval '696 hours')#>>'{}' AS cutoff FROM native CROSS JOIN clock"""
# A receipt has one expiry clock: its source clock, or, once marked source_expired,
# its creation (a page-token hash left on a marked receipt means work at once).
_RECEIPT_CLOCK="""CASE WHEN r.result ? 'source_expired' THEN CASE WHEN r.next_page_token_hash IS NOT NULL
 THEN '-infinity'::timestamptz ELSE coalesce(r.created_at,'-infinity'::timestamptz) END
 ELSE coalesce(r.source_checked_at,'-infinity'::timestamptz) END"""
# A scan is not hot (SCAN_IS_HOT_SQL) exactly when its earliest window clock is past
# the cutoff, its latest clock is in the future, or a rule that does not depend on
# the time fails: a source clock before the scan boundary, or no source clock on a
# scan that is not an untouched running one. Each branch has an index.
_SCAN_EARLIEST="""least(coalesce(s.started_at,'-infinity'::timestamptz),coalesce(s.scan_boundary_at,'-infinity'::timestamptz),
 CASE WHEN s.status='completed' THEN coalesce(s.completed_at,'-infinity'::timestamptz) ELSE 'infinity'::timestamptz END)"""
_SCAN_LATEST="""greatest(coalesce(s.started_at,'-infinity'::timestamptz),coalesce(s.scan_boundary_at,'-infinity'::timestamptz),
 coalesce(s.source_checked_at,'-infinity'::timestamptz),
 CASE WHEN s.status='completed' THEN coalesce(s.completed_at,'-infinity'::timestamptz) ELSE '-infinity'::timestamptz END)"""
_SCAN_STATIC="""(CASE WHEN s.source_checked_at IS NULL THEN NOT coalesce(s.status='running' AND s.coverage='partial'
 AND s.pages_completed=0 AND s.items_seen=0 AND s.new_memberships_found=0 AND s.resume_page_token IS NULL
 AND s.last_transition_id IS NULL AND s.completed_at IS NULL,false)
 ELSE s.source_checked_at<coalesce(s.scan_boundary_at,'infinity'::timestamptz) END)"""
_PAGE={
 'playlist_state':"""SELECT channel_id AS a,video_type AS b,jsonb_build_array(channel_id,video_type)::text AS cursor
 FROM public.channel_playlist_state WHERE %(after_a)s::text IS NULL OR (channel_id,video_type)>(%(after_a)s::text,%(after_b)s::text)
 ORDER BY a,b LIMIT %(limit)s::integer""",
 'inventory_scans':"""SELECT a,b,cursor FROM (
 (SELECT 0 AS branch,s.scan_id::text AS a,''::text AS b,"""+_SCAN_EARLIEST+""" AS clock,s.scan_id AS key FROM public.channel_video_inventory_scans s
 WHERE """+_SCAN_EARLIEST+"""<%(cutoff)s::timestamptz AND (%(after_branch)s::integer IS NULL OR (%(after_branch)s::integer=0
 AND ("""+_SCAN_EARLIEST+""",s.scan_id)>(%(after_clock)s::timestamptz,%(after_a)s::uuid)))
 ORDER BY """+_SCAN_EARLIEST+""",s.scan_id LIMIT %(limit)s::integer)
 UNION ALL
 (SELECT 1,s.scan_id::text,'',"""+_SCAN_LATEST+""",s.scan_id FROM public.channel_video_inventory_scans s
 WHERE """+_SCAN_LATEST+""">%(at)s::timestamptz AND """+_SCAN_EARLIEST+""">=%(cutoff)s::timestamptz
 AND (%(after_branch)s::integer IS NULL OR %(after_branch)s::integer=0 OR (%(after_branch)s::integer=1
 AND ("""+_SCAN_LATEST+""",s.scan_id)>(%(after_clock)s::timestamptz,%(after_a)s::uuid)))
 ORDER BY """+_SCAN_LATEST+""",s.scan_id LIMIT %(limit)s::integer)
 UNION ALL
 (SELECT 2,s.scan_id::text,'',NULL::timestamptz,s.scan_id FROM public.channel_video_inventory_scans s
 WHERE """+_SCAN_STATIC+""" AND """+_SCAN_EARLIEST+""">=%(cutoff)s::timestamptz AND """+_SCAN_LATEST+"""<=%(at)s::timestamptz
 AND (%(after_branch)s::integer IS NULL OR %(after_branch)s::integer<2 OR s.scan_id>%(after_a)s::uuid)
 ORDER BY s.scan_id LIMIT %(limit)s::integer)) page
 CROSS JOIN LATERAL (SELECT jsonb_build_array(page.branch,to_jsonb(page.clock)#>>'{}',page.a)::text AS cursor) c
 ORDER BY page.branch,page.clock,page.key LIMIT %(limit)s::integer""",
 'inventory_receipts':"""SELECT a,b,cursor FROM (
 (SELECT r.receipt_scope AS a,r.transition_id::text AS b,"""+_RECEIPT_CLOCK+""" AS clock FROM public.typed_inventory_receipts r
 WHERE """+_RECEIPT_CLOCK+"""<%(cutoff)s::timestamptz AND (%(after_clock)s::timestamptz IS NULL
 OR ("""+_RECEIPT_CLOCK+""",r.receipt_scope,r.transition_id)>(%(after_clock)s::timestamptz,%(after_a)s::text,%(after_b)s::uuid))
 ORDER BY """+_RECEIPT_CLOCK+""",r.receipt_scope,r.transition_id LIMIT %(limit)s::integer)
 UNION ALL
 (SELECT r.receipt_scope,r.transition_id::text,"""+_RECEIPT_CLOCK+""" FROM public.typed_inventory_receipts r
 WHERE """+_RECEIPT_CLOCK+""">%(at)s::timestamptz AND (%(after_clock)s::timestamptz IS NULL
 OR ("""+_RECEIPT_CLOCK+""",r.receipt_scope,r.transition_id)>(%(after_clock)s::timestamptz,%(after_a)s::text,%(after_b)s::uuid))
 ORDER BY """+_RECEIPT_CLOCK+""",r.receipt_scope,r.transition_id LIMIT %(limit)s::integer)) page
 CROSS JOIN LATERAL (SELECT jsonb_build_array(to_jsonb(page.clock)#>>'{}',page.a,page.b)::text AS cursor) c
 ORDER BY page.clock,page.a,page.b LIMIT %(limit)s::integer""",
}
_STATE="""SELECT to_jsonb(s)::text AS value,s.active_scan_id::text AS active_scan_id,
 s.last_completed_scan_id::text AS last_completed_scan_id
 FROM public.channel_playlist_state s WHERE s.channel_id=%(a)s AND s.video_type=%(b)s FOR UPDATE NOWAIT"""
_SCAN="""SELECT to_jsonb(scan)::text AS value,scan.channel_id,scan.video_type,
 to_jsonb(least(scan.started_at,scan.source_checked_at))#>>'{}' AS value_clock,
 """+SCAN_IS_HOT_SQL+""" AS hot FROM public.channel_video_inventory_scans scan WHERE scan.scan_id=%(scan_id)s::uuid"""
_PLAYLIST_FACTS="""WITH state AS(SELECT * FROM jsonb_populate_record(NULL::public.channel_playlist_state,%(state)s::jsonb)),
 active AS(SELECT scan.*,"""+SCAN_IS_HOT_SQL+""" AS hot FROM public.channel_video_inventory_scans scan JOIN state ON scan.scan_id=state.active_scan_id),
 completed AS(SELECT scan.*,"""+SCAN_IS_HOT_SQL+""" AS hot FROM public.channel_video_inventory_scans scan JOIN state ON scan.scan_id=state.last_completed_scan_id),
 facts AS(SELECT state.*,
 coalesce(a.scan_id IS NOT NULL AND a.channel_id=state.channel_id AND a.video_type=state.video_type
 AND a.playlist_id=state.playlist_id AND a.status='running' AND a.coverage='partial' AND a.token_history_verified AND a.hot,false) AS active_valid,
 CASE WHEN state.last_completed_scan_id IS NULL AND state.inventory_state_origin='legacy_long_backfill' AND state.inventory_status='completed'
 THEN coalesce(isfinite(state.source_checked_at) AND state.source_checked_at BETWEEN %(cutoff)s::timestamptz AND %(scan_at)s::timestamptz
 AND isfinite(state.inventory_completed_at) AND state.inventory_completed_at BETWEEN %(cutoff)s::timestamptz AND %(scan_at)s::timestamptz,false)
 ELSE coalesce(c.scan_id IS NOT NULL AND c.channel_id=state.channel_id AND c.video_type=state.video_type AND c.playlist_id=state.playlist_id
 AND c.status='completed' AND c.coverage='complete' AND c.scan_kind IN('initial_inventory','full_reconciliation') AND c.hot,false) END AS completed_valid,
 coalesce(isfinite(state.source_checked_at) AND state.source_checked_at BETWEEN %(cutoff)s::timestamptz AND %(scan_at)s::timestamptz,false) AS source_valid,
 to_jsonb(least(state.source_checked_at,a.started_at,c.started_at))#>>'{}' AS value_clock
 FROM state LEFT JOIN active a ON true LEFT JOIN completed c ON true)
 SELECT active_valid,completed_valid,source_valid,value_clock,
 (active_scan_id IS NULL OR active_valid)
 AND ((last_completed_scan_id IS NULL AND inventory_status<>'completed' AND NOT product_exposure_ready AND expected_item_count IS NULL) OR completed_valid)
 AND (source_valid OR (source_checked_at IS NULL AND expected_item_count IS NULL AND inventory_completed_at IS NULL
 AND last_completed_scan_id IS NULL AND (active_valid OR (active_scan_id IS NULL AND resume_page_token IS NULL AND resume_started_at IS NULL AND scan_boundary_at IS NULL)))) AS noop
 FROM facts"""
# This post-refusal observation uses one committed snapshot, never a write lock.
# Reuse the same proof predicate with an exact key and a fresh native clock.
_PLAYLIST_OBSERVATION=_PLAYLIST_FACTS.replace(
    'WITH state AS(SELECT * FROM jsonb_populate_record(NULL::public.channel_playlist_state,%(state)s::jsonb))',
    "WITH clock AS MATERIALIZED(SELECT clock_timestamp() AS at), state AS MATERIALIZED(SELECT * FROM public.channel_playlist_state WHERE channel_id=%(a)s AND video_type=%(b)s)"
).replace('%(scan_at)s::timestamptz','(SELECT at FROM clock)').replace(
    'SELECT active_valid,completed_valid,source_valid,value_clock,',
    "SELECT active_valid,completed_valid,source_valid,value_clock,(SELECT to_jsonb(at)#>>'{}' FROM clock) AS upper_at,")
_LEASE="""SELECT lease_token IS NOT NULL AND lease_expires_at>clock_timestamp() AS busy
 FROM jsonb_populate_record(NULL::public.channel_playlist_state,%(state)s::jsonb) state"""
_FAIL_SCAN="""UPDATE public.channel_video_inventory_scans s SET status='failed',coverage='partial',completed_at=NULL,resume_page_token=NULL,
 items_seen=NULL,new_memberships_found=NULL,token_history_verified=false,error_code='hot_source_expired',error_at=%(scan_at)s::timestamptz
 WHERE s.scan_id=%(scan_id)s::uuid"""
_SCRUB_STATE="""UPDATE public.channel_playlist_state s SET
 active_scan_id=CASE WHEN %(active_valid)s THEN s.active_scan_id END,
 resume_page_token=CASE WHEN %(active_valid)s THEN s.resume_page_token END,
 resume_started_at=CASE WHEN %(active_valid)s THEN s.resume_started_at END,
 scan_boundary_at=CASE WHEN %(active_valid)s THEN s.scan_boundary_at END,
 last_completed_scan_id=CASE WHEN %(completed_valid)s THEN s.last_completed_scan_id END,
 inventory_completed_at=CASE WHEN %(completed_valid)s THEN s.inventory_completed_at END,
 expected_item_count=CASE WHEN %(completed_valid)s THEN s.expected_item_count END,
 product_exposure_ready=%(completed_valid)s AND s.product_exposure_ready,
 inventory_status=CASE WHEN %(completed_valid)s THEN 'completed' WHEN %(active_valid)s THEN 'running'
 WHEN %(source_valid)s AND s.inventory_status='playlist_not_found' THEN s.inventory_status ELSE 'pending' END,
 source_checked_at=CASE WHEN %(source_valid)s THEN s.source_checked_at END,
 status=CASE WHEN s.runtime_enabled THEN 'ready' ELSE 'paused' END,
 next_due_at=CASE WHEN s.runtime_enabled THEN coalesce(s.next_due_at,%(scan_at)s::timestamptz) ELSE s.next_due_at END,
 lease_token=NULL,lease_owner=NULL,lease_run_id=NULL,lease_expires_at=NULL,lease_channel_epoch=NULL,
 discovery_started_at=CASE WHEN %(active_valid)s THEN s.discovery_started_at END,
 last_result=CASE WHEN %(source_valid)s THEN s.last_result ELSE 'idle' END,
 updated_at=%(scan_at)s::timestamptz,inventory_updated_at=%(scan_at)s::timestamptz
 WHERE s.channel_id=%(a)s AND s.video_type=%(b)s"""
_RECEIPT="""SELECT r.scan_id::text AS scan_id,r.next_page_token_hash IS NOT NULL AS has_hash,
 r.result ? 'source_expired' AS expired_marker,
 coalesce(isfinite(r.created_at) AND r.created_at BETWEEN %(cutoff)s::timestamptz AND %(scan_at)s::timestamptz,false) AS created_hot,
 coalesce(isfinite(r.source_checked_at) AND r.source_checked_at BETWEEN %(cutoff)s::timestamptz AND %(scan_at)s::timestamptz,false) AS source_hot,
 to_jsonb(r.source_checked_at)#>>'{}' AS value_clock
 FROM public.typed_inventory_receipts r WHERE r.receipt_scope=%(a)s AND r.transition_id=%(b)s::uuid"""
_TELEMETRY="""WITH clocks AS(SELECT * FROM jsonb_to_recordset(%s::jsonb) c(value_clock timestamptz,upper_at timestamptz,proof_invalid boolean)),
 floor AS(SELECT %s::timestamptz AS cutoff)
 SELECT to_jsonb(min(value_clock) FILTER(WHERE isfinite(value_clock) AND value_clock<cutoff))#>>'{}' AS oldest,
 count(*) FILTER(WHERE value_clock IS NULL OR NOT isfinite(value_clock) OR value_clock>upper_at
 OR (proof_invalid AND value_clock>=cutoff)) AS invalid FROM clocks CROSS JOIN floor"""


def _reject(code):raise DatabaseServerError(code,code='P0001')
def _busy():raise DatabaseServerError('database_operation_failed',code='55P03')


def _cursor(tx,value,target):
    """(after_a, after_b, after_clock, after_branch) of the target's cursor."""
    if value is None:return None,None,None,None
    shape={'playlist_state':2,'inventory_scans':3,'inventory_receipts':3}[target]
    try:
        with tx.savepoint():
            row=tx.fetch_one("""WITH decoded AS(SELECT %s::jsonb AS value,%s::integer AS size)
             SELECT jsonb_typeof(value)='array' AND CASE WHEN jsonb_typeof(value)='array' THEN jsonb_array_length(value)=size
              AND CASE WHEN size=2 THEN jsonb_typeof(value->0)='string' AND jsonb_typeof(value->1)='string'
               WHEN jsonb_typeof(value->0)='number' THEN (value->>0) IN ('0','1','2') AND jsonb_typeof(value->1) IN ('string','null')
                AND jsonb_typeof(value->2)='string'
               ELSE jsonb_typeof(value->0)='string' AND jsonb_typeof(value->1)='string' AND jsonb_typeof(value->2)='string' END
             ELSE false END AS valid,
             value->>0 AS first,value->>1 AS second,value->>2 AS third FROM decoded""",(value,shape))
            if row['valid'] is not True:_reject('hot_inventory_copies_cursor_invalid')
            if target=='playlist_state':return row['first'],row['second'],None,None
            if target=='inventory_scans':
                branch=int(row['first'])
                if branch<2 and row['second'] is None:_reject('hot_inventory_copies_cursor_invalid')
                exact=tx.fetch_one("SELECT %s::uuid::text AS scan,to_jsonb(%s::timestamptz)#>>'{}' AS clock",(row['third'],row['second']))
                return exact['scan'],None,exact['clock'],branch
            exact=tx.fetch_one("SELECT to_jsonb(%s::timestamptz)#>>'{}' AS clock,%s::uuid::text AS transition",(row['first'],row['third']))
            return row['second'],exact['transition'],exact['clock'],None
    except DatabaseServerError as error:
        if error.code not in ('22P02','22007','22008'):raise
        _reject('hot_inventory_copies_cursor_invalid')


def _playlist(tx,p,step):
    state=tx.fetch_one(_STATE,p)
    if state is None:return 0,0,0
    tx.fetch_all('SELECT scan_id::text FROM public.channel_video_inventory_scans WHERE scan_id=ANY(%s::uuid[]) ORDER BY scan_id FOR UPDATE NOWAIT',
        ([state['active_scan_id'],state['last_completed_scan_id']],),max_rows=2)
    p={**p,'state':state['value']}
    facts=tx.fetch_one(_PLAYLIST_FACTS,p)
    if facts['noop'] is True:return 0,0,0
    step.update(value_clock=facts['value_clock'],clock_observed=True,proof_invalid=True)
    if tx.fetch_one(_LEASE,p)['busy'] is True:_busy()
    if any(chains.pinned_scan(tx,scan) for scan in (state['active_scan_id'],state['last_completed_scan_id'])):
        return 0,0,1
    if state['active_scan_id'] is not None and not facts['active_valid']:
        tx.execute(_FAIL_SCAN,{**p,'scan_id':state['active_scan_id']})
    tx.execute(_SCRUB_STATE,{**p,**{name:facts[name] for name in ('active_valid','completed_valid','source_valid')}})
    return 0,1,0


def _receipts(tx,p,step):
    receipt=tx.fetch_one(_RECEIPT,p)
    if receipt is None:return 0,0,0
    if (receipt['expired_marker'] and not receipt['has_hash'] and receipt['created_hot']) or (not receipt['expired_marker'] and receipt['source_hot']):return 0,0,0
    step.update(value_clock=receipt['value_clock'],clock_observed=True,proof_invalid=True)
    if receipt['scan_id'] is not None:
        scan=tx.fetch_one('SELECT channel_id,video_type FROM public.channel_video_inventory_scans WHERE scan_id=%s::uuid',(receipt['scan_id'],))
        if scan is not None:
            tx.fetch_all('SELECT channel_id FROM public.channel_playlist_state WHERE channel_id=%s AND video_type=%s FOR UPDATE NOWAIT',
                (scan['channel_id'],scan['video_type']),max_rows=1)
            tx.fetch_all('SELECT scan_id::text FROM public.channel_video_inventory_scans WHERE scan_id=%s::uuid FOR UPDATE NOWAIT',(receipt['scan_id'],),max_rows=1)
    elif tx.fetch_one('SELECT pg_try_advisory_xact_lock(hashtextextended(%s::text,781631247)) AS acquired',(p['b'],))['acquired'] is not True:_busy()
    receipt=tx.fetch_one(_RECEIPT+' FOR UPDATE NOWAIT',p)
    if receipt is None:return 0,0,0
    step.update(value_clock=receipt['value_clock'],clock_observed=True,proof_invalid=True)
    if not receipt['expired_marker'] and receipt['source_hot']:return 0,0,0
    if chains.pinned_receipt(tx,receipt['scan_id'],p['b']):return 0,0,1
    if receipt['has_hash']:
        tx.execute('UPDATE public.channel_video_inventory_scans SET token_history_verified=false WHERE scan_id=%s::uuid AND token_history_verified',(receipt['scan_id'],))
    if not receipt['created_hot']:
        tx.execute('DELETE FROM public.typed_inventory_receipts WHERE receipt_scope=%(a)s AND transition_id=%(b)s::uuid',p)
        return 1,0,0
    tx.execute("UPDATE public.typed_inventory_receipts SET claims=NULL,result='{\"source_expired\":true}'::jsonb,next_page_token_hash=NULL WHERE receipt_scope=%(a)s AND transition_id=%(b)s::uuid",p)
    return 0,1,0


def _scans(tx,p,step):
    p={**p,'scan_id':p['a']}
    scan=tx.fetch_one(_SCAN,p)
    if scan is None or scan['hot']:return 0,0,0
    step.update(value_clock=scan['value_clock'],clock_observed=True,proof_invalid=True)
    state=tx.fetch_one(_STATE,{**p,'a':scan['channel_id'],'b':scan['video_type']})
    if state is not None and tx.fetch_one(_LEASE,{'state':state['value']})['busy'] is True:_busy()
    scan=tx.fetch_one(_SCAN+' FOR UPDATE NOWAIT',p)
    if scan is None or scan['hot']:return 0,0,0
    step.update(value_clock=scan['value_clock'],clock_observed=True,proof_invalid=True)
    if chains.pinned_scan(tx,p['scan_id']):return 0,0,1
    scrubbed=tx.execute(_FAIL_SCAN+" AND (s.resume_page_token IS NOT NULL OR s.items_seen IS NOT NULL OR s.new_memberships_found IS NOT NULL OR s.token_history_verified OR s.status<>'failed' OR s.coverage<>'partial')",p)
    refs=tx.fetch_one("""SELECT (%(state)s::jsonb IS NOT NULL AND (
      (%(state)s::jsonb->>'active_scan_id')::uuid=%(scan_id)s::uuid OR (%(state)s::jsonb->>'last_completed_scan_id')::uuid=%(scan_id)s::uuid))
      OR EXISTS(SELECT 1 FROM public.operator_jobs WHERE inventory_scan_id=%(scan_id)s::uuid) AS owner_reference""",{**p,'state':state['value'] if state else None})
    if refs['owner_reference'] is True:return 0,scrubbed,1
    refs=tx.fetch_one("""SELECT EXISTS(SELECT 1 FROM public.video_inventory_memberships WHERE first_seen_scan_id=%(scan_id)s::uuid
      OR last_seen_scan_id=%(scan_id)s::uuid OR absence_confirmed_by_scan_id=%(scan_id)s::uuid)
      OR EXISTS(SELECT 1 FROM public.typed_inventory_receipts WHERE scan_id=%(scan_id)s::uuid) AS retained_reference""",p)
    if not refs['retained_reference']:
        tx.execute('DELETE FROM public.channel_video_inventory_scans WHERE scan_id=%(scan_id)s::uuid',p)
        return 1,scrubbed,0
    return 0,scrubbed,0


_PROTOCOLS={'playlist_state':_playlist,'inventory_scans':_scans,'inventory_receipts':_receipts}


_EXACT_PAGE={
 'playlist_state':"""SELECT s.channel_id AS a,s.video_type AS b,NULL::text AS cursor
 FROM jsonb_to_recordset(%(references)s::jsonb) k(a text,b text)
 JOIN public.channel_playlist_state s ON s.channel_id=k.a AND s.video_type=k.b ORDER BY a,b""",
 'inventory_scans':"""SELECT s.scan_id::text AS a,''::text AS b,NULL::text AS cursor
 FROM jsonb_to_recordset(%(references)s::jsonb) k(a uuid,b text)
 JOIN public.channel_video_inventory_scans s ON s.scan_id=k.a ORDER BY s.scan_id""",
 'inventory_receipts':"""SELECT r.receipt_scope AS a,r.transition_id::text AS b,NULL::text AS cursor
 FROM jsonb_to_recordset(%(references)s::jsonb) k(a text,b uuid)
 JOIN public.typed_inventory_receipts r ON r.receipt_scope=k.a AND r.transition_id=k.b ORDER BY a,r.transition_id""",
}


def cleanup_hot_inventory_copies(transaction,*,target,after_id=None,limit=100,references=None):
    tx=transaction
    with tx.borrow():
        header=tx.fetch_one(_HEADER,(target,after_id,limit))
        target=header['target']
        limit=header['limit']
        if target not in TARGETS:_reject('hot_inventory_copies_target_invalid')
        if limit is None or not 1<=limit<=100:_reject('hot_inventory_copies_limit_invalid')
        if header['cursor'] is not None and not 1<=header['cursor_length']<=4096:_reject('hot_inventory_copies_cursor_invalid')
        after_a,after_b,after_clock,after_branch=_cursor(tx,header['cursor'],target)
        params=dict(after_a=after_a,after_b=after_b,after_clock=after_clock,after_branch=after_branch,
            cutoff=header['cutoff'],at=header['at'],limit=limit)
        if references is not None:
            params['references']=Jsonb([dict(a=key[0],b=key[1] if len(key)>1 else '') for key in references])
        rows=tx.fetch_all(_PAGE[target] if references is None else _EXACT_PAGE[target],params,max_rows=limit)
        deleted=scrubbed=deferred=0
        clocks=[]
        unresolved=[]
        for candidate in rows:
            step={'value_clock':None,'upper_at':None,'clock_observed':False,'proof_invalid':False}
            p=dict(a=candidate['a'],b=candidate['b'],cutoff=header['cutoff'])
            try:
                with tx.savepoint():
                    step['upper_at']=tx.fetch_one("SELECT to_jsonb(clock_timestamp())#>>'{}' AS at")['at']
                    p=dict(a=candidate['a'],b=candidate['b'],scan_at=step['upper_at'],cutoff=header['cutoff'])
                    removed,cleared,held=_PROTOCOLS[target](tx,p,step)
                deleted+=removed
                scrubbed+=cleared
                deferred+=held
            except DatabaseServerError as error:
                if error.code!='55P03':raise
                # The row savepoint rolled back every write and its counters.
                # Its value_clock assignment remains, as in PL/pgSQL variables.
                held=1
                deferred+=1
            if held:
                if target=='playlist_state' and not step['clock_observed']:
                    observed=tx.fetch_one(_PLAYLIST_OBSERVATION,p)
                    if observed is not None:
                        step.update(value_clock=observed['value_clock'],upper_at=observed['upper_at'],
                                    clock_observed=True,proof_invalid=observed['noop'] is not True)
                        # A freshly observed valid state supplies no age cause.
                        if observed['noop'] is True:step['valid_observed']=True
                clocks.append(step)
                if references is not None or (step['clock_observed'] and not step.get('valid_observed')):
                    unresolved.append(dict(target=target,key=[candidate['a']] if target=='inventory_scans' else [candidate['a'],candidate['b']]))
        observed_clocks=[{name:step[name] for name in ('value_clock','upper_at','proof_invalid')} for step in clocks
                         if step['clock_observed'] and not step.get('valid_observed')]
        summary=tx.fetch_one(_TELEMETRY,(Jsonb(observed_clocks),header['cutoff'])) if observed_clocks else {'oldest':None,'invalid':0}
        complete=references is not None or len(rows)<limit
        return dict(target=target,as_of=header['at'],cutoff=header['cutoff'],scanned=len(rows),deleted=deleted,scrubbed=scrubbed,deferred=deferred,
            next_cursor=None if complete else rows[-1]['cursor'],complete=complete,telemetry_scope='scanned_page',
            oldest_remaining_expired_source_at=summary['oldest'],invalid_clock_remaining=summary['invalid'],
            unknown_expiry_remaining=sum(not step['clock_observed'] for step in clocks),
            priority_complete=len(unresolved)<=100,priority_references=unresolved[:100])
