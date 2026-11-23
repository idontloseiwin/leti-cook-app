"""Count generations are hints, never a proof of catalogue completeness.

Old/new values are the locked accepted channel writer rows. Source receipts bind
their generation before HTTP; head completion and the decision to request full
coverage are durable and independently replayable.
"""
from datetime import datetime, timezone

from hot_storage import RETENTION_HOURS
from .discovery import operation, fail



def credit_candidates(inserted_ids, items, source_checked_at):
    """Bounded immutable positive source evidence, never a count of local rows.

    Missing/invalid publication evidence only loses the optional count credit;
    it never prevents ordinary discovery. The provider's video publication date
    is accepted here, not a playlist item's added date or a local creation clock.
    """
    def clock(value):
        if isinstance(value, datetime):
            parsed=value
        elif isinstance(value,str):
            try:parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
            except ValueError:return None
        else:return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:return None
        try:return parsed.astimezone(timezone.utc)
        except (ValueError,OverflowError):return None
    observed=clock(source_checked_at)
    if observed is None:return []
    if not isinstance(items,(list,tuple)) or len(items)>50:
        fail('discovery_credit_batch_invalid')
    inserted=set(inserted_ids)
    result={}
    for item in items:
        if not isinstance(item,dict) or item.get('video_id') not in inserted:continue
        published=clock(item.get('published_at'))
        if published is not None and published<=observed:
            result[item['video_id']]={'video_id':item['video_id'],'published_at':published.isoformat()}
    return [result[key] for key in sorted(result)]


_CREDITS="""WITH receipts AS MATERIALIZED (
 SELECT r.result,r.source_checked_at,s.count_reconcile_token=%(token)s::uuid AS same_generation,
 false AS addressed,false AS disabled_stream
 FROM public.channel_video_inventory_scans s
 JOIN public.typed_inventory_receipts r USING(scan_id)
 WHERE s.channel_id=%(channel)s AND s.video_type=ANY(%(types)s::text[])
   AND s.discovery_channel_epoch=%(epoch)s AND r.receipt_scope='scan_transition'
   AND r.transition_kind='page' AND r.source_checked_at>%(lower)s::timestamptz
 UNION ALL
 SELECT r.result,r.source_checked_at,false,true,false
 FROM public.typed_inventory_receipts r
 WHERE r.receipt_scope='addressed_membership' AND r.result->>'channel_id'=%(channel)s
   AND r.result->>'video_type'=ANY(%(types)s::text[])
   AND (NOT(r.result ? 'observation_kind') OR r.result->>'observation_kind'='head_page')
   AND r.result->'control_epoch'=to_jsonb(%(epoch)s::bigint)
   AND r.source_checked_at>%(lower)s::timestamptz
 UNION ALL
 SELECT r.result,r.source_checked_at,false,true,true
 FROM public.typed_inventory_receipts r
 WHERE r.receipt_scope='addressed_membership' AND r.result->>'channel_id'=%(channel)s
   AND r.result->>'observation_kind'='disabled_stream_positive'
   AND r.result->>'video_type'='stream' AND NOT('stream'=ANY(%(types)s::text[]))
   AND r.result->>'playlist_id'='UULV'||substring(%(channel)s::text FROM 3)
   AND r.result->'control_epoch'=to_jsonb(%(epoch)s::bigint)
   AND r.source_checked_at>%(lower)s::timestamptz
), candidates AS (
 SELECT r.*,candidate->>'video_id' AS video_id,
 CASE WHEN jsonb_typeof(candidate->'published_at')='string'
     AND pg_input_is_valid(candidate->>'published_at','timestamptz')
     THEN (candidate->>'published_at')::timestamptz END AS published_at
 FROM receipts r CROSS JOIN LATERAL jsonb_array_elements(
   CASE WHEN jsonb_typeof(r.result->'count_credit_candidates')='array'
        THEN r.result->'count_credit_candidates' ELSE '[]'::jsonb END) candidate
 WHERE NOT(r.result ? 'source_expired') AND isfinite(r.source_checked_at)
   AND r.source_checked_at BETWEEN statement_timestamp()-make_interval(hours=>%(hours)s) AND statement_timestamp()
   AND (r.source_checked_at<=%(upper)s::timestamptz OR r.same_generation OR r.addressed)
)
SELECT count(DISTINCT video_id)::integer AS new_count FROM candidates
 WHERE isfinite(published_at) AND published_at>%(lower)s::timestamptz
   AND published_at<=%(upper)s::timestamptz AND published_at<=source_checked_at
   AND ((NOT disabled_stream AND jsonb_typeof(result->'new_video_ids')='array'
         AND result->'new_video_ids' ? video_id)
     OR (disabled_stream AND jsonb_typeof(result->'first_observed_video_ids')='array'
         AND result->'first_observed_video_ids' ? video_id
         AND jsonb_typeof(result->'video_ids')='array' AND result->'video_ids' ? video_id))"""

