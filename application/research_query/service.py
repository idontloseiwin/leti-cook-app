"""Read-only query coordination; no routes are installed by importing this module.

SQL owns source selection, normalization, boolean logic, ordering and paging.
Python sees one bounded page and shapes evidence, clocks and continuation.
"""
from datetime import datetime, timezone
from contextlib import nullcontext
import time

from pydantic import ValidationError

from hot_storage import HOT_DATA_WINDOW
# Stored data only: the provider gate refuses every request inside these calls.
from provider_boundary import stored_data_only

from . import growth
from .contracts import MAX_RESPONSE_BYTES, QueryResult
from .cursor import CursorCodec
from .frozen import CELL_KEYS, FrozenStore
from .operations import FIELD_OPERATIONS
from .registry import FIELDS, REGISTRY_VERSION, SOURCE, CHANNEL_SOURCE_ID
from .observation_limits import change_age_limit
from .validation import QueryRejected, canonical, check_budget, validate

# A frozen build reads up to MAX_FROZEN_ROWS rows in one statement. Waiting
# for a read slot is not its work: it waits at most
# FROZEN_WAIT_SECONDS from arrival, then has FROZEN_BUILD_SECONDS from getting
# the slot for the read, shaping and scope recheck (worst answer 12 s). Pages of
# a frozen result, current pages and operations keep one budget, waits included.
FROZEN_WAIT_SECONDS = 4.0
FROZEN_BUILD_SECONDS = 8.0


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


def coverage_from(data, request, extra=()):
    """`extra`: further (reason, count) pairs, e.g. those of a frozen result."""
    collection = request.scope.kind == "collection"
    channel_source=request.source.id==CHANNEL_SOURCE_ID
    coverage = {
        "basis": "channel_profiles" if channel_source else "collection_members" if collection else "channel_inventory",
        "channels_in_scope": data["channels_in_scope"], "channels_complete": data["channels_complete"],
        "eligible_count": None, "reasons": {},
    }
    for key, value in (("profiles_incomplete" if channel_source else "inventory_incomplete", data["channels_in_scope"] - data["channels_complete"]),
                       ("source_rows_unavailable", data["rejected_rows"]),
                       ("required_fields_missing", data["required_missing"]),
                       ("collection_members_unavailable", data["unresolved_collection_members"]),
                       ("collection_members_hidden", data.get("hidden_collection_members", 0)), *extra):
        if type(value) is not int or value < 0:
            raise ValueError("invalid_coverage")
        # Every saved member has its own proof/admission check. A full
        # channel scan neither adds members nor proves them unavailable.
        if value and not (collection and key == "inventory_incomplete"):
            coverage["reasons"][key] = value
    coverage["status"] = "partial" if coverage["reasons"] else "complete"
    if coverage["status"] != "complete" and request.coverage.mode == "require_complete":
        raise QueryRejected("incomplete_source")
    return coverage


_RAW, _TEXT, _INSTANT = "raw", "text", "instant"  # How a known value is written.
_UNSET = object()


def _written_utc(raw):
    """iso(raw) of a str that already is iso()'s UTC text with "+00:00", else None.

    iso() writes datetime.isoformat(): YYYY-MM-DDTHH:MM:SS, then six fraction
    digits unless they are all zero, then the offset. The parser still checks
    the string (digits, ranges) and raises what iso() would raise."""
    if ((len(raw) == 25 or len(raw) == 32 and raw[19] == "." and raw[20:26] != "000000") and raw.endswith("+00:00")
            and raw[4] == raw[7] == "-" and raw[10] == "T" and raw[13] == raw[16] == ":"):
        datetime.fromisoformat(raw)
        return raw[:-6] + "Z"
    return None


