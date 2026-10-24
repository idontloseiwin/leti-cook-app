"""Private explicit-ID Collections; stored membership is not a metrics snapshot."""
from __future__ import annotations

import base64
import json
import re
from functools import partial

from hot_storage import HOT_DATA_MAX_AGE, iso_utc, source_is_current, utc_instant, utc_now

from channel_explorer import PUBLIC_VIDEO_FIELDS, _json_integer
from saved_work import (
    SavedObjectService, SavedWorkError, channel_id, decode_cursor, encode_cursor,
    exact_object, invalid, revision, saved_id, saved_name,
    fingerprint, saved_response, saved_pagination, saved_delete_revision,
)
from youtube_categories import youtube_video_category_name
from video_thumbnails import video_thumbnail_url
from source_expiry import captured_source_call, minimum_expiry
from youtube_source_fields import youtube_counter_integer, youtube_duration_seconds

SOURCE_UNAVAILABLE_REASONS = frozenset({
    "inventory_missing", "type_unverified", "type_conflict", "type_unsupported",
    "channel_unavailable", "source_missing", "source_mismatch", "source_unavailable",
    "source_unpublished", "source_stale",
})


def collection_unavailable():
    return SavedWorkError(503, "saved_work_unavailable", "Collections are unavailable. No change is confirmed.")


def collection_changed():
    return SavedWorkError(409, "collection_changed", "This collection changed. Reload it before continuing.")


def video_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", value):
        raise invalid("A valid video ID is required.")
    return value


def video_ids(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 500:
        raise invalid("A collection command must contain 1 to 500 video IDs.")
    result = sorted({video_id(item) for item in value})
    if len(result) != len(value):
        raise invalid("A collection command cannot contain duplicate video IDs.")
    return result


def timestamp(value):
    if not isinstance(value, str):
        raise invalid()
    try:
        # PostgreSQL may omit fractional trailing zeros; the shared parser
        # preserves these instants on Python 3.10 as well as newer Python.
        return utc_instant(value).isoformat(timespec="microseconds")
    except (ValueError, OverflowError) as error:
        raise invalid() from error


def member_cursor(collection, member):
    raw = json.dumps({"version": 1, "id": collection["id"], "revision": collection["revision"],
                      "after": [member["added_at"], member["video_id"]]}, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def parse_member_cursor(value, collection):
    if value is None:
        return None
    try:
        if not isinstance(value, str) or len(value) > 512 or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise invalid()
        data = exact_object(json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))), ("version", "id", "revision", "after"))
        if type(data["version"]) is not int or data["version"] != 1 or saved_id(data["id"]) != collection["id"]:
            raise invalid("Collection cursor does not match this collection.")
        if revision(data["revision"]) != collection["revision"]:
            raise collection_changed()
        if not isinstance(data["after"], list) or len(data["after"]) != 2:
            raise invalid()
        return timestamp(data["after"][0]), video_id(data["after"][1])
    except (ValueError, TypeError, OverflowError) as error:
        raise invalid("Collection cursor is invalid.") from error


