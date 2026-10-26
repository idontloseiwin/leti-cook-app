"""Python bounded inventory/membership retention; borrowed maintenance only.

The cutoff belongs to the page; fresh upper clocks belong to each proof/lock
phase. Python owns the finite loop and counters. PostgreSQL owns native source
comparisons, PK ordering, evidence predicates and mutation. No retained business
function, automatic retry or new source timestamp is introduced.
"""
from psycopg.types.json import Jsonb
from ..base.postgres import DatabaseServerError
from ..inventory.inventory_review_events import after_inventory_source_write,after_membership_source_write
from .queue_retention import QUEUE_PAYLOAD_SQL

ERRORS=frozenset({'hot_inventory_target_invalid','hot_inventory_limit_invalid','hot_inventory_cursor_invalid'})
_HEADER="""WITH native AS(SELECT %s::text AS target,%s::text AS cursor,%s::integer AS limit),
 clock AS MATERIALIZED(SELECT clock_timestamp() AS at)
 SELECT native.*,length(cursor) AS cursor_length,to_jsonb(at)#>>'{}' AS at,
 to_jsonb(at-interval '696 hours')#>>'{}' AS cutoff FROM native CROSS JOIN clock"""
_CLOCK="SELECT to_jsonb(clock_timestamp())#>>'{}' AS at"
_MEMBER_SOURCE="""coalesce(isfinite(CASE WHEN x.membership_status='present' THEN x.last_present_at ELSE x.absent_since END)
 AND (CASE WHEN x.membership_status='present' THEN x.last_present_at ELSE x.absent_since END) BETWEEN %(cutoff)s::timestamptz AND %(upper)s::timestamptz
 AND isfinite(x.last_seen_at) AND x.last_seen_at BETWEEN %(cutoff)s::timestamptz AND %(upper)s::timestamptz,false)"""
_MEMBER_FIRST="coalesce(isfinite(x.first_seen_at) AND x.first_seen_at BETWEEN %(cutoff)s::timestamptz AND %(upper)s::timestamptz,false)"
_MEMBER_POSITIVE="coalesce(isfinite(x.last_present_at) AND x.last_present_at BETWEEN %(cutoff)s::timestamptz AND %(upper)s::timestamptz,false)"
_MEMBER_NOOP='('+_MEMBER_SOURCE+' AND ('+_MEMBER_FIRST+" OR (x.first_seen_at IS NULL AND x.first_seen_scan_id IS NULL)) AND ("+_MEMBER_POSITIVE+" OR (x.last_present_at IS NULL AND x.source_published_at IS NULL)))"
def _pair_current(value, clock):
    return ('((' + value + ' IS NULL AND ' + clock + ' IS NULL) OR (' + value + ' IS NOT NULL AND '
            + 'coalesce(isfinite(' + clock + ') AND ' + clock
            + ' BETWEEN %(cutoff)s::timestamptz AND %(upper)s::timestamptz,false)))')


_PUBLICATION=_pair_current('iv.published_at','iv.published_acquired_at')
_QUEUE_PUBLICATION=_pair_current('qs.typed_source_published_at','qs.typed_source_published_acquired_at')
_QUEUE_STREAM_END=_pair_current('qs.typed_stream_actual_end_at','qs.typed_source_stream_end_acquired_at')
_QUEUE_NOOP='(qs.video_id IS NULL OR ('+_QUEUE_PUBLICATION+' AND '+_QUEUE_STREAM_END+'))'
# Each value is scrubbed independently under the existing queue row lock. A
# current stream end never renews an expired publication (or the reverse).
_QUEUE_SCRUB='UPDATE public.video_collection_state qs SET '+','.join(
    column+'=CASE WHEN '+valid+' THEN '+column+' END'
    for columns,valid in ((('typed_source_published_at','typed_source_published_acquired_at'),_QUEUE_PUBLICATION),
                         (('typed_stream_actual_end_at','typed_source_stream_end_acquired_at'),_QUEUE_STREAM_END))
    for column in columns)+' WHERE video_id=%(video_id)s'
