"""A manual full-list request shares the collector's per-format lease and budget."""
from psycopg.types.json import Jsonb

from ..channel.channel_control import assert_channel_scope
from ..discovery import operation
from saved_work import channel_id as canonical_channel_id

# Trusted aliases d=playlist, c=channel control. A paused installation keeps the intent.
MANUAL_PENDING_SQL = """coalesce((d.manual_list_check->>'state' IN ('queued','running')
 AND d.manual_list_check->'epoch'=to_jsonb(c.control_epoch)
 AND EXISTS(SELECT 1 FROM public.discovery_runtime_control WHERE singleton AND enabled)),false)"""
FULL_KINDS = ('initial_inventory', 'full_reconciliation')


class ManualCheckRefused(Exception):
    def __init__(self, reason, status):
        self.reason, self.status = reason, status
        super().__init__(reason)


def request_check(database, channel, kind, epoch, previous, request_id):
    channel = canonical_channel_id(channel)
    with database.transaction(operation('library_request_list_check')) as tx:
        # Serialize against another tab and the existing scan owner before reading its status.
        source = tx.fetch_one("""SELECT active_scan_id FROM public.channel_playlist_state
            WHERE channel_id=%s AND video_type=%s FOR UPDATE""", (channel, kind))
        state = tx.fetch_one("""SELECT control_epoch,unavailable_reason,request
            FROM readable.library_manual_list_check WHERE channel_id=%s AND video_type=%s""", (channel, kind))
        if not source or not state:
            raise ManualCheckRefused('source_unavailable', 404)
        if state['control_epoch'] != epoch or state['unavailable_reason']:
            raise ManualCheckRefused(state['unavailable_reason'] or 'source_changed', 409)
        current = state['request'] or {}
        if current.get('id') == request_id or current.get('state') in ('queued','running'):
            assert_channel_scope(tx, channel, epoch)
            return {'request': current, 'joined': True}
        if current.get('id') != previous:
            raise ManualCheckRefused('request_changed', 409)
        scan = tx.fetch_one("""SELECT s.scan_id::text AS id,s.pages_completed
            FROM public.channel_video_inventory_scans s JOIN readable.channel_scan_sources proof USING(scan_id)
            WHERE s.scan_id=%s AND s.discovery_channel_epoch=%s AND s.status='running'
            AND s.channel_id=%s AND s.video_type=%s AND s.token_history_verified
            AND s.scan_kind IN ('initial_inventory','full_reconciliation')""",
            (source['active_scan_id'], epoch, channel, kind))
        request = dict(id=request_id, epoch=epoch, state='running' if scan else 'queued',
                       scan=scan['id'] if scan else None, pages=scan['pages_completed'] if scan else 0,
                       general=0, wait=None)
        tx.execute("""UPDATE public.channel_playlist_state SET manual_list_check=%s
                      WHERE channel_id=%s AND video_type=%s""", (Jsonb(request), channel, kind))
        assert_channel_scope(tx, channel, epoch)
        return {'request': request, 'joined': bool(scan)}


def bind(tx, source, scan_id, scan_kind):
    request = source.get('manual_list_check') or {}
    if (scan_kind not in FULL_KINDS or request.get('state') not in ('queued','running')
            or request['epoch'] != source['lease_channel_epoch']):
        return
    tx.execute("""UPDATE public.channel_playlist_state SET manual_list_check=manual_list_check ||
        jsonb_build_object('state','running','scan',%s::text,'wait',NULL,'retry_at',NULL,'pages',0)
        WHERE channel_id=%s AND video_type=%s""", (str(scan_id), source['channel_id'], source['video_type']))


def settle(tx, source, scan, result):
    request = source.get('manual_list_check') or {}
    if (request.get('state') != 'running' or request.get('scan') != str(scan['scan_id'])
            or request['epoch'] != source['lease_channel_epoch']
            or request['epoch'] != scan.get('discovery_channel_epoch')):
        return
    pages = result.get('pages_completed', scan['pages_completed'])
    first_absent = (result.get('outcome') == 'playlist_not_found'
                    and result.get('error_code') == 'typed_playlist_not_found'
                    and pages == 0 and scan['resume_page_token'] is None and scan['token_history_verified'])
    if first_absent:
        state = 'absent'
    elif result['status'] == 'completed' and result['coverage'] == 'complete':
        state = 'complete'
    elif result['status'] == 'running' or result.get('scan_retained'):
        state = 'running'
    else:
        state = 'failed'
    retrying = result.get('scan_retained') and state == 'running'
    retry_at = result.get('next_due_at') if retrying else None
    request.update(state=state, pages=pages, retry_at=str(retry_at) if retry_at else None,
                   general=request['general'] + result['provider_attempts'], wait='retry' if retrying else None)
    tx.execute("""UPDATE public.channel_playlist_state SET manual_list_check=%s
                  WHERE channel_id=%s AND video_type=%s""", (Jsonb(request), source['channel_id'], source['video_type']))


def record_wait(database, claim, reason, retry_at):
    with database.transaction(operation('library_check_budget_wait')) as tx:
        tx.execute("""UPDATE public.channel_playlist_state SET manual_list_check=manual_list_check ||
            jsonb_build_object('wait',%s::text,'retry_at',%s::timestamptz)
            WHERE channel_id=%s AND video_type=%s AND lease_token=%s AND lease_channel_epoch=%s
            AND manual_list_check->'epoch'=to_jsonb(lease_channel_epoch)
            AND manual_list_check->>'state' IN ('queued','running')
            AND (manual_list_check->>'wait' IS DISTINCT FROM %s
                 OR (manual_list_check->>'retry_at')::timestamptz IS DISTINCT FROM %s::timestamptz)""",
            (reason, retry_at, claim.channel_id, claim.video_type, claim.lease_token, claim.lease_channel_epoch,
             reason, retry_at))
