"""videos.list and videos.batchGetStats hooks.

videos.list needs no hook: the registry describes it, ``statistics`` is refused by
the planner and an omitted requested ID is OMITTED, never deletion proof.
videos.batchGetStats accounting (unique, requested, summary counts, failed IDs)
is owned by ``youtube_batch_stats.normalize_batch_stats_response``; this hook
calls it and turns its fixed refusal code into a rejected page.
"""
from ..errors import ResponseInvalid
from .base import Hook


class VideosList(Hook):
    pass


class VideoBatchStats(Hook):
    def batch_accounting(self, payload, targets):
        # Lazy: the module also holds the collector's fetch function.
        from youtube_batch_stats import YouTubeBatchStatsError, normalize_batch_stats_response
        try:
            normalized = normalize_batch_stats_response(payload, list(targets))
        except YouTubeBatchStatsError as error:
            raise ResponseInvalid("batch_" + error.code) from None
        summary = normalized["summary"]
        return frozenset(summary["failed_ids"]), summary["source"] == "api"