def _field_specs(request):
    """Facts of each selected field, computed once per call."""
    specs = []
    growth_carrier = growth.evidence_carrier(request.select)
    for field_id in request.select:
        field = FIELDS[field_id]
        column = ({"video.id": "video_id", "channel.id":"channel_id", "video.channel_id": "channel_id", "video.type": "video_type"}.get(field_id)
                  or (field_id.replace('.', '_') if field_id.startswith(('channel.', 'observation.')) else field_id.removeprefix('video.')))
        core = field.clock_family in {"membership","scope"}
        observation = field_id in FIELD_OPERATIONS
        # The five growth cells of a row share one clock pair and evidence object.
        growth_cell = field_id in growth.FIELDS
        evidence_column = growth.EVIDENCE_PREFIX if growth_cell else column
        clock_key = (evidence_column + "_observed_at") if observation else {"scope":"identity_clock", "membership":"identity_clock", "counters":"checked_at", "metadata":"metadata_checked_at",
                     "channel_profile":"channel_metadata_checked_at", "channel_country":"country_collected_at",
                     "description":"description_checked_at", "paid_promotion":"paid_promotion_checked_at"}[field.clock_family]
        # Without its own expiry a clock is current for HOT_DATA_WINDOW (None).
        expiry_key = evidence_column + "_expires_at" if observation else "identity_expiry" if core else None
        base = {"origin": field.origin, "source_paths": list(field.api_paths),
                "method": "tracked_channel_reference_v1" if field_id=="channel.id" else "canonical_membership_v1" if core else "reported_current_v1",
                "definition_epoch": None}
        if field_id == 'video.thumbnail_url':
            base['method'] = 'youtube_mqdefault_reference_v1'
        writes = (_RAW if field.value_type in {"boolean", "text_array"} else
                  _INSTANT if field.value_type == "utc_instant" else _TEXT)
        specs.append((field_id, column, column + "_state" if field.nullable else None, writes, clock_key, expiry_key,
                      evidence_column + "_evidence" if observation else None, observation, growth_cell,
                      field_id == growth_carrier, FIELD_OPERATIONS.get(field_id), base))
    return specs


