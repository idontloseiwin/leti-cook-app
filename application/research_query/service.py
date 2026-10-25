"""Read-only query coordination; no routes are installed by importing this module.

SQL owns source selection, normalization, boolean logic, ordering and paging.
Python sees one bounded page and shapes evidence, clocks and continuation.
"""
from datetime import datetime, timedelta, timezone
import time

from pydantic import ValidationError

from .contracts import MAX_RESPONSE_BYTES, QueryResult
from .cursor import CursorCodec
from .registry import FIELDS, REGISTRY_VERSION, SOURCE
from .validation import QueryRejected, canonical, validate


def instant(value):
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = datetime.fromisoformat(value)
    else:
        raise ValueError("invalid_source_clock")
    if dt.tzinfo is None:
        raise ValueError("invalid_source_clock")
    return dt.astimezone(timezone.utc)


def iso(value):
    return instant(value).isoformat().replace("+00:00", "Z")


def optional_clock(value):
    try:
        return instant(value)
    except (ValueError, TypeError, OverflowError):
        return None


class QueryService:
    def __init__(self, repository, context_provider, *, clock=None, codec=None):
        self.repository = repository
        self.context_provider = context_provider
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.codec = codec or CursorCodec()

    def validate(self, raw):
        return validate(raw, self.context_provider())

    def execute(self, raw, *, cancelled=lambda: False):
        started = time.monotonic()
        context = self.context_provider()
        plan = validate(raw, context)
        request = plan.request
        continuation = self.codec.decode(request.page.after, plan, self.clock())
        deadline = started + 8.0  # Includes validation, pool wait and output.
        data = self.repository.read_page(plan, continuation, deadline=deadline, cancelled=cancelled)
        try:
            evaluated, valid_before = instant(data["evaluated_at"]), instant(data["valid_before"])
            if continuation:
                valid_before = min(valid_before, instant(continuation["until"]))
            coverage = {
                "channels_in_scope": data["channels_in_scope"], "channels_complete": data["channels_complete"],
                "eligible_count": None, "reasons": {},
            }
            for key, value in (("inventory_incomplete", data["channels_in_scope"] - data["channels_complete"]),
                               ("source_rows_unavailable", data["rejected_rows"]),
                               ("required_fields_missing", data["required_missing"])):
                if type(value) is not int or value < 0:
                    raise ValueError("invalid_coverage")
                if value:
                    coverage["reasons"][key] = value
            coverage["status"] = "partial" if coverage["reasons"] else "complete"
            if coverage["status"] != "complete" and request.coverage.mode == "require_complete":
                raise QueryRejected("incomplete_source")
            source_rows = data["rows"]
            if not isinstance(source_rows, list) or len(source_rows) > request.page.size + 1:
                raise ValueError("invalid_page")
            has_more = len(source_rows) > request.page.size
            rows, provenance = [], {}
            for source in source_rows[:request.page.size]:
                cells = {}
                for field_id in request.select:
                    field = FIELDS[field_id]
                    column = {"video.id": "video_id", "video.channel_id": "channel_id", "video.type": "video_type"}.get(field_id, field_id.removeprefix("video."))
                    core = not field.nullable
                    state = "known" if core else source[column + "_state"]
                    value = source[column] if state == "known" else None
                    if value is not None:
                        value = iso(value) if field_id == "video.published_at" else str(value)
                    clock_key = "identity_clock" if core else "checked_at" if field.clock_family == "counters" else "metadata_checked_at"
                    observed = optional_clock(source[clock_key])
                    expiry = optional_clock(source["identity_expiry"]) if core else observed + timedelta(hours=696) if observed else None
                    ref = field_id
                    provenance[ref] = {"origin": field.origin, "source_paths": list(field.api_paths),
                                       "method": "canonical_membership_v1" if core else "reported_current_v1",
                                       "definition_epoch": None}
                    cells[field_id] = {"state": state, "value": value, "provenance_ref": ref,
                                       "observed_at": iso(observed) if observed else None,
                                       "expires_at": iso(expiry) if expiry else None}
                rows.append({"id": source["video_id"], "cells": cells})
            next_cursor = self.codec.encode(plan, scope_revision=data["scope_revision"],
                keys=source_rows[request.page.size-1]["sort_keys"], valid_before=iso(valid_before)) if has_more else None
            output = {
                "query_version": 1, "query_fingerprint": plan.fingerprint,
                "context": {"source_id": SOURCE.id, "source_schema_version": 1,
                    "registry_version": REGISTRY_VERSION,
                    "lifecycle_profile": {"id": "public_standard_696h", "version": 1, "revision": context.lifecycle_revision},
                    "preferences_revision": context.preferences_revision,
                    "type_policy": {"id": "canonical_membership", "version": 1, "revision": context.type_revision},
                    "evaluated_at": iso(evaluated), "valid_before": iso(valid_before), "consistency": "current"},
                "columns": request.select, "provenance": provenance, "rows": rows,
                "coverage": coverage, "page": {"has_more": has_more, "next_cursor": next_cursor},
                "warnings": ["current_pages_may_change"],
            }
            output = QueryResult.model_validate(output).model_dump(mode="json")
            encoded = canonical(output)
            if len(encoded) > MAX_RESPONSE_BYTES:
                raise QueryRejected("query_budget_exceeded")
        except QueryRejected:
            raise
        except (KeyError, ValueError, TypeError, OverflowError, ValidationError):
            raise QueryRejected("backend_unavailable") from None
        # A short separate read detects hide/delete/move committed during the
        # page snapshot. Never silently restart page one after invalidation.
        self.repository.recheck_scope(plan, data["scope_revision"], deadline=deadline, cancelled=cancelled)
        if self.context_provider().fingerprint() != context.fingerprint():
            raise QueryRejected("cursor_context_changed")
        if instant(self.clock()) >= valid_before:
            raise QueryRejected("cursor_expired")
        if cancelled() or time.monotonic() >= deadline:
            raise QueryRejected("query_budget_exceeded")
        return output
