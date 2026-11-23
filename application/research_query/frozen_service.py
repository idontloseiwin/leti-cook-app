"""Frozen results: one complete read, then every page from process memory.

The lifetime, admission and invalidation rules are described in frozen.py.
Each access repeats the checks of a current page: the owner's access context,
then (inside read admission) the scope revision of the frozen result: channel
visibility, folder, label or collection membership. Expiry, invalidation,
eviction or a restart answer cursor_expired; a changed access context or scope
answers cursor_context_changed. Both are 409, like current cursors.
"""
from contextlib import nullcontext
from datetime import timedelta
import hashlib
import json
import secrets
import time
from types import MappingProxyType

from pydantic import ValidationError

from .contracts import MAX_BODY_BYTES, MAX_FROZEN_ROWS, MAX_RESPONSE_BYTES, FrozenIdsResult, QueryResult
from .frozen import (FROZEN_TTL_SECONDS, ROW_OVERHEAD_BYTES, FrozenReference, FrozenSession,
                     compact_cells, expand_row)
from .registry import CHANNEL_SOURCE_ID
from .service import (FROZEN_BUILD_SECONDS, FROZEN_WAIT_SECONDS, coverage_from, instant, result_context,
                      shape_cells, wire_row)
from .validation import QueryRejected, _json_budget, _pairs, _reject_constant, canonical, check_budget

WARNING = "frozen_values_as_of_evaluated_at"
# Room for a page's own cursors, reference and envelope beyond its rows.
_PAGE_ENVELOPE_BYTES = 16 * 1024


def conditions_key(plan):
    """The query without sort, projection and paging: what selects the rows."""
    obj = json.loads(plan.encoded)
    return hashlib.sha256(canonical({key: obj.get(key) for key in (
        "query_version", "source", "scope", "video_types", "where", "time", "observations", "coverage")})).hexdigest()


def _slot(service, deadline, cancelled):
    return (service.admission.enter(deadline=deadline, cancelled=cancelled)
            if service.admission is not None else nullcontext())


def _same_context(service, fingerprint):
    if service.context_provider().fingerprint() != fingerprint:
        raise QueryRejected("cursor_context_changed")


def _in_budget(deadline, cancelled):
    check_budget(deadline, cancelled)


def _recheck(service, session, deadline, cancelled):
    """The cheap per-access check: access context, then visibility and scope."""
    store = service.frozen
    try:
        _same_context(service, session.context_fingerprint)
        with _slot(service, deadline, cancelled):
            _same_context(service, session.context_fingerprint)
            service.repository.recheck_scope(session.plan, session.scope_revision,
                                             deadline=deadline, cancelled=cancelled)
            _same_context(service, session.context_fingerprint)
    except QueryRejected as error:
        if error.code == "cursor_context_changed":
            store.discard(session)  # It can never be served again.
        raise
    store.require(session)