def shape_cells(request, source_rows):
    """Row IDs, cells and provenance from SQL rows; one rule for every mode.

    A cell is a tuple in frozen.CELL_KEYS order. A frozen read shapes up to
    50,000 rows whose cells share clocks, and the five growth cells of a row
    share one evidence object: each distinct clock is parsed and written once,
    each row's growth evidence is built once. Values, errors and provenance
    (keys in order of first use, the last row's proof) are those of shaping
    every cell on its own."""
    channel_source = request.source.id == CHANNEL_SOURCE_ID
    id_key = "channel_id" if channel_source else "video_id"
    specs = _field_specs(request)
    provenance, ids, rows = {}, [], []
    # Only str and datetime inputs are kept (hashable, compared by value; equal
    # datetimes are one instant). A failure raises and is never kept.
    clocks, windows, instants, proofs = {}, {}, {}, {}

    def clock_text(raw):
        """The clock's UTC text, or None when it is not a clock (optional_clock)."""
        if raw is None:
            return None
        keep = type(raw) is str or type(raw) is datetime
        text = clocks.get(raw, _UNSET) if keep else _UNSET
        if text is _UNSET:
            text = None
            if type(raw) is str:
                try:
                    text = _written_utc(raw)
                except ValueError:
                    pass  # Not a clock: optional_clock() says so below.
            if text is None:
                clock = optional_clock(raw)
                text = iso(clock) if clock else None
            if keep:
                clocks[raw] = text
        return text

    def window_texts(raw):
        """Texts of the clock and of its end HOT_DATA_WINDOW later (may overflow)."""
        if raw is None:
            return None, None
        keep = type(raw) is str or type(raw) is datetime
        pair = windows.get(raw) if keep else None
        if pair is None:
            clock = optional_clock(raw)
            pair = (iso(clock), iso(clock + HOT_DATA_WINDOW)) if clock else (None, None)
            if keep:
                windows[raw] = pair
        return pair

    def instant_text(raw):
        keep = type(raw) is str or type(raw) is datetime
        text = instants.get(raw) if keep else None
        if text is None:
            text = (type(raw) is str and _written_utc(raw)) or iso(raw)
            if keep:
                instants[raw] = text
        return text

    for source in source_rows:
        cells, row_growth = [], None
        for (field_id, column, state_key, writes, clock_key, expiry_key, evidence_key, observation,
             growth_cell, carrier, operation, base) in specs:
            state = "known" if state_key is None else source[state_key]
            value = source[column] if state == "known" else None
            if value is not None and writes is not _RAW:
                value = instant_text(value) if writes is _INSTANT else str(value)
            if expiry_key is None:
                observed, expires = window_texts(source[clock_key])
            else:
                observed = clock_text(source[clock_key])
                expires = clock_text(source[expiry_key])
            ref, evidence = field_id, None
            if field_id == 'video.type':
                method = source.get('type_method', 'unknown')
                if method not in {'typed_playlist_membership_v1', 'automatic_long_short_v1', 'manual_decision_v1', 'unknown'}:
                    raise ValueError('invalid_type_provenance')
                ref = field_id + ':' + method
                proof = {**base, 'source_paths': list(base['source_paths']), 'method': method}
                if method == 'automatic_long_short_v1':
                    proof['source_paths'].append('videos.contentDetails.duration')
                    proof['type_rule'] = {'id':'long_short_overlap_duration', 'version':source['type_rule_version'],
                                          'max_short_seconds':source['type_threshold']}
                provenance[ref] = proof
            elif observation:
                if growth_cell and row_growth is not None:
                    evidence = row_growth
                else:
                    evidence = dict(source[evidence_key])
                    for key in ("from_at", "to_at"):
                        if evidence[key] is not None: evidence[key] = instant_text(evidence[key])
                    if growth_cell:
                        row_growth = evidence
                epoch = evidence["definition_epoch"]
                ref = field_id + ":" + (epoch or "unknown")
                kept = (field_id, epoch) if epoch is None or type(epoch) is str else None
                proof = proofs.get(kept) if kept is not None else None
                if proof is None:
                    proof = {**base, 'source_paths': list(base['source_paths']), 'method': operation, 'definition_epoch': epoch}
                    if kept is not None:
                        proofs[kept] = proof
                provenance[ref] = proof
                if growth_cell and not carrier:
                    evidence = None  # One growth evidence object per row (see growth.EVIDENCE_PREFIX).
            elif field_id not in provenance:
                provenance[field_id] = {**base, 'source_paths': list(base['source_paths'])}
            cells.append((state, value, ref, observed, expires, evidence))
        ids.append(source[id_key])
        rows.append(cells)
    return ids, rows, provenance


def wire_row(columns, row_id, cells):
    """A QueryResult row from shape_cells output."""
    return {"id": row_id, "cells": {column: dict(zip(CELL_KEYS, cell)) for column, cell in zip(columns, cells)}}


def shape_rows(request, source_rows):
    """Wire rows and their provenance from SQL rows; one rule for every mode."""
    ids, rows, provenance = shape_cells(request, source_rows)
    columns = request.select
    return [wire_row(columns, row_id, cells) for row_id, cells in zip(ids, rows)], provenance


def result_context(request, context, evaluated, valid_before, plan, consistency="current"):
    channel_source = request.source.id == CHANNEL_SOURCE_ID
    return {"source_id": request.source.id, "source_schema_version": 1,
            "registry_version": REGISTRY_VERSION,
            "lifecycle_profile": {"id": "public_standard_696h", "version": 1, "revision": context.lifecycle_revision},
            "preferences_revision": context.preferences_revision,
            "type_policy": {"id": "channel_identity" if channel_source else "canonical_membership", "version": 1, "revision": context.type_revision},
            "evaluated_at": iso(evaluated), "valid_before": iso(valid_before), "consistency": consistency,
            "observation_age_limit_seconds": change_age_limit(plan), "time_zone": request.time.timezone}


