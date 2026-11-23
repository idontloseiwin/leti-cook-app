"""Private operator decisions over contradictory typed-playlist evidence.

No provider calls. The database owns detection, revision checks, leases and the
atomic canonical decision; HTTP never treats an uncertain write as success.
"""
import json
import logging
import re
from datetime import datetime
from uuid import UUID

from saved_work import SavedWorkError, validate_mutation_headers, validate_read_headers
from hot_storage import capture_read_lifetime, register_read_deadline

TYPES = {"long", "short", "stream"}
MAX_BODY_BYTES = 8192
MAX_REVISION = 9007199254740991
LOG = logging.getLogger(__name__)


class ReviewError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def invalid(message="Video type review request is invalid."):
    return ReviewError(422, "invalid_request", message)


def unavailable():
    return ReviewError(503, "video_type_review_unavailable", "Video type review is unavailable. No new change is confirmed; reload or retry the same command.")


def identity(value, *, channel=False):
    pattern = r"UC[A-Za-z0-9_-]{22}" if channel else r"[A-Za-z0-9_-]{11}"
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise invalid("A valid channel ID is required." if channel else "A valid video ID is required.")
    return value


def positive_integer(value):
    if type(value) is not int or not 1 <= value <= MAX_REVISION:
        raise invalid("A positive revision is required.")
    return value


def request_id(value):
    try:
        parsed = UUID(value) if isinstance(value, str) else None
    except ValueError:
        parsed = None
    if parsed is None or parsed.version != 4 or str(parsed) != value.lower():
        raise invalid("A UUIDv4 command ID is required.")
    return str(parsed)


def case_id(value):
    try:
        return request_id(value)
    except ReviewError as error:
        raise invalid("A UUIDv4 review ID is required.") from error


def reason_text(value):
    if not isinstance(value, str):
        raise invalid("Explain the manual decision in 1 to 1000 characters.")
    value = value.strip()
    if not 1 <= len(value) <= 1000 or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value):
        raise invalid("Explain the manual decision in 1 to 1000 characters, without control characters.")
    return value


def video_type(value, *, nullable=False):
    if nullable and value is None:
        return None
    if not isinstance(value, str) or value not in TYPES:
        raise invalid("Choose Long, Short or Stream.")
    return value


def timestamp(value):
    if not isinstance(value, str):
        raise invalid()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise invalid()
        return value
    except (ValueError, OverflowError) as error:
        raise invalid() from error


