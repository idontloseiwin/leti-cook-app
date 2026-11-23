"""Reuse one complete, already observed General response through the VIDEO owner.

This adapter never infers a type, writes a profile or calls a provider. Claims
and durable event bindings must already exist before the response is consumed.
Incomplete profiles return None, asking the caller to use the ordinary fetch;
contradictory identities and clocks are rejected, never repaired or restamped.
"""
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from collection.initial_video_hydration import InitialResponse

REQUIRED_METADATA_PARTS = frozenset({
    'snippet', 'contentDetails', 'status', 'topicDetails',
    'liveStreamingDetails', 'statistics',
})


@dataclass(frozen=True)
class DiscoveryMetadataResponse(InitialResponse):
    source_request_id: str | None = None
    metadata_received_at: datetime | None = None
    requested_ids: tuple = ()
    requested_parts: frozenset = frozenset()


def reusable_metadata_response(service, claims, *, requested_ids, items,
        observed_at, received_at, source_request_id, requested_parts):
    """Return a claim-subset response with original clocks, or None if incomplete."""
    if (type(source_request_id) is not str or str(UUID(source_request_id)) != source_request_id
            or not isinstance(requested_ids, (list, tuple)) or not 1 <= len(requested_ids) <= 50
            or any(type(value) is not str or not value or value != value.strip() for value in requested_ids)
            or len(set(requested_ids)) != len(requested_ids)
            or not isinstance(items, (list, tuple)) or len(items) > 50):
        raise ValueError('discovery_prefetch_invalid')
    for clock in (observed_at, received_at):
        if not isinstance(clock, datetime) or clock.tzinfo is None or clock.utcoffset() is None:
            raise ValueError('discovery_prefetch_clock_invalid')
    if observed_at > received_at or received_at > datetime.now(timezone.utc):
        raise ValueError('discovery_prefetch_clock_invalid')
    if (not isinstance(requested_parts, (set, frozenset, list, tuple))
            or any(type(part) is not str for part in requested_parts)):
        raise ValueError('discovery_prefetch_parts_invalid')
    rows = tuple(claims)
    ids = [claim.video_id for claim in rows]
    if (not 1 <= len(rows) <= 50 or len(set(ids)) != len(ids)
            or not set(ids) <= set(requested_ids)):
        raise ValueError('discovery_prefetch_claims_invalid')
    by_id = {}
    for item in items:
        if (not isinstance(item, Mapping) or type(item.get('id')) is not str
                or item['id'] not in requested_ids or item['id'] in by_id):
            raise ValueError('discovery_prefetch_identity_invalid')
        by_id[item['id']] = item
    if not REQUIRED_METADATA_PARTS <= set(requested_parts):
        return None
    for claim in rows:
        if claim.collection_mode == 'counters':
            return None  # A cached profile never changes an existing lease's operation.
        item = by_id.get(claim.video_id)
        if item is None:
            continue  # A complete response may omit IDs; canonical omission policy owns this.
        snippet = item.get('snippet')
        if not isinstance(snippet, Mapping):
            return None
        if snippet.get('channelId') != claim.channel_id:
            raise ValueError('discovery_prefetch_channel_mismatch')
        try:
            _, lifecycle = service._validate_common_metadata(claim, item, observed_at)
        except (ValueError, TypeError, KeyError, OverflowError):
            return None
        # Missing counters require the ordinary General/Stats continuation.
        # They must not become a false successful full hydration from a cache.
        stats = item.get('statistics')
        if not lifecycle and (not isinstance(stats, Mapping) or stats.get('viewCount') in (None, '')):
            return None
    return DiscoveryMetadataResponse(
        items=tuple(deepcopy(by_id[identity]) for identity in ids if identity in by_id),
        metadata_observed_at=observed_at, metadata_received_at=received_at,
        source_request_id=source_request_id, requested_ids=tuple(requested_ids),
        requested_parts=frozenset(requested_parts), dispatched=True,
    )
