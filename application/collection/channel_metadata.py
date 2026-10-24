"""Channel metadata refresh and recovery, borrowing an explicit collection scope."""

import re
from datetime import datetime, timezone, timedelta
from collection_policy import YOUTUBE_QUOTA_TIMEZONE

from collection.context import CollectionContext
from pipeline_trace import event
from collection.provider_steps import ProviderStep, run_provider_steps
from collection.lifecycle import update_channel_values
from channel_inventory_estimate import long_inventory_estimate_values
from channel_sync_policy import channel_sync_eligibility
from channel_collection_scope import channel_scope, require_control_epoch
from collection_shutdown import stop_at_boundary
from collection_outcome import copy_collection_stop as _copy_collection_stop
from db import ChannelMetadataColumnsUnavailableError, ChannelSyncRepository, TypedInventoryRepository
from source_log import source_log_message
from youtube_channel_source_fields import youtube_channel_counter_values
from youtube_channels import CHANNEL_METADATA_MAX_IDS_PER_REQUEST
from youtube_source_fields import clean_optional_text
from typed_playlist import PlaylistTraversalError
from youtube_errors import YouTubeAPIError, YouTubePlaylistNotFoundError, youtube_rate_limit_retry_after


CHANNEL_TITLE_MAX_CHARS = 1000
CHANNEL_AVATAR_URL_MAX_CHARS = 2048
CHANNEL_METADATA_REFRESH_INTERVAL = timedelta(hours=1)
CHANNEL_METADATA_STATUS_RUNNING = "running"
CHANNEL_METADATA_STATUS_OK = "ok"
CHANNEL_METADATA_STATUS_ERROR = "error"
CHANNEL_METADATA_STATUS_NOT_FOUND = "channel_not_found"
RUNNING_STATUS_STALE_AFTER = timedelta(hours=6)
CHANNEL_METADATA_RUNNING_STALE_AFTER = timedelta(minutes=15)
STALE_RUNNING_RECOVERY_CODE = "stale_running_recovered"
QUOTA_DEFERRAL_CODES = frozenset({
    "youtube_quota_exhausted",
    "metadata_recheck_quota_deferred",
    "metadata_recheck_quota_exhausted",
})
NO_LONG_REACTIVATION_ATTEMPTS = 3


INITIAL_LONG_INVENTORY_AUTO_LIMIT = 2500
STORED_ERROR_CODE_RE = re.compile(r"[a-z0-9_]{1,64}")


class MetadataSettlementUnknown(RuntimeError):
    """A write or its receipt needs reconciliation; never replay it here."""


class MetadataSettlementDenied(RuntimeError):
    """Only a proven pre-dispatch HOT denial, with no prior unknown write."""


class MetadataSettlement:
    """Opt-in owner-thread certainty and acknowledged running-marker tracking."""
    def __init__(self):
        self.summary, self.pending = {}, {}

    def unknown(self):
        self.summary['settlement_unknown'] = True
        raise MetadataSettlementUnknown('channel_metadata_settlement_unknown') from None

    def call(self, action, *args, **kwargs):
        from db.hot.hot_collection_control import admission_denial
        if self.summary.get('settlement_unknown'):
            self.unknown()
        try:
            return action(*args, **kwargs)
        except Exception as error:
            denied = admission_denial(error)
            if denied is not None and not getattr(denied, 'outcome_may_be_committed', False):
                raise MetadataSettlementDenied('channel_metadata_hot_denied') from None
            self.unknown()

    def marked(self, channels, started_at):
        self.pending.update({channel['channel_id']: {**channel_scope(channel), 'started_at': started_at}
                             for channel in channels})

    def settled(self, channel_id):
        self.pending.pop(channel_id, None)


def _metadata_options(settlement):
    # Preserve existing wrapper/callback keyword contracts in legacy mode.
    return {} if settlement is None else {'settlement': settlement}


def _metadata_write(settlement, action, *args, **kwargs):
    return action(*args, **kwargs) if settlement is None else settlement.call(action, *args, **kwargs)


def _metadata_followup(certainty, action, *args, **kwargs):
    # A prior metadata control action may already be committed. A denied later
    # enqueue/priority cannot turn an empty marker set into terminal proof.
    try:
        return action(*args, **kwargs) if certainty is None else certainty.call(action, *args, **kwargs)
    except MetadataSettlementDenied:
        certainty.unknown()


def _metadata_update(channel_id, values, *, repository, expected_epoch, settlement=None):
    if settlement is None:
        return update_channel_values(channel_id, values, repository=repository, expected_epoch=expected_epoch)
    settlement.call(repository.update_channel, channel_id, values, expected_epoch=expected_epoch)
    if values.get('channel_metadata_status') != 'running' and 'channel_metadata_started_at' in values:
        settlement.settled(channel_id)


def _propagate_metadata_uncertainty(error, settlement):
    if settlement is not None and isinstance(error, (MetadataSettlementUnknown, MetadataSettlementDenied)):
        raise error


def get_long_video_count(channel_id, *, context: CollectionContext):
    playlist_id = context.sources.get_longform_playlist_id(channel_id)
    if not playlist_id:
        return None
    response = context.sources.fetch_playlist_items_page(
        playlist_id,
        max_results=1,
        phase="channel_inventory_count",
    )
    page_info = response.get("pageInfo") or {}
    total_results = page_info.get("totalResults")
    if total_results is None:
        return None
    return int(total_results)


def has_completed_initial_long_inventory(channel):
    # Linked verified work and explicit permission own readiness. Legacy
    # progress markers cannot grant or revoke the canonical decision.
    return channel_sync_eligibility(channel).allow_discovery


