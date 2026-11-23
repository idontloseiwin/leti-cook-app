"""Bounded provider response observations; no collection or database ownership."""
from collections import Counter
from collections.abc import Mapping
import re

from pipeline_trace import event
from youtube_source_fields import youtube_counter_integer


def provider_response_observed(requested_ids, payload, *, endpoint,
                               include_statistics=True, normalization_failed=False):
    """Observe bounded response identities before values are discarded; never decide availability.

    Only requested IDs and closed codes leave this helper. A malformed response
    cannot provide trusted omission evidence. Successful response manifests plus
    later write acknowledgements show reappearance without inventing restoration.
    """
    try:
        if endpoint not in ("videos", "videos:batchGetStats"):
            return
        requested = list(dict.fromkeys(requested_ids))
        if not 1 <= len(requested) <= 50 or any(
            not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_-]{11}", value) is None
            for value in requested
        ):
            return
        batch = endpoint == "videos:batchGetStats"
        # A shared request can mix channels and types. Only the later claim/write
        # knows each item's type; do not inherit the first item's batch label.
        facts = {"endpoint": endpoint, "quota_bucket": "video_batch_get_stats" if batch else "general",
                 "channel_id": None, "video_type": None}
        items = payload.get("items") if isinstance(payload, Mapping) else None
        if items is None and batch and isinstance(payload, Mapping):
            items = []  # The Batch contract permits an omitted empty items list.
        if not isinstance(items, list) or len(items) > 50:
            event("video_provider_manifest", "invalid", **facts, reason="items_invalid",
                  requested_count=len(requested), video_id=None, video_ids=None)
            return
        returned, malformed = {}, False
        counts = Counter()
        for item in items:
            identity = item.get("id") if isinstance(item, Mapping) else None
            if batch:
                identity = str(identity or "").strip()  # Same boundary as Batch normalization.
            if not isinstance(identity, str) or identity not in requested:
                malformed = True
                continue
            returned[identity] = item
            counts[identity] += 1
        malformed = malformed or any(count > 1 for count in counts.values())
        trusted = not (malformed or normalization_failed)
        # A manifest is a response observation, not a successful write or a new
        # source of truth. Preserve only identities that were actually requested.
        event("video_provider_manifest", "received" if trusted else "invalid", **facts,
              reason="returned_requested_ids" if trusted else "response_invalid",
              video_id=None,
              video_ids=[identity for identity in requested if identity in returned],
              requested_count=len(requested), received_count=len(returned), count=len(items),
              missing_count=len(requested) - len(returned))
        summary = payload.get("summary") if batch else None
        explicit_failed = summary.get("failedVideoIds", []) if isinstance(summary, Mapping) else []
        explicit_failed = {str(value or "").strip() for value in explicit_failed} if isinstance(explicit_failed, list) else set()
        for identity in requested:
            reason, field = None, "id"
            if counts[identity] > 1:
                reason = "duplicate_id"
            elif identity not in returned:
                reason = ("omission_untrusted" if not trusted else
                          "explicit_failed_id" if batch and identity in explicit_failed else "id_omitted")
            elif counts[identity] == 1 and include_statistics:
                statistics = returned[identity].get("statistics")
                field = "statistics.viewCount"
                if statistics is None:
                    reason = "view_count_missing"
                elif not isinstance(statistics, Mapping):
                    reason, field = "statistics_invalid", "statistics"
                elif statistics.get("viewCount") in (None, ""):
                    reason = "view_count_missing"
                elif youtube_counter_integer(statistics["viewCount"]) is None:
                    reason = "view_count_invalid"
            if reason:
                event("video_provider_item", "observed" if trusted else "invalid", **facts,
                      video_id=identity, video_ids=None, phase=field, reason=reason)
    except Exception:
        pass  # Diagnostics must not change parsing, quota, writes or retries.