def _announced(tx, channel_id, increase):
    """Whether WebSub notifications announced this whole increase; a failed lookup means no."""
    from .base.postgres import DatabaseServerError
    from .websub import explained_increase
    try:
        with tx.savepoint():
            return explained_increase(tx, channel_id, int(increase))
    except DatabaseServerError as error:
        if error.code in ('57014', 'P0004'):
            raise
        return False


def _resolve_growth(tx, new_text):
    """Defer an unexplained increase until another channel observation when notifications are pending."""
    from collection_formats import collection_formats
    row = tx.fetch_one('''SELECT c.channel_id,c.count_reconcile_token AS token,
        c.count_reconcile_previous AS previous,c.count_reconcile_current AS current,
        c.count_reconcile_source_checked_at AS lower,c.count_reconcile_observed_at AS upper,
        c.count_reconcile_epoch AS epoch
        FROM public.channels c JOIN public.channel_collection_state s USING(channel_id)
        CROSS JOIN jsonb_to_record(%s::jsonb) new(channel_id text,channel_metadata_checked_at timestamptz)
        WHERE c.channel_id=new.channel_id AND c.count_reconcile_epoch=s.control_epoch
        AND c.count_reconcile_token IS DISTINCT FROM c.count_reconcile_settled_token
        AND c.count_reconcile_token IS NOT NULL AND c.count_reconcile_current>c.count_reconcile_previous
        AND c.count_reconcile_observed_at<=new.channel_metadata_checked_at
        AND c.count_reconcile_source_checked_at BETWEEN now()-make_interval(hours=>%s) AND c.count_reconcile_observed_at
        AND EXISTS(SELECT 1 FROM public.discovery_runtime_control WHERE singleton AND enabled)''',
        (new_text,RETENTION_HOURS))
    if not row:
        return False
    args = dict(channel=row['channel_id'],token=row['token'],epoch=row['epoch'],
        lower=row['lower'],upper=row['upper'],hours=RETENTION_HOURS,types=list(collection_formats()))
    credits = tx.fetch_one(_CREDITS + ''' AND EXISTS(SELECT 1 FROM public.websub_events e
        WHERE e.channel_id=%(channel)s AND e.video_id=candidates.video_id AND e.kind='entry'
        AND e.received_at>%(lower)s)''',args)['new_count']
    if credits >= row['current']-row['previous']:
        tx.execute('UPDATE public.channels SET count_reconcile_settled_token=count_reconcile_token WHERE channel_id=%s',
                   (row['channel_id'],))
    elif tx.fetch_one('''SELECT 1 AS pending FROM public.websub_events WHERE channel_id=%s AND kind='entry'
            AND status='pending' AND received_at>%s AND work_phase IN ('metadata_wait','membership')
            AND paused_at IS NULL AND NOT review_required AND NOT EXISTS(SELECT 1 FROM public.video_inventory i
                WHERE i.video_id=websub_events.video_id AND i.channel_id=websub_events.channel_id) LIMIT 1''',
            (row['channel_id'],row['lower'])):
        # Preserve the two observed counts and their clocks without arming a list read.
        tx.execute('UPDATE public.channels SET count_reconcile_token=NULL WHERE channel_id=%s',(row['channel_id'],))
    else:
        return False
    tx.execute('UPDATE public.channel_playlist_state SET head_signal_token=NULL WHERE channel_id=%s AND head_signal_token IS NOT NULL',
               (row['channel_id'],))
    return True