def int_or_none(value):
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def stored_channel_error_code(error, *, default, context: CollectionContext):
    """The value for a stored channel error column: a fixed code, never error text.

    Provider and transport messages can contain request URLs and, in older
    versions, the API key; only the class of failure is kept.
    """
    if isinstance(error, str):
        return error if STORED_ERROR_CODE_RE.fullmatch(error) else default
    if context.sources.is_youtube_daily_quota_exhausted(error):
        return "youtube_quota_exhausted"
    if youtube_rate_limit_retry_after(error) is not None:
        return "provider_rate_limited"
    return safe_initial_inventory_error_code(error, default=default)


def safe_initial_inventory_error_code(error, *, default="initial_inventory_failed"):
    if isinstance(error, YouTubePlaylistNotFoundError):
        return "long_playlist_not_found"
    if isinstance(error, PlaylistTraversalError):
        return "initial_inventory_" + error.code
    if isinstance(error, YouTubeAPIError):
        return "youtube_api_error"
    return default


def clear_initial_inventory_failure(channel_id, *, context: CollectionContext, expected_epoch, settlement=None):
    expected_epoch = require_control_epoch(expected_epoch)
    if settlement is not None:
        result = settlement.call(ChannelSyncRepository(context.database).clear_initial_inventory_error,
                                 channel_id, expected_epoch=expected_epoch)
        if not isinstance(result, dict) or result.get('channel_id') != channel_id or result.get('cleared') is not True:
            settlement.unknown()
        settlement.summary['initial_inventory_errors_cleared'] = settlement.summary.get('initial_inventory_errors_cleared', 0) + 1
        return result
    try:
        return ChannelSyncRepository(context.database).clear_initial_inventory_error(channel_id, expected_epoch=expected_epoch)
    except Exception as error:
        print(
            source_log_message('inventory_error_clear_failed', lambda: ("[channel-sync] safe inventory error clear skipped for "
            f"{channel_id}: {error.__class__.__name__}"), error=error),
            flush=True,
        )
        return None


def bounded_avatar_url(value):
    """An avatar URL within the stored bound; a longer one is dropped, not stored."""
    value = clean_optional_text(value)
    return value if value is None or len(value) <= CHANNEL_AVATAR_URL_MAX_CHARS else None


def normalize_country_code(value):
    value = clean_optional_text(value)
    if not value:
        return None
    value = value.upper()
    return value if len(value) == 2 and value.isalpha() else None


def channel_country_code(item):
    """Return the official channel country with the branding fallback."""

    snippet = item.get("snippet") or {}
    branding_channel = (item.get("brandingSettings") or {}).get("channel") or {}
    return normalize_country_code(
        snippet.get("country") or branding_channel.get("country")
    )


def channel_created_at(item):
    """Return the immutable YouTube channel creation timestamp when present."""

    return clean_optional_text((item.get("snippet") or {}).get("publishedAt"))


def channel_metadata_update_values(item, now):
    snippet = item.get("snippet") if isinstance(item, dict) else None
    # This requested part must provide the required textual identity. A broken
    # response follows the existing error path without renewing source clocks.
    if (
        not isinstance(snippet, dict)
        or not isinstance(snippet.get("title"), str)
        or len(snippet["title"]) > CHANNEL_TITLE_MAX_CHARS
    ):
        raise ValueError("channel_metadata_snippet_invalid")
    stats = item.get("statistics") or {}
    content_details = item.get("contentDetails") or {}
    related_playlists = content_details.get("relatedPlaylists") or {}
    thumbnails = snippet.get("thumbnails") or {}
    default_thumb = thumbnails.get("default") or {}

    values = {
        "channel_metadata_checked_at": now.isoformat(),
        "channel_metadata_next_after": (
            now + CHANNEL_METADATA_REFRESH_INTERVAL
        ).isoformat(),
        "channel_metadata_status": CHANNEL_METADATA_STATUS_OK,
        "channel_metadata_started_at": None,
        "channel_metadata_error": None,
        "channel_metadata_error_count": 0,
    }

    values["title"] = snippet["title"]
    values["avatar_url"] = bounded_avatar_url(default_thumb.get("url"))
    values["uploads_playlist_id"] = clean_optional_text(related_playlists.get("uploads"))

    values["country_code"] = channel_country_code(item)
    values["country_collected_at"] = now.isoformat()
    values["channel_created_at"] = channel_created_at(item)

    values.update(youtube_channel_counter_values(stats))

    return values


def youtube_quota_reset_at(now):
    """The next Pacific midnight, when the YouTube Data API daily quota resets."""
    from collection_policy import next_pacific_midnight
    return next_pacific_midnight(now)


def no_long_reactivation_retry_due(channel):
    """A failed reactivation leaves its error code on a no-long channel."""
    return (
        channel.get("initial_long_inventory_status") == "skipped_no_long_playlist"
        and bool(channel.get("initial_long_inventory_error_code"))
        and int(channel.get("initial_long_inventory_error_count") or 0)
        < NO_LONG_REACTIVATION_ATTEMPTS
    )


def channel_metadata_is_due(channel, now, *, parse_dt):
    next_after = parse_dt(channel.get("channel_metadata_next_after"))
    if not next_after:
        return True
    if next_after.tzinfo is None:
        next_after = next_after.replace(tzinfo=timezone.utc)
    return next_after <= now


