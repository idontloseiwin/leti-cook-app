"""Pure outcomes for newly executed, bounded collection work.

Do not apply this projection to historical summaries without an outcome marker:
old ``completed`` rows did not prove that their selected work was complete.
"""

from collections.abc import Mapping


def collection_outcome_fields(summary: Mapping, *, explicit_selected=None) -> dict:
    """Describe this admitted batch, not the global due backlog or limit."""
    def count(key):
        return max(int(summary.get(key) or 0), 0)

    claimed = count("claimed") if "claimed" in summary else count("requested")
    # New summaries include all types; the old stream count remains a subset.
    terminal = count("updated") + count("unavailable_marked") + max(
        count("publication_waiting"), count("stream_lifecycle_probed"))
    unresolved = max(claimed - terminal, count("metadata_deferred"), 0)
    result = {}
    if explicit_selected is not None:
        selected = max(int(explicit_selected), 0)
        unclaimed = max(selected - claimed, 0)
        result.update(explicit_selected=selected, explicit_unclaimed=unclaimed)
        unresolved += unclaimed
    failed = count("errors") > 0 or any(summary.get(key) for key in (
        "infrastructure_error", "settlement_unknown", "quota_exhausted",
        "general_quota_exhausted", "batch_quota_exhausted", "batch_unavailable", "rate_limited",
    ))
    result.update(
        completion_outcome="error" if failed else "partial" if unresolved or summary.get("yield_requested") else "completed",
        incomplete=unresolved > 0 or bool(summary.get("yield_requested")),
        unresolved=unresolved,
    )
    return result


def copy_collection_outcome(target: dict, source: Mapping) -> None:
    """Preserve only explicit new evidence; absence never proves completeness."""
    if source.get("completion_outcome") not in {"completed", "partial", "error"}:
        return
    for key in ("completion_outcome", "incomplete", "unresolved", "explicit_selected", "explicit_unclaimed"):
        if key in source:
            target[key] = source[key]


def sync_completion_fields(summary: Mapping) -> dict:
    """Combine marked collection results in a newly executed sync summary."""
    def incomplete(value):
        if isinstance(value, Mapping):
            if value.get("completion_outcome") in {"partial", "error"} and value.get("incomplete"):
                return True
            return any(incomplete(child) for child in value.values() if isinstance(child, (Mapping, list)))
        return isinstance(value, list) and any(incomplete(child) for child in value)

    # The root is a derived progress projection, not a stage result. Reusing
    # its previous marker would make a transient partial outcome irreversible.
    partial = bool(summary.get("yield_requested")) or any(
        incomplete(child)
        for child in summary.values()
        if isinstance(child, (Mapping, list))
    )
    failed = int(summary.get("errors") or 0) > 0 or any(summary.get(key) for key in (
        "infrastructure_error", "settlement_unknown", "quota_exhausted",
        "general_quota_exhausted", "batch_quota_exhausted", "batch_unavailable", "rate_limited",
    ))
    return {"completion_outcome": "error" if failed else "partial" if partial else "completed", "incomplete": partial}


def copy_collection_stop(target, source):
    """Preserve a collector stop across wrappers, without copying raw errors."""
    quota_flags = ("quota_exhausted", "general_quota_exhausted", "batch_quota_exhausted")
    flags = ("settlement_unknown", "infrastructure_error", *quota_flags, "rate_limited", "batch_unavailable")
    if not any(source.get(key) for key in flags):
        if source.get("yield_requested"):
            target.update(yield_requested=True, stopped_reason="scheduled_handoff")
            return True
        return False
    for key in flags:
        target[key] = bool(target.get(key) or source.get(key))
    target["stopped_reason"] = str(
        source.get("stopped_reason")
        or ("settlement_unknown" if target["settlement_unknown"]
            else "infrastructure_error" if target["infrastructure_error"]
            else "quota_exhausted" if any(target[key] for key in quota_flags)
            else "provider_rate_limited" if target["rate_limited"]
            else "batch_unavailable")
    )[:80]
    return True


