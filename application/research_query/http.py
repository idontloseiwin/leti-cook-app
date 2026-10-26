"""Bounded API adapter. Owner authentication and service binding are mandatory."""
import asyncio
from threading import Event

import anyio
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from hot_storage import register_read_deadline

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
        headers = {"Cache-Control": "no-store"}
        if status == 503 and value.get('code') == 'catalogue_preparing':
            headers['Retry-After'] = '2'
        return JSONResponse(value, status_code=status, headers=headers)

    @router.get("/capabilities")
    async def get_capabilities(request: Request):
        try:
            service = service_provider(request)
            context = await anyio.to_thread.run_sync(service.context_provider)
            return response(capabilities(context))
        except QueryRejected as error:
            return response(error.payload(), error.status)

    @router.post("/queries/validate")
    async def validate_query(request: Request):
        try:
            raw = await body(request)
            service = service_provider(request)
            plan = await anyio.to_thread.run_sync(lambda: service.validate(raw))
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
            service = service_provider(request)

            async def watch_disconnect():
                # Request.is_disconnected uses its own cancellation scope. A
                # simultaneous task cancellation can be consumed there, so the
                # shared stop flag must also terminate this loop.
                while not cancelled.is_set():
                    if await request.is_disconnected():
                        cancelled.set()
                        return
                    await asyncio.sleep(.05)

            monitor = asyncio.create_task(watch_disconnect())
            result = await anyio.to_thread.run_sync(lambda: service.execute(raw, cancelled=cancelled.is_set))
            register_read_deadline(instant(result["context"]["valid_before"]))
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
