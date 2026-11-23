"""Bounded API adapter. Owner/scoped-agent authentication and service binding are mandatory."""
import asyncio
from threading import Event

import anyio
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from hot_storage import register_read_deadline

from .contracts import MAX_BODY_BYTES
from .service import instant
from .validation import QueryRejected, capabilities


def create_router(service_provider, authorize_owner, source_search=None):
    router = APIRouter(prefix="/research", dependencies=[Depends(authorize_owner)])

    async def body(request):
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > MAX_BODY_BYTES:
                raise QueryRejected("query_budget_exceeded", limit="request")
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
            if set(request.query_params) - {'source', 'describe'} or any(
                    len(request.query_params.getlist(k)) != 1 for k in request.query_params):
                raise QueryRejected('invalid_query')
            if request.query_params.get('describe', '0') not in ('0', '1'):
                raise QueryRejected('invalid_query')
            service = service_provider(request)
            context = await anyio.to_thread.run_sync(service.context_provider)
            from .discovery import describe
            build = describe if request.query_params.get('describe') == '1' else capabilities
            return response(build(context,request.query_params.get('source','public.videos.current.v1')))
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
        return await execute_read(request,"query")

    @router.get('/contracts')
    async def get_contracts(request: Request):
        from .discovery import contracts
        try:
            if set(request.query_params) - {'section', 'operation'} or any(
                    len(request.query_params.getlist(key)) != 1 for key in request.query_params):
                raise QueryRejected('invalid_query')
            service = service_provider(request)
            context = await anyio.to_thread.run_sync(service.context_provider)
            return response(contracts(context, request.query_params.get('section', 'query'), request.query_params.get('operation')))
        except QueryRejected as error:
            return response(error.payload(), error.status)

    @router.get('/sources')
    async def get_sources(request: Request):
        try:
            params = request.query_params
            if set(params) - {'kind', 'search', 'limit', 'after'} or any(len(params.getlist(key)) != 1 for key in params):
                raise QueryRejected('invalid_query')
            kind, search, after = params.get('kind', 'folder'), params.get('search', ''), params.get('after', '')
            limit_text = params.get('limit', '20')
            if (kind not in ('folder', 'label', 'collection') or len(search) > 120 or len(after) > 96
                    or len(limit_text) > 2 or not limit_text.isascii() or not limit_text.isdigit() or not 1 <= int(limit_text) <= 50
                    or any(ord(c) < 32 for c in search + after)):
                raise QueryRejected('invalid_query')
            if source_search is None:
                raise QueryRejected('backend_unavailable')
            service = service_provider(request)
            before = await anyio.to_thread.run_sync(service.context_provider)
            result = await anyio.to_thread.run_sync(lambda: source_search(kind, search, int(limit_text), after))
            after_context = await anyio.to_thread.run_sync(service.context_provider)
            if after_context.access_revision != before.access_revision:
                raise QueryRejected('policy_denied')
            return response(result)
        except QueryRejected as error:
            return response(error.payload(), error.status)

    @router.post("/queries/frozen-ids")
    async def frozen_ids(request: Request):
        # Select all matching: every row ID of one frozen result, in order.
        return await execute_read(request,"frozen_ids")

    @router.post("/operations/execute")
    async def execute_operation(request: Request):
        return await execute_read(request,"operation")

    @router.post("/graphs/validate")
    async def validate_research(request:Request):
        try:
            from .graph import validate_graph
            raw=await body(request);service=service_provider(request)
            plan=await anyio.to_thread.run_sync(lambda:validate_graph(raw,service.context_provider()))
            return response({"valid":True,"graph_fingerprint":plan.fingerprint,"order":plan.order,"drafts":sorted(plan.drafts)})
        except QueryRejected as error:return response(error.payload(),error.status)

    @router.post("/graphs/execute")
    async def execute_research(request:Request):
        return await execute_read(request,"graph")

    async def execute_read(request:Request,kind:str):
        cancelled = Event()
        monitor = None
        try:
            raw = await body(request)
            service = service_provider(request)

            async def watch_disconnect():
                # The body is read, so the next message can only be the disconnect: wait for it.
                # Polling request.is_disconnected() never sees it behind the runtime's
                # @app.middleware("http") wrapper, whose receive the poll's own cancellation
                # scope cancels. The shared stop flag still ends this loop.
                try:
                    while not cancelled.is_set():
                        if (await request.receive()).get("type") == "http.disconnect":
                            cancelled.set()
                            return
                        await asyncio.sleep(.05)
                except Exception:
                    return

            monitor = asyncio.create_task(watch_disconnect())
            execute=(service.execute_graph if kind=="graph" else service.execute_operation if kind=="operation"
                     else service.frozen_ids if kind=="frozen_ids" else service.execute)
            result = await anyio.to_thread.run_sync(lambda: execute(raw, cancelled=cancelled.is_set))
            register_read_deadline(instant(result["context"]["valid_before"]))
            answer = response(result)
            if instant(service.clock()) >= instant(result["context"]["valid_before"]):
                raise QueryRejected("cursor_expired")
            if kind == 'query':
                import support_journal
                receipt = support_journal.api_result(result)
                if receipt:
                    answer.headers[support_journal.HEADER] = receipt
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
