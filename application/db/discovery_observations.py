"""Explicit negative outcomes and bounded resumes, never successful VIDEO ACKs."""
from .discovery import _claims, _update, _uuid, locked_claim, operation, fail
from .channel.channel_read_policy import CHANNEL_CONTROL_SQL
from typed_playlist import VIDEO_ID_RE


def terminal(tx, claim, outcome):
    _update(tx, claim, "status='skipped',outcome=%s,processed_at=clock_timestamp(),"
        "work_phase='settled',wait_reason=NULL,claim_token=NULL,claim_expires_at=NULL,"
        "paused_at=NULL,pause_reason=NULL,review_required=CASE WHEN %s THEN false ELSE review_required END,"
        "resume_served_token=resume_token", (outcome, outcome == 'no_public_stats_or_membership'))


def request_proof(tx, row, request_id, observed_at, *, metadata=False):
    if (request_id is None or observed_at is None
            or str(row['source_request_id']) != _uuid(request_id)
            or row['work_phase'] == 'owner_wait'):
        fail('discovery_request_stale')
    from .inventory.inventory_scan_steps import _hot
    _hot(tx, observed_at)
    if not tx.fetch_one('SELECT %s::timestamptz BETWEEN %s::timestamptz AND clock_timestamp() AS valid',
            (observed_at, row['source_request_started_at']))['valid']:
        fail('discovery_source_clock_invalid')
    if metadata and (str(row['discovery_attempt_request_id']) != str(request_id)
                     or row['metadata_source_checked_at'] != observed_at):
        fail('discovery_metadata_observation_required')



def _record_disabled_stream_positive(tx, rows, *, request_id, observed_at, playlist_id, items):
    """One positive source receipt; no catalogue/type/queue ownership acquired.

    The same per-ID lock as canonical insertion fences first observation. An
    existing canonical ID or earlier positive diagnostic is not a new credit.
    The existing receipt owner scrubs this entire payload after its source TTL.
    """
    from hashlib import sha256
    import json
    from psycopg.types.json import Jsonb
    from .discovery_counts import credit_candidates
    channels={row['channel_id'] for row in rows}
    epochs={row['claim_channel_epoch'] for row in rows}
    if len(channels)!=1 or len(epochs)!=1:
        fail('discovery_membership_scope_invalid')
    channel=next(iter(channels));epoch=next(iter(epochs))
    ids=sorted({row['video_id'] for row in rows})
    actual=[dict(video_id=item['video_id'],published_at=item.get('published_at'))
            for item in items if item['video_id'] in ids]
    fingerprint=sha256(json.dumps([channel,epoch,playlist_id,str(observed_at),actual],
        sort_keys=True,separators=(',',':')).encode()).hexdigest()
    transition=_uuid(request_id)
    tx.fetch_one('SELECT pg_advisory_xact_lock(hashtextextended(%s,781631247))',(transition,))
    prior=tx.fetch_one("SELECT result FROM public.typed_inventory_receipts WHERE receipt_scope='addressed_membership' AND transition_id=%s::uuid",(transition,))
    if prior:
        if prior['result'].get('request_hash')!=fingerprint:
            fail('discovery_receipt_conflict')
        return
    for identity in ids:
        tx.fetch_one('SELECT pg_advisory_xact_lock(hashtextextended(%s,962106))',(identity,))
    existing={row['video_id'] for row in tx.fetch_all('SELECT video_id FROM public.video_inventory WHERE video_id=ANY(%s::text[])',
        (ids,),max_rows=50)}
    seen={row['video_id'] for row in tx.fetch_all("""SELECT id AS video_id FROM unnest(%s::text[]) id
        WHERE EXISTS(SELECT 1 FROM public.typed_inventory_receipts r
         WHERE r.receipt_scope='addressed_membership' AND r.result->>'channel_id'=%s
           AND r.result->>'observation_kind'='disabled_stream_positive'
           AND jsonb_typeof(r.result->'video_ids')='array' AND r.result->'video_ids' ? id)""",(ids,channel),max_rows=50)}
    first=[identity for identity in ids if identity not in existing|seen]
    result=dict(channel_id=channel,control_epoch=epoch,video_type='stream',playlist_id=playlist_id,
        observation_kind='disabled_stream_positive',video_ids=ids,first_observed_video_ids=first,
        new_video_ids=[],count_credit_candidates=credit_candidates(first,actual,observed_at),
        request_id=transition,request_hash=fingerprint)
    tx.execute("INSERT INTO public.typed_inventory_receipts(receipt_scope,transition_id,result,source_checked_at) "
        "VALUES('addressed_membership',%s::uuid,%s::jsonb,%s)",(transition,Jsonb(result),observed_at))