def _freeze(service, plan, context, data):
    request = plan.request
    try:
        count, source_rows = data["frozen_count"], data["rows"]
        if type(count) is not int or count < 0:
            raise ValueError()
        if count > MAX_FROZEN_ROWS or source_rows is None:
            raise QueryRejected("query_budget_exceeded", "consistency", limit="rows")
        if not isinstance(source_rows, list) or len(source_rows) != count:
            raise ValueError()
        evaluated, fresh_until = instant(data["evaluated_at"]), instant(data["fresh_until"])
        if not evaluated < fresh_until <= evaluated + timedelta(seconds=FROZEN_TTL_SECONDS):
            raise ValueError()
        # Cells as tuples in CELL_KEYS order: no wire dict per cell.
        ids, rows, provenance = shape_cells(request, source_rows)
        # Only admitted sources bound the lifetime. SQL admits a value or row
        # only when it outlives fresh_until, so this is fresh_until unless a
        # clock disagrees; rows and values it withheld never shorten it.
        valid_before = fresh_until
        known = {cell[4] for row in rows for cell in row if cell[0] == "known" and cell[4] is not None}
        known.update(source["type_expiry"] for source in source_rows if source.get("type_expiry") is not None)
        for value in known:
            valid_before = min(valid_before, instant(value))
        expired_values = sum(1 for row in rows if any(cell[0] == "expired" for cell in row))
        coverage = coverage_from(data, request, (("source_rows_expiring", data["expiring_rows"]),
                                                 ("rows_with_expired_values", expired_values)))
        channels = data["scope_channels"]
        if not isinstance(channels, list) or any(type(c) is not str for c in channels):
            raise ValueError()
        ids = tuple(ids)
        if len(set(ids)) != len(ids) or any(type(i) is not str for i in ids):
            raise ValueError()
        columns = tuple(request.select)
        encoded = tuple(compact_cells(row_id, cells) for row_id, cells in zip(ids, rows))
        # A page's wire row is its compact row plus the same keys for every row.
        if rows:
            delta = len(canonical(wire_row(columns, ids[0], rows[0]))) - len(encoded[0])
            budget = MAX_RESPONSE_BYTES - _PAGE_ENVELOPE_BYTES - len(canonical(provenance))
            per_page = request.page.size
            for start in range(0, len(encoded), per_page):
                if sum(len(row) + delta for row in encoded[start:start + per_page]) > budget:
                    raise QueryRejected("query_budget_exceeded", "page", limit="response")
        wire_context = result_context(request, context, evaluated, valid_before, plan, consistency="frozen")
        metadata = canonical({"context": wire_context, "coverage": coverage, "provenance": provenance})
        size = (len(metadata) + len(plan.encoded) + sum(map(len, encoded))
                + len(encoded) * ROW_OVERHEAD_BYTES + 64 * len(channels))
    except QueryRejected:
        raise
    except (KeyError, ValueError, TypeError, OverflowError, ValidationError):
        raise QueryRejected("backend_unavailable") from None
    if valid_before <= evaluated:
        raise QueryRejected("cursor_expired")
    if size > service.frozen.session_limit_bytes:
        raise QueryRejected("query_budget_exceeded", "consistency", limit="memory")
    return FrozenSession(
        session_id=secrets.token_hex(16), plan=plan, query_fingerprint=plan.fingerprint,
        conditions_key=conditions_key(plan), context_fingerprint=plan.context_fingerprint,
        source_id=request.source.id, video_type=(request.video_types or [None])[0],
        scope_kind=request.scope.kind, scope_id=request.scope.id, scope_revision=data["scope_revision"],
        channel_ids=frozenset(channels), page_size=request.page.size, evaluated_at=evaluated,
        valid_before=valid_before, columns=columns, metadata=metadata, ids=ids, rows=encoded,
        positions=MappingProxyType({row_id: index for index, row_id in enumerate(ids)}), size=size)


def _page(service, session, offset):
    """One page of the frozen order; anchor_id is set by the caller."""
    total, size = session.total_count, session.page_size
    if not (0 <= offset < total or offset == total == 0) or offset % size:
        raise QueryRejected("cursor_expired")
    end = min(offset + size, total)
    try:
        metadata = json.loads(session.metadata)
        rows = [expand_row(session.columns, session.rows[index]) for index in range(offset, end)]
        refs = {cell["provenance_ref"] for row in rows for cell in row["cells"].values()}
        output = {
            "query_version": 1, "query_fingerprint": session.query_fingerprint,
            "context": {**metadata["context"], "frozen_ref": service.codec.encode_frozen_ref(session)},
            "columns": list(session.columns),
            "provenance": {ref: metadata["provenance"][ref] for ref in sorted(refs)},
            "rows": rows, "coverage": metadata["coverage"],
            "page": {"has_more": end < total,
                     "next_cursor": service.codec.encode_frozen(session, end) if end < total else None,
                     "previous_cursor": service.codec.encode_frozen(session, offset - size) if offset else None,
                     "start_index": offset, "total_count": total, "anchor_id": None},
            "warnings": [WARNING]}
        return output
    except (KeyError, ValueError, TypeError) as error:
        if isinstance(error, QueryRejected):
            raise
        raise QueryRejected("backend_unavailable") from None


def _sealed(output):
    try:
        output = QueryResult.model_validate(output).model_dump(mode="json")
    except (ValidationError, ValueError, TypeError):
        raise QueryRejected("backend_unavailable") from None
    if len(canonical(output)) > MAX_RESPONSE_BYTES:
        raise QueryRejected("query_budget_exceeded", limit="response")
    return output


