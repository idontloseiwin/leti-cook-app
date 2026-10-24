"""One request-body limit for every HTTP route; backup restores allow more.

Routes with their own smaller caps (access, setup, saved work, reviews) keep them;
this is the outer bound for everything else, so no handler buffers an unbounded
body. A declared Content-Length is refused before any byte is read; a streamed
body is counted as it arrives.
"""
from __future__ import annotations

import json

DEFAULT_BODY_LIMIT = 1024 * 1024
# The restore routes check their own 16 MiB cap first and answer with their own
# code; this outer bound only stops bodies that bypass it.
BACKUP_BODY_LIMIT = 17 * 1024 * 1024
PATH_BODY_LIMITS = {
    "/user-work/restore": BACKUP_BODY_LIMIT,
    "/user-work/restore/preview": BACKUP_BODY_LIMIT,
}
TOO_LARGE = json.dumps({"code": "request_too_large"}).encode()


class RequestBodyTooLarge(Exception):
    pass


def _declared_length(scope):
    for name, value in scope.get("headers") or ():
        if name.lower() == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


async def _reject(send):
    await send({"type": "http.response.start", "status": 413, "headers": [
        (b"content-type", b"application/json"), (b"cache-control", b"no-store"),
        (b"content-length", str(len(TOO_LARGE)).encode()),
    ]})
    await send({"type": "http.response.body", "body": TOO_LARGE})


class RequestBodyLimitMiddleware:
    def __init__(self, app, *, default_limit=DEFAULT_BODY_LIMIT, path_limits=None):
        self.app = app
        self.default_limit = default_limit
        self.path_limits = dict(PATH_BODY_LIMITS if path_limits is None else path_limits)

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        limit = self.path_limits.get(scope.get("path", ""), self.default_limit)
        declared = _declared_length(scope)
        if declared is not None and declared > limit:
            return await _reject(send)
        received = 0
        exceeded = started = replaced = False

        async def limited_receive():
            nonlocal received, exceeded
            if exceeded:
                raise RequestBodyTooLarge()
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body") or b"")
                if received > limit:
                    exceeded = True
                    raise RequestBodyTooLarge()
            return message

        async def limited_send(message):
            nonlocal started, replaced
            if replaced:
                return
            if message.get("type") == "http.response.start":
                started = True
                if exceeded:
                    # FastAPI answers a failed body read with its own 400; the client is told 413.
                    replaced = True
                    return await _reject(send)
            await send(message)

        try:
            await self.app(scope, limited_receive, limited_send)
        except RequestBodyTooLarge:
            if not started:
                await _reject(send)
            elif not replaced:
                raise