def observe_count_change(tx, old_text, new_text):
    changed = tx.execute('''UPDATE public.channels c SET count_reconcile_token=gen_random_uuid(),
        count_reconcile_previous=CASE WHEN ((c.count_reconcile_token IS NOT NULL AND c.count_reconcile_token IS DISTINCT FROM c.count_reconcile_settled_token)
            OR (c.count_reconcile_token IS NULL AND c.count_reconcile_current>c.count_reconcile_previous))
            AND c.count_reconcile_epoch=(SELECT control_epoch FROM public.channel_collection_state WHERE channel_id=c.channel_id)
            THEN c.count_reconcile_previous ELSE old.video_count END,count_reconcile_current=new.video_count,
        count_reconcile_source_checked_at=CASE WHEN ((c.count_reconcile_token IS NOT NULL AND c.count_reconcile_token IS DISTINCT FROM c.count_reconcile_settled_token)
            OR (c.count_reconcile_token IS NULL AND c.count_reconcile_current>c.count_reconcile_previous))
            AND c.count_reconcile_epoch=(SELECT control_epoch FROM public.channel_collection_state WHERE channel_id=c.channel_id)
            THEN c.count_reconcile_source_checked_at ELSE least(old.channel_metadata_checked_at,new.channel_metadata_checked_at) END,
        count_reconcile_observed_at=CASE WHEN new.video_count=old.video_count THEN c.count_reconcile_observed_at ELSE new.channel_metadata_checked_at END,
        count_reconcile_epoch=(SELECT control_epoch FROM public.channel_collection_state WHERE channel_id=c.channel_id)
        FROM jsonb_to_record(%s::jsonb) old(video_count bigint,channel_metadata_checked_at timestamptz)
        CROSS JOIN jsonb_to_record(%s::jsonb) new(channel_id text,video_count bigint,
            channel_metadata_checked_at timestamptz,channel_metadata_status text)
        WHERE c.channel_id=new.channel_id AND old.video_count IS NOT NULL AND new.video_count IS NOT NULL
        AND (old.video_count<>new.video_count OR (c.count_reconcile_token IS NULL
            AND c.count_reconcile_current>c.count_reconcile_previous AND new.video_count=c.count_reconcile_current))
        AND new.channel_metadata_status='ok'
        AND isfinite(new.channel_metadata_checked_at)
        AND (old.channel_metadata_checked_at IS NULL OR new.channel_metadata_checked_at>old.channel_metadata_checked_at)''',
        (old_text,new_text))
    if not changed or _resolve_growth(tx,new_text):
        return
    # Until the discovery runtime is enabled, the backend notification handler
    # (db/websub.py) makes a channel's playlists due for every announced upload.
    # Growth that the verified subscription announced in full is therefore not
    # signalled a second time. Once the runtime is enabled
    # the tally is never read and every increase is signalled.
    growth = tx.fetch_one('''SELECT new.channel_id,(new.video_count-old.video_count) AS increase
        FROM public.channels c CROSS JOIN jsonb_to_record(%s::jsonb) old(video_count bigint)
        CROSS JOIN jsonb_to_record(%s::jsonb) new(channel_id text,video_count bigint,
            channel_metadata_checked_at timestamptz,channel_metadata_status text)
        WHERE c.channel_id=new.channel_id AND new.channel_metadata_status='ok' AND new.video_count>old.video_count
          AND c.count_reconcile_observed_at=new.channel_metadata_checked_at AND c.count_reconcile_token IS NOT NULL
          AND EXISTS(SELECT 1 FROM public.discovery_runtime_control WHERE singleton AND NOT enabled)''',
        (old_text,new_text))
    if growth is not None and _announced(tx, growth['channel_id'], growth['increase']):
        return
    # Inactive collection retains bounded count-growth urgency, including a
    # format previously observed missing. This is one exact signal generation,
    # never a reset of full coverage/backoff evidence.
    tx.execute('''UPDATE public.channel_playlist_state p SET
        head_signal_token=c.count_reconcile_token,
        head_next_due_at=CASE WHEN EXISTS(SELECT 1 FROM public.discovery_runtime_control WHERE singleton AND enabled)
            THEN p.head_next_due_at ELSE least(coalesce(p.head_next_due_at,p.source_next_after,p.next_due_at),clock_timestamp()) END
        FROM public.channels c CROSS JOIN jsonb_to_record(%s::jsonb) old(video_count bigint)
        CROSS JOIN jsonb_to_record(%s::jsonb) new(channel_id text,video_count bigint,
            channel_metadata_checked_at timestamptz,channel_metadata_status text)
        WHERE p.channel_id=c.channel_id AND c.channel_id=new.channel_id
          AND new.channel_metadata_status='ok' AND new.video_count>old.video_count
          AND c.count_reconcile_observed_at=new.channel_metadata_checked_at
          AND c.count_reconcile_token IS NOT NULL''', (old_text,new_text))