def _channel_metadata_write_is_committed(channel, observed_at, *, parse_dt):
    if not channel or channel.get("channel_metadata_status") != CHANNEL_METADATA_STATUS_OK:
        return False
    checked_at = parse_dt(channel.get("channel_metadata_checked_at"))
    if not checked_at:
        return False
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=timezone.utc)
    expected = observed_at
    if expected.tzinfo is None:
        expected = expected.replace(tzinfo=timezone.utc)
    return checked_at == expected


def running_status_is_stale(started_at, now=None, stale_after=RUNNING_STATUS_STALE_AFTER, *, parse_dt):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    started = parse_dt(started_at)
    if not started:
        return True
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return started <= now - stale_after


def load_due_channel_metadata_channels(limit=150, now=None, *, context: CollectionContext):
    if limit <= 0:
        return []

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    due = []
    page = 0
    page_size = max(limit, 100)
    repository = ChannelSyncRepository(context.database)

    while len(due) < limit:
        try:
            rows = repository.load_due_metadata_page(
                now,
                page=page,
                page_size=page_size,
            )
        except ChannelMetadataColumnsUnavailableError:
            print(
                "[channel-metadata-sync] skipped: metadata columns unavailable",
                flush=True,
            )
            return None

        for channel in rows:
            if not channel_sync_eligibility(channel).allow_channel_metadata:
                continue
            if channel.get("channel_metadata_status") == CHANNEL_METADATA_STATUS_RUNNING:
                continue
            if not channel_metadata_is_due(channel, now, parse_dt=context.sources.parse_dt):
                continue
            due.append(channel)
            if len(due) >= limit:
                break

        if len(rows) < page_size:
            break
        page += 1

    return due


def mark_channel_metadata_error(channel, error, now, *, repository, context: CollectionContext, settlement=None):
    code = stored_channel_error_code(error, default="channel_metadata_failed", context=context)
    error_count = int(channel.get("channel_metadata_error_count") or 0)
    deferred = True
    if code == "provider_rate_limited":
        next_after = now + timedelta(seconds=youtube_rate_limit_retry_after(error) or 60)
    elif code in QUOTA_DEFERRAL_CODES:
        # The channel did not fail: wait for the quota reset without backoff.
        next_after = max(now + timedelta(hours=1), youtube_quota_reset_at(now))
    else:
        deferred = False
        error_count += 1
        next_after = now + timedelta(hours=min(24, 2 ** error_count))
    # Waiting for YouTube is not a channel failure (RR-25). An "error" status
    # would hide the last confirmed profile and make cleanup treat it as
    # expired. Keep "ok": the stored profile and its acquisition time are
    # unchanged, so the 29-day window still applies to them.
    keep_ok = deferred and channel.get("channel_metadata_status") == CHANNEL_METADATA_STATUS_OK
    _metadata_update(
        channel["channel_id"],
        {
            "channel_metadata_next_after": next_after.isoformat(),
            "channel_metadata_status": CHANNEL_METADATA_STATUS_OK if keep_ok else CHANNEL_METADATA_STATUS_ERROR,
            "channel_metadata_started_at": None,
            "channel_metadata_error": code,
            "channel_metadata_error_count": error_count,
        },
        repository=repository,
        expected_epoch=channel_scope(channel)["control_epoch"], **_metadata_options(settlement),
    )


def _reconcile_or_mark_channel_metadata_persistence_error(channel, observed_at, primary_error, *, context: CollectionContext):
    channel_id = channel["channel_id"]
    current = None
    try:
        with context.fresh_channel_repository() as repository:
            current = repository.load_channel_record(channel_id)
    except Exception as reconciliation_error:
        print(
            source_log_message('metadata_reconcile_error', lambda: ("[channel-metadata-sync] persistence reconcile failed "
            f"channel_id={channel_id} "
            f"primary_error_type={primary_error.__class__.__name__} "
            f"reconciliation_error_type={reconciliation_error.__class__.__name__}"), error=primary_error, secondary_error=reconciliation_error),
            flush=True,
        )
    else:
        if (current and channel_scope(current) == channel_scope(channel)
                and not current.get("is_hidden")
                and _channel_metadata_write_is_committed(current, observed_at, parse_dt=context.sources.parse_dt)):
            print(
                source_log_message('metadata_response_reconciled', lambda: ("[channel-metadata-sync] persistence response reconciled "
                f"channel_id={channel_id} "
                f"error_type={primary_error.__class__.__name__}"), error=primary_error),
                flush=True,
            )
            return True

    try:
        with context.fresh_channel_repository() as repository:
            mark_channel_metadata_error(
                channel,
                "channel_metadata_persistence_"
                + primary_error.__class__.__name__.lower(),
                observed_at,
                repository=repository,
                context=context,
            )
    except Exception as cleanup_error:
        print(
            source_log_message('metadata_error_state_failed', lambda: ("[channel-metadata-sync] persistence error state failed "
            f"channel_id={channel_id} "
            f"primary_error_type={primary_error.__class__.__name__} "
            f"cleanup_error_type={cleanup_error.__class__.__name__}"), error=primary_error, secondary_error=cleanup_error),
            flush=True,
        )
    return False


