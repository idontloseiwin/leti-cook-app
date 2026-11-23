"""Finite owner-only reads and untrusted browser reports for the support journal."""
import json

import anyio
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
import support_journal

router = APIRouter()


def _owner(request):
    check = getattr(request.state, 'owner_access_recheck', None)
    if getattr(request.state, 'installation_access', False) is not True or not callable(check) or check() is None:
        raise HTTPException(401, detail='authentication_required')


@router.get('/diagnostics/discovery-journal')
async def recent(request: Request):
    await anyio.to_thread.run_sync(_owner, request)
    result = await anyio.to_thread.run_sync(support_journal.snapshot)
    await anyio.to_thread.run_sync(_owner, request)
    return JSONResponse(result, headers={'Cache-Control': 'no-store'})


@router.post('/diagnostics/discovery-journal/browser')
async def browser(request: Request):
    # Owner rechecks can read the local authentication store; keep them off the
    # event loop. The remaining parse/map/enqueue work is bounded and in memory.
    await anyio.to_thread.run_sync(_owner, request)
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > 2048:
            raise HTTPException(413, detail='request_too_large')
        raw.extend(chunk)
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise HTTPException(400, detail='invalid_report') from None
    await anyio.to_thread.run_sync(_owner, request)
    support_journal.browser_report(value)
    return Response(status_code=204, headers={'Cache-Control': 'no-store'})