def bind_scan(tx, scan_id, channel_id, video_type):
    # Caller owns the existing source/scan locks; no new lease or queue.
    tx.execute('''UPDATE public.channel_video_inventory_scans s SET count_reconcile_token=p.discovery_attempt_count_token,
        full_reconcile_token=p.discovery_attempt_full_token,discovery_channel_epoch=p.lease_channel_epoch FROM public.channels c
        JOIN public.channel_playlist_state p USING(channel_id)
        WHERE s.scan_id=%s::uuid AND c.channel_id=%s AND p.video_type=%s''',(scan_id,channel_id,video_type))


def serve_scan(tx, scan, result):
    """Same transaction as the exact source receipt, never a continuation ACK."""
    token = scan.get('count_reconcile_token')
    terminal = result.get('status')=='completed' or result.get('outcome')=='playlist_not_found'
    if not terminal:
        return
    if token:
        tx.execute('''UPDATE public.channel_playlist_state SET count_reconcile_served_token=%s::uuid,count_reconcile_served_epoch=%s
            WHERE channel_id=%s AND video_type=%s''',(token,scan.get('discovery_channel_epoch'),scan['channel_id'],scan['video_type']))
    full = (result.get('status')=='completed' and result.get('coverage')=='complete'
            and scan.get('scan_kind') in ('initial_inventory','full_reconciliation'))
    absent = (result.get('outcome')=='playlist_not_found' and result.get('error_code')=='typed_playlist_not_found'
              and result.get('first_page_absence_epoch') is not None
              and result['first_page_absence_epoch']==scan.get('discovery_channel_epoch'))
    if (full or absent) and scan.get('full_reconcile_token'):
        tx.execute('''UPDATE public.channel_playlist_state SET full_reconcile_served_token=%s::uuid
            WHERE channel_id=%s AND video_type=%s AND full_reconcile_required_token=%s::uuid''',
            (scan['full_reconcile_token'],scan['channel_id'],scan['video_type'],scan['full_reconcile_token']))