def defer_failed_no_long_reactivation(channel, inventory_result, now, *, context: CollectionContext, settlement=None):
    """Return a failed reactivation to no-long with a retry clock.

    The failure is already recorded (code, time, count). A quota stop is not
    the channel's failure and does not use an attempt. Returns False, keeping
    the operator hold, once the attempts are used up.
    """
    previous_count = int(channel.get("initial_long_inventory_error_count") or 0)
    quota = bool(inventory_result.get("quota_exhausted"))
    count = previous_count if quota else previous_count + 1
    if count >= NO_LONG_REACTIVATION_ATTEMPTS:
        return False
    retry_at = (
        max(now + timedelta(hours=1), youtube_quota_reset_at(now))
        if quota else now + timedelta(hours=2 ** count)
    )
    values = {
        "initial_long_inventory_status": "skipped_no_long_playlist",
        "initial_long_inventory_started_at": None,
        "initial_28d_stats_status": "skipped",
        "channel_metadata_next_after": retry_at.isoformat(),
    }
    if quota:
        values["initial_long_inventory_error_count"] = previous_count
    _metadata_update(channel["channel_id"], values, repository=ChannelSyncRepository(context.database), expected_epoch=channel_scope(channel)["control_epoch"], **_metadata_options(settlement))
    return True


def recheck_no_long_playlist_after_video_count_change(channel, values, *, context: CollectionContext):
    return run_provider_steps(recheck_no_long_playlist_steps(channel, values, context=context))


def recheck_no_long_playlist_steps(channel, values, *, context: CollectionContext, settlement=None):
    """Check a no-long channel after a count increase or a failed reactivation."""
    if (channel_sync_eligibility(channel).mode != "metadata_only"
            or channel.get("initial_long_inventory_status") != "skipped_no_long_playlist"):
        return None

    channel_id = channel["channel_id"]
    try:
        response = yield ProviderStep(
            lambda ids, reason: get_long_video_count(ids[0], context=context),
            (channel_id,), "channel_inventory_count",
        )
        observed_at = _metadata_source_time(response)
        long_video_count = response.unwrap()
    except YouTubePlaylistNotFoundError:
        values.update({
            **long_inventory_estimate_values(0, status="playlist_not_found", observed_at=observed_at),
            "deep_inventory_error": "long_playlist_not_found",
        })
        clear_initial_inventory_failure(channel_id, context=context, expected_epoch=channel_scope(channel)["control_epoch"], **_metadata_options(settlement))
        return {"status": "still_no_long_playlist", "run_initial_inventory": False}
    except Exception as error:
        return {
            "status": "no_long_playlist_recheck_error",
            "run_initial_inventory": False,
            "error": stored_channel_error_code(error, default="long_playlist_recheck_failed", context=context),
            "quota_exhausted": context.sources.is_youtube_daily_quota_exhausted(error),
            "rate_limited": youtube_rate_limit_retry_after(error) is not None,
        }

    values.update({
        **long_inventory_estimate_values(long_video_count, observed_at=observed_at),
        "deep_inventory_error": None,
    })
    if long_video_count is None:
        return {"status": "no_long_playlist_recheck_error",
                "run_initial_inventory": False, "error": "inventory_count_unknown"}
    if long_video_count <= 0:
        if channel.get("initial_long_inventory_error_code"):
            # Nothing is left to load: end a failed reactivation's retries.
            clear_initial_inventory_failure(channel_id, context=context, expected_epoch=channel_scope(channel)["control_epoch"], **_metadata_options(settlement))
        return {"status": "still_no_long_playlist", "run_initial_inventory": False}
    if long_video_count <= INITIAL_LONG_INVENTORY_AUTO_LIMIT:
        values.update({
            "initial_long_inventory_status": "pending",
            "initial_28d_stats_status": "pending",
        })
        return {"status": "long_playlist_available", "run_initial_inventory": True}

    values.update({
        "initial_long_inventory_status": "manual_required",
        "initial_28d_stats_status": None,
    })
    return {"status": "long_playlist_manual_required", "run_initial_inventory": False}


def _prepare_channel_metadata_update(channel, item, observed_at, *, context: CollectionContext):
    return run_provider_steps(channel_metadata_update_steps(channel, item, observed_at, context=context))


def channel_metadata_update_steps(channel, item, observed_at, *, context: CollectionContext, settlement=None):
    # Only yielded source callbacks run in provider threads. Mutations and all
    # generator resumes belong to the coordinator's database owner thread.
    channel_id = channel["channel_id"]
    values = channel_metadata_update_values(item, observed_at)

    old_video_count = int_or_none(channel.get("video_count"))
    new_video_count = int_or_none(values.get("video_count"))
    video_count_changed = (
        old_video_count is not None
        and new_video_count is not None
        and old_video_count != new_video_count
    )
    video_count_increased = video_count_changed and new_video_count > old_video_count
    run_initial_inventory = False
    initial_inventory_completed = (
        has_completed_initial_long_inventory(channel)
        if video_count_changed
        else False
    )
    recheck = None
    change_direction = None
    discovery_prioritized = False
    recheck_summary_status = None
    recheck_error_message = None

    if (
        (video_count_increased and not initial_inventory_completed)
        or no_long_reactivation_retry_due(channel)
    ):
        recheck = yield from recheck_no_long_playlist_steps(
            channel,
            values,
            context=context, **_metadata_options(settlement),
        )

    if video_count_changed:
        values["channel_video_count_changed_at"] = observed_at.isoformat()
        if new_video_count > old_video_count:
            change_direction = "increased"
            if initial_inventory_completed:
                values["channel_discovery_no_new_streak"] = 0
        elif new_video_count < old_video_count:
            change_direction = "decreased"

        if video_count_increased and initial_inventory_completed:
            values["channel_discovery_next_after"] = observed_at.isoformat()
            discovery_prioritized = True
    if recheck:
        recheck_summary_status = recheck.get("status")
        if recheck_summary_status == "long_playlist_available":
            run_initial_inventory = bool(
                recheck.get("run_initial_inventory")
            )
        elif recheck_summary_status == "no_long_playlist_recheck_error":
            recheck_error_message = (
                f"{channel_id}: {recheck.get('error')}"
            )
    return {
        "channel": channel,
        "values": values,
        "video_count_changed": video_count_changed,
        "initial_inventory_completed": initial_inventory_completed,
        "change_direction": change_direction,
        "discovery_prioritized": discovery_prioritized,
        "recheck_summary_status": recheck_summary_status,
        "recheck_error_message": recheck_error_message,
        "run_initial_inventory": run_initial_inventory,
        "quota_exhausted": bool(recheck and recheck.get("quota_exhausted")),
        "rate_limited": bool(recheck and recheck.get("rate_limited")),
    }


