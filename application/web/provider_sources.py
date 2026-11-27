"""Owner-only source-layer routes: registry, preview, execute, daily cap status.

``GET /sources/registry`` and ``POST /sources/acquisitions/preview`` read no
YouTube data and send nothing (admission ``user_work``). ``POST
/sources/acquisitions/execute`` spends quota through the shared transport
(admission ``api_read`` like ``POST /add-channel``; the transport itself waits
for collection readiness): it needs the owner session, the X-Saved-Work write
header, the previewed plan fingerprint and the confirmed cost. A dropped
connection cancels the acquisition between and inside requests. Results stay in
request memory: no cache, no storage, no payload logging.
"""
import asyncio
from threading import Event

import anyio
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from provider_sources.channel_search import ChannelSearch
from provider_sources.errors import PlanRejected
from provider_sources.service import ServiceRefused
from provider_sources.wire import MAX_BODY_BYTES
from saved_work import SavedWorkError, validate_mutation_headers, validate_read_headers


def create_provider_sources_router(service, library_states=None):
    search = ChannelSearch(service, library_states)
    def authorize_owner(request: Request):
        if (getattr(request.state, "installation_access", False) is not True
                or not callable(getattr(request.state, "owner_access_recheck", None))):
            raise HTTPException(401, detail="authentication_required")

    router = APIRouter(prefix="/sources", dependencies=[Depends(authorize_owner)])

    def answer(value, status=200, headers=None):
        return JSONResponse(value, status_code=status, headers={"Cache-Control": "no-store", **(headers or {})})

    def refused(error):
        headers = {"Retry-After": str(error.retry_after)} if error.retry_after else None
        return answer(error.payload(), error.status, headers)

    def recheck(request):
        if request.state.owner_access_recheck() is None:
            raise ServiceRefused("policy_denied", 403)

    async def body(request):
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > MAX_BODY_BYTES:
                raise ServiceRefused("request_too_large", 413)
            raw.extend(chunk)
        return bytes(raw)

    @router.get("/registry")
    async def registry(request: Request, method: str | None = None):
        try:
            if set(request.query_params) - {"method"} or len(request.query_params.getlist("method")) > 1:
                raise ServiceRefused("request_invalid", 422)
            return answer(await anyio.to_thread.run_sync(lambda: service.registry_projection(method)))
        except ServiceRefused as error:
            return refused(error)

    @router.get("/acquisitions/status")
    async def status(request: Request):
        if request.query_params:
            return answer({"code": "request_invalid"}, 422)
        return answer(await anyio.to_thread.run_sync(service.status))

    @router.post("/acquisitions/preview")
    async def preview(request: Request):
        try:
            try:
                validate_read_headers(request.headers, request.url.scheme)
            except SavedWorkError:
                raise ServiceRefused("cross_origin", 403) from None
            raw = await body(request)
            return answer(await anyio.to_thread.run_sync(lambda: service.preview(raw)))
        except ServiceRefused as error:
            return refused(error)

    @router.post("/channel-search/preview")
    async def search_preview(request: Request):
        try:
            validate_read_headers(request.headers, request.url.scheme)
            raw = await body(request)
            owner = request.state.owner_access_recheck()
            if owner is None:
                raise ServiceRefused("policy_denied", 403)
            result = await anyio.to_thread.run_sync(lambda: search.preview(raw, owner))
            return answer(result)
        except SavedWorkError:
            return refused(ServiceRefused("cross_origin", 403))
        except PlanRejected as error:
            return refused(ServiceRefused(error.code, 422))
        except ServiceRefused as error:
            return refused(error)

    @router.post("/channel-search/execute")
    async def search_execute(request: Request):
        return await paid_action(request, finding=True)

    @router.post("/acquisitions/execute")
    async def execute(request: Request):
        return await paid_action(request, finding=False)

    async def paid_action(request, *, finding):
        cancelled = Event()
        monitor = None
        try:
            try:
                validate_mutation_headers(request.headers, request.url.scheme)
            except SavedWorkError:
                raise ServiceRefused("write_guard", 403) from None
            raw = await body(request)
            owner = request.state.owner_access_recheck()
            if owner is None:
                raise ServiceRefused("policy_denied", 403)

            async def watch_disconnect():
                # The body is read, so the next message can only be the disconnect. Wait for it:
                # polling request.is_disconnected() never sees it behind the runtime's
                # @app.middleware("http") wrapper (its receive is cancelled by the poll).
                try:
                    while not cancelled.is_set():
                        if (await request.receive()).get("type") == "http.disconnect":
                            cancelled.set()
                            return
                        await asyncio.sleep(.05)
                except Exception:
                    return

            monitor = asyncio.create_task(watch_disconnect())
            if finding:
                result = await anyio.to_thread.run_sync(lambda: search.execute(raw, owner, cancelled, request.state.owner_access_recheck))
            else:
                result = await anyio.to_thread.run_sync(lambda: service.execute(raw, cancelled=cancelled))
            recheck(request)   # owner access revoked meanwhile: the values are not handed out
            return answer(result)
        except PlanRejected as error:
            return refused(ServiceRefused(error.code, 422))
        except ServiceRefused as error:
            return refused(error)
        finally:
            cancelled.set()
            if monitor is not None:
                monitor.cancel()
                try:
                    await monitor
                except asyncio.CancelledError:
                    pass

    return router
