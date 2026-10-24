"""Private Saved views service and isolated HTTP routes; no startup DB probe."""
from __future__ import annotations

from hot_storage import utc_instant

from saved_work import (
    SavedObjectService, SavedWorkError, channel_id, decode_cursor, encode_cursor,
    invalid, normalize_conditions, revision, saved_id, saved_name, unavailable,
    saved_response, saved_pagination, saved_delete_revision,
)


class SavedViewsService(SavedObjectService):
    kind = "view"

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
                "channel_id": channel_id(row["channel_id"]), "conditions": normalize_conditions(row["conditions"]),
                "revision": revision(row["revision"]), "created_at": row["created_at"], "updated_at": row["updated_at"],
                "channel_available": row["channel_id"] in available,
            }
        except SavedWorkError as error:
            if error.status_code == 410:
                raise
            raise unavailable() from error
        except (ValueError, TypeError, KeyError, OverflowError) as error:
            raise unavailable() from error

    def _envelope(self, row, expected_id=None):
        self._identity(row, expected_id)
        if row.get("deleted_at") is not None:
            raise SavedWorkError(410, "view_deleted", "This saved view was deleted.")
        available = self._call(self.repository.load_available_channels, [row.get("channel_id")])
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
        available = self._call(self.repository.load_available_channels, [row.get("channel_id") for row in page])
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

    # Resolve locally imported Request for postponed annotations.
    for handler in (list_views, get_view, create_view, rename_view, delete_view):
        handler.__annotations__["request"] = Request
    router.add_api_route("", list_views, methods=["GET"])
    router.add_api_route("", create_view, methods=["POST"])
    router.add_api_route("/{view_id}", get_view, methods=["GET"])
    router.add_api_route("/{view_id}", rename_view, methods=["PATCH"])
    router.add_api_route("/{view_id}", delete_view, methods=["DELETE"])
    return router