def _persist_channel_metadata_batch(prepared_updates, observed_at, *, context: CollectionContext, settlement=None):
    if not prepared_updates:
        return []
    rows = []
    for prepared in prepared_updates:
        row = {**channel_scope(prepared["channel"]), "values": prepared["values"]}
        action = {
            "long_playlist_available": "no_long_restart",
            "long_playlist_manual_required": "no_long_manual_required",
        }.get(prepared.get("recheck_summary_status"))
        if action is not None:
            row["control_action"] = action
        rows.append(row)
    try:
        outcomes = _metadata_write(settlement, ChannelSyncRepository(context.database).update_metadata_batch, rows)
    except Exception as error:
        if settlement is not None:
            raise
        # An ambiguous batch response may conceal committed rows. Never replay
        # the mutation: inspect each observation clock through a fresh gateway.
        errors = []
        for prepared in prepared_updates:
            committed = _reconcile_or_mark_channel_metadata_persistence_error(
                prepared["channel"], observed_at, error, context=context)
            errors.append(None if committed else error.__class__.__name__)
        return errors
    errors = []
    for prepared in prepared_updates:
        status = outcomes[prepared["channel"]["channel_id"]]
        if status == "updated":
            if settlement is not None:
                settlement.settled(prepared["channel"]["channel_id"])
                # A later reactivation/progress failure must retain proof of
                # every row in this already committed batch, not just rows
                # whose legacy summary loop happened to run first.
                settlement.summary.setdefault('metadata_persisted_channel_scopes', []).append(
                    channel_scope(prepared["channel"]))
            errors.append(None)
            continue
        error = RuntimeError("channel_metadata_batch_" + status)
        if settlement is None:
            _reconcile_or_mark_channel_metadata_persistence_error(
                prepared["channel"], observed_at, error, context=context)
        else:
            mark_channel_metadata_error(prepared["channel"], error, observed_at,
                context=context, repository=ChannelSyncRepository(context.database), **_metadata_options(settlement))
        errors.append(error.__class__.__name__)
    return errors


def run_channel_metadata_sync_batch(limit=150, *, context: CollectionContext):
    return run_provider_steps(channel_metadata_steps(limit=limit, context=context))


def _metadata_source_time(response):
    observed_at = response.observed_at
    if (not isinstance(observed_at, datetime) or observed_at.tzinfo is None
            or observed_at.utcoffset() is None):
        raise ValueError("channel_metadata_source_clock_invalid")
    return observed_at


