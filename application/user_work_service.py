"""Self-hosted user-work backup HTTP boundary; no implicit provider calls or retries."""
from __future__ import annotations

import json
from db.base.postgres import DatabaseUnavailable
from saved_work import SavedWorkError, validate_mutation_headers, validate_read_headers, channel_id, revision
from user_channel_input import normalize_user_channel_input
from user_work_backup import (
    MAX_BYTES, UserWorkError, build_backup, normalize_backup, import_payload,
    decode_backup, uid, rows, unique,
)

# Non-backup bodies: pending edits, resolutions and the export request, whose
# browser preferences hold at most 10,000 channel IDs and 10,000 pending refs.
REQUEST_MAX_BYTES = 1024 * 1024
# A malformed request is not a malformed backup: it gets its own code.
INVALID_REQUEST = 'invalid_user_work_request'


def request_fields(value, fields):
    if type(value) is not dict or set(value) != set(fields):
        raise UserWorkError(INVALID_REQUEST)
    return value


class UserWorkService:
    def __init__(self, repository):
        self.repository = repository

    def call(self, method, *args):
        try:
            return method(*args)
        except (UserWorkError, DatabaseUnavailable):
            raise
        except Exception as error:
            if getattr(error, 'message', None) in ('user_work_export_limit', 'user_work_import_limit', 'user_work_limit_reached'):
                raise UserWorkError('user_work_limit_exceeded', 413) from None
            raise UserWorkError('user_work_unavailable', 503) from None

    def outcome(self, result):
        if not isinstance(result, dict):
            raise UserWorkError('user_work_unavailable', 503)
        outcome = result.get('outcome')
        if outcome in ('ready', 'restored', 'resolved'):
            return result
        if outcome in ('workspace_not_empty', 'conflict', 'parent_pending', 'channel_unavailable', 'needs_input', 'busy', 'parent_missing', 'hidden', 'lease_lost'):
            raise UserWorkError(outcome, 409)
        if outcome == 'not_found':
            raise UserWorkError(outcome, 404)
        raise UserWorkError('user_work_unavailable', 503)

    def export(self, body):
        request_fields(body, ('browser_preferences',))
        return build_backup(self.call(self.repository.snapshot), body['browser_preferences'])

    def status(self, pending_offset=0):
        if type(pending_offset) is not int or not 0 <= pending_offset <= 10000:
            raise UserWorkError('invalid_pending_offset')
        result = self.call(self.repository.status) if pending_offset == 0 else self.call(self.repository.status, pending_offset)
        if not isinstance(result, dict) or type(result.get('can_restore')) is not bool:
            raise UserWorkError('user_work_unavailable', 503)
        mappings = [{'id': uid(r['id']), 'channel_id': channel_id(r['channel_id'])} for r in result['channel_mappings']]
        user_refs = [uid(value) for value in rows(result['user_channel_refs'], 10000)]
        folder_ids = [uid(value) for value in rows(result['folder_ids'], 1000)]
        unique(user_refs)
        unique(folder_ids)
        live = {r['id'] for r in mappings}
        pending = [{'id': uid(r['id']), 'revision': revision(r['revision']), 'user_input': r['user_input'],
                    'parent_ref': uid(r['parent_ref'], nullable=True), 'is_hidden': r['is_hidden'],
                    'reconnect_state': r['reconnect_state'], 'reconnect_not_before': r['reconnect_not_before'],
                    'reconnect_error_code': r['reconnect_error_code'],
                    'reason': 'needs_input' if r['user_input'] is None else
                        ('parent_pending' if r['parent_ref'] is not None and r['parent_ref'] not in live else 'ready')}
                   for r in result['pending'][:100]]
        return {'available': True, 'can_restore': result['can_restore'], 'pending': pending,
                'pending_more': bool(result.get('pending_more')) or len(result['pending']) > 100,
                'channel_mappings': mappings, 'user_channel_refs': user_refs, 'folder_ids': folder_ids,
                'reconnect': result['reconnect']}

    def preview(self, document, *, normalized=False):
        document = document if normalized else normalize_backup(document)
        # The same size check as a restore, so a preview never promises more.
        import_payload(document, normalized=True)
        can_restore = self.status()['can_restore']
        return {'counts': {k: len(v) for k, v in document['user_work'].items()},
                'can_restore': can_restore, 'reason': None if can_restore else 'workspace_not_empty',
                'backup': document}

    def restore(self, document, *, normalized=False):
        document = document if normalized else normalize_backup(document)
        # Every structural/size/provenance validation precedes the single atomic write.
        self.outcome(self.call(self.repository.import_document, import_payload(document, normalized=True)))
        return {'status': 'restored', 'pending_count': len(document['user_work']['channels']),
                'browser_preferences': document['browser_preferences']}

    # The HTTP routes run these in the thread pool: one decode and one
    # normalization per request, none of it on the event loop.
    def preview_backup(self, data):
        return self.preview(decode_backup(data), normalized=True)

    def restore_backup(self, data):
        return self.restore(decode_backup(data), normalized=True)

    def edit_pending(self, item_id, body):
        request_fields(body, ('expected_revision', 'user_input', 'parent_ref'))
        identity = uid(item_id)
        try:
            locator = None if body['user_input'] is None else normalize_user_channel_input(body['user_input'])
        except ValueError:
            raise UserWorkError('invalid_user_input') from None
        parent = uid(body['parent_ref'], nullable=True)
        if parent == identity:
            raise UserWorkError('cyclic_channel_relationship')
        result = self.outcome(self.call(self.repository.update_pending, identity,
                              revision(body['expected_revision']), locator, parent))
        return {'status': 'updated', 'id': identity, 'revision': revision(result['item']['revision'])}

    def enqueue_reconnect(self, body, item_id=None):
        if item_id is None:
            request_fields(body, ('expected_snapshot',))
        else:
            request_fields(body, ('expected_revision',))
            item_id = uid(item_id)
            body = {'expected_revision': revision(body['expected_revision'])}
        result = self.outcome(self.call(self.repository.enqueue_reconnect, body, item_id))
        return {'status': 'queued', **{key: result[key] for key in (
            'snapshot','queued_count','blocked_count','already_requested_count','excluded_hidden')}}

    def prepare_resolution(self, item_id, body):
        request_fields(body, ('expected_revision',))
        identity, expected = uid(item_id), revision(body['expected_revision'])
        item = self.call(self.repository.pending_item, identity)
        if item is None:
            raise UserWorkError('not_found', 404)
        if item['revision'] != expected:
            raise UserWorkError('conflict', 409)
        if item['user_input'] is None:
            raise UserWorkError('needs_input', 409)
        if item['parent_ref'] is not None and item['parent_ref'] not in {r['id'] for r in self.status()['channel_mappings']}:
            raise UserWorkError('parent_pending', 409)
        return identity, expected, normalize_user_channel_input(item['user_input'])

    def finish_resolution(self, identity, expected, prepared_channel):
        canonical_id = channel_id(prepared_channel['channel_id'])
        self.outcome(self.call(self.repository.bind_pending, identity, expected, canonical_id, prepared_channel))
        return {'status': 'resolved', 'id': identity, 'channel_id': canonical_id}


