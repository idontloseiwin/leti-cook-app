"""Private Saved views service and isolated HTTP routes; no startup DB probe."""
from __future__ import annotations

from hot_storage import utc_instant

from saved_work import (
    SavedObjectService, SavedWorkError, channel_id, decode_cursor, encode_cursor,
    invalid, normalize_conditions, revision, saved_id, saved_name, saved_scopes, unavailable,
    saved_response, saved_pagination, saved_delete_revision, translate_library_conditions,
)


def _scope_key(scope):
    """How the availability reads name a scope: a channel by its ID, anything else as kind:id."""
    return scope["id"] if scope["kind"] == "channel" else scope["kind"] + ":" + scope["id"]


class SavedViewsService(SavedObjectService):
    kind = "view"

    def rename(self,object_id,body):
        if type(body) is dict and "conditions" in body:
            from saved_work import exact_object
            data=exact_object(body,("name","conditions","expected_revision"))
            identity=saved_id(object_id);conditions=normalize_conditions(data["conditions"])
            if conditions["version"] not in (3,4):raise invalid("Only research definitions can be edited.")
            result=self._call(self.repository.update_research,identity,saved_name(data["name"]),conditions,revision(data["expected_revision"]))
            return self._lifecycle_result(result,identity)
        return super().rename(object_id,body)

    def _view(self, row, available):
        try:
            self._identity(row)
            if row.get("deleted_at") is not None:
                raise SavedWorkError(410, "view_deleted", "This saved view was deleted.")
            for key in ("created_at", "updated_at"):
                if not isinstance(row.get(key), str):
                    raise ValueError()
                utc_instant(row[key])
            return {
                "id": saved_id(row["id"]), "kind": "view", "name": saved_name(row["name"]),
                "channel_id": channel_id(row["channel_id"]) if row["channel_id"] is not None else None, "conditions": normalize_conditions(row["conditions"]),
                "revision": revision(row["revision"]), "created_at": row["created_at"], "updated_at": row["updated_at"],
                "channel_available": row["channel_id"] in available if row["channel_id"] is not None
                    else all(_scope_key(scope) in available for scope in saved_scopes(normalize_conditions(row["conditions"]))),
            }
        except SavedWorkError as error:
            if error.status_code == 410:
                raise
            raise unavailable() from error
        except (ValueError, TypeError, KeyError, OverflowError) as error:
            raise unavailable() from error

    def _availability(self, rows):
        # A canvas can read up to 16 sources: channel sources are checked with the bound channels, the others by
        # scope, in reads of at most 100.
        channels, scopes = [row.get("channel_id") for row in rows], {}
        for row in rows:
            if row.get("channel_id") is None:
                for scope in saved_scopes(normalize_conditions(row["conditions"])):
                    if scope["kind"] == "channel":
                        channels.append(scope["id"])
                    else:
                        scopes[_scope_key(scope)] = scope
        channels, scopes, available = sorted({c for c in channels if c is not None}), list(scopes.values()), set()
        for start in range(0, len(channels), 100):
            available |= self._call(self.repository.load_available_channels, channels[start:start + 100])
        for start in range(0, len(scopes), 100):
            available |= self._call(self.repository.load_available_scopes, scopes[start:start + 100])
        return available

    def _envelope(self, row, expected_id=None):
        self._identity(row, expected_id)
        if row.get("deleted_at") is not None:
            raise SavedWorkError(410, "view_deleted", "This saved view was deleted.")
        available = self._availability([row])
        return {"contract_version": 1, "view": self._view(row, available)}

    def _outcome(self, result):
        if not isinstance(result, dict):
            raise unavailable()
        outcome = result.get("outcome")
        if not isinstance(outcome, str):
            raise unavailable()
        if outcome in {"ready", "deleted"}:
            return result
        errors = {
            "conflict": (409, "view_conflict", "This saved view changed or its ID was already used. Reload before editing."),
            "not_found": (404, "view_not_found", "Saved view not found."),
            "gone": (410, "view_deleted", "This saved view was deleted."),
            "channel_unavailable": (404, "channel_unavailable", "The channel is not available."),
        }
        if outcome in errors:
            raise SavedWorkError(*errors[outcome])
        raise unavailable()

    def list(self, limit=50, cursor=None):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise invalid("Saved views page size must be between 1 and 100.")
        rows = self._call(self.repository.list_objects, limit + 1, decode_cursor(cursor))
        if not isinstance(rows, list) or len(rows) > limit + 1 or any(not isinstance(row, dict) for row in rows):
            raise unavailable()
        page = rows[:limit]
        if len({str(row.get("id")) for row in rows}) != len(rows):
            raise unavailable()
        available = self._availability(page)
        views = [self._view(row, available) for row in page]
        next_cursor = encode_cursor(views[-1]["created_at"], views[-1]["id"]) if len(rows) > limit else None
        return {"contract_version": 1, "views": views, "next_cursor": next_cursor}


def create_saved_views_router(repository):
    from fastapi import APIRouter, Request

    router, service = APIRouter(prefix="/saved/views"), SavedViewsService(repository)

    async def list_views(request: Request):
        return await saved_response(request, lambda: service.list(*saved_pagination(request)))

    async def get_view(view_id: str, request: Request):
        return await saved_response(request, service.get, view_id)

    async def create_view(request: Request):
        return await saved_response(request, service.create, body=True, write=True)

    async def rename_view(view_id: str, request: Request):
        return await saved_response(request, service.rename, view_id, body=True, write=True)

    async def delete_view(view_id: str, request: Request):
        return await saved_response(request, lambda: service.delete(view_id, saved_delete_revision(request)), write=True)

    async def translate_view(request: Request):
        # A read: v1 conditions in, the shared query (or missing capabilities) out.
        return await saved_response(request, translate_library_conditions, body=True)

    # Resolve locally imported Request for postponed annotations.
    for handler in (list_views, get_view, create_view, rename_view, delete_view, translate_view):
        handler.__annotations__["request"] = Request
    router.add_api_route("", list_views, methods=["GET"])
    router.add_api_route("", create_view, methods=["POST"])
    router.add_api_route("/translate", translate_view, methods=["POST"])
    router.add_api_route("/{view_id}", get_view, methods=["GET"])
    router.add_api_route("/{view_id}", rename_view, methods=["PATCH"])
    router.add_api_route("/{view_id}", delete_view, methods=["DELETE"])
    return router