def channel_metadata_steps(limit=150, *, context: CollectionContext, captured_channels=None,
                           settlement=None, started_at=None):
    """The metadata pass as bounded source steps, with owner-thread settlement.

    The synchronous entrypoint drives this same iterator. A response's source
    clock is captured by the provider step before it can wait for settlement.
    This iterator is not itself a worker callback: it reads and writes the DB.
    """
    now = datetime.now(timezone.utc) if started_at is None else started_at
    channels = (load_due_channel_metadata_channels(limit=limit, now=now, context=context)
                if captured_channels is None else list(captured_channels))
    if settlement is not None and (captured_channels is None or len(channels) > 50):
        raise ValueError('channel_metadata_captured_chunk_required')
    summary = {} if settlement is None else settlement.summary
    summary.update({
        "requested": 0,
        "processed": 0,
        "updated": 0,
        "video_count_changed": 0,
        "video_count_increased": 0,
        "video_count_decreased": 0,
        "video_count_changed_channel_ids": [],
        "video_count_changed_channel_scopes": [],
        "video_count_increased_channel_ids": [],
        "video_count_increased_channel_scopes": [],
        "video_count_decreased_channel_ids": [],
        "video_count_decreased_channel_scopes": [],
        "channel_discovery_prioritized": 0,
        "typed_inventory_prioritized": 0,
        "no_long_reactivated": 0,
        "no_long_reactivation_deferred": 0,
        "still_no_long_playlist": 0,
        "manual_required": 0,
        "missing": 0,
        "batches": 0,
        "errors": 0,
        "error_messages": [],
        "skipped_missing_columns": channels is None,
    })
    if channels is None:
        return summary
    if not channels:
        return summary

    def record_quota_stop(error):
        if youtube_rate_limit_retry_after(error) is not None:
            summary.update(rate_limited=True, stopped_reason="provider_rate_limited")
            return True
        if not context.sources.is_youtube_daily_quota_exhausted(error):
            return False
        summary.update(quota_exhausted=True, general_quota_exhausted=True,
                       stopped_reason="quota_exhausted")
        return True

    _metadata_write(settlement, ChannelSyncRepository(context.database).assert_metadata_batch_ready)
    summary["requested"] = len(channels)
    print(
        f"[channel-metadata-sync] start requested={len(channels)} limit={limit}",
        flush=True,
    )

    for start in range(0, len(channels), CHANNEL_METADATA_MAX_IDS_PER_REQUEST):
        if stop_at_boundary(summary):
            break
        chunk = channels[start:start + CHANNEL_METADATA_MAX_IDS_PER_REQUEST]
        channel_by_id = {channel["channel_id"]: channel for channel in chunk}
        summary["batches"] += 1

        _metadata_write(settlement, ChannelSyncRepository(context.database).mark_metadata_running,
                        chunk, now.isoformat())
        if settlement is not None:
            settlement.marked(chunk, now.isoformat())
        try:
            response = yield ProviderStep(
                lambda ids, reason: context.sources.fetch_channel_metadata_batch(ids),
                tuple(channel_by_id), "channel_metadata", materialize=True,
            )
            items = response.unwrap()
        except Exception as error:
            _propagate_metadata_uncertainty(error, settlement)
            failed_at = datetime.now(timezone.utc)
            summary["errors"] += 1
            summary["error_messages"].append(str(error))
            for channel in chunk:
                mark_channel_metadata_error(channel, error, failed_at, context=context, repository=ChannelSyncRepository(context.database), **_metadata_options(settlement))
            print(source_log_message('metadata_batch_error', lambda: (f"[channel-metadata-sync] batch error: {error}"), error=error), flush=True)
            if record_quota_stop(error):
                break
            continue

        observed_at = _metadata_source_time(response)
        returned_ids = set()
        prepared_updates = []
        for item in items:
            channel_id = item.get("id")
            if (
                not channel_id
                or channel_id not in channel_by_id
                or channel_id in returned_ids
            ):
                continue
            channel = channel_by_id[channel_id]
            returned_ids.add(channel_id)
            # A count-change recheck may exhaust General quota within a valid
            # channels.list response. Keep the old observation/count for these
            # deferred channels so their existing retry can detect the change.
            if summary.get("rate_limited"):
                mark_channel_metadata_error(channel, "provider_rate_limited", observed_at, context=context, repository=ChannelSyncRepository(context.database), **_metadata_options(settlement))
                continue
            if summary.get("quota_exhausted"):
                mark_channel_metadata_error(channel, "metadata_recheck_quota_deferred", observed_at, context=context, repository=ChannelSyncRepository(context.database), **_metadata_options(settlement))
                continue
            try:
                prepared = yield from channel_metadata_update_steps(channel, item, observed_at, context=context, **_metadata_options(settlement))
                if prepared.get("rate_limited"):
                    record_quota_stop("provider_rate_limited")
                    summary["errors"] += 1
                    summary["error_messages"].append(prepared["recheck_error_message"])
                    mark_channel_metadata_error(channel, "provider_rate_limited", observed_at, context=context, repository=ChannelSyncRepository(context.database), **_metadata_options(settlement))
                    continue
                if prepared.get("quota_exhausted"):
                    record_quota_stop("reason=quotaExceeded")
                    summary["errors"] += 1
                    summary["error_messages"].append(prepared["recheck_error_message"])
                    mark_channel_metadata_error(channel, "metadata_recheck_quota_exhausted", observed_at, context=context, repository=ChannelSyncRepository(context.database), **_metadata_options(settlement))
                    continue
                prepared_updates.append(prepared)
            except Exception as error:
                _propagate_metadata_uncertainty(error, settlement)
                summary["errors"] += 1
                message = f"{channel_id}: {error}"
                summary["error_messages"].append(message)
                mark_channel_metadata_error(channel, error, observed_at, context=context, repository=ChannelSyncRepository(context.database), **_metadata_options(settlement))
                print(source_log_message('metadata_item_error', lambda: (f"[channel-metadata-sync] item failed {message}"), error=error), flush=True)
                record_quota_stop(error)
                continue

        # Normal source updates can settle together. Reactivations are persisted
        # only immediately before their kickoff: a quota stop must not leave an
        # unstarted channel pending with its count-change trigger already consumed.
        normal_updates = [p for p in prepared_updates if not p["run_initial_inventory"]]
        persistence_errors = iter(_persist_channel_metadata_batch(normal_updates, observed_at, context=context, **_metadata_options(settlement)))
        for prepared in prepared_updates:
            channel_id = prepared["channel"]["channel_id"]
            if prepared["run_initial_inventory"]:
                if _copy_collection_stop({}, summary):
                    mark_channel_metadata_error(prepared["channel"], "metadata_followup_deferred", observed_at, context=context, repository=ChannelSyncRepository(context.database), **_metadata_options(settlement))
                    continue
                error_type = _persist_channel_metadata_batch([prepared], observed_at, context=context, **_metadata_options(settlement))[0]
            else:
                error_type = next(persistence_errors)
            if error_type:
                event("channel_metadata_write", "unconfirmed", channel_id=channel_id, reason="persistence_unconfirmed")
                summary["errors"] += 1
                summary["error_messages"].append(
                    f"{channel_id}: channel metadata persistence failed ({error_type})"
                )
                print(
                    source_log_message('metadata_update_error', lambda: ("[channel-metadata-sync] update failed "
                    f"channel_id={channel_id} error_type={error_type}"), error=error_type),
                    flush=True,
                )
                continue
            event("channel_metadata_write", "confirmed", channel_id=channel_id, observed_at=observed_at.isoformat())
            video_count_changed = prepared["video_count_changed"]
            initial_inventory_completed = prepared["initial_inventory_completed"]
            change_direction = prepared["change_direction"]
            discovery_prioritized = prepared["discovery_prioritized"]
            recheck_summary_status = prepared["recheck_summary_status"]
            recheck_error_message = prepared["recheck_error_message"]
            manual_hold = recheck_summary_status == "long_playlist_manual_required"
            run_initial_inventory = prepared["run_initial_inventory"] and not manual_hold
            summary["updated"] += 1
            summary["processed"] += 1
            if video_count_changed:
                event("channel_count_change", "confirmed", channel_id=channel_id, observed_at=observed_at.isoformat(),
                      reason="decrease_notice_only" if change_direction == "decreased" else
                      "increase_discovery_prioritized" if discovery_prioritized else "increase_observed")
                summary["video_count_changed"] += 1
                # Keep both directions for reporting. Only an increase can
                # accelerate discovery; a decrease is not a video identity.
                if not manual_hold:
                    summary["video_count_changed_channel_ids"].append(channel_id)
                    summary["video_count_changed_channel_scopes"].append(channel_scope(prepared["channel"]))
                if change_direction == "increased":
                    summary["video_count_increased"] += 1
                    if not manual_hold:
                        summary["video_count_increased_channel_ids"].append(channel_id)
                        summary["video_count_increased_channel_scopes"].append(channel_scope(prepared["channel"]))
                elif change_direction == "decreased":
                    summary["video_count_decreased"] += 1
                    if initial_inventory_completed and not manual_hold:
                        summary["video_count_decreased_channel_ids"].append(channel_id)
                        summary["video_count_decreased_channel_scopes"].append(channel_scope(prepared["channel"]))
            if discovery_prioritized:
                summary["channel_discovery_prioritized"] += 1
            if recheck_summary_status == "still_no_long_playlist":
                summary["still_no_long_playlist"] += 1
            elif recheck_summary_status == "long_playlist_manual_required":
                summary["manual_required"] += 1
            if recheck_error_message:
                summary["error_messages"].append(recheck_error_message)

            if run_initial_inventory and not (summary.get("quota_exhausted") or summary.get("rate_limited")):
                try:
                    inventory_result = _metadata_followup(settlement, context.enqueue_initial_inventory, channel_id,
                        expected_epoch=channel_scope(prepared["channel"])["control_epoch"])
                    if settlement is not None and (not isinstance(inventory_result, dict)
                            or inventory_result.get('channel_id') != channel_id
                            or not isinstance(inventory_result.get('job_id'), str)
                            or not inventory_result['job_id'] or inventory_result.get('status') in (None, 'unknown')):
                        settlement.unknown()
                    _copy_collection_stop(summary, inventory_result)
                    summary["no_long_reactivated"] += 1
                except Exception as error:
                    _propagate_metadata_uncertainty(error, settlement)
                    summary["errors"] += 1
                    message = (
                        f"{channel_id}: initial inventory failed after "
                        f"no-long reactivation: {error}"
                    )
                    summary["error_messages"].append(message)
                    print(source_log_message('metadata_initial_error', lambda: (f"[channel-metadata-sync] {message}"), error=error), flush=True)
                    record_quota_stop(error)
                    continue
                if inventory_result.get("status") == "error":
                    summary["errors"] += 1
                    summary["error_messages"].append(
                        f"{channel_id}: initial inventory failed after no-long reactivation"
                    )
                    try:
                        if _metadata_followup(settlement, defer_failed_no_long_reactivation,
                            prepared["channel"], inventory_result, datetime.now(timezone.utc),
                            context=context, **_metadata_options(settlement),
                        ):
                            summary["no_long_reactivation_deferred"] += 1
                    except Exception as error:
                        _propagate_metadata_uncertainty(error, settlement)
                        # The recorded failure stays in operator hold.
                        print(source_log_message('metadata_reactivation_defer_error', lambda: (
                            f"[channel-metadata-sync] reactivation retry not scheduled "
                            f"channel_id={channel_id} error_type={error.__class__.__name__}"), error=error), flush=True)

        missing_ids = set(channel_by_id.keys()) - returned_ids
        for missing_id in missing_ids:
            channel = channel_by_id[missing_id]
            _metadata_update(missing_id, {
                "channel_metadata_checked_at": observed_at.isoformat(),
                "channel_metadata_next_after": (
                    observed_at + timedelta(days=1)
                ).isoformat(),
                "channel_metadata_status": CHANNEL_METADATA_STATUS_NOT_FOUND,
                "channel_metadata_started_at": None,
                "channel_metadata_error": "youtube_channel_not_found",
                "channel_metadata_error_count": int(
                    channel.get("channel_metadata_error_count") or 0
                ) + 1,
            }, repository=ChannelSyncRepository(context.database), expected_epoch=channel_scope(channel)["control_epoch"], **_metadata_options(settlement))
            summary["missing"] += 1
            summary["processed"] += 1

        if _copy_collection_stop({}, summary):
            break

    if (
        context.settings.typed_playlist_inventory_enabled
        and summary["video_count_increased_channel_ids"]
    ):
        try:
            summary["typed_inventory_prioritized"] = (
                _metadata_followup(settlement, TypedInventoryRepository(context.database).prioritize_channels,
                    summary["video_count_increased_channel_ids"],
                    expected_channel_scopes=summary["video_count_increased_channel_scopes"],
                )
            )
        except Exception as error:
            _propagate_metadata_uncertainty(error, settlement)
            summary["errors"] += 1
            summary["error_messages"].append(
                "typed inventory priority failed: " + error.__class__.__name__
            )

    print(
        "[channel-metadata-sync] done "
        f"processed={summary['processed']} updated={summary['updated']} "
        f"video_count_changed={summary['video_count_changed']} "
        f"increased={summary['video_count_increased']} "
        f"decreased={summary['video_count_decreased']} "
        f"discovery_prioritized={summary['channel_discovery_prioritized']} "
        f"errors={summary['errors']}",
        flush=True,
    )
    return summary