_TYPE="""(iv.video_type IS NULL OR (SELECT true FROM public.video_type_review_cases c
 WHERE c.video_id=iv.video_id AND c.channel_id=iv.channel_id AND c.decision_type=iv.video_type LIMIT 1) IS NOT NULL
 OR (SELECT true FROM public.video_inventory_memberships w WHERE w.video_id=iv.video_id AND w.channel_id=iv.channel_id
 AND w.video_type=iv.video_type AND w.membership_status='present' AND isfinite(w.last_present_at)
 AND w.last_present_at BETWEEN %(cutoff)s::timestamptz AND %(upper)s::timestamptz
 AND isfinite(w.last_seen_at) AND w.last_seen_at BETWEEN %(cutoff)s::timestamptz AND %(upper)s::timestamptz LIMIT 1) IS NOT NULL)"""
_FRESH="""((SELECT true FROM public.video_inventory_memberships witness WHERE witness.video_id=iv.video_id
 AND isfinite(witness.last_seen_at) AND witness.last_seen_at BETWEEN %(cutoff)s::timestamptz AND %(upper)s::timestamptz
 AND CASE WHEN witness.membership_status='present' THEN isfinite(witness.last_present_at)
 AND witness.last_present_at BETWEEN %(cutoff)s::timestamptz AND %(upper)s::timestamptz
 ELSE isfinite(witness.absent_since) AND witness.absent_since BETWEEN %(cutoff)s::timestamptz AND %(upper)s::timestamptz END LIMIT 1) IS NOT NULL
 OR EXISTS(SELECT 1 FROM (SELECT recorded_at FROM public.video_snapshots WHERE video_id=iv.video_id LIMIT 101) proof
 WHERE isfinite(proof.recorded_at) AND proof.recorded_at BETWEEN %(cutoff)s::timestamptz AND %(upper)s::timestamptz)
 OR EXISTS(SELECT 1 FROM (SELECT recorded_at FROM public.typed_video_collection_receipts WHERE video_id=iv.video_id LIMIT 101) proof
 WHERE isfinite(proof.recorded_at) AND proof.recorded_at BETWEEN %(cutoff)s::timestamptz AND %(upper)s::timestamptz)
 OR (SELECT true FROM public.videos source WHERE source.video_id=iv.video_id AND (
 (isfinite(source.checked_at) AND source.checked_at BETWEEN %(cutoff)s::timestamptz AND %(upper)s::timestamptz)
 OR (isfinite(source.metadata_checked_at) AND source.metadata_checked_at BETWEEN %(cutoff)s::timestamptz AND %(upper)s::timestamptz)
 OR (isfinite(source.availability_checked_at) AND source.availability_checked_at BETWEEN %(cutoff)s::timestamptz AND %(upper)s::timestamptz)) LIMIT 1) IS NOT NULL
 OR (qs.video_id IS NOT NULL AND isfinite(qs.typed_collection_checked_at)
 AND qs.typed_collection_checked_at BETWEEN %(cutoff)s::timestamptz AND %(upper)s::timestamptz)
 OR (iv.published_at IS NOT NULL AND isfinite(iv.published_acquired_at)
 AND iv.published_acquired_at BETWEEN %(cutoff)s::timestamptz AND %(upper)s::timestamptz)
 OR (qs.typed_source_published_at IS NOT NULL AND isfinite(qs.typed_source_published_acquired_at)
 AND qs.typed_source_published_acquired_at BETWEEN %(cutoff)s::timestamptz AND %(upper)s::timestamptz)
 OR (qs.typed_stream_actual_end_at IS NOT NULL AND isfinite(qs.typed_source_stream_end_acquired_at)
 AND qs.typed_source_stream_end_acquired_at BETWEEN %(cutoff)s::timestamptz AND %(upper)s::timestamptz))"""
# Each witness probe is a scalar subquery that looks up one key per row. As EXISTS,
# PostgreSQL may hash the whole witness table once per page instead, which made a
# pass read every membership about three times per page (O(pages x rows)).
_INVENTORY_NOOP='('+_PUBLICATION+' AND '+_QUEUE_NOOP+' AND (iv.skeleton_since IS NOT NULL OR ('+_TYPE+' AND '+_FRESH+')))'
# A present membership can prove type and freshness, but publication has an
# independent clock even if another retained witness now contains another value.
_PAGE_WITNESS="""SELECT w.video_id,w.video_type FROM public.video_inventory_memberships w
 WHERE w.video_id=iv.video_id AND w.channel_id=iv.channel_id AND w.membership_status='present'
 AND isfinite(w.last_present_at) AND w.last_present_at BETWEEN %(cutoff)s::timestamptz AND %(upper)s::timestamptz
 AND isfinite(w.last_seen_at) AND w.last_seen_at BETWEEN %(cutoff)s::timestamptz AND %(upper)s::timestamptz LIMIT 1"""