class SavedCollectionsService(SavedObjectService):
    kind = "collection"
    unavailable_error = staticmethod(collection_unavailable)

    def __init__(self, repository, *, now=None):
        super().__init__(repository)
        self.now = now or utc_now

    def _metadata(self, row, expected_id=None):
        self._identity(row, expected_id)
        if row.get("deleted_at") is not None:
            raise SavedWorkError(410, "collection_deleted", "This collection was deleted.")
        try:
            identity = saved_id(row["id"])
            if expected_id is not None and identity != expected_id:
                raise invalid()
            count = row["member_count"]
            if type(count) is not int or not 0 <= count <= 9007199254740991:
                raise invalid()
            timestamp(row["created_at"])
            timestamp(row["updated_at"])
            return {"id": identity, "kind": "collection", "name": saved_name(row["name"]),
                    "revision": revision(row["revision"]), "member_count": count,
                    "created_at": row["created_at"], "updated_at": row["updated_at"]}
        except (SavedWorkError, KeyError, TypeError) as error:
            raise collection_unavailable() from error

    def _load(self, collection_id):
        row = self._call(self.repository.get_object, collection_id)
        if row is None:
            raise SavedWorkError(404, "collection_not_found", "Collection not found.")
        return self._metadata(row, collection_id)

    def _outcome(self, result):
        if not isinstance(result, dict) or not isinstance(result.get("outcome"), str):
            raise collection_unavailable()
        if result["outcome"] in {"ready", "deleted"}:
            return result
        errors = {
            "conflict": (409, "collection_conflict", "This collection changed or the command ID was already used. Reload before editing."),
            "gone": (410, "collection_deleted", "This collection was deleted."),
            "not_found": (404, "collection_not_found", "Collection not found."),
            "source_unavailable": (409, "collection_sources_unavailable", "Some selected videos are unavailable. This batch was not added."),
        }
        if result["outcome"] in errors:
            raise SavedWorkError(*errors[result["outcome"]])
        raise collection_unavailable()

    def _envelope(self, row, expected_id=None):
        return {"contract_version": 1, "collection": self._metadata(row, expected_id)}

    def change_members(self, collection_id, action, body):
        identity = saved_id(collection_id)
        if action not in {"add", "remove"}:
            raise invalid()
        data = exact_object(body, ("request_id", "expected_revision", "video_ids"))
        request_id, expected, ids = saved_id(data["request_id"]), revision(data["expected_revision"]), video_ids(data["video_ids"])
        digest = fingerprint({"id": identity, "request_id": request_id, "action": action, "expected_revision": expected, "video_ids": ids})
        result = self._outcome(self._call(self.repository.change_members, identity, request_id, action, ids, expected, digest))
        meta = self._metadata(result.get("collection"), identity)
        try:
            ack = exact_object(result.get("mutation"), ("request_id", "replayed", "requested_count", "changed_count", "applied_revision"))
            if ack["request_id"] != request_id or type(ack["replayed"]) is not bool:
                raise invalid()
            if type(ack["requested_count"]) is not int or ack["requested_count"] != len(ids):
                raise invalid()
            if type(ack["changed_count"]) is not int or not 0 <= ack["changed_count"] <= len(ids):
                raise invalid()
            applied = revision(ack["applied_revision"])
            # A command that changed no member keeps the revision it expected.
            if (applied != expected + (1 if ack["changed_count"] else 0) or applied > meta["revision"]
                    or (not ack["replayed"] and applied != meta["revision"])):
                raise invalid()
        except SavedWorkError as error:
            raise collection_unavailable() from error
        return {"contract_version": 1, "collection": meta, "mutation": dict(ack)}

    def list(self, limit=50, cursor=None):
        self._limit(limit)
        rows = self._call(self.repository.list_objects, limit + 1, decode_cursor(cursor))
        self._page(rows, limit, "id")
        page = [self._metadata(row) for row in rows[:limit]]
        return {"contract_version": 1, "collections": page,
                "next_cursor": encode_cursor(page[-1]["created_at"], page[-1]["id"]) if len(rows) > limit else None}

    @staticmethod
    def _limit(limit):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise invalid("Collection page size must be between 1 and 100.")

    @staticmethod
    def _page(rows, limit, key):
        if not isinstance(rows, list) or len(rows) > limit + 1 or any(not isinstance(row, dict) or not isinstance(row.get(key), str) for row in rows):
            raise collection_unavailable()
        if len({row[key] for row in rows}) != len(rows):
            raise collection_unavailable()

    def _member_page(self, collection_id, limit, cursor):
        self._limit(limit)
        identity = saved_id(collection_id)
        meta = self._load(identity)
        after = parse_member_cursor(cursor, meta)
        rows = self._call(self.repository.list_members, identity, limit + 1, after)
        self._page(rows, limit, "video_id")
        page = rows[:limit]
        try:
            for row in rows:
                video_id(row["video_id"])
                timestamp(row["added_at"])
        except (SavedWorkError, KeyError) as error:
            raise collection_unavailable() from error
        return identity, meta, after, rows, page

    def _confirm_member_page(self, identity, meta, rows, limit, after):
        current = self._load(identity)
        if current["revision"] != meta["revision"]:
            raise collection_changed()
        if meta["member_count"] < len(rows) or (after is None and len(rows) <= limit and meta["member_count"] != len(rows)):
            raise collection_unavailable()

    def member_identities(self, collection_id, limit=50, cursor=None):
        """Local saved IDs remain editable while API reads are paused."""
        identity, meta, after, rows, page = self._member_page(collection_id, limit, cursor)
        self._confirm_member_page(identity, meta, rows, limit, after)
        return {"contract_version": 1, "collection": meta,
                "members": [{"video_id": row["video_id"], "added_at": row["added_at"]} for row in page],
                "next_cursor": member_cursor(meta, page[-1]) if len(rows) > limit else None}

    def members(self, collection_id, limit=50, cursor=None):
        identity, meta, after, rows, page = self._member_page(collection_id, limit, cursor)
        ids = [row["video_id"] for row in page]
        read_at = utc_instant(self.now())
        as_of = iso_utc(read_at)
        sources = self._call(self.repository.load_sources, ids, as_of=as_of) if ids else []
        if not isinstance(sources, list) or len(sources) != len(ids):
            raise collection_unavailable()
        seen, members = set(), []
        for member, source in zip(page, sources):
            if not isinstance(source, dict) or source.get("video_id") != member["video_id"] or source.get("video_id") in seen:
                raise collection_unavailable()
            seen.add(source["video_id"])
            expires_at = None
            status, reason = source.get("source_status"), source.get("source_reason")
            if status == "unavailable":
                if reason not in SOURCE_UNAVAILABLE_REASONS or source.get("channel_id") is not None or source.get("video_row") is not None:
                    raise collection_unavailable()
                source_id, video = None, None
            elif status == "available" and reason is None:
                try:
                    source_id = channel_id(source.get("channel_id"))
                    raw = exact_object(source.get("video_row"), (*PUBLIC_VIDEO_FIELDS,
                        "metadata_checked_at", "availability_checked_at", "membership_checked_at", "source_expires_at"))
                    if raw["video_id"] != member["video_id"]:
                        raise invalid()
                    try:
                        clocks = [utc_instant(raw[field]) for field in
                                  ("checked_at", "metadata_checked_at", "availability_checked_at", "membership_checked_at")]
                        deadline = utc_instant(raw["source_expires_at"])
                        fresh = all(source_is_current(clock, now=read_at) for clock in clocks)
                        fresh = fresh and deadline == min(clocks) + HOT_DATA_MAX_AGE
                    except (ValueError, TypeError, OverflowError):
                        fresh = False
                    if not fresh:
                        status, reason, source_id, video = "unavailable", "source_stale", None, None
                        members.append({"video_id": member["video_id"], "added_at": member["added_at"],
                            "status": status, "reason": reason, "channel_id": None, "video": None,
                            "source_expires_at": None})
                        continue
                    expires_at = iso_utc(deadline)
                    video = {field: raw[field] for field in PUBLIC_VIDEO_FIELDS}
                    video["checked_at"] = iso_utc(clocks[0])
                    video["thumbnail_url"] = video_thumbnail_url(raw["video_id"])
                    for field in ("views", "like_count", "comment_count"):
                        count = youtube_counter_integer(raw.get(field))
                        video[field] = None if count is None else _json_integer(count)
                    category = str(raw.get("category_id") or "").strip()
                    video["category_id"] = category or None
                    category_name, category_expiry = captured_source_call(youtube_video_category_name, category) if category else (None, None)
                    video["category_name"] = category_name
                    expires_at = minimum_expiry(expires_at, category_expiry)
                    video["source_expires_at"] = expires_at
                    video["duration_seconds"] = youtube_duration_seconds(raw.get("duration"))
                except SavedWorkError as error:
                    raise collection_unavailable() from error
            else:
                raise collection_unavailable()
            members.append({"video_id": member["video_id"], "added_at": member["added_at"],
                            "status": status, "reason": reason, "channel_id": source_id, "video": video,
                            "source_expires_at": expires_at})
        # Metadata/rows/source projections are separate bounded reads, not a DB
        # snapshot. A changed collection must never splice paginated generations.
        self._confirm_member_page(identity, meta, rows, limit, after)
        # A slow source/header read cannot publish a payload that expired while
        # this response was assembled. Saved membership and its revision survive.
        completed_at = utc_instant(self.now())
        for member in members:
            if member["status"] == "available" and utc_instant(member["source_expires_at"]) <= completed_at:
                member.update(status="unavailable", reason="source_stale", channel_id=None,
                              video=None, source_expires_at=None)
        return {"contract_version": 1, "collection": meta, "members": members,
                "source_read_at": iso_utc(completed_at),
                "next_cursor": member_cursor(meta, page[-1]) if len(rows) > limit else None}


