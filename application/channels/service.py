"""Channel scenarios; explicit adapters own effects and resource lifetimes."""
from operator_jobs import INITIAL_CHANNEL_HYDRATION_AUTO_LIMIT
from user_channel_input import parse_user_channel_input, UnsupportedLegacyChannelLink
from channels.preparation import initial_channel_status, prepared_channel_row
from source_log import source_log_message
from channel_collection_scope import channel_scope
from saved_work import channel_id
from youtube_client import response_http_status
from channels.sources import ChannelLookupUnavailable, lookup_refusal, response_refusal_code
from collection.api import (
    completed_initial_hydration_has_errors,
    initial_hydration_has_target_count_mismatch,
    initial_hydration_verified_target_count,
    initial_hydration_verified_target_error_count,
    initial_hydration_scope_expired,
    initial_hydration_job_is_retryable,
    initial_hydration_estimate_int,
    normalized_initial_hydration_estimate,
    project_initial_hydration_retry_estimate,
    initial_hydration_payload,
    initial_hydration_error_payload,
    require_full_hydration_schema,
    load_initial_hydration_context,
    with_initial_format_readiness,
    enqueue_initial_hydration,
    prepare_initial_typed_inventory
)

USER_WORK_LIMIT = "user_work_limit_reached"

def user_work_limit_reached(error):
    return getattr(error, "message", None) == USER_WORK_LIMIT


def prepare_channel_add_hydration(channel, *, ensure_auto, get_mode, get_repository, get_typed_repository, typed_inventory_enabled, load_settings, schedule_initial_inventory, wake_worker):
    try:
        mode = get_mode()
    except Exception:
        return initial_hydration_error_payload(channel, typed_inventory_enabled=typed_inventory_enabled)
    if mode == "legacy":
        if (
            ensure_auto
            and channel.get("initial_long_inventory_status") == "pending"
        ):
            schedule_initial_inventory(channel['channel_id'], expected_epoch=channel_scope(channel)['control_epoch'])
        return with_initial_format_readiness(channel, initial_hydration_payload(channel, mode=mode), get_typed_repository=get_typed_repository, typed_inventory_enabled=typed_inventory_enabled)
    try:
        repository = get_repository()
        require_full_hydration_schema(repository)
        raw_estimate, row = load_initial_hydration_context(
            repository, channel["channel_id"], expected_epoch=channel_scope(channel)["control_epoch"])
        estimate = normalized_initial_hydration_estimate(raw_estimate, channel=channel)
        if (
            mode == "durable"
            and ensure_auto
            and not row
            and channel.get("initial_long_inventory_status") == "pending"
            and estimate
            and estimate.get("items_upper_bound", estimate.get("items")) is not None
            and estimate.get("items_upper_bound", estimate.get("items")) <= INITIAL_CHANNEL_HYDRATION_AUTO_LIMIT
            and raw_estimate.get("auto_hydration_allowed", True) is True
        ):
            result, raw_estimate, _settings = enqueue_initial_hydration(channel, repository=repository, confirmed=False, load_settings=load_settings)
            row = result["job"]
        if mode == "durable" and isinstance(row, dict) and str(
            row.get("status") or ""
        ).strip().lower() in {"queued", "running"}:
            wake_worker(channel["channel_id"])
        if mode == "durable" and ensure_auto:
            prepare_initial_typed_inventory(channel['channel_id'], get_typed_repository=get_typed_repository, typed_inventory_enabled=typed_inventory_enabled)
        return with_initial_format_readiness(channel, initial_hydration_payload(channel, row=row, estimate=raw_estimate, mode=mode), get_typed_repository=get_typed_repository, typed_inventory_enabled=typed_inventory_enabled)
    except Exception as error:
        print(
            source_log_message('initial_hydration_enqueue_error', lambda: ("[initial-hydration-enqueue] "
            f"channel={channel.get('channel_id')} error_type={error.__class__.__name__}"), error=error),
            flush=True,
        )
        return initial_hydration_error_payload(channel, typed_inventory_enabled=typed_inventory_enabled)