def terminal_observation(database, claims, outcome, *, request_id=None, observed_at=None,
                         observed_channel_id=None, playlist_id=None, items=None,
                         enabled_types=('long', 'short'), metadata_request_id=None, metadata_at=None, checked_types=()):
    """Reject a mismatched envelope or record one scoped negative observation.

    A terminal observation never says that a video was loaded. Provider facts
    are scoped to a persisted request/accepted metadata round; the service passes
    the actual parsed response. Unknown entry omissions remain pending.
    """
    claims = _claims(claims)
    if outcome not in ('channel_mismatch', 'unknown_deletion_checked', 'format_off', 'no_public_stats_or_membership'):
        fail('discovery_terminal_outcome_invalid')
    enabled = frozenset(enabled_types)
    if not {'long', 'short'} <= enabled <= {'long', 'short', 'stream'}:
        fail('discovery_formats_invalid')
    if items is not None and (not isinstance(items, (list, tuple)) or len(items) > 50
            or any(not isinstance(item, dict) for item in items)):
        fail('discovery_terminal_observation_invalid')
    if outcome=='format_off' and (items is None
            or {item.get('video_id') for item in items}!={claim['video_id'] for claim in claims}
            or len({item.get('video_id') for item in items})!=len(items)):
        fail('discovery_terminal_observation_invalid')
    settled = []
    positive = []
    with database.transaction(operation('discovery_terminal_observation')) as tx:
        for claim in claims:
            row = locked_claim(tx, claim, fresh=True)
            if row['work_phase'] == 'owner_wait':
                continue
            if outcome == 'channel_mismatch':
                if request_id is None:
                    existing = tx.fetch_one('SELECT channel_id FROM public.video_inventory WHERE video_id=%s FOR SHARE',
                                            (row['video_id'],))
                    valid = existing is not None and existing['channel_id'] != row['channel_id']
                else:
                    request_proof(tx, row, request_id, observed_at, metadata=True)
                    matches = [item for item in (items or ()) if item.get('id') == row['video_id']]
                    snippet=matches[0].get('snippet') if len(matches)==1 else None
                    channel=snippet.get('channelId') if isinstance(snippet,dict) else None
                    import re
                    valid = isinstance(channel, str) and channel != row['channel_id'] and re.fullmatch(r'UC[A-Za-z0-9_-]{22}',channel) is not None
            elif outcome == 'no_public_stats_or_membership':
                from youtube_source_fields import public_video_without_view_count
                request_proof(tx, row, request_id, observed_at)
                valid = (claim['origin'] == 'websub' and row['kind'] == 'entry'
                         and metadata_request_id is not None and metadata_at is not None
                         and str(row['discovery_attempt_request_id']) == _uuid(metadata_request_id)
                         and row['metadata_source_checked_at'] == metadata_at
                         and metadata_at <= observed_at and set(checked_types) == enabled
                         and len(items or ()) == 1 and items[0].get('id') == row['video_id']
                         and public_video_without_view_count(items[0])
                         and items[0]['snippet'].get('channelId') == row['channel_id'])
                if valid:
                    tx.fetch_one('SELECT pg_advisory_xact_lock(hashtextextended(%s,962106))', (row['video_id'],))
                    valid = tx.fetch_one('SELECT 1 FROM public.video_inventory WHERE video_id=%s',
                                         (row['video_id'],)) is None
            elif outcome == 'unknown_deletion_checked':
                request_proof(tx, row, request_id, observed_at, metadata=True)
                # The exact frozen deleted notification is the authority for
                # this negative outcome; an entry cannot borrow its semantics.
                valid = (row['kind'] == 'deleted' and items is not None
                         and all(item.get('id') != row['video_id'] for item in items)
                         and tx.fetch_one('SELECT video_id FROM public.video_inventory WHERE video_id=%s FOR SHARE',
                                          (row['video_id'],)) is None)
            else:
                request_proof(tx, row, request_id, observed_at)
                valid = ('stream' not in enabled and playlist_id == 'UULV' + row['channel_id'][2:]
                         and items is not None and sum(item.get('video_id') == row['video_id'] for item in items) == 1
                         and all(VIDEO_ID_RE.fullmatch(str(item.get('video_id', ''))) for item in items))
            if valid:
                if outcome=='format_off':positive.append((claim,row))
                else:
                    terminal(tx, claim, outcome)
                    settled.append(claim['work_id'])
        if positive:
            _record_disabled_stream_positive(tx,[row for _,row in positive],request_id=request_id,
                observed_at=observed_at,playlist_id=playlist_id,items=items)
            for claim,_ in positive:
                terminal(tx,claim,outcome)
                settled.append(claim['work_id'])
    return settled