def create_user_work_router(repository, *, resolve_channel=None, on_change=None, after_bind=None, on_reconnect=None):
    from fastapi import APIRouter, Request
    from fastapi.responses import JSONResponse
    from starlette.concurrency import run_in_threadpool

    router, service = APIRouter(prefix='/user-work'), UserWorkService(repository)

    async def read_body(request, *, backup=False):
        """Read a size-capped body; a backup stays raw bytes for decoding in the thread pool."""
        if request.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/json':
            raise UserWorkError('json_body_required', 415)
        limit = MAX_BYTES if backup else REQUEST_MAX_BYTES
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > limit:
                raise UserWorkError('user_work_limit_exceeded', 413)
            data.extend(chunk)
        if backup:
            return bytes(data)
        def pairs(values):
            result = dict(values)
            if len(result) != len(values):
                raise UserWorkError('duplicate_json_key')
            return result
        try:
            return json.loads(data.decode('utf-8'), object_pairs_hook=pairs,
                              parse_constant=lambda _: (_ for _ in ()).throw(UserWorkError(INVALID_REQUEST)))
        except (ValueError, UnicodeError, RecursionError):
            raise UserWorkError(INVALID_REQUEST) from None

    async def reply(request, action, *, success_status=200):
        try:
            (validate_read_headers if request.method == 'GET' else validate_mutation_headers)(request.headers, request.url.scheme)
            result = await action()
            # Backup exports and previews can be large; encode their JSON off-loop too.
            return await run_in_threadpool(JSONResponse, result, status_code=success_status, headers={'Cache-Control': 'no-store'})
        except DatabaseUnavailable as error:
            unknown = error.outcome_unknown is True
            return JSONResponse({'code': 'database_write_outcome_unknown' if unknown else 'database_unavailable',
                                 'outcome_unknown': unknown}, status_code=503,
                                headers={'Cache-Control':'no-store', **({'Retry-After':'5'} if request.method=='GET' else {})})
        except (UserWorkError, SavedWorkError) as error:
            return JSONResponse({'code': error.code}, status_code=getattr(error, 'status', getattr(error, 'status_code', 422)),
                                headers={'Cache-Control': 'no-store'})
        except Exception:
            # Do not place provider data, parsed backups or credentials in logs/HTTP.
            return JSONResponse({'code': 'user_work_unavailable'}, status_code=503,
                                headers={'Cache-Control': 'no-store'})

    async def status(request: Request):
        async def action():
            raw_offset = request.query_params.get('pending_offset', '0')
            if not raw_offset.isascii() or not raw_offset.isdigit() or len(raw_offset) > 5:
                raise UserWorkError('invalid_pending_offset')
            return await run_in_threadpool(service.status, int(raw_offset))
        return await reply(request, action)

    async def backup(request: Request):
        async def action():
            return await run_in_threadpool(service.export, await read_body(request))
        return await reply(request, action)

    async def preview(request: Request):
        async def action():
            return await run_in_threadpool(service.preview_backup, await read_body(request, backup=True))
        return await reply(request, action)

    def notify_change():
        # A cache notification cannot turn an acknowledged commit into a failed
        # import response; all source reads still enforce their own admission.
        if on_change is not None:
            try:
                on_change()
            except Exception:
                pass

    async def restore(request: Request):
        async def action():
            result = await run_in_threadpool(service.restore_backup, await read_body(request, backup=True))
            notify_change()
            return result
        return await reply(request, action)

    async def edit(item_id: str, request: Request):
        async def action():
            return await run_in_threadpool(service.edit_pending, item_id, await read_body(request))
        return await reply(request, action)

    def wake_reconnect():
        if on_reconnect is not None:
            try:
                on_reconnect()
            except Exception:
                pass  # Durable intent already committed; wake is only a hint.

    async def resolve(item_id: str, request: Request):
        async def action():
            result = await run_in_threadpool(service.enqueue_reconnect, await read_body(request), item_id)
            wake_reconnect()
            notify_change()
            return result
        return await reply(request, action, success_status=202)

    async def reconnect(request: Request):
        async def action():
            result = await run_in_threadpool(service.enqueue_reconnect, await read_body(request))
            wake_reconnect()
            notify_change()
            return result
        return await reply(request, action, success_status=202)

    for handler in (status, backup, preview, restore, edit, resolve, reconnect):
        handler.__annotations__['request'] = Request
    router.add_api_route('/status', status, methods=['GET'])
    router.add_api_route('/backup', backup, methods=['POST'])
    router.add_api_route('/restore/preview', preview, methods=['POST'])
    router.add_api_route('/restore', restore, methods=['POST'])
    router.add_api_route('/pending/{item_id}', edit, methods=['PATCH'])
    router.add_api_route('/pending/{item_id}/resolve', resolve, methods=['POST'])
    router.add_api_route('/pending/reconnect', reconnect, methods=['POST'])
    return router
