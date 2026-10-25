"""Minimal versioned definitions for the first research catalogue.

Definitions are not permissions or executable capabilities. Source admission,
workspace authorization and lifecycle checks belong to the server binding.
No SQL, credentials, environment reads or application startup imports belong here.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

REGISTRY_VERSION = 1
SOURCE_ID = "public.videos.current.v1"
VIDEO_TYPES = ("long", "short", "stream")
UNICODE_VERSION = "15.0.0"  # Python 3.12 casefold; SQL lower is not equivalent.
UINT64_MAX = 2**64 - 1
POSTGRES_BIGINT_MAX = 2**63 - 1
OperatorId = Literal["eq", "in", "gt", "gte", "lt", "lte", "contains_casefold_v1", "is_known", "is_unknown", "state_is"]


@dataclass(frozen=True)
class OperatorDefinition:
    id: str
    argument: str
    value_types: tuple[str, ...]
    semantics: str
    use_class: str = "source_selection"
    version: int = 1


_ALL_TYPES = ("video_id", "channel_id", "video_type", "text", "utc_instant", "uint_decimal")
OPERATORS = MappingProxyType({item.id: item for item in (
    OperatorDefinition("eq", "scalar", _ALL_TYPES, "typed exact equality; unknown yields unknown"),
    OperatorDefinition("in", "nonempty_list_max_1000", _ALL_TYPES, "typed set membership; unknown yields unknown"),
    *(OperatorDefinition(key, "scalar", ("utc_instant", "uint_decimal"),
                         "ordered comparison; unknown yields unknown") for key in ("gt", "gte", "lt", "lte")),
    OperatorDefinition("contains_casefold_v1", "string", ("text",), "Unicode 15.0 casefold literal substring"),
    OperatorDefinition("is_known", "none", _ALL_TYPES, "true only for state known"),
    OperatorDefinition("is_unknown", "none", _ALL_TYPES, "true for state unknown, expired or unavailable"),
    OperatorDefinition("state_is", "state_enum", _ALL_TYPES, "known/unknown/expired/unavailable; never disabled"),
)})


@dataclass(frozen=True)
class Dependency:
    id: str
    role: str
    clock: str
    storage_evidence: tuple[str, ...]
    description: str


# These are internal admission inputs, never selectable columns or client flags.
DEPENDENCIES = MappingProxyType({item.id: item for item in (
    Dependency("scope", "core", "user_work_revision", ("channels.folder_id", "channels.is_hidden"),
               "Trusted workspace, visible channel and exact direct folder membership."),
    Dependency("membership", "core", "membership_proof", ("video_inventory", "video_inventory_memberships"),
               "Fresh canonical type/channel proof, including valid manual or automatic resolution."),
    Dependency("availability", "core", "availability_checked_at", ("videos.availability_status", "videos.availability_checked_at"),
               "Fresh public availability; unknown is not public."),
    Dependency("publication", "core", "metadata_checked_at", ("videos.publication_status", "videos.published_at"),
               "No known future/upcoming/live publication; unknown publication date remains explicit."),
    Dependency("stream_completion", "core_for_stream", "metadata_and_counter", ("videos.stream_actual_end_at", "videos.metadata_checked_at", "videos.checked_at"),
               "For Streams, actual end must precede both real clocks and evaluation time."),
    Dependency("metadata", "feature", "metadata_checked_at", ("videos.metadata_checked_at",),
               "Freshness of title/date/duration only; counters cannot renew it."),
    Dependency("counters", "feature", "checked_at", ("videos.checked_at",),
               "Freshness of current counter; metadata cannot renew it."),
    Dependency("lifecycle", "core", "server_policy_revision", ("hot_storage", "readable views"),
               "Effective server lifecycle profile and deadlines; client cannot extend them."),
)})


@dataclass(frozen=True)
class SourceDefinition:
    id: str = SOURCE_ID
    version: int = 1
    grain: str = "video"
    scopes: tuple[str, ...] = ("channel", "folder")
    video_types: tuple[str, ...] = VIDEO_TYPES
    max_video_types: int = 1
    admission_dependencies: tuple[str, ...] = ("scope", "membership", "availability", "publication", "lifecycle")
    stream_dependencies: tuple[str, ...] = ("stream_completion",)
    folder_descendants: bool = False
    executable: bool = False


SOURCE = SourceDefinition()


@dataclass(frozen=True)
class FieldDefinition:
    id: str
    value_type: str
    unit: str
    origin: str
    api_paths: tuple[str, ...]
    access: str
    use_class: str
    privacy: str
    clock_family: str
    definition_epoch: str
    nullable: bool
    empty_meaning: str
    storage_role: str
    acquisition_granularity: str
    supported_modes: tuple[str, ...]
    effective_default: str
    dependencies: tuple[str, ...]
    operators: tuple[str, ...]
    sortable: bool
    mapping_key: str
    storage_evidence: tuple[str, ...]
    normalization: str
    value_bounds: tuple[str, ...]
    index_candidate: str
    support: tuple[str, ...]
    source_id: str = SOURCE_ID
    schema_version: int = 1
    grain: str = "video"
    video_types: tuple[str, ...] = VIDEO_TYPES
    mapping_owner: str = "db/reads/research_sql.py"
    policy_evidence: str = "youtube-public-current-v1: source/operation/storage/presentation review required"
    query_implementation: str = "implemented_preview"


_STATE_OPS = ("is_known", "is_unknown", "state_is")
_EXACT = ("eq", "in")
_RANGE = _EXACT + ("gt", "gte", "lt", "lte") + _STATE_OPS
_CORE = ("scope", "membership", "availability", "publication", "lifecycle")


def _field(field_id, value_type, *, unit="none", origin="youtube_reported",
           paths=(), privacy="none_identified", clock="metadata", epoch="not_applicable",
           nullable=True, empty="not_applicable", role="feature", granularity="shared_part",
           modes=("current", "display"), dependencies=(), operators=_RANGE,
           evidence=(), normalization="identity", bounds=(), index="measure_before_adding"):
    return FieldDefinition(
        id=field_id, value_type=value_type, unit=unit, origin=origin, api_paths=paths,
        access="public", use_class="public_current_source", privacy=privacy,
        clock_family=clock, definition_epoch=epoch, nullable=nullable, empty_meaning=empty,
        storage_role=role, acquisition_granularity=granularity, supported_modes=modes,
        effective_default="preserve_existing_collection; no new preference in stage_1",
        dependencies=_CORE + dependencies, operators=operators, sortable=True,
        mapping_key=field_id.replace(".", "_") + "_v1", storage_evidence=evidence,
        normalization=normalization, value_bounds=bounds, index_candidate=index,
        support=("api_available", "requested", "stored", "legacy_exposed"),
    )


FIELDS = MappingProxyType({item.id: item for item in (
    _field("video.id", "video_id", paths=("videos.id",), privacy="public_identity",
           clock="membership", nullable=False, role="core", granularity="identity",
           modes=("identity",), operators=_EXACT, evidence=("video_inventory.video_id", "videos.video_id"),
           bounds=("11 ASCII URL-safe characters",), index="existing_video_primary_key"),
    _field("video.channel_id", "channel_id", paths=("videos.snippet.channelId",), privacy="public_identity",
           clock="membership", nullable=False, role="core", granularity="identity",
           modes=("identity",), operators=_EXACT, evidence=("video_inventory.channel_id", "videos.channel_id"),
           bounds=("UC plus 22 ASCII URL-safe characters",), index="existing_channel_scope"),
    _field("video.type", "video_type", origin="normalized",
           paths=("playlistItems.snippet.resourceId.videoId",), clock="membership",
           epoch="type_resolution_policy_version", nullable=False, role="core",
           granularity="membership", modes=("current",), operators=_EXACT,
           evidence=("video_inventory_type_resolution",), normalization="canonical_membership_resolution",
           bounds=VIDEO_TYPES),
    _field("video.title", "text", paths=("videos.snippet.title",),
           privacy="free_text_may_contain_personal_data", empty="known_empty_string",
           dependencies=("metadata",), operators=_EXACT + ("contains_casefold_v1",) + _STATE_OPS,
           evidence=("videos.title",), normalization="preserve_source; casefold_v1_for_matching_and_order",
           bounds=("stored title <=1000 Unicode code points",), index="casefold_search_key_only_after_parity_measurement"),
    _field("video.published_at", "utc_instant", unit="instant", origin="normalized",
           paths=("videos.snippet.publishedAt",), dependencies=("metadata",),
           evidence=("videos.published_at",), normalization="UTC_RFC3339_microseconds; no_inventory_fallback_v1",
           bounds=("0001-01-01T00:00:00Z..9999-12-31T23:59:59.999999Z",),
           index="scope_type_published_video_id_candidate"),
    _field("video.duration_seconds", "uint_decimal", unit="seconds", origin="normalized",
           paths=("videos.contentDetails.duration",), dependencies=("metadata",),
           evidence=("videos.duration",), normalization="youtube_duration_seconds_v1_integer; fractional_unknown",
           bounds=("0", str(UINT64_MAX)), index="normalized_duration_candidate; no_parser_per_python_row"),
    _field("video.views", "uint_decimal", unit="views", paths=("videos.statistics.viewCount",),
           clock="counters", epoch="source_observation_definition; legacy_unknown_not_inferred",
           dependencies=("counters",), modes=("current", "existing_history", "display"),
           evidence=("videos.views", "videos.checked_at"), normalization="exact_nonnegative_integer_decimal_string",
           bounds=("wire/query:0.." + str(UINT64_MAX), "existing_storage:0.." + str(POSTGRES_BIGINT_MAX)),
           index="scope_type_views_video_id_candidate"),
)})


def describe_definitions() -> dict:
    """Safe design projection, deliberately NOT a runtime capabilities endpoint."""
    return {
        "registry_version": REGISTRY_VERSION, "source_id": SOURCE_ID,
        "executable": False, "max_video_types": 1,
        "fields": [{"id": field.id, "value_type": field.value_type,
                    "unit": field.unit, "nullable": field.nullable,
                    "operators": list(field.operators), "sortable": field.sortable}
                   for field in FIELDS.values()],
    }