def pause(database, claims, reason):
    if reason not in ('long_wait', 'availability_retry_expired'):
        fail('discovery_pause_reason_invalid')
    with database.transaction(operation('discovery_pause')) as tx:
        for claim in _claims(claims):
            row = locked_claim(tx, claim)
            if reason == 'availability_retry_expired':
                target = tx.fetch_one('SELECT paused_reason FROM public.video_collection_state WHERE video_id=%s AND channel_id=%s FOR SHARE',
                                      (row['video_id'], row['channel_id']))
                if not target or target['paused_reason'] != reason:
                    fail('discovery_pause_proof_missing')
            _update(tx, claim, 'paused_at=coalesce(paused_at,clock_timestamp()),pause_reason=%s,'
                    'wait_reason=%s,review_required=true,resume_served_token=resume_token,'
                    'claim_token=NULL,claim_expires_at=NULL', (reason, reason))
    return len(claims)


def resume_matching(tx, channel_id, video_ids):
    """One fresh signal/finding can resume paused work without resetting age.

    Source writers hold source/ID locks before this hook; SKIP LOCKED prevents
    inversion with discovery's event-first lock order. An in-flight obligation
    retains its current owner. New WebSub events also remain independent work.
    """
    ids = list(dict.fromkeys(video_ids))
    # Empty successful pages and pages containing only existing IDs have no
    # newly discovered work to resume; they must not touch the database here.
    if not ids:
        return 0
    if not 1 <= len(ids) <= 50 or any(not isinstance(i, str) or VIDEO_ID_RE.fullmatch(i) is None for i in ids):
        fail('discovery_resume_ids_invalid')
    if not tx.fetch_one('SELECT enabled FROM public.discovery_runtime_control WHERE singleton')['enabled']:
        return 0
    eligible = tx.fetch_one('SELECT typed_runtime_eligible FROM (' + CHANNEL_CONTROL_SQL + ') policy WHERE channel_id=%s', (channel_id,))
    if not eligible or not eligible['typed_runtime_eligible']:
        return 0
    changed = 0
    for table, key in (('websub_events', 'event_id'), ('discovery_repair_targets', 'target_id')):
        changed += tx.execute(f'''UPDATE public.{table} target SET resume_token=gen_random_uuid(),next_try_at=clock_timestamp()
            WHERE {key} IN (SELECT {key} FROM public.{table} WHERE channel_id=%s AND video_id=ANY(%s::text[])
                AND status='pending' AND paused_at IS NOT NULL
                AND (claim_token IS NULL OR claim_expires_at<=clock_timestamp())
                ORDER BY {key} LIMIT 500 FOR UPDATE SKIP LOCKED)''', (channel_id, ids))
    return changed


def resume(database, channel_id, video_ids):
    """Internal explicit retry only; no new public route or owner bypass."""
    with database.transaction(operation('discovery_resume')) as tx:
        return resume_matching(tx, channel_id, video_ids)