def settle_count_heads(database, channel_id, *, video_types):
    """Atomic delta/N decision plus full intent, after every enabled head served.

    A newer channel token is never settled by older scans. Missing/expired exact
    new-ID evidence chooses conservative full work rather than an invented N.
    """
    if not isinstance(video_types,(list,tuple)) or not video_types or set(video_types)-{'long','short','stream'}:
        fail('discovery_formats_invalid')
    with database.transaction(operation('discovery_count_settle')) as tx:
        if not tx.fetch_one('SELECT enabled FROM public.discovery_runtime_control WHERE singleton')['enabled']:
            return {'settled':False,'reason':'disabled'}
        channel=tx.fetch_one('''SELECT c.count_reconcile_token,c.count_reconcile_settled_token,
            CASE WHEN valid THEN c.count_reconcile_previous END AS count_reconcile_previous,
            CASE WHEN valid THEN c.count_reconcile_current END AS count_reconcile_current,
            CASE WHEN valid THEN c.count_reconcile_source_checked_at END AS lower_clock,
            CASE WHEN valid THEN c.count_reconcile_observed_at END AS upper_clock,
            control.control_epoch AS epoch FROM public.channels c
            JOIN public.channel_collection_state control USING(channel_id)
            CROSS JOIN LATERAL (SELECT coalesce(
                c.count_reconcile_epoch=control.control_epoch
                AND isfinite(c.count_reconcile_source_checked_at) AND isfinite(c.count_reconcile_observed_at)
                AND c.count_reconcile_source_checked_at>=statement_timestamp()-make_interval(hours=>%s)
                AND c.count_reconcile_source_checked_at<c.count_reconcile_observed_at
                AND c.count_reconcile_observed_at<=statement_timestamp(),false) AS valid) proof
            WHERE c.channel_id=%s FOR UPDATE OF c''', (RETENTION_HOURS,channel_id))
        if not channel or not channel['count_reconcile_token'] or channel['count_reconcile_token']==channel['count_reconcile_settled_token']:
            return {'settled':False,'reason':'no_demand'}
        token=channel['count_reconcile_token']
        from .inventory.inventory_heads import ABSENT_UNTIL_RECHECK_SQL
        sources=tx.fetch_all('''SELECT d.video_type,count_reconcile_served_token,count_reconcile_served_epoch,
            ('''+ABSENT_UNTIL_RECHECK_SQL+''') AS absent FROM public.channel_playlist_state d
            JOIN public.channel_collection_state c USING(channel_id)
            WHERE d.channel_id=%s AND d.runtime_enabled AND video_type=ANY(%s::text[]) ORDER BY video_type FOR UPDATE OF d''',(channel_id,list(video_types)),max_rows=3)
        if (set(s['video_type'] for s in sources)!=set(video_types)
                or any(s['count_reconcile_served_token']!=token or s['count_reconcile_served_epoch']!=channel['epoch'] for s in sources if not s['absent'])):
            return {'settled':False,'reason':'heads_pending'}
        # Addressed evidence can arrive before or after the count observation,
        # while publication still belongs strictly to its (lower,upper] window.
        # Post-count scan receipts retain the exact-generation requirement.
        # Disabled-Stream observations record a first positive proof, not an
        # inventory insertion or an assertion about Google's videoCount rules.
        # Old/unknown publications lose credit, never ordinary discovery.
        facts=tx.fetch_one(_CREDITS,dict(channel=channel_id,token=token,epoch=channel['epoch'],
            types=list(video_types),lower=channel['lower_clock'],upper=channel['upper_clock'],hours=RETENTION_HOURS))
        delta=(channel['count_reconcile_current']-channel['count_reconcile_previous']
               if channel['count_reconcile_current'] is not None and channel['count_reconcile_previous'] is not None else None)
        full=delta is None or (delta>0 and delta>facts['new_count'])
        if full:
            tx.execute('''UPDATE public.channel_playlist_state SET full_reconcile_required_token=%s::uuid,next_due_at=least(next_due_at,clock_timestamp())
                WHERE channel_id=%s AND runtime_enabled AND video_type=ANY(%s::text[])''',(token,channel_id,[s['video_type'] for s in sources if not s['absent']]))
        tx.execute('UPDATE public.channels SET count_reconcile_settled_token=%s::uuid WHERE channel_id=%s',(token,channel_id))
        return {'settled':True,'token':str(token),'full_requested':full,'new_count':None if delta is None else facts['new_count']}