def recover_stale_running_statuses(channel_id=None, now=None, stale_after=RUNNING_STATUS_STALE_AFTER, channel_metadata_stale_after=CHANNEL_METADATA_RUNNING_STALE_AFTER, *, context: CollectionContext):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    recovered = {
        "checked": 0,
        "updated": 0,
        "initial_long_inventory": 0,
        "initial_28d_stats": 0,
        "channel_discovery": 0,
        "channel_metadata": 0,
        "deep_inventory": 0,
        "errors": 0,
        "error_messages": [],
    }
    page_size = 100
    page = 0
    rows_to_check = []
    repository = ChannelSyncRepository(context.database)
    while True:
        try:
            rows = repository.load_running_status_page(
                channel_id=channel_id,
                page=page,
                page_size=page_size,
            )
        except Exception as error:
            recovered["errors"] += 1
            recovered["error_messages"].append(str(error))
            print(source_log_message('lifecycle_recovery_error', lambda: (f"[lifecycle-recovery] skipped: {error}"), error=error), flush=True)
            return recovered

        rows_to_check.extend(rows)
        if channel_id or len(rows) < page_size:
            break
        page += 1

    # An operator job's lease owns these statuses: its expired lease is
    # reclaimed and the chunk resumes. Marking them error would suspend the job.
    try:
        job_owned = repository.channels_with_active_initial_jobs(
            [str(row.get("channel_id")) for row in rows_to_check
             if row.get("initial_long_inventory_status") == "running"
             or row.get("initial_28d_stats_status") == "running"]
        )
    except Exception as error:
        recovered["errors"] += 1
        recovered["error_messages"].append(error.__class__.__name__)
        return recovered

    for channel in rows_to_check:
        recovered["checked"] += 1
        channel_values = {}
        current_channel_id = channel.get("channel_id")
        initial_job_owned = str(current_channel_id) in job_owned

        if (
            not initial_job_owned
            and channel.get("initial_long_inventory_status") == "running"
            and running_status_is_stale(
                channel.get("initial_long_inventory_started_at"),
                now=now,
                stale_after=stale_after,
                parse_dt=context.sources.parse_dt,
            )
        ):
            channel_values.update({
                "initial_long_inventory_status": "error",
                "initial_long_inventory_started_at": None,
            })
            recovered["initial_long_inventory"] += 1

        if (
            not initial_job_owned
            and channel.get("initial_28d_stats_status") == "running"
            and running_status_is_stale(
                channel.get("initial_28d_stats_started_at"),
                now=now,
                stale_after=stale_after,
                parse_dt=context.sources.parse_dt,
            )
        ):
            channel_values.update({
                "initial_28d_stats_status": "error",
                "initial_28d_stats_started_at": None,
            })
            recovered["initial_28d_stats"] += 1

        # Inventory lease expiry is recovered by the common SQL claim owner.
        # Client-side status repair must not rewrite its cursor/source clock or
        # invalidate a receipt while an initial job is reconciling its ACK.

        if (
            channel.get("channel_metadata_status") == CHANNEL_METADATA_STATUS_RUNNING
            and running_status_is_stale(
                channel.get("channel_metadata_started_at"),
                now=now,
                stale_after=channel_metadata_stale_after,
                parse_dt=context.sources.parse_dt,
            )
        ):
            channel_values.update({
                "channel_metadata_status": CHANNEL_METADATA_STATUS_ERROR,
                "channel_metadata_started_at": None,
                "channel_metadata_next_after": (
                    now + CHANNEL_METADATA_REFRESH_INTERVAL
                ).isoformat(),
                "channel_metadata_error": STALE_RUNNING_RECOVERY_CODE,
                "channel_metadata_error_count": int(
                    channel.get("channel_metadata_error_count") or 0
                ) + 1,
            })
            recovered["channel_metadata"] += 1

        if (
            channel.get("deep_inventory_status") == "running"
            and running_status_is_stale(
                channel.get("deep_inventory_started_at"),
                now=now,
                stale_after=stale_after,
                parse_dt=context.sources.parse_dt,
            )
        ):
            channel_values.update({
                "deep_inventory_status": (
                    "paused" if channel.get("deep_inventory_page_token") else "error"
                ),
                "deep_inventory_started_at": None,
                "deep_inventory_error": STALE_RUNNING_RECOVERY_CODE,
            })
            recovered["deep_inventory"] += 1

        if not channel_values:
            continue

        try:
            repository.update_channel(current_channel_id, channel_values, expected_epoch=channel_scope(channel)["control_epoch"])
            recovered["updated"] += 1
        except Exception as error:
            recovered["errors"] += 1
            message = f"{current_channel_id}: {error}"
            recovered["error_messages"].append(message)
            print(source_log_message('lifecycle_recovery_update_error', lambda: (f"[lifecycle-recovery] update failed {message}"), error=error), flush=True)

    if recovered["updated"]:
        print(
            "[lifecycle-recovery] recovered "
            f"channels={recovered['updated']} "
            f"initial_long={recovered['initial_long_inventory']} "
            f"initial_28d={recovered['initial_28d_stats']} "
            f"channel_discovery={recovered['channel_discovery']} "
            f"channel_metadata={recovered['channel_metadata']} "
            f"deep_inventory={recovered['deep_inventory']}",
            flush=True,
        )
    return recovered