def collection_explanation(summary: Mapping) -> dict:
    """Explain recorded work without changing its outcome or guessing recovery.

    Counts are observations, not disjoint buckets: errors may count requests or
    targets, and a stop can affect the same work as a deferral. Missing historical
    fields stay unknown. Queue state, not this summary, owns the next deadline.
    """
    summary = summary if isinstance(summary, Mapping) else {}

    def count(key):
        value = summary.get(key)
        return value if type(value) is int and value >= 0 else None

    reasons = []

    def reason(code, value=None):
        reasons.append({"code": code, "count": value})

    deferred = count("metadata_deferred")
    counterless = count("source_view_count_absent")
    if counterless:
        reason("counters_pending", counterless)
    if deferred:
        if counterless is not None and counterless <= deferred:
            # These two recorded counters have distinct producers: a saved
            # counterless General profile, or Stats work deferred to General.
            if deferred > counterless:
                reason("metadata_followup", deferred - counterless)
        else:
            reason("metadata_deferred_unspecified", deferred)

    for key, code in (
        ("source_view_count_invalid", "source_counter_invalid"),
        ("metadata_duplicate_items", "source_duplicate_items"),
        ("explicit_unclaimed", "selected_not_claimed"),
    ):
        value = count(key)
        if value:
            reason(code, value)

    flags = (
        (("settlement_unknown", "completion_unknown"), "settlement_unknown"),
        (("infrastructure_error",), "infrastructure_error"),
        (("quota_exhausted", "general_quota_exhausted", "batch_quota_exhausted", "daily_quota_exhausted"), "quota_wait"),
        (("rate_limited",), "provider_rate_limited"),
        (("batch_unavailable",), "batch_unavailable"),
        (("yield_requested",), "scheduled_handoff"),
    )
    for keys, code in flags:
        if any(summary.get(key) is True for key in keys):
            reason(code)

    errors = count("errors")
    if errors:
        # Keep this separate even when a narrower observation exists: aggregate
        # summaries cannot prove that it explains every recorded error.
        reason("recorded_errors", errors)
    unresolved = count("unresolved")
    outcome = summary.get("completion_outcome")
    outcome = outcome if isinstance(outcome, str) and outcome in {"completed", "partial", "error"} else None
    incomplete = summary.get("incomplete")
    incomplete = incomplete if type(incomplete) is bool else None
    if (unresolved or incomplete or outcome in {"partial", "error"}) and not reasons:
        reason("cause_not_recorded", unresolved)

    publication_waiting = count("publication_waiting")
    if publication_waiting is None:
        publication_waiting = count("stream_lifecycle_probed")
    return {
        "completion_outcome": outcome,
        "incomplete": incomplete,
        "unresolved": unresolved,
        "reasons": reasons,
        "handled": {
            "publication_waiting": publication_waiting,
            "unavailable_marked": count("unavailable_marked"),
        },
        "scope": "recorded_run",
        "current_recovery": "not_evaluated",
        "next_attempt": "stored_queue",
    }


def sync_run_explanation(row: Mapping) -> dict:
    """Bounded historical explanation for one persisted run, never live health.

    The common video section already includes all types. Its legacy Long
    projection must not be counted again. No adjacent successful run is read.
    """
    row = row if isinstance(row, Mapping) else {}
    summary = row.get("summary_json")
    summary = summary if isinstance(summary, Mapping) else {}
    root = collection_explanation(summary)
    reasons = [{**item, "stage": "run"} for item in root["reasons"]]
    stages = {}
    sections = ["channel_metadata", "typed_inventory", "new_video_stats", "long_availability_loss"]
    if isinstance(summary.get("video_collection"), Mapping) and summary["video_collection"]:
        sections.append("video_collection")
    else:
        sections.extend(("regular_video_stats", "short_video_collection", "completed_stream_collection"))
    discovery = summary.get("channel_discovery")
    routing = discovery.get("routing") if isinstance(discovery, Mapping) else None
    shared_discovery = isinstance(routing, Mapping) and routing.get("mode") == "shared_inventory_long_projection"
    if not shared_discovery or not isinstance(summary.get("typed_inventory"), Mapping) or not summary["typed_inventory"]:
        sections.append("channel_discovery")
    for stage in sections:
        section = summary.get(stage)
        if not isinstance(section, Mapping) or not section:
            continue
        explanation = collection_explanation(section)
        stages[stage] = {
            key: explanation[key]
            for key in ("completion_outcome", "incomplete", "unresolved", "handled")
        }
        reasons.extend({**item, "stage": stage} for item in explanation["reasons"])

    # The root marker summarizes its children; an explained stage supplies its
    # reason. Never replace an explicit root error/stop with a child's success.
    if any(item["stage"] != "run" for item in reasons):
        reasons = [item for item in reasons if not (
            item["stage"] == "run" and item["code"] == "cause_not_recorded"
        )]
    errors = row.get("errors")
    if type(errors) is int and errors > 0 and not any(item["code"] == "recorded_errors" for item in reasons):
        reasons.append({"code": "recorded_errors", "count": errors, "stage": "run"})
    status = row.get("status")
    status = status if isinstance(status, str) and status in {"running", "completed", "partial", "error"} else None
    if status in {"partial", "error"} and not reasons:
        reasons.append({"code": "cause_not_recorded", "count": None, "stage": "run"})
    return {
        "status": status,
        "reasons": reasons,
        "stages": stages,
        "scope": "recorded_run",
        "current_recovery": "not_evaluated",
        "next_attempt": "stored_queue",
    }