_PAGE_NOOP='('+_PUBLICATION+' AND '+_QUEUE_NOOP+' AND (iv.skeleton_since IS NOT NULL OR ((witness.video_type=iv.video_type OR '+_TYPE+') AND (witness.video_id IS NOT NULL OR '+_FRESH+'))))'
# A membership needs work when a clock it keeps is outside [cutoff, now]: the
# earliest relevant clock is past the cutoff, or the latest is in the future. A
# missing required clock counts as past; an optional one (a first sighting
# without its scan, a last presence without a publication date) is ignored.
# Exactly the complement of _MEMBER_NOOP; both expressions have indexes.
_MEMBER_EARLIEST="""least(coalesce(CASE WHEN m.membership_status='present' THEN m.last_present_at ELSE m.absent_since END,'-infinity'::timestamptz),
 m.last_seen_at,
 CASE WHEN m.first_seen_at IS NULL AND m.first_seen_scan_id IS NULL THEN 'infinity'::timestamptz ELSE coalesce(m.first_seen_at,'-infinity'::timestamptz) END,
 CASE WHEN m.last_present_at IS NULL AND m.source_published_at IS NULL THEN 'infinity'::timestamptz ELSE coalesce(m.last_present_at,'-infinity'::timestamptz) END)"""
_MEMBER_LATEST="""greatest(coalesce(CASE WHEN m.membership_status='present' THEN m.last_present_at ELSE m.absent_since END,'-infinity'::timestamptz),
 m.last_seen_at,coalesce(m.first_seen_at,'-infinity'::timestamptz),coalesce(m.last_present_at,'-infinity'::timestamptz))"""
_MEMBER_KEY='m.video_id,m.channel_id,m.video_type'
# Expired memberships first (branch 0), then those with a future clock (branch 1),
# each oldest first through its index; the cursor names the branch.
_MEMBERSHIP_PAGE="""WITH page AS MATERIALIZED(
 (SELECT 0 AS branch,"""+_MEMBER_KEY+""","""+_MEMBER_EARLIEST+""" AS clock FROM public.video_inventory_memberships m
 WHERE """+_MEMBER_EARLIEST+"""<%(cutoff)s::timestamptz AND (%(after_branch)s::integer IS NULL OR (%(after_branch)s::integer=0
 AND ("""+_MEMBER_EARLIEST+""","""+_MEMBER_KEY+""")>(%(after_clock)s::timestamptz,%(after_video)s::text,%(after_channel)s::text,%(after_type)s::text)))
 ORDER BY """+_MEMBER_EARLIEST+""","""+_MEMBER_KEY+""" LIMIT %(size)s::integer)
 UNION ALL
 (SELECT 1,"""+_MEMBER_KEY+""","""+_MEMBER_LATEST+""" FROM public.video_inventory_memberships m
 WHERE """+_MEMBER_LATEST+""">%(upper)s::timestamptz AND """+_MEMBER_EARLIEST+""">=%(cutoff)s::timestamptz
 AND (%(after_branch)s::integer IS NULL OR %(after_branch)s::integer=0 OR ("""+_MEMBER_LATEST+""","""+_MEMBER_KEY+""")>
 (%(after_clock)s::timestamptz,%(after_video)s::text,%(after_channel)s::text,%(after_type)s::text))
 ORDER BY """+_MEMBER_LATEST+""","""+_MEMBER_KEY+""" LIMIT %(size)s::integer))
 SELECT page.video_id,page.channel_id,page.video_type,
 jsonb_build_array(page.branch,to_jsonb(page.clock)#>>'{}',page.video_id,page.channel_id,page.video_type)::text AS cursor,
 iv.video_id IS NULL AS noop
 FROM page LEFT JOIN LATERAL(SELECT i.video_id FROM public.video_inventory i WHERE i.video_id=page.video_id LIMIT 1) iv ON true
 ORDER BY page.branch,page.clock,page.video_id,page.channel_id,page.video_type LIMIT %(size)s::integer"""