def create_saved_collections_router(repository):
    from fastapi import APIRouter, Request

    router, service = APIRouter(prefix="/saved/collections"), SavedCollectionsService(repository)

    respond = partial(saved_response, noun="Collection")

    async def listing(request: Request):
        return await respond(request, lambda: service.list(*saved_pagination(request)))

    async def detail(collection_id: str, request: Request):
        return await respond(request, service.get, collection_id)

    async def members(collection_id: str, request: Request):
        return await respond(request, lambda: service.members(collection_id, *saved_pagination(request)))

    async def member_identities(collection_id: str, request: Request):
        return await respond(request, lambda: service.member_identities(collection_id, *saved_pagination(request)))

    async def create(request: Request):
        return await respond(request, service.create, body=True, write=True)

    async def rename(collection_id: str, request: Request):
        return await respond(request, service.rename, collection_id, body=True, write=True)

    async def add(collection_id: str, request: Request):
        return await respond(request, service.change_members, collection_id, "add", body=True, write=True)

    async def remove(collection_id: str, request: Request):
        return await respond(request, service.change_members, collection_id, "remove", body=True, write=True)

    async def delete(collection_id: str, request: Request):
        return await respond(request, lambda: service.delete(collection_id, saved_delete_revision(request)), write=True)

    for path, handler, method in (("", listing, "GET"), ("", create, "POST"),
        ("/{collection_id}", detail, "GET"), ("/{collection_id}", rename, "PATCH"),
        ("/{collection_id}", delete, "DELETE"), ("/{collection_id}/members", members, "GET"),
        ("/{collection_id}/member-identities", member_identities, "GET"),
        ("/{collection_id}/members", add, "POST"), ("/{collection_id}/members/remove", remove, "POST")):
        handler.__annotations__["request"] = Request
        router.add_api_route(path, handler, methods=[method])
    return router