def add_channel(raw_input, *, deployment_profile, repository, trash_repository, lookup_cache, channel_lock, parse_legacy_input, get_channel_id_from_video, lookup_channel_response, estimate_initial_inventory, metadata_update_values, prepare_hydration, on_change, clock, prepare_only=False):
    # Restore prepares real provider data without writes, unhide or hydration.
    # Its service commits the final visibility and user work atomically in SQL.
    user_input = None
    if deployment_profile == "self-hosted":
        try:
            user_input, kind, value = parse_user_channel_input(raw_input)
        except UnsupportedLegacyChannelLink:
            return {"error": "Older /c/ and /user/ links are not supported. Use the channel's @handle or a youtube.com/channel/UC... link.",
                    "code": "unsupported_legacy_channel_link"}
        except ValueError:
            # Not a locator at all: say what to enter, not that a search failed.
            return {"error": "Enter a YouTube channel link, @handle or channel ID.",
                    "code": "user_channel_input_invalid"}
    else:
        try:
            kind, value = parse_legacy_input(raw_input)
        except ChannelLookupUnavailable as error:
            return lookup_refusal(error.code)
    if not kind or not value:
        return {"error": "Channel not found"}

    locator = (kind, value)
    known, remembered_id = lookup_cache.get(*locator)
    if known and not remembered_id:
        return {"error": "Channel not found"}
    if known:
        kind, value = "id", remembered_id
    elif kind == "video":
        # A video already in the catalogue names its channel without a request.
        channel_id = repository.load_video_channel_id(value)
        if channel_id is None:
            try:
                channel_id = get_channel_id_from_video(value)
            except ChannelLookupUnavailable as error:
                return lookup_refusal(error.code)
            lookup_cache.put(*locator, channel_id)
        if not channel_id:
            return {"error": "Channel not found"}
        kind, value = "id", channel_id

    if kind == "id":
        with channel_lock(value):
            existing = repository.load_channel_record(value)
            if existing and prepare_only:
                # A restore item for a tracked channel merges into it in SQL;
                # that needs no provider data, so no quota is spent.
                return {"status": "prepared", "channel": {"channel_id": existing["channel_id"]}}
            if existing and not prepare_only:
                if user_input is not None:
                    repository.preserve_user_input(value, user_input)
                if existing.get("is_hidden"):
                    if deployment_profile == "self-hosted":
                        result = trash_repository.restore_channel(existing["channel_id"], existing.get("hidden_at"))
                        if result.get("outcome") != "restored":
                            return {"error": "This deleted channel is awaiting permanent cleanup or changed. Refresh before adding again.", "code": "channel_trash_conflict"}
                    else:
                        raise RuntimeError("unsupported_deployment_profile")
                    on_change(value)
                    restored_channel = repository.load_channel_record(existing["channel_id"])
                    if not restored_channel:
                        raise RuntimeError("channel_restore_state_missing")
                    return {
                        "status": "restored",
                        "title": restored_channel.get("title"),
                        "channel": restored_channel,
                        "hydration": prepare_hydration(restored_channel, ensure_auto=False),
                    }
                return {
                    "status": "exists",
                    "title": existing.get("title"),
                    "channel": existing,
                    "hydration": prepare_hydration(existing, ensure_auto=False),
                }
    # for handle-based lookup we can't check DB without resolving the ID first

    params = {"part": "snippet,statistics,contentDetails,brandingSettings"}
    if kind == "id":
        params["id"] = value
    else:
        params["forHandle"] = value
    profile_observed_at = clock()
    response = lookup_channel_response(params)
    # A quota, rate-limit or access error is not "not found" and is not remembered.
    if response_http_status(response) != 200:
        return lookup_refusal(response_refusal_code(response))
    items = response.json().get("items")
    lookup_cache.put(*locator, items[0]["id"] if items else None)
    if not items:
        return {"error": "Channel not found"}
    channel = items[0]
    with channel_lock(channel["id"]):
        existing = repository.load_channel_record(channel["id"])
        if existing and not prepare_only:
            if user_input is not None:
                repository.preserve_user_input(channel["id"], user_input)
            if existing.get("is_hidden"):
                if deployment_profile == "self-hosted":
                    result = trash_repository.restore_channel(existing["channel_id"], existing.get("hidden_at"))
                    if result.get("outcome") != "restored":
                        return {"error": "This deleted channel is awaiting permanent cleanup or changed. Refresh before adding again.", "code": "channel_trash_conflict"}
                else:
                    raise RuntimeError("unsupported_deployment_profile")
                on_change(channel["id"])
                restored_channel = repository.load_channel_record(existing["channel_id"])
                if not restored_channel:
                    raise RuntimeError("channel_restore_state_missing")
                return {
                    "status": "restored",
                    "title": restored_channel.get("title"),
                    "channel": restored_channel,
                    "hydration": prepare_hydration(restored_channel, ensure_auto=False),
                }
            return {
                "status": "exists",
                "title": existing.get("title"),
                "channel": existing,
                "hydration": prepare_hydration(existing, ensure_auto=False),
            }
        stats = channel["statistics"]
        snippet = channel["snippet"]
        try:
            initial_estimate = estimate_initial_inventory(channel["id"])
            initial_long_inventory_status = initial_channel_status(channel, initial_estimate)
        except Exception as error:
            print(source_log_message(
                "initial_counts_error",
                lambda: "Failed to get initial video counts "
                        f"channel_id={channel['id']} error_type={error.__class__.__name__}",
                error=error,
            ), flush=True)
            return {
                "error": "Unable to verify the channel video inventory. Try again.",
                "code": "initial_inventory_count_unavailable",
            }

        row = prepared_channel_row(
            channel, initial_estimate, initial_long_inventory_status,
            observed_at=profile_observed_at, metadata_update_values=metadata_update_values,
        )
        if prepare_only:
            if user_input is not None:
                row["user_input"] = user_input
            return {"status": "prepared", "channel": row}
        existing = repository.load_channel_record(channel["id"])
        if existing and not prepare_only:
            if user_input is not None:
                repository.preserve_user_input(channel["id"], user_input)
            if existing.get("is_hidden"):
                if deployment_profile == "self-hosted":
                    result = trash_repository.restore_channel(existing["channel_id"], existing.get("hidden_at"))
                    if result.get("outcome") != "restored":
                        return {"error": "This deleted channel is awaiting permanent cleanup or changed. Refresh before adding again.", "code": "channel_trash_conflict"}
                else:
                    raise RuntimeError("unsupported_deployment_profile")
                on_change(channel["id"])
                restored_channel = repository.load_channel_record(existing["channel_id"])
                if not restored_channel:
                    raise RuntimeError("channel_restore_state_missing")
                return {
                    "status": "restored",
                    "title": restored_channel.get("title") or row["title"],
                    "channel": restored_channel,
                    "hydration": prepare_hydration(restored_channel, ensure_auto=False),
                }
            return {
                "status": "exists",
                "title": existing.get("title") or row["title"],
                "channel": existing,
                "hydration": prepare_hydration(existing, ensure_auto=False),
            }
        else:
            if user_input is not None:
                row["user_input"] = user_input
            try:
                row = repository.insert_channel(row, permission=(
                    "accepted" if initial_long_inventory_status == "pending" else "manual_required"
                ))
            except Exception as error:
                if not user_work_limit_reached(error):
                    raise
                return {"error": "The workspace holds its limit of 10,000 channels, including the trash "
                                 "and channels waiting to be reconnected. Remove channels before adding more.",
                        "code": USER_WORK_LIMIT}
            on_change(channel["id"])
            return {
                "status": "added",
                "title": row["title"],
                "channel": row,
                "hydration": prepare_hydration(row, ensure_auto=True),
            }


def activate_restored_channel(channel_id, *, repository, prepare_hydration):
        try:
            channel = repository.load_channel_record(channel_id)
            if channel and channel.get("is_hidden") is False:
                prepare_hydration(channel, ensure_auto=True)
        except Exception as error:
            print(source_log_message(
                "initial_hydration_enqueue_error",
                lambda: "Restored channel initial hydration preparation failed",
                error=error,
            ), flush=True)


class TrashError(Exception):
    def __init__(self, code, status=422):
        self.code, self.status = code, status


def require_trash_outcome(result, expected):
    if not isinstance(result, dict):
        raise TrashError("trash_unavailable", 503)
    code = result.get("outcome")
    if code in ("not_found", "conflict", "expired"):
        raise TrashError(code, 404 if code == "not_found" else 409)
    if code != expected:
        raise TrashError("trash_unavailable", 503)
    return channel_id(result.get("channel_id"))


def remove_channel(channel_identity, *, repository):
    return require_trash_outcome(repository.remove(channel_identity), 'removed')


def restore_channel(saved_identity, removed_at, *, repository):
    return require_trash_outcome(repository.restore(saved_identity, removed_at), 'restored')