# The list is read whole, by key, a thousand rows a page; the index order lets
# PostgreSQL stop at the page size.
_INVENTORY_PAGE="""WITH page AS MATERIALIZED(SELECT x.* FROM public.video_inventory x
 WHERE (x.skeleton_since IS NULL OR x.published_at IS NOT NULL OR x.published_acquired_at IS NOT NULL
 OR EXISTS(SELECT 1 FROM public.video_collection_state q WHERE q.video_id=x.video_id AND
 (q.typed_source_published_at IS NOT NULL OR q.typed_source_published_acquired_at IS NOT NULL
 OR q.typed_stream_actual_end_at IS NOT NULL OR q.typed_source_stream_end_acquired_at IS NOT NULL)))
 AND (%(after_video)s::text IS NULL OR x.video_id>%(after_video)s::text)
 ORDER BY x.video_id LIMIT %(size)s::integer)
 SELECT iv.video_id,iv.channel_id,iv.video_type,iv.video_id AS cursor,
 coalesce("""+_PAGE_NOOP+""",false) AS noop
 FROM page iv LEFT JOIN LATERAL(SELECT q.* FROM public.video_collection_state q
 WHERE q.video_id>=iv.video_id AND (q.typed_collection_checked_at IS NOT NULL
 OR q.typed_source_published_at IS NOT NULL OR q.typed_stream_actual_end_at IS NOT NULL
 OR q.typed_source_published_acquired_at IS NOT NULL OR q.typed_source_stream_end_acquired_at IS NOT NULL)
 ORDER BY q.video_id LIMIT 1) qs ON qs.video_id=iv.video_id
 LEFT JOIN LATERAL("""+_PAGE_WITNESS+""") witness ON true
 ORDER BY iv.video_id"""
_INVENTORY_REFERENCES="""SELECT iv.video_id,iv.channel_id,iv.video_type,iv.video_id AS cursor,false AS noop
 FROM public.video_inventory iv WHERE iv.video_id=ANY(%(ids)s::text[]) ORDER BY iv.video_id"""
_MEMBERSHIP_REFERENCES="""SELECT m.video_id,m.channel_id,m.video_type,m.video_id AS cursor,
 NOT EXISTS(SELECT 1 FROM public.video_inventory iv WHERE iv.video_id=m.video_id) AS noop
 FROM jsonb_to_recordset(%(identities)s::jsonb) requested(video_id text,channel_id text,video_type text)
 JOIN public.video_inventory_memberships m USING(video_id,channel_id,video_type)
 ORDER BY m.video_id,m.channel_id,m.video_type"""
_INVENTORY="SELECT iv.channel_id,to_jsonb(iv)::text AS value FROM public.video_inventory iv WHERE video_id=%(video_id)s"
_QUEUE=QUEUE_PAYLOAD_SQL
_MEMBER="SELECT to_jsonb(x)::text AS value FROM public.video_inventory_memberships x WHERE video_id=%(video_id)s AND channel_id=%(channel_id)s AND video_type=%(video_type)s"
_MEMBER_FACTS='SELECT '+_MEMBER_SOURCE+' AS source_valid,'+_MEMBER_FIRST+' AS first_valid,'+_MEMBER_POSITIVE+' AS positive_valid,'+_MEMBER_NOOP+' AS noop FROM jsonb_populate_record(NULL::public.video_inventory_memberships,%(member)s::jsonb) x'
_PLAYLISTS="""SELECT to_jsonb(s)::text AS value FROM public.channel_playlist_state s
 WHERE s.channel_id IN(%(channel_id)s,%(observed_channel)s) ORDER BY s.channel_id,s.video_type FOR UPDATE NOWAIT"""
_QUEUE_BUSY="SELECT lease_token IS NOT NULL AND lease_expires_at>clock_timestamp() AS busy FROM jsonb_populate_record(NULL::public.video_collection_state,%s::jsonb) s"
_MEMBER_SCRUB="""UPDATE public.video_inventory_memberships SET
 first_seen_at=CASE WHEN %(first_valid)s THEN first_seen_at ELSE NULL END,
 first_seen_scan_id=CASE WHEN %(first_valid)s THEN first_seen_scan_id ELSE NULL END,
 last_present_at=CASE WHEN %(positive_valid)s THEN last_present_at ELSE NULL END,
 source_published_at=CASE WHEN %(positive_valid)s THEN source_published_at ELSE NULL END
 WHERE video_id=%(video_id)s AND channel_id=%(channel_id)s AND video_type=%(video_type)s
 AND ((NOT %(first_valid)s AND (first_seen_at IS NOT NULL OR first_seen_scan_id IS NOT NULL))
 OR (NOT %(positive_valid)s AND (last_present_at IS NOT NULL OR source_published_at IS NOT NULL)))"""