class VideoTypeReviewService:
    def __init__(self, repository, *, on_decision=None):
        self.repository = repository
        self.on_decision = on_decision

    @staticmethod
    def _call(method, *args):
        try:
            return method(*args)
        except Exception as error:
            if getattr(error, "message", None) == "user_work_limit_reached":
                raise ReviewError(413, "video_type_review_limit_reached",
                                  "Manual decisions hold their backup limit of 1,000 decided videos and "
                                  "10,000 history entries. This decision was not saved.") from None
            raise unavailable() from error

    @staticmethod
    def _case(row, expected_id=None):
        try:
            if not isinstance(row, dict):
                raise invalid()
            review_id = case_id(row["case_id"])
            if expected_id is not None and review_id != expected_id:
                raise invalid()
            detached_at = row["source_detached_at"]
            detached = detached_at is not None
            if detached:
                detached_at = timestamp(detached_at)
                if row["video_id"] is not None or row["channel_id"] is not None:
                    raise invalid()
                vid, channel = None, None
            else:
                vid, channel = identity(row["video_id"]), identity(row["channel_id"], channel=True)
            canonical = video_type(row["canonical_type"], nullable=True)
            observed = row["observed_types"]
            if not isinstance(observed, list) or len(observed) > 3 or any(not isinstance(t, str) or t not in TYPES for t in observed) or len(set(observed)) != len(observed):
                raise invalid()
            revision = positive_integer(row["revision"])
            state, can_decide, blocked = row["state"], row["can_decide"], row["blocked_reason"]
            if state not in {"open", "resolved"} or type(can_decide) is not bool or blocked not in {None, "channel_conflict", "source_unavailable"} or can_decide != (blocked is None):
                raise invalid()
            if detached:
                if canonical is not None or observed or blocked != "source_unavailable":
                    raise invalid()
            # An attached identity can remain locally blocked after borrowed
            # details expire; the generic reason does not claim fresh evidence.
            review_status = row.get("review_status", "history" if row["decision"] is not None else "awaiting_refresh")
            if review_status not in {"needs_review", "awaiting_refresh", "channel_conflict", "confirmed", "history"}:
                raise invalid()
            decision = row["decision"]
            if decision is not None:
                if not isinstance(decision, dict):
                    raise invalid()
                decision = {"video_type": video_type(decision["video_type"]), "reason": reason_text(decision["reason"]),
                            "decided_at": timestamp(decision["decided_at"]), "revision": positive_integer(decision["revision"])}
                if decision["revision"] > revision:
                    raise invalid()
            if state == "resolved" and (decision is None or (not detached and (canonical != decision["video_type"]))):
                raise invalid()
            return {"case_id": review_id, "video_id": vid, "channel_id": channel, "source_detached_at": detached_at,
                    "canonical_type": canonical, "observed_types": observed, "review_status": review_status, "state": state,
                    "revision": revision, "first_seen_at": timestamp(row["first_seen_at"]),
                    "last_seen_at": timestamp(row["last_seen_at"]), "can_decide": can_decide,
                    "blocked_reason": blocked, "decision": decision,
                    **({"source_expires_at": row["source_expires_at"]} if row.get("source_expires_at") else {})}
        except (ReviewError, KeyError, TypeError) as error:
            raise unavailable() from error

    @staticmethod
    def _limit(limit):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise invalid("Page size must be 1 to 100.")

    def _load(self, review_id):
        row = self._call(self.repository.get_case, review_id)
        if row is None:
            raise ReviewError(404, "video_type_review_not_found", "Video type review not found.")
        return self._case(row, review_id)

    def list(self, state="open", limit=50, cursor=None):
        if state not in {"open", "awaiting", "resolved", "all"}:
            raise invalid()
        self._limit(limit)
        after = case_id(cursor) if cursor is not None else None
        rows = self._call(self.repository.list_cases, state, limit + 1, after)
        if not isinstance(rows, list) or len(rows) > limit + 1:
            raise unavailable()
        cases = [self._case(row) for row in rows]
        ids = [row["case_id"] for row in cases]
        # The RPC owns UUID ordering and its matching keyset. Source withdrawal
        # must not hide or merge retained cases through their source IDs.
        if len(ids) != len(set(ids)) or (after is not None and after in ids) or any(
                state == "open" and row["review_status"] not in {"needs_review", "channel_conflict"}
                or state == "awaiting" and row["review_status"] != "awaiting_refresh"
                or state == "resolved" and row["decision"] is None for row in cases):
            raise unavailable()
        return {"contract_version": 2, "cases": cases[:limit],
                "next_cursor": cases[limit - 1]["case_id"] if len(cases) > limit else None}

    def detail(self, review_id, limit=20, before_revision=None):
        vid = case_id(review_id)
        self._limit(limit)
        before = positive_integer(before_revision) if before_revision is not None else None
        case = self._load(vid)
        rows = self._call(self.repository.list_history, vid, limit + 1, before)
        if not isinstance(rows, list) or len(rows) > limit + 1:
            raise unavailable()
        history = []
        try:
            for row in rows:
                history.append({"revision": positive_integer(row["revision"]), "previous_type": video_type(row["previous_type"], nullable=True),
                                "video_type": video_type(row["video_type"]), "reason": reason_text(row["reason"]),
                                "decided_at": timestamp(row["decided_at"]), "request_id": request_id(row["request_id"])})
        except (ReviewError, KeyError, TypeError) as error:
            raise unavailable() from error
        revisions = [row["revision"] for row in history]
        if revisions != sorted(set(revisions), reverse=True) or any(rev > case["revision"] or (before is not None and rev >= before) for rev in revisions):
            raise unavailable()
        # Separate bounded reads must not splice two decision generations.
        if self._load(vid)["revision"] != case["revision"]:
            raise ReviewError(409, "video_type_review_changed", "This review changed. Reload before continuing.")
        return {"contract_version": 2, "case": case, "history": history[:limit],
                "next_before_revision": history[limit - 1]["revision"] if len(history) > limit else None}

    @staticmethod
    def _source_free_receipt_case(case):
        """Retain a confirmed user decision, never observations from a writer."""
        result = dict(case)
        result["observed_types"] = []
        detached = result["source_detached_at"] is not None
        result["canonical_type"] = (
            result["decision"]["video_type"]
            if not detached and result["state"] == "resolved" and result["decision"] else None
        )
        # A mutation receipt proves the saved decision, not current permission
        # for a subsequent decision when its protected reread is unavailable.
        result["blocked_reason"] = "source_unavailable"
        result["can_decide"] = False
        return result

    def _read_committed_case(self, case):
        fallback = self._source_free_receipt_case(case)
        with capture_read_lifetime() as lifetime:
            try:
                current = self._load(case["case_id"])
            except ReviewError:
                return fallback
            # The mutation reply acknowledges its applied revision. A concurrent
            # generation is available through GET, never mistaken for a failed write.
            if current["revision"] != case["revision"]:
                return fallback
        if lifetime.expires_at is not None:
            register_read_deadline(lifetime.expires_at)
        return current

    def decide(self, review_id, body):
        vid = case_id(review_id)
        if not isinstance(body, dict) or set(body) != {"request_id", "expected_revision", "video_type", "reason"}:
            raise invalid()
        command = request_id(body["request_id"])
        expected, chosen, reason = positive_integer(body["expected_revision"]), video_type(body["video_type"]), reason_text(body["reason"])
        result = self._call(self.repository.decide, vid, command, expected, chosen, reason)
        if not isinstance(result, dict):
            raise unavailable()
        outcomes = {
            "changed": (409, "video_type_review_changed", "This review changed. Reload before saving your decision."),
            "busy": (409, "video_type_review_busy", "Collection or another review is in progress. No decision was changed; retry shortly."),
            "blocked": (409, "video_type_review_blocked", "This case has conflicting channel identity and cannot be resolved by choosing a video type."),
            "not_found": (404, "video_type_review_not_found", "Video type review not found."),
            "request_conflict": (409, "video_type_review_request_conflict", "This command ID was used for a different decision. Reload before editing."),
            "source_unavailable": (409, "video_type_review_source_unavailable", "Current evidence does not require a new manual decision. Wait for automatic rechecking; recorded decisions remain available."),
        }
        outcome = result.get("outcome")
        if isinstance(outcome, str) and outcome in outcomes:
            raise ReviewError(*outcomes[outcome])
        if outcome != "ready":
            raise unavailable()
        case = self._case(result.get("case"), vid)
        ack = result.get("mutation")
        if not isinstance(ack, dict) or ack.get("request_id") != command or type(ack.get("replayed")) is not bool or type(ack.get("applied_revision")) is not int:
            raise unavailable()
        applied = ack["applied_revision"]
        if applied != expected + 1 or applied > case["revision"]:
            raise unavailable()
        if not ack["replayed"] and (not case["can_decide"] or applied != case["revision"] or case["decision"] is None or case["decision"]["video_type"] != chosen or case["decision"]["reason"] != reason or case["state"] != "resolved"):
            raise unavailable()
        receipt_case = case
        case = self._read_committed_case(receipt_case)
        if self.on_decision is not None and receipt_case["channel_id"] is not None:
            try:
                self.on_decision(receipt_case["channel_id"])
            except Exception:
                LOG.warning("Video type decision confirmed; cache invalidation failed case_id=%s", vid)
        LOG.info("Video type review confirmed case_id=%s request_id=%s revision=%s replayed=%s", vid, command, applied, ack["replayed"])
        return {"contract_version": 2, "case": case,
                "mutation": {"request_id": command, "replayed": ack["replayed"], "applied_revision": applied}}


