"""Pure outcomes for newly executed, bounded collection work.

Do not apply this projection to historical summaries without an outcome marker:
old ``completed`` rows did not prove that their selected work was complete.
"""

from collections.abc import Mapping


# The one rule for what a collection stop ends.
# "pass": the whole automatic pass and the one-off jobs sharing its window.
# "general" / "stats": one exhausted quota bucket (quota_buckets). Until
# midnight Pacific no further request goes to it: steps that use only that
# bucket are skipped or end, the video step goes on with the other bucket and
# defers its own work without a request. "phase": only the automatic step
# that met it (video refreshes, channel profiles or playlist checks); the next
# step still runs. A one-off job's own dispatch stops on every reason, as
# before. Every reason marks its run as an error. The order is the reporting
# priority.
COLLECTION_STOP_SCOPES = {
    "settlement_unknown": "pass",
    # An unavailable operational quota gate is a local safety failure, not a
    # refusal from YouTube. Stop all new sending until a new portion can read it.
    "quota_control_unknown": "pass",
    "infrastructure_error": "pass",
    # Exhausted quota whose bucket is not named: the whole pass.
    # With a bucket flag beside it, this is only the aggregate of that flag.
    "quota_exhausted": "pass",
    "general_quota_exhausted": "general",
    "batch_quota_exhausted": "stats",
    # A failed Batch counter request: the video step sends no further batches.
    "batch_unavailable": "phase",
    # The shared limiter pauses; the next step waits for that pause first.
    "rate_limited": "phase",
}
COLLECTION_STOP_FLAGS = tuple(COLLECTION_STOP_SCOPES)
PASS_STOP_FLAGS = tuple(name for name, scope in COLLECTION_STOP_SCOPES.items() if scope == "pass")
PHASE_STOP_FLAGS = tuple(name for name, scope in COLLECTION_STOP_SCOPES.items() if scope == "phase")
BUCKET_STOP_FLAGS = tuple(name for name, scope in COLLECTION_STOP_SCOPES.items() if scope in ("general", "stats"))
BUCKET_FLAG = {COLLECTION_STOP_SCOPES[name]: name for name in BUCKET_STOP_FLAGS}
# Stops a YouTube limit explains: daily quota (a named bucket or not) and a
# rate limit (HTTP 429). Every other recorded stop mark is a failure,
# including a stop flag added later.
LIMIT_STOP_FLAGS = ("quota_exhausted", "general_quota_exhausted", "batch_quota_exhausted", "rate_limited")
FAILURE_STOP_FLAGS = tuple(sorted(
    (set(COLLECTION_STOP_FLAGS) | {"completion_unknown", "hot_runtime_interrupted", "stale_run_recovered"})
    - set(LIMIT_STOP_FLAGS)))
# ``limit_errors`` counts the errors that a quota or rate-limit refusal
# explains. It is counted next to ``errors`` at each refusal and adds up
# through every step exactly like it, so a run whose errors are all
# explained waits in yellow and any other error makes it a failure.
# A refusal site that is not counted makes a pure stop show as failed, never
# a failure as waiting.


def limit_stop_only(flags: Mapping) -> bool:
    """True when the stop marks are YouTube quota or rate limit and nothing else."""
    return (any(flags.get(name) for name in LIMIT_STOP_FLAGS)
            and not any(flags.get(name) for name in FAILURE_STOP_FLAGS))


def count_stop_as_error(summary: dict, flags: Mapping) -> None:
    """A stop makes its run an error, so at least one error is counted.

    The one error added here is explained when the stop's own marks are
    YouTube limits only (``limit_errors``); otherwise it stays a failure.
    """
    if int(summary.get("errors") or 0) >= 1:
        return
    summary["errors"] = 1
    summary["limit_errors"] = 1 if limit_stop_only(flags) else 0


def collection_stop_reason(summary: Mapping, scope=None):
    """The first recorded stop flag in table order, optionally of one scope."""
    named = any(summary.get(name) for name in BUCKET_STOP_FLAGS)
    for name, owner in COLLECTION_STOP_SCOPES.items():
        if not summary.get(name) or scope not in (None, owner):
            continue
        if scope is not None and name == "quota_exhausted" and named:
            continue
        return name
    return None


def exhausted_buckets(summary: Mapping):
    """The quota buckets this summary reports exhausted, in table order."""
    return tuple(COLLECTION_STOP_SCOPES[name] for name in BUCKET_STOP_FLAGS if summary.get(name))


def collection_outcome_fields(summary: Mapping, *, explicit_selected=None) -> dict:
    """Describe this admitted batch, not the global due backlog or limit."""
    def count(key):
        return max(int(summary.get(key) or 0), 0)

    claimed = count("claimed") if "claimed" in summary else count("requested")
    # Each acknowledged write completes one claim. A repeat identity check
    # can confirm absence without newly marking that video unavailable.
    waiting = max(count("publication_waiting"), count("stream_lifecycle_probed"))
    terminal = count("updated") + count("unavailable_marked") + count("identity_missing_settled") + waiting
    unresolved = max(claimed - terminal, count("metadata_deferred"), 0)
    result = {}
    if explicit_selected is not None:
        selected = max(int(explicit_selected), 0)
        unclaimed = max(selected - claimed, 0)
        result.update(explicit_selected=selected, explicit_unclaimed=unclaimed)
        unresolved += unclaimed
    failed = count("errors") > 0 or any(summary.get(key) for key in COLLECTION_STOP_FLAGS)
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
    failed = int(summary.get("errors") or 0) > 0 or any(summary.get(key) for key in COLLECTION_STOP_FLAGS)
    return {"completion_outcome": "error" if failed else "partial" if partial else "completed", "incomplete": partial}


def copy_collection_stop(target, source):
    """Preserve a collector stop across wrappers, without copying raw errors."""
    quota_flags = ("quota_exhausted", "general_quota_exhausted", "batch_quota_exhausted")
    flags = COLLECTION_STOP_FLAGS
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
            else "quota_state_unavailable" if target["quota_control_unknown"]
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
    unconfirmed = count("missing_unconfirmed")
    if unconfirmed:
        # Left out of one General response; the next pass confirms.
        reason("missing_unconfirmed", unconfirmed)

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
        (("quota_control_unknown",), "quota_state_unavailable"),
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
            "identity_missing_settled": count("identity_missing_settled"),
        },
        "scope": "recorded_run",
        "current_recovery": "not_evaluated",
        "next_attempt": "stored_queue",
    }


def sync_run_explanation(row: Mapping) -> dict:
    """Bounded historical explanation for one persisted run, never live health.

    The common video section already includes all types. Its separate Long
    projection must not be counted again. No adjacent successful run is read.
    """
    row = row if isinstance(row, Mapping) else {}
    summary = row.get("summary_json")
    summary = summary if isinstance(summary, Mapping) else {}
    root = collection_explanation(summary)
    reasons = [{**item, "stage": "run"} for item in root["reasons"]]
    stages = {}
    sections = ["channel_metadata", "typed_inventory", "discovery", "new_video_stats", "long_availability_loss"]
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