_MEMBER_DELETE_EFFECT="""DELETE FROM public.video_inventory_memberships
 WHERE video_id=%(video_id)s AND channel_id=%(channel_id)s AND video_type=%(video_type)s
 RETURNING to_jsonb(video_inventory_memberships)::text AS value"""
_MEMBER_SCRUB_EFFECT=_MEMBER_SCRUB+" RETURNING to_jsonb(video_inventory_memberships)::text AS value"
# The original owner already holds this exact inventory row FOR UPDATE. Capture
# the actual OLD after any earlier publication scrub, retaining its own snapshot
# variables for the remaining retention predicates.
_WITHDRAW_TYPE_EFFECT="""UPDATE public.video_inventory iv SET video_type=NULL,published_at=NULL,published_acquired_at=NULL,
 skeleton_since=coalesce(iv.skeleton_since,clock_timestamp())
 FROM public.video_inventory previous
 WHERE iv.video_id=%(video_id)s AND previous.video_id=iv.video_id
 RETURNING to_jsonb(previous)::text AS old_text,to_jsonb(iv)::text AS new_text"""
_RECOVERY_QUEUE="""INSERT INTO public.video_collection_state AS state
 (video_id,channel_id,video_type,hydration_needed,next_due_at)
 VALUES(%(video_id)s,%(channel_id)s,NULL,true,clock_timestamp())
 ON CONFLICT(video_id) DO UPDATE SET video_type=NULL,hydration_needed=true,
 stats_due_at=NULL,metadata_due_at=NULL,stats_source_checked_at=NULL,metadata_source_checked_at=NULL,
 next_due_at=CASE WHEN (state.availability_first_missing_at IS NOT NULL OR state.availability_resume_observed_at IS NOT NULL)
 OR state.paused_reason='availability_retry_expired' THEN state.next_due_at ELSE clock_timestamp() END,
 profile_metadata_version=0,typed_collection_status=NULL,
 typed_collection_checked_at=NULL,typed_source_published_at=NULL,typed_stream_actual_end_at=NULL,
 typed_source_published_acquired_at=NULL,typed_source_stream_end_acquired_at=NULL,
 paused_reason=CASE WHEN (state.availability_first_missing_at IS NOT NULL OR state.availability_resume_observed_at IS NOT NULL)
 OR state.paused_reason='availability_retry_expired' THEN state.paused_reason END,
 lease_channel_epoch=NULL,lease_token=NULL,lease_owner=NULL,
 lease_started_at=NULL,lease_expires_at=NULL,
 consecutive_failures=CASE WHEN (state.availability_first_missing_at IS NOT NULL OR state.availability_resume_observed_at IS NOT NULL) THEN state.consecutive_failures ELSE 0 END,
 last_error_type=CASE WHEN (state.availability_first_missing_at IS NOT NULL OR state.availability_resume_observed_at IS NOT NULL) THEN state.last_error_type END,
 last_success_at=NULL,updated_at=now()"""
_CHILDREN="""SELECT EXISTS(SELECT 1 FROM public.video_inventory_memberships WHERE video_id=%(video_id)s)
 OR EXISTS(SELECT 1 FROM public.video_snapshots WHERE video_id=%(video_id)s)
 OR EXISTS(SELECT 1 FROM public.typed_video_collection_receipts WHERE video_id=%(video_id)s) AS present"""
# Every videos column except the identity and the three source clocks.
VIDEO_PAYLOAD_COLUMNS=('title','views','like_count','comment_count','published_at','duration','tags','category_id',
    'youtube_topic_categories','default_audio_language','default_language','caption_available','stream_actual_end_at',
    'publication_status','scheduled_start_at','availability_status','privacy_status','upload_status')
def video_payload(alias):
    """True while a video row still carries API data, not only its identity."""
    return '('+' OR '.join(alias+'.'+column+' IS NOT NULL' for column in VIDEO_PAYLOAD_COLUMNS)+')'
_CURRENT="SELECT "+video_payload('v')+" AS payload FROM public.videos v WHERE v.video_id=%(video_id)s FOR UPDATE NOWAIT"
# Four preflight round trips become one native snapshot. The clock is fresh,
# the page cutoff is fixed, and every writable candidate is rechecked below
# after the existing ownership locks. No preflight row authorizes a write.
_PREFLIGHT_INVENTORY="""WITH clock AS MATERIALIZED(SELECT clock_timestamp() AS at)
 SELECT coalesce("""+_INVENTORY_NOOP.replace('%(upper)s::timestamptz','clock.at')+""",false) AS noop
 FROM public.video_inventory iv CROSS JOIN clock
 LEFT JOIN LATERAL(SELECT q.* FROM public.video_collection_state q
 WHERE q.video_id>=%(video_id)s ORDER BY q.video_id LIMIT 1) qs ON qs.video_id=iv.video_id
 WHERE iv.video_id=%(video_id)s"""