def create_video_type_reviews_router(repository, *, on_decision=None):
    from fastapi import APIRouter, Request
    from starlette.concurrency import run_in_threadpool
    from starlette.responses import JSONResponse

    router = APIRouter(prefix="/system/video-type-reviews")
    service = VideoTypeReviewService(repository, on_decision=on_decision)

    def query(request, allowed, default_limit):
        values = request.query_params
        if set(values) - allowed or any(len(values.getlist(key)) != 1 for key in values):
            raise invalid()
        raw = values.get("limit", str(default_limit))
        if not raw.isascii() or not raw.isdigit() or len(raw) > 3:
            raise invalid()
        return int(raw)

    async def respond(request, action, *, write=False):
        try:
            try:
                if write:
                    if request.headers.get("x-video-type-review") != "1":
                        raise ReviewError(403, "write_guard", "Video type review write header is required.")
                    # Reuse the existing origin/Fetch Metadata policy after our
                    # feature-specific guard; the adapter is server-side only.
                    validate_mutation_headers({**request.headers, "x-saved-work": "1"}, request.url.scheme)
                else:
                    validate_read_headers(request.headers, request.url.scheme)
            except SavedWorkError as error:
                raise ReviewError(error.status_code, error.code, "Video type review request origin cannot be verified.") from error
            if write:
                if request.query_params:
                    raise invalid()
                if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                    raise invalid("Video type decisions require JSON.")
                raw = bytearray()
                async for chunk in request.stream():
                    if len(raw) + len(chunk) > MAX_BODY_BYTES:
                        raise ReviewError(413, "request_too_large", "Video type review request is too large.")
                    raw.extend(chunk)
                def no_duplicate_keys(pairs):
                    result = {}
                    for key, value in pairs:
                        if key in result:
                            raise ValueError("duplicate key")
                        result[key] = value
                    return result
                try:
                    body = json.loads(raw, object_pairs_hook=no_duplicate_keys)
                except (ValueError, UnicodeDecodeError, RecursionError) as error:
                    raise invalid("Video type review requires valid JSON without duplicate keys.") from error
                value = await run_in_threadpool(action, body)
            else:
                value = await run_in_threadpool(action)
            return JSONResponse(value, headers={"Cache-Control": "no-store"})
        except ReviewError as error:
            return JSONResponse({"detail": {"code": error.code, "message": error.message}}, status_code=error.status,
                                headers={"Cache-Control": "no-store"})

    async def listing(request: Request):
        return await respond(request, lambda: service.list(request.query_params.get("state", "open"),
                             query(request, {"state", "limit", "cursor"}, 50), request.query_params.get("cursor")))

    async def hidden_identities(request: Request):
        def read():
            limit = query(request, {"channel_id", "limit", "cursor"}, 50)
            channel_id, cursor = request.query_params.get("channel_id"), request.query_params.get("cursor")
            if (not 0 <= limit <= 100
                    or (channel_id is not None and not re.fullmatch(r'UC[A-Za-z0-9_-]{22}', channel_id))
                    or (cursor is not None and not re.fullmatch(r'[A-Za-z0-9_-]{11}', cursor))):
                raise invalid()
            return service._call(lambda: repository.hidden_identities(channel_id=channel_id, after_id=cursor, limit=limit))
        return await respond(request, read)

    async def detail(case_id: str, request: Request):
        def read():
            limit = query(request, {"limit", "before_revision"}, 20)
            raw = request.query_params.get("before_revision")
            if raw is not None and (not raw.isascii() or not raw.isdigit() or len(raw) > 16):
                raise invalid()
            return service.detail(case_id, limit, int(raw) if raw is not None else None)
        return await respond(request, read)

    async def decision(case_id: str, request: Request):
        return await respond(request, lambda body: service.decide(case_id, body), write=True)

    router.add_api_route("", listing, methods=["GET"])
    router.add_api_route("/hidden", hidden_identities, methods=["GET"])
    router.add_api_route("/{case_id}", detail, methods=["GET"])
    router.add_api_route("/{case_id}/decision", decision, methods=["POST"])
    return router
