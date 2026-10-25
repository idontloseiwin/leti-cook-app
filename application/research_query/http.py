"""Unmounted API adapter. Owner authentication and service binding are mandatory.

Stage 1.6 must mount these routes in the existing HOT/read-lifetime pipeline and
classify POST execute as api_read. Importing this module changes no live routes.
"""
import asyncio
from threading import Event

import anyio
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from .contracts import MAX_BODY_BYTES
from .service import instant
from .validation import QueryRejected, capabilities


def create_router(service_provider, authorize_owner):
    router = APIRouter(prefix="/research", dependencies=[Depends(authorize_owner)])

    async def body(request):
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > MAX_BODY_BYTES:
                raise QueryRejected("query_budget_exceeded")
            raw.extend(chunk)
        return bytes(raw)

    def response(value, status=200):
        return JSONResponse(value, status_code=status, headers={"Cache-Control": "no-store"})

    @router.get("/capabilities")
    async def get_capabilities():
        try:
            return response(capabilities(service_provider().context_provider()))
        except QueryRejected as error:
            return response(error.payload(), error.status)

    @router.post("/queries/validate")
    async def validate_query(request: Request):
        try:
            plan = service_provider().validate(await body(request))
            return response({"valid": True, "query_version": 1, "query_fingerprint": plan.fingerprint,
                             "referenced_fields": plan.referenced_fields})
        except QueryRejected as error:
            return response(error.payload(), error.status)

    @router.post("/queries/execute")
    async def execute_query(request: Request):
        cancelled = Event()
        monitor = None
        try:
            raw = await body(request)
            service = service_provider()

            async def watch_disconnect():
                while True:
                    if await request.is_disconnected():
                        cancelled.set()
                        return
                    await asyncio.sleep(.05)

            monitor = asyncio.create_task(watch_disconnect())
            result = await anyio.to_thread.run_sync(lambda: service.execute(raw, cancelled=cancelled.is_set))
            answer = response(result)
            if instant(service.clock()) >= instant(result["context"]["valid_before"]):
                raise QueryRejected("cursor_expired")
            return answer
        except QueryRejected as error:
            return response(error.payload(), error.status)
        finally:
            cancelled.set()
            if monitor is not None:
                monitor.cancel()
                try:
                    await monitor
                except asyncio.CancelledError:
                    pass

    return router