_PREFLIGHT_MEMBERSHIP="""WITH clock AS MATERIALIZED(SELECT clock_timestamp() AS at)
 SELECT x.video_id IS NULL OR coalesce("""+_MEMBER_NOOP.replace('%(upper)s::timestamptz','clock.at')+""",false) AS noop
 FROM public.video_inventory iv CROSS JOIN clock
 LEFT JOIN public.video_inventory_memberships x ON x.video_id=iv.video_id
 AND x.channel_id=%(channel_id)s AND x.video_type=%(video_type)s WHERE iv.video_id=%(video_id)s"""
# All (at most six) playlist locks are acquired in their original order before
# sampling this clock. Native row texts avoid Python timestamp/JSON coercion.
_PLAYLISTS_BUSY="""WITH clock AS MATERIALIZED(SELECT clock_timestamp() AS at)
 SELECT coalesce(bool_or(s.lease_token IS NOT NULL AND s.lease_expires_at>clock.at),false) AS busy
 FROM unnest(%s::text[]) raw(value)
 CROSS JOIN LATERAL jsonb_populate_record(NULL::public.channel_playlist_state,raw.value::jsonb) s CROSS JOIN clock"""
_MEMBER_INVALID="""x.last_seen_at IS NULL OR NOT isfinite(x.last_seen_at) OR x.last_seen_at>clock.at
 OR (x.membership_status='present' AND (x.last_present_at IS NULL OR NOT isfinite(x.last_present_at) OR x.last_present_at>clock.at))
 OR (x.membership_status='absent' AND (x.absent_since IS NULL OR NOT isfinite(x.absent_since) OR x.absent_since>clock.at))
 OR (x.first_seen_at IS NOT NULL AND (NOT isfinite(x.first_seen_at) OR x.first_seen_at>clock.at))"""
# Post-write remaining evidence for <=100 exact composite keys, once per page.
# A late candidate rollback occurs before this read; surviving/fresh/missing rows
# are sampled together rather than keeping a stale pre-write fact per candidate.
_MEMBERSHIP_LAG_BATCH="""WITH clock AS MATERIALIZED(SELECT clock_timestamp() AS at)
 SELECT x.video_id,x.channel_id,x.video_type,
 to_jsonb((SELECT min(c.value) FROM (VALUES(x.first_seen_at),(x.last_seen_at),(x.last_present_at),(x.absent_since)) c(value)
 WHERE isfinite(c.value) AND c.value<%(cutoff)s::timestamptz))#>>'{}' AS oldest,
 """+_MEMBER_INVALID+""" AS invalid
 FROM jsonb_to_recordset(%(identities)s::jsonb) requested(video_id text,channel_id text,video_type text)
 JOIN public.video_inventory_memberships x USING(video_id,channel_id,video_type) CROSS JOIN clock
 ORDER BY x.video_id,x.channel_id,x.video_type"""



def _reject(code):raise DatabaseServerError(code,code='P0001')
def _busy():raise DatabaseServerError('database_operation_failed',code='55P03')


def _cursor(tx,value,target):
    if value is None:return None,None,None,None,None
    if target=='inventory':return None,None,value,None,None
    try:
        with tx.savepoint():
            row=tx.fetch_one("""WITH input AS(SELECT %s::jsonb AS value) SELECT
             jsonb_typeof(value)='array' AND CASE WHEN jsonb_typeof(value)='array' THEN jsonb_array_length(value)=5
              AND jsonb_typeof(value->0)='number' AND (value->>0) IN ('0','1') AND jsonb_typeof(value->1)='string'
              AND jsonb_typeof(value->2)='string' AND jsonb_typeof(value->3)='string' AND jsonb_typeof(value->4)='string' ELSE false END AS valid,
             value->>0 AS branch,value->>1 AS clock,value->>2 AS video,value->>3 AS channel,value->>4 AS type FROM input""",(value,))
            if row['valid'] is not True:_reject('hot_inventory_cursor_invalid')
            clock=tx.fetch_one("SELECT to_jsonb(%s::timestamptz)#>>'{}' AS value",(row['clock'],))['value']
            return int(row['branch']),clock,row['video'],row['channel'],row['type']
    except DatabaseServerError as error:
        if error.code not in ('22P02','22007','22008'):raise
        _reject('hot_inventory_cursor_invalid')


