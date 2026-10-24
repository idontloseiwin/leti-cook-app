"""Read-lifetime and HOT runtime HTTP guards, installed before owner access."""
import json

from starlette.requests import Request
from fastapi.responses import JSONResponse
from deployment_profile import selected_deployment_profile
from hot_storage import (
    capture_read_lifetime,
    utc_now as hot_read_now,
    iso_utc as hot_iso_utc,
    HotSourceReadExpired,
)
from youtube_errors import KEY_REFUSALS
from db.base.read_context import http_read_scope
from db.base.postgres import DatabaseUnavailable
from hot_runtime import runtime_request_access
import ui_read_trace


async def _source_envelope(response, *, read_at, expires_at):
    """Add response evidence to finite JSON, preserving independent item clocks.

    JSONResponse has already rendered a bounded application result. Only that
    declared body is collected; event streams and unbounded JSON streams are
    never buffered. Array responses retain their public shape and use headers.
    """
    declared = response.headers.get("content-length", "")
    if not declared.isdecimal() or response.headers.get("content-encoding"):
        raise HotSourceReadExpired("A source response must be finite JSON")
    size = int(declared)
    body = getattr(response, "body", None)
    if body is None:
        chunks, received = [], 0
        async for chunk in response.body_iterator:
            received += len(chunk)
            if received > size:
                raise HotSourceReadExpired("Source response size changed")
            chunks.append(chunk)
        body = b"".join(chunks)
    if len(body) != size:
        raise HotSourceReadExpired("Source response size changed")
    value = json.loads(body)
    if isinstance(value, dict):
        value["source_read_at"] = read_at
        value["source_expires_at"] = expires_at
        body = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")

    async def delivery():
        yield body

    if hasattr(response, "body_iterator"):
        response.body_iterator = delivery()
    else:
        response.body = body
    response.headers["content-length"] = str(len(body))
    return response


def _expired_read_response(*, invalid_envelope=False):
    if invalid_envelope:
        ui_read_trace.event("response_serialization", status="error",
                            reason="invalid_response_envelope")
    else:
        ui_read_trace.failure("source_envelope", HotSourceReadExpired())
    return JSONResponse(status_code=503, content={"detail": "Source data expired during this read; retry."},
                        headers={"Cache-Control": "no-store"})


class HttpReadScopeMiddleware:
    """Cover setup and owner middleware as well as the main API's read paths."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return
        with http_read_scope(), runtime_request_access(scope.get("method", ""), scope.get("path", "")):
            await ui_read_trace.observe_http(self.app, scope, receive, send)


def install_runtime_middleware(app, *, core):
    if hasattr(app, "middleware"):
        @app.middleware("http")
        async def no_store_application_data(request, call_next):
            with capture_read_lifetime() as lifetime:
                response = await call_next(request)
            if response.headers.get("content-type", "").split(";", 1)[0] == "application/json":
                now = hot_read_now()
                if lifetime.expires_at is not None and lifetime.expires_at <= now:
                    return _expired_read_response()
                response.headers["Cache-Control"] = "no-store"
                response.headers["X-Source-Read-At"] = hot_iso_utc(now)
                if lifetime.expires_at is not None:
                    response.headers["X-Source-Expires-At"] = hot_iso_utc(lifetime.expires_at)
                    try:
                        response = await _source_envelope(
                            response, read_at=hot_iso_utc(now), expires_at=hot_iso_utc(lifetime.expires_at),
                        )
                    except (HotSourceReadExpired, ValueError, TypeError):
                        return _expired_read_response(invalid_envelope=True)
                    if lifetime.expires_at <= hot_read_now():
                        return _expired_read_response()
            return response

    if hasattr(app, "exception_handler"):
        @app.exception_handler(DatabaseUnavailable)
        async def database_unavailable(request: Request, error):
            ui_read_trace.failure("request_guard", error)
            headers = {"Cache-Control": "no-store"}
            if request.method in {"GET", "HEAD"} and not error.outcome_unknown:
                headers["Retry-After"] = "5"
            # A lost commit acknowledgement must not invite a blind write retry.
            # Existing command receipts remain the authority for its outcome.
            code = "database_write_outcome_unknown" if error.outcome_unknown else "database_unavailable"
            return JSONResponse(status_code=503, content={
                "code": code, "outcome_unknown": error.outcome_unknown,
            }, headers=headers)

        @app.exception_handler(HotSourceReadExpired)
        async def source_read_expired(request, error):
            ui_read_trace.failure("source_envelope", error)
            return JSONResponse(status_code=503, content={"detail": "Source data changed during this read; retry."}, headers={"Cache-Control": "no-store"})

    if selected_deployment_profile() == "self-hosted":
        from hot_runtime import HotRuntimeMiddleware, HotRuntimeUnavailable, signed_status
        app.add_middleware(HotRuntimeMiddleware)

        @app.exception_handler(HotRuntimeUnavailable)
        async def hot_runtime_unavailable(request: Request, error):
            return JSONResponse({"code": "hot_runtime_not_ready"}, status_code=503,
                                headers={"Cache-Control": "no-store", "Retry-After": "5"})

        from self_hosted_settings import SettingsError

        @app.exception_handler(SettingsError)
        async def provider_key_refused(request: Request, error):
            # A refused stored key stops a request before anything is sent. Other setup errors stay server errors.
            if str(error) not in KEY_REFUSALS:
                raise error
            return JSONResponse({"code": str(error)}, status_code=409, headers={"Cache-Control": "no-store"})

        @app.get("/internal/hot-runtime")
        async def hot_runtime_proof(request: Request):
            if request.client is None or request.client.host not in {"127.0.0.1", "::1"}:
                return JSONResponse({"code": "internal_only"}, status_code=403, headers={"Cache-Control": "no-store"})
            runtime = getattr(app.state, "hot_runtime", None)
            if runtime is None:
                return JSONResponse({"code": "hot_runtime_not_ready"}, status_code=503, headers={"Cache-Control": "no-store"})
            return JSONResponse(signed_status(runtime, admission=core.HOT_ADMISSION_CONFIG), headers={"Cache-Control": "no-store"})

        @app.get("/internal/update-status")
        async def update_runtime_proof(request: Request):
            if request.client is None or request.client.host not in {"127.0.0.1", "::1"}:
                return JSONResponse({"code": "internal_only"}, status_code=403, headers={"Cache-Control": "no-store"})
            from update_runtime_status import signed_update_status
            from update_runtime_protocol import UpdateRuntimeError
            from starlette.concurrency import run_in_threadpool
            try:
                result = await run_in_threadpool(signed_update_status,
                    getattr(app.state, "hot_runtime", None), getattr(app.state, "setup_manager", None),
                    admission=core.HOT_ADMISSION_CONFIG)
                return JSONResponse(result, headers={"Cache-Control": "no-store"})
            except UpdateRuntimeError:
                return JSONResponse({"code": "update_runtime_unavailable"}, status_code=503,
                                    headers={"Cache-Control": "no-store"})

        @app.get("/system/hot-runtime")
        async def hot_runtime_status():
            runtime = getattr(app.state, "hot_runtime", None)
            result = runtime.status() if runtime is not None else {"status": "blocked", "maintenance_failed": False}
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