class QueryService:
    def __init__(self, repository, context_provider, *, clock=None, codec=None,
                 admission=None, preparation_check=lambda: None, frozen=None):
        self.repository = repository
        self.context_provider = context_provider
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.codec = codec or CursorCodec()
        self.admission, self.preparation_check = admission, preparation_check
        # Process-owned like the cursor key: frozen results outlive one request.
        self.frozen = frozen if frozen is not None else FrozenStore(clock=self.clock)

    def validate(self, raw):
        return validate(raw, self.context_provider())

    @stored_data_only
    def execute(self, raw, *, cancelled=lambda: False):
        started = time.monotonic()
        context = self.context_provider()
        plan = validate(raw, context)
        request = plan.request
        if request.consistency == "frozen":
            from .frozen_service import execute_frozen
            return execute_frozen(self, plan, context, started=started, cancelled=cancelled)
        continuation = self.codec.decode(request.page.after, plan, self.clock())
        deadline = started + 8.0  # Includes validation, pool wait and output.
        if request.source.id!=CHANNEL_SOURCE_ID:self.preparation_check()
        slot = (self.admission.enter(deadline=deadline, cancelled=cancelled)
                if self.admission is not None else nullcontext())
        with slot:
            # Access may have been revoked while waiting outside the DB pool.
            if self.context_provider().fingerprint() != context.fingerprint():
                raise QueryRejected("cursor_context_changed")
            if request.source.id!=CHANNEL_SOURCE_ID:self.preparation_check()
            return self._execute(plan, context, continuation, deadline, cancelled)

    @stored_data_only
    def execute_graph(self,raw,*,cancelled=lambda:False):
        from .graph_service import execute_graph
        return execute_graph(self,raw,cancelled=cancelled)

    @stored_data_only
    def execute_operation(self, raw, *, cancelled=lambda:False):
        from .aggregate_service import execute_operation
        return execute_operation(self,raw,cancelled=cancelled)

    @stored_data_only
    def frozen_ids(self, raw, *, cancelled=lambda: False):
        """Every row ID of one frozen result, in order (select all matching)."""
        from .frozen_service import frozen_ids
        return frozen_ids(self, raw, cancelled=cancelled)

    @stored_data_only
    def frozen_reference(self, token, *, cancelled=lambda: False):
        """A checked FrozenReference for a dependent read (see frozen.py)."""
        from .frozen_service import frozen_reference
        return frozen_reference(self, token, cancelled=cancelled)

    @stored_data_only
    def frozen_member(self, token, row_id, *, cancelled=lambda: False):
        """One frozen row and the frozen clocks, or source_unavailable."""
        return self.frozen_reference(token, cancelled=cancelled).member(row_id)

    def _execute(self, plan, context, continuation, deadline, cancelled):
        request = plan.request
        data = self.repository.read_page(plan, continuation, deadline=deadline, cancelled=cancelled)
        try:
            evaluated, valid_before = instant(data["evaluated_at"]), instant(data["valid_before"])
            if continuation:
                valid_before = min(valid_before, instant(continuation["until"]))
            coverage=coverage_from(data,request)
            source_rows = data["rows"]
            if not isinstance(source_rows, list) or len(source_rows) > request.page.size + 1:
                raise ValueError("invalid_page")
            has_more = len(source_rows) > request.page.size
            rows, provenance = shape_rows(request, source_rows[:request.page.size])
            next_cursor = self.codec.encode(plan, scope_revision=data["scope_revision"],
                keys=source_rows[request.page.size-1]["sort_keys"], valid_before=iso(valid_before)) if has_more else None
            output = {
                "query_version": 1, "query_fingerprint": plan.fingerprint,
                "context": result_context(request, context, evaluated, valid_before, plan),
                "columns": request.select, "provenance": provenance, "rows": rows,
                "coverage": coverage, "page": {"has_more": has_more, "next_cursor": next_cursor},
                "warnings": ["current_pages_may_change"],
            }
            output = QueryResult.model_validate(output).model_dump(mode="json")
            encoded = canonical(output)
            if len(encoded) > MAX_RESPONSE_BYTES:
                raise QueryRejected("query_budget_exceeded", limit="response")
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
        check_budget(deadline, cancelled)
        return output