def _fact(tx,p,expression):
    return tx.fetch_one('SELECT '+expression+' AS value FROM jsonb_populate_record(NULL::public.video_inventory,%(inventory)s::jsonb) iv CROSS JOIN jsonb_populate_record(NULL::public.video_collection_state,%(queue)s::jsonb) qs',p)['value'] is True


def _candidate(tx,p,target):
    """Return local counters and whether retained control flow reaches telemetry."""
    preflight=tx.fetch_one(_PREFLIGHT_MEMBERSHIP if target=='memberships' else _PREFLIGHT_INVENTORY,p)
    if preflight is None or preflight['noop'] is True:return 0,0,0,False
    if tx.fetch_one('SELECT pg_try_advisory_xact_lock(hashtextextended(%s::text,962106)) AS acquired',(p['video_id'],))['acquired'] is not True:_busy()
    observed=tx.fetch_one('SELECT channel_id FROM public.video_inventory WHERE video_id=%(video_id)s',p)
    p['observed_channel']=observed['channel_id'] if observed else None
    states=tx.fetch_all(_PLAYLISTS,p,max_rows=6)
    if states and tx.fetch_one(_PLAYLISTS_BUSY,([state['value'] for state in states],))['busy'] is True:_busy()
    queue=tx.fetch_one(_QUEUE+' FOR UPDATE NOWAIT',p)
    p['queue']=queue['value'] if queue else None
    if queue and tx.fetch_one(_QUEUE_BUSY,(queue['value'],))['busy'] is True:_busy()
    inventory=tx.fetch_one(_INVENTORY+' FOR UPDATE NOWAIT',p)
    if inventory is None:return 0,0,0,False
    if inventory['channel_id']!=p['observed_channel'] or (target=='inventory' and inventory['channel_id']!=p['channel_id']):_busy()
    p.update(inventory=inventory['value'],upper=tx.fetch_one(_CLOCK)['at'])
    tx.fetch_one('SELECT case_id::text FROM public.video_type_review_cases WHERE video_id=%(video_id)s FOR UPDATE NOWAIT',p)
    if target=='memberships':
        member=tx.fetch_one(_MEMBER+' FOR UPDATE NOWAIT',p)
        if member is None:return 0,0,0,False
        facts=tx.fetch_one(_MEMBER_FACTS,{**p,'member':member['value']})
        if not facts['source_valid']:
            removed=tx.returning_one(_MEMBER_DELETE_EFFECT,p)
            if removed is not None:after_membership_source_write(tx,operation='delete',old_text=removed['value'])
            return 1,0,0,True
        changed=tx.returning_one(_MEMBER_SCRUB_EFFECT,{**p,**facts})
        if changed is not None:after_membership_source_write(tx,operation='update',old_text=member['value'],new_text=changed['value'])
        return 0,int(changed is not None),0,True
    scrubbed=0
    if not _fact(tx,p,_PUBLICATION):
        tx.execute('UPDATE public.video_inventory SET published_at=NULL,published_acquired_at=NULL WHERE video_id=%(video_id)s',p)
        scrubbed+=1
    queue_clear=not _fact(tx,p,_QUEUE_NOOP)
    if queue_clear:
        tx.execute(_QUEUE_SCRUB,p)
        scrubbed+=1
    if not _fact(tx,p,_TYPE):
        tx.execute(_RECOVERY_QUEUE,p)
        changed=tx.returning_one(_WITHDRAW_TYPE_EFFECT,p)
        if changed is not None:after_inventory_source_write(tx,operation='update',**changed)
        queue_clear=True
        scrubbed+=1
    if _fact(tx,p,_FRESH):return 0,scrubbed,0,False
    if tx.fetch_one(_CHILDREN,p)['present']:return 0,scrubbed,1,False
    current=tx.fetch_one(_CURRENT,p)
    if current and current['payload']:return 0,scrubbed,1,False
    if queue and not queue_clear and _fact(tx,p,'(qs.typed_source_published_at IS NOT NULL OR qs.typed_stream_actual_end_at IS NOT NULL)'):
        return 0,scrubbed,1,False
    tx.execute('DELETE FROM public.videos WHERE video_id=%(video_id)s',p)
    # The existing ownership locks also protect the identity-only transition.
    # Keep user decisions bound to this ID; withdrawing API evidence neither
    # detaches that link nor creates a new review case.
    tx.execute(_RECOVERY_QUEUE,p)
    changed=tx.returning_one(_WITHDRAW_TYPE_EFFECT,p)
    if changed is not None:after_inventory_source_write(tx,operation='update',**changed)
    return 0,scrubbed+1,0,True