def execute_frozen(service, plan, context, *, started, cancelled):
    """`started`: the request's arrival (time.monotonic()).

    A page of a stored result has FROZEN_BUILD_SECONDS from arrival, its slot
    wait included. A new build waits at most FROZEN_WAIT_SECONDS from arrival
    for a read slot (query_busy after that) and then has FROZEN_BUILD_SECONDS
    from getting it: read, shaping and the scope recheck, whose slot wait is
    part of that work (query_busy) like the read's own limit (time)."""
    request = plan.request
    store = service.frozen
    deadline = started + FROZEN_BUILD_SECONDS
    if request.page.after is not None:
        position = service.codec.decode_frozen(request.page.after)
        session = store.get(position.session_id)
        if position.query_fingerprint != session.query_fingerprint or position.context_fingerprint != session.context_fingerprint:
            raise QueryRejected("cursor_expired")
        # Access changed since the freeze: this result can never be served again.
        if session.context_fingerprint != plan.context_fingerprint:
            store.discard(session)
            raise QueryRejected("cursor_context_changed")
        # The same query and page size: a cursor never crosses queries.
        if session.query_fingerprint != plan.fingerprint:
            raise QueryRejected("cursor_context_changed")
        _recheck(service, session, deadline, cancelled)
        output = _sealed(_page(service, session, position.offset))
    else:
        video_source = request.source.id != CHANNEL_SOURCE_ID
        if video_source:
            service.preparation_check()
        with store.build_slot():
            with _slot(service, started + FROZEN_WAIT_SECONDS, cancelled):
                # Time spent behind other reads was not this build's work.
                deadline = time.monotonic() + FROZEN_BUILD_SECONDS
                # Access may have been revoked while waiting outside the DB pool.
                _same_context(service, context.fingerprint())
                if video_source:
                    service.preparation_check()
                generation = store.generation()
                data = service.repository.read_frozen(plan, deadline=deadline, cancelled=cancelled)
            # Shaping every row is CPU work: it holds no read admission slot.
            session = _freeze(service, plan, context, data)
            del data
            _in_budget(deadline, cancelled)
            with _slot(service, deadline, cancelled):
                # Hide, move or membership change committed during the read.
                service.repository.recheck_scope(plan, session.scope_revision, deadline=deadline, cancelled=cancelled)
                _same_context(service, context.fingerprint())
            _in_budget(deadline, cancelled)
            session = store.insert(session, generation)
        anchor = request.page.anchor_id
        index = session.positions.get(anchor) if anchor is not None else None
        output = _page(service, session, 0 if index is None else index // session.page_size * session.page_size)
        # The anchor is echoed only when it is a row of this result.
        output["page"]["anchor_id"] = anchor if index is not None else None
        output = _sealed(output)
    store.require(session)
    _same_context(service, session.context_fingerprint)
    if instant(service.clock()) >= session.valid_before:
        raise QueryRejected("cursor_expired")
    _in_budget(deadline, cancelled)
    return output


def frozen_reference(service, token, *, cancelled=lambda: False, deadline=None):
    deadline = time.monotonic() + FROZEN_BUILD_SECONDS if deadline is None else deadline
    # Like validate(): no access is policy_denied before any token is read.
    context = service.context_provider()
    if not context.enabled or not context.allow_frozen:
        raise QueryRejected("policy_denied")
    position = service.codec.decode_frozen(token, reference=True)
    session = service.frozen.get(position.session_id)
    if position.query_fingerprint != session.query_fingerprint or position.context_fingerprint != session.context_fingerprint:
        raise QueryRejected("cursor_expired")
    _recheck(service, session, deadline, cancelled)
    if instant(service.clock()) >= session.valid_before:
        raise QueryRejected("cursor_expired")
    return FrozenReference(session, service.frozen)


def frozen_page(service, reference, offset=0):
    """One page of a checked reference's result, as the query route serves it.

    A research graph's row block shows the reference's list this way: the same
    rows, values, coverage and position as that query's own page at this
    offset; only the tokens are sealed anew. Call reference.require_current()
    before answering."""
    return _sealed(_page(service, reference.session, offset))


def _ids_request(raw):
    if not isinstance(raw, bytes) or len(raw) > MAX_BODY_BYTES:
        raise QueryRejected("query_budget_exceeded", limit="request")
    _json_budget(raw)
    try:
        value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_reject_constant)
        if (type(value) is not dict or set(value) != {"version", "frozen_ref"} or type(value["version"]) is not int
                or value["version"] != 1 or type(value["frozen_ref"]) is not str):
            raise ValueError()
        return value["frozen_ref"]
    except QueryRejected:
        raise
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise QueryRejected("invalid_query") from None


def frozen_ids(service, raw, *, cancelled=lambda: False):
    deadline = time.monotonic() + FROZEN_BUILD_SECONDS
    reference = frozen_reference(service, _ids_request(raw), cancelled=cancelled, deadline=deadline)
    session = reference.session
    try:
        context = json.loads(session.metadata)["context"]
        output = FrozenIdsResult.model_validate({
            "version": 1, "query_fingerprint": session.query_fingerprint,
            "context": {key: context[key] for key in ("source_id", "evaluated_at", "valid_before", "consistency", "time_zone")},
            "ids": list(session.ids), "total_count": session.total_count}).model_dump(mode="json")
    except (KeyError, ValueError, TypeError, ValidationError):
        raise QueryRejected("backend_unavailable") from None
    if len(canonical(output)) > MAX_RESPONSE_BYTES:
        raise QueryRejected("query_budget_exceeded", limit="response")
    reference.require_current()
    _same_context(service, session.context_fingerprint)
    if instant(service.clock()) >= session.valid_before:
        raise QueryRejected("cursor_expired")
    _in_budget(deadline, cancelled)
    return output
