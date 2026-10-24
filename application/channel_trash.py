"""Source-free trash and separate expiring title enrichment. Undo never renews sources."""
from channels.service import TrashError, remove_channel, restore_channel
from fastapi import APIRouter, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from hot_storage import HOT_DATA_WINDOW, utc_now, utc_instant, iso_utc
from saved_work import SavedWorkError, saved_id, channel_id, validate_read_headers, validate_mutation_headers



def create_channel_trash_router(repository, *, on_change=None, after_restore=None, clock=utc_now):
    router = APIRouter(prefix="/user-work/deleted-channels")

    async def respond(request, action):
        try:
            (validate_read_headers if request.method == "GET" else validate_mutation_headers)(request.headers, request.url.scheme)
            return JSONResponse(await action(), headers={"Cache-Control": "no-store"})
        except (TrashError, SavedWorkError) as error:
            return JSONResponse({"code": error.code}, status_code=getattr(error, "status", getattr(error, "status_code", 422)), headers={"Cache-Control": "no-store"})
        except Exception:
            # A transport failure may follow a committed mutation. Never retry.
            return JSONResponse({"code": "trash_unavailable"}, status_code=503, headers={"Cache-Control": "no-store"})


    def changed(identity):
        if on_change is not None:
            # Mutation is already committed; do not report an ambiguous failure
            # merely because a notification failed.
            try: on_change(identity)
            except Exception: pass

    @router.get("")
    async def listing(request: Request):
        async def action():
            after = request.query_params.get("after")
            if after is not None: after = saved_id(after)
            rows = await run_in_threadpool(repository.list_removed, after)
            now = utc_instant(clock())
            items = []
            for row in rows[:100]:
                stamp = row.get("hidden_at")
                deadline = None
                try:
                    removed = utc_instant(stamp)
                    if removed <= now: deadline = removed + HOT_DATA_WINDOW
                except (ValueError, TypeError, OverflowError): pass
                locator = row.get("user_input")
                if locator is not None and (not isinstance(locator, str) or len(locator)>2048):
                    raise TrashError("trash_unavailable",503)
                items.append({"id": saved_id(row["user_work_id"]), "user_input": locator,
                              "removed_at": iso_utc(stamp) if deadline else None,
                              "restore_until": iso_utc(deadline) if deadline else None,
                              "can_restore": deadline is not None and now < deadline})
            return {"items": items, "next_cursor": items[-1]["id"] if len(rows)>100 else None}
        return await respond(request, action)

    @router.get("/titles")
    async def titles(request: Request):
        async def action():
            values = request.query_params.getlist("ids")
            if len(values) != 1 or len(values[0]) > 3700:
                raise TrashError("invalid_request")
            identities = values[0].split(",")
            if not 1 <= len(identities) <= 100:
                raise TrashError("invalid_request")
            identities = [saved_id(value) for value in identities]
            if len(set(identities)) != len(identities):
                raise TrashError("invalid_request")
            rows = await run_in_threadpool(repository.removed_titles, identities)
            items = []
            for row in rows:
                identity = saved_id(row["user_work_id"])
                if identity not in identities:
                    raise TrashError("trash_unavailable", 503)
                title, deadline = row.get("title"), row.get("source_expires_at")
                if isinstance(title, str) and title and deadline and utc_instant(deadline) > utc_instant(clock()):
                    items.append({"id": identity, "title": title, "source_expires_at": iso_utc(deadline)})
            return {"items": items}
        return await respond(request, action)

    @router.post("/remove/{identity}")
    async def remove(identity: str, request: Request):
        async def action():
            identifier = channel_id(identity)
            removed_id = await run_in_threadpool(remove_channel, identifier, repository=repository)
            changed(removed_id)
            return {"status": "removed"}
        return await respond(request, action)

    @router.post("/{identity}/restore")
    async def restore(identity: str, request: Request, background_tasks: BackgroundTasks):
        async def action():
            identifier = saved_id(identity)
            if request.headers.get("content-type", "").split(";")[0].lower() != "application/json":
                raise TrashError("json_body_required",415)
            content = bytearray()
            async for part in request.stream():
                content.extend(part)
                if len(content)>256: raise TrashError("invalid_request",413)
            import json
            try:
                def unique_pairs(pairs):
                    result = dict(pairs)
                    if len(result) != len(pairs): raise ValueError()
                    return result
                body = json.loads(content, object_pairs_hook=unique_pairs)
                if not isinstance(body,dict) or set(body)!={"removed_at"}: raise ValueError()
                stamp = iso_utc(body["removed_at"])
            except (ValueError,TypeError,OverflowError): raise TrashError("invalid_request") from None
            restored_id = await run_in_threadpool(restore_channel, identifier, stamp, repository=repository)
            changed(restored_id)
            if after_restore is not None:
                try: await run_in_threadpool(after_restore, restored_id, background_tasks)
                except Exception: pass
            return {"status": "restored"}
        return await respond(request, action)
    return router