def cleanup_hot_inventory(transaction,*,target,after_id=None,limit=100,references=None):
    tx=transaction
    with tx.borrow():
        header=tx.fetch_one(_HEADER,(target,after_id,limit))
        target=header['target']
        limit=header['limit']
        if target not in ('memberships','inventory'):_reject('hot_inventory_target_invalid')
        if limit is None or not 1<=limit<=100:_reject('hot_inventory_limit_invalid')
        if header['cursor'] is not None and not 1<=header['cursor_length']<=4096:_reject('hot_inventory_cursor_invalid')
        after_branch,after_clock,after_video,after_channel,after_type=_cursor(tx,header['cursor'],target)
        # Memberships: only rows that need work are selected. The list: a thousand
        # rows by key, of which at most `limit` need work.
        size=limit if target=='memberships' else limit*10
        params=dict(target=target,after_branch=after_branch,after_clock=after_clock,after_video=after_video,
                    after_channel=after_channel,after_type=after_type,cutoff=header['cutoff'],upper=header['at'],size=size)
        if references is None:
            selected=tx.fetch_all(_MEMBERSHIP_PAGE if target=='memberships' else _INVENTORY_PAGE,params,max_rows=size)
        elif target=='inventory':
            selected=tx.fetch_all(_INVENTORY_REFERENCES,dict(params,ids=[key[0] for key in references]),max_rows=len(references))
        else:
            identities=[dict(zip(('video_id','channel_id','video_type'),key)) for key in references]
            selected=tx.fetch_all(_MEMBERSHIP_REFERENCES,dict(params,identities=Jsonb(identities)),max_rows=len(references))
        scanned=deleted=scrubbed=deferred=examined=invalid=unknown=0
        truncated=False
        cursor=None
        old=[]
        blockers=[]
        samples=[]
        for row in selected:
            if not row['noop'] and scanned==limit:truncated=True;break
            examined+=1
            cursor=row['cursor']
            if row['noop']:continue
            scanned+=1
            p={**row,'cutoff':header['cutoff']}
            try:
                with tx.savepoint():removed,cleared,held,sample=_candidate(tx,p,target)
                deleted+=removed
                scrubbed+=cleared
                deferred+=held
                if target=='inventory':
                    unknown+=held
                    if held and references is not None:blockers.append(dict(target=target,key=[row['video_id']]))
            except DatabaseServerError as error:
                if error.code!='55P03':raise
                deferred+=1
                sample=True
                if target=='inventory':
                    unknown+=1
                    if references is not None:blockers.append(dict(target=target,key=[row['video_id']]))
            if sample and target=='memberships':
                samples.append({name:row[name] for name in ('video_id','channel_id','video_type')})
        if samples:
            remaining=tx.fetch_all(_MEMBERSHIP_LAG_BATCH,dict(identities=Jsonb(samples),cutoff=header['cutoff']),max_rows=len(samples))
            for lag in remaining:
                if lag['oldest'] is not None:old.append(lag['oldest'])
                invalid_clock=lag['invalid'] is True
                invalid+=int(invalid_clock)
                if lag['oldest'] is not None or invalid_clock:
                    blockers.append(dict(target=target,key=[lag['video_id'],lag['channel_id'],lag['video_type']]))
        oldest=tx.fetch_one("SELECT to_jsonb(min(value::timestamptz))#>>'{}' AS value FROM unnest(%s::text[]) clocks(value)",(old,))['value'] if old else None
        complete=True if references is not None else not truncated and examined<size
        return dict(target=target,as_of=header['at'],cutoff=header['cutoff'],scanned=scanned,deleted=deleted,scrubbed=scrubbed,deferred=deferred,
            next_cursor=None if complete else cursor,complete=complete,telemetry_scope='scanned_page',
            oldest_remaining_expired_source_at=oldest,invalid_clock_remaining=invalid,
            # Busy is not expiry: the root has no source clock of its own.
            # Membership/current/evidence owners report their actual clocks.
            unknown_expiry_remaining=unknown,priority_references=blockers,priority_complete=True)
