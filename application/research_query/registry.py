"""Minimal versioned definitions for the first research catalogue.

Definitions are not permissions or executable capabilities. Source admission,
workspace authorization and lifecycle checks belong to the server binding.
No SQL, credentials, environment reads or application startup imports belong here.
The preset ranges of fields (the presets section) are read from one checked-in file.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from . import growth

REGISTRY_VERSION = 11
SOURCE_ID = "public.videos.current.v1"
VIDEO_TYPES = ("long", "short", "stream")
UNICODE_VERSION = "15.0.0"  # Python 3.12 casefold; SQL lower is not equivalent.
UINT64_MAX = 2**64 - 1
POSTGRES_BIGINT_MAX = 2**63 - 1
OperatorId = Literal["eq", "in", "gt", "gte", "lt", "lte", "contains_casefold_v1", "has_any", "has_all", "is_known", "is_unknown", "state_is",
                     "within_last_hours_v1"]


@dataclass(frozen=True)
class OperatorDefinition:
    id: str
    argument: str
    value_types: tuple[str, ...]
    semantics: str
    use_class: str = "source_selection"
    version: int = 1


# local_date: a YYYY-MM-DD calendar date in the request's time zone (time.timezone).
_SCALAR_TYPES = ("video_id", "channel_id", "video_type", "text", "text_code", "utc_instant", "local_date", "uint_decimal", "signed_decimal")
_ALL_TYPES = _SCALAR_TYPES + ("boolean", "text_array")
OPERATORS = MappingProxyType({item.id: item for item in (
    OperatorDefinition("eq", "scalar", _SCALAR_TYPES + ("boolean",), "typed exact equality; unknown yields unknown"),
    OperatorDefinition("in", "nonempty_list_max_1000", _SCALAR_TYPES, "typed set membership; unknown yields unknown"),
    *(OperatorDefinition(key, "scalar", ("utc_instant", "local_date", "uint_decimal", "signed_decimal"),
                         "ordered comparison; unknown yields unknown") for key in ("gt", "gte", "lt", "lte")),
    # The Library title search: the literal is trimmed and each run
    # of Unicode whitespace becomes one space; the stored text is not collapsed.
    OperatorDefinition("contains_casefold_v1", "string", ("text",),
                       "Unicode 15.0 casefold substring; literal whitespace runs collapsed to one space and trimmed"),
    *(OperatorDefinition(key, "nonempty_list_max_1000", ("text_array",),
                         "exact case-sensitive element membership; null yields unknown; empty stored array yields false")
      for key in ("has_any", "has_all")),
    OperatorDefinition("is_known", "none", _ALL_TYPES, "true only for state known"),
    OperatorDefinition("is_unknown", "none", _ALL_TYPES, "true for state unknown, expired or unavailable"),
    OperatorDefinition("state_is", "state_enum", _ALL_TYPES, "known/unknown/expired/unavailable; never disabled"),
    # The publication window ends at evaluation time and measures whole hours.
    OperatorDefinition("within_last_hours_v1", "hours_uint_1_87600", ("utc_instant",),
                       "value >= evaluation time minus N whole hours; unknown yields unknown"),
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
    Dependency("scope", "core", "user_work_revision", ("channels.folder_id", "channels.is_hidden",
               "channel_label_assignments", "saved_collection_members", "channels.user_work_id"),
               "Trusted workspace, visible channel, exact folder/label membership or saved video IDs bound to their user-work owner."),
    Dependency("membership", "core", "membership_proof", ("video_inventory", "video_inventory_memberships"),
               "Fresh canonical type/channel proof, including valid manual or automatic resolution."),
    Dependency("availability", "core", "availability_checked_at", ("videos.availability_status", "videos.availability_checked_at"),
               "Fresh public availability; unknown is not public."),
    Dependency("publication", "core", "metadata_checked_at", ("videos.publication_status", "videos.published_at"),
               "No known future/upcoming/live publication; unknown publication date remains explicit."),
    Dependency("stream_completion", "core_for_stream", "metadata_and_counter", ("videos.stream_actual_end_at", "videos.metadata_checked_at", "videos.checked_at"),
               "For Streams, actual end must precede both real clocks and evaluation time."),
    Dependency("observations", "feature", "recorded_at", ("video_snapshots",),
               "Real protected observations; independent expiry and recorded metric definition."),
    Dependency("description", "optional", "description_checked_at", ("video_optional_metadata.description",),
               "Independent description observation; disabled fields still expire."),
    Dependency("paid_promotion", "optional", "paid_promotion_checked_at", ("video_optional_metadata.paid_promotion",),
               "YouTube declared boolean; missing is unknown, not false."),
    Dependency("metadata", "feature", "metadata_checked_at", ("videos.metadata_checked_at",),
               "Freshness of stored video metadata; counters cannot renew it."),
    Dependency("counters", "feature", "checked_at", ("videos.checked_at",),
               "Freshness of current counter; metadata cannot renew it."),
    Dependency("channel_profile", "feature", "channel_metadata_checked_at", ("channels.channel_metadata_checked_at", "channels.channel_metadata_status"),
               "Related channel profile and reported counters; video clocks never renew these values."),
    Dependency("channel_country", "feature", "country_collected_at", ("channels.country_collected_at",),
               "Independent country collection clock, not the profile or video clock."),
    Dependency("lifecycle", "core", "server_policy_revision", ("hot_storage", "readable views"),
               "Effective server lifecycle profile and deadlines; client cannot extend them."),
)})


@dataclass(frozen=True)
class SourceDefinition:
    id: str = SOURCE_ID
    version: int = 1
    grain: str = "video"
    # library: every visible channel of the owner (not hidden, not in the trash).
    scopes: tuple[str, ...] = ("channel", "folder", "label", "collection", "library")
    video_types: tuple[str, ...] = VIDEO_TYPES
    max_video_types: int = 1
    admission_dependencies: tuple[str, ...] = ("scope", "membership", "availability", "publication", "lifecycle")
    stream_dependencies: tuple[str, ...] = ("stream_completion",)
    folder_descendants: bool = False
    executable: bool = False


SOURCE = SourceDefinition()
CHANNEL_SOURCE_ID = 'public.channels.current.v1'
CHANNEL_SOURCE = SourceDefinition(id=CHANNEL_SOURCE_ID, grain='channel',
    scopes=('channel','folder','label','library'), video_types=(), max_video_types=0,
    admission_dependencies=('scope','lifecycle'), stream_dependencies=())
SOURCES = MappingProxyType({source.id:source for source in (SOURCE,CHANNEL_SOURCE)})


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
    max_text_length: int = 1000
    max_items: int | None = None
    max_total_bytes: int | None = None
    # A closed vocabulary: literals and returned values must be one of these.
    allowed_values: tuple[str, ...] | None = None


_STATE_OPS = ("is_known", "is_unknown", "state_is")
_EXACT = ("eq", "in")
_RANGE = _EXACT + ("gt", "gte", "lt", "lte") + _STATE_OPS
_CORE = ("scope", "membership", "availability", "publication", "lifecycle")


def _field(field_id, value_type, *, unit="none", origin="youtube_reported",
           paths=(), privacy="none_identified", clock="metadata", epoch="not_applicable",
           nullable=True, empty="not_applicable", role="feature", granularity="shared_part",
           modes=("current", "display"), dependencies=(), operators=_RANGE,
           evidence=(), normalization="identity", bounds=(), index="measure_before_adding",
           sortable=True, max_text_length=1000, max_items=None, max_total_bytes=None, use_class="public_current_source",
           allowed_values=None):
    return FieldDefinition(
        id=field_id, value_type=value_type, unit=unit, origin=origin, api_paths=paths,
        access="public", use_class=use_class, privacy=privacy,
        clock_family=clock, definition_epoch=epoch, nullable=nullable, empty_meaning=empty,
        storage_role=role, acquisition_granularity=granularity, supported_modes=modes,
        effective_default="off" if role=="optional" else "preserve_existing_collection",
        dependencies=_CORE + dependencies, operators=operators, sortable=sortable,
        mapping_key=field_id.replace(".", "_") + "_v1", storage_evidence=evidence,
        normalization=normalization, value_bounds=bounds, index_candidate=index,
        support=("api_available", "optional_acquisition", "optional_storage", "catalogue_exposed") if role=="optional" else ("api_available", "requested", "stored", "legacy_exposed"),
        max_text_length=max_text_length, max_items=max_items, max_total_bytes=max_total_bytes,
        allowed_values=allowed_values,
    )


FIELDS = MappingProxyType({item.id: item for item in (
    *(_field(field, kind, unit=unit, origin=origin, paths=("videos.statistics.viewCount",),
        clock="observations", epoch="recorded_snapshot_epoch", dependencies=("observations", "metadata"),
        evidence=("video_snapshots",), normalization=method, bounds=("signed exact decimal, six fractional places at most",),
        use_class="additional_analytics" if origin=="calculated" else "source_observation")
      for field,kind,unit,origin,method in (
        ("observation.views_delta","signed_decimal","views","calculated","video.observed_change.v1"),
        ("observation.views_per_hour","signed_decimal","views_per_hour","calculated","video.observed_change.v1"),
        ("observation.views_at_age","uint_decimal","views","youtube_reported","video.observed_age.v1"))),
    # 28-day growth of the Library (video.observed_growth_28d.v1): one
    # classification per admitted video and its signed comparison, if any.
    *(_field(field, kind, unit=unit, origin="calculated", paths=("videos.statistics.viewCount",),
        clock="observations", epoch="recorded_snapshot_epoch", dependencies=("observations", "metadata", "counters"),
        evidence=("video_snapshots", "videos.checked_at", "videos.metadata_checked_at", "video_inventory_memberships"),
        normalization=growth.OPERATION_ID, bounds=bounds, operators=operators, sortable=sortable,
        max_text_length=32 if values else 1000, allowed_values=values, use_class="additional_analytics")
      for field, kind, unit, operators, sortable, values, bounds in (
        (growth.STATUS, "text_code", "none", _EXACT + _STATE_OPS, False, growth.STATUSES,
         ("ready, partial, started_today, waiting_next_snapshot or unavailable",)),
        (growth.GAINED, "signed_decimal", "views", _RANGE, True, None, ("signed exact integer; ready and partial only",)),
        (growth.PER_DAY, "signed_decimal", "views_per_day", _RANGE, True, None,
         ("signed exact decimal, two fractional places at most; ready and partial only",)),
        (growth.INTERVAL, "uint_decimal", "seconds", _RANGE, True, None,
         ("whole seconds, at most 2505600; every status except unavailable",)),
        (growth.TRACKED_DAYS, "uint_decimal", "days", _RANGE, True, None, ("whole days 1..27; partial only",)))),
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
    _field("video.description", "text", paths=("videos.snippet.description",), role="optional",
           privacy="free_text_may_contain_personal_data", clock="description", empty="known_empty_string",
           modes=("off","current"), dependencies=("description",),
           operators=_EXACT + ("contains_casefold_v1",) + _STATE_OPS, max_text_length=5000,
           evidence=("video_optional_metadata.description",), normalization="preserve_source; casefold_v1",bounds=("5000 UTF-8 bytes",)),
    _field("video.paid_promotion", "boolean", paths=("videos.paidProductPlacementDetails.hasPaidProductPlacement",), role="optional",
           clock="paid_promotion", empty="false_is_known",modes=("off","current"),dependencies=("paid_promotion",),
           operators=("eq",)+_STATE_OPS,evidence=("video_optional_metadata.paid_promotion",),normalization="reported_boolean; absent_unknown"),
    _field("video.title", "text", paths=("videos.snippet.title",),
           privacy="free_text_may_contain_personal_data", empty="known_empty_string",
           dependencies=("metadata",), operators=_EXACT + ("contains_casefold_v1",) + _STATE_OPS,
           evidence=("videos.title",), normalization="preserve_source; casefold_v1_for_matching_and_order",
           bounds=("stored title <=1000 Unicode code points",), index="casefold_search_key_only_after_parity_measurement"),
    _field("video.published_at", "utc_instant", unit="instant", origin="normalized",
           paths=("videos.snippet.publishedAt",), dependencies=("metadata",), operators=_RANGE + ("within_last_hours_v1",),
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
    *(_field("video." + name, "uint_decimal", unit=name,
             paths=("videos.statistics." + path,), clock="counters", role="optional",
             dependencies=("counters",), modes=("current", "existing_history", "display"),
             evidence=("videos." + column, "videos.checked_at"),
             normalization="exact_nonnegative_integer_decimal_string",
             bounds=("wire/query:0.." + str(UINT64_MAX), "existing_storage:0.." + str(POSTGRES_BIGINT_MAX)))
      for name, column, path in (("likes", "like_count", "likeCount"), ("comments", "comment_count", "commentCount"))),
    *(_field("video." + name, "text_array", paths=(path,), role="optional", privacy=privacy,
             empty="known_empty_array", dependencies=("metadata",),
             operators=("has_any", "has_all") + _STATE_OPS, sortable=False,
             evidence=("videos." + column,), normalization="preserve_elements; exact_case_sensitive_membership",
             max_items=limit, max_text_length=25600, max_total_bytes=25600,
             bounds=(f"one-dimensional array; at most {limit} non-null strings", "25600 UTF-8 bytes total"))
      for name, column, path, limit, privacy in (
          ("tags", "tags", "videos.snippet.tags", 500, "free_text_may_contain_personal_data"),
          ("topic_urls", "youtube_topic_categories", "videos.topicDetails.topicCategories", 50, "none_identified"))),
    *(_field("video." + name, "text_code", paths=(path,), role="optional",
             empty="known_empty_string", dependencies=("metadata",), operators=_EXACT + _STATE_OPS,
             evidence=("videos." + column,), max_text_length=64,
             normalization="preserve_source_code; exact_case_sensitive_match_and_order", bounds=("0..64 Unicode code points",))
      for name, column, path in (
          ("category_id", "category_id", "videos.snippet.categoryId"),
          ("audio_language", "default_audio_language", "videos.snippet.defaultAudioLanguage"),
          ("metadata_language", "default_language", "videos.snippet.defaultLanguage"))),
    _field("video.caption_available", "boolean", origin="normalized", role="optional",
           paths=("videos.contentDetails.caption",), empty="false_is_known", dependencies=("metadata",),
           operators=("eq",) + _STATE_OPS, evidence=("videos.caption_available",),
           normalization="stored_boolean; unknown_is_not_false", bounds=("false", "true")),
    *(_field("video." + name, "utc_instant", unit="instant", origin="normalized",
             paths=(path,), dependencies=("metadata",), evidence=("videos." + column,),
             normalization="UTC_RFC3339_microseconds; no_scheduled_to_actual_inference")
      for name, column, path in (
          ("scheduled_start_at", "scheduled_start_at", "videos.liveStreamingDetails.scheduledStartTime"),
          ("actual_end_at", "stream_actual_end_at", "videos.liveStreamingDetails.actualEndTime"))),
    _field("video.thumbnail_url", "text_code", origin="normalized", clock="membership",
           paths=("videos.id",), operators=_STATE_OPS, sortable=False,
           evidence=("video_inventory.video_id",), normalization="youtube_mqdefault_reference_v1; not_fetched_or_cached"),
    # Calculated from the current stored row with the Library's definitions and
    # gated with the other Baseline calculations (operations: video.static_metrics.v1).
    _field("video.title_length", "uint_decimal", unit="unicode_code_points", origin="calculated",
           paths=("videos.snippet.title",), dependencies=("metadata",), evidence=("videos.title",),
           normalization="char_length_of_stored_title_v1; unknown_title_unknown; empty_title_zero",
           bounds=("0..1000",), use_class="descriptive_analytics"),
    _field("video.tag_count", "uint_decimal", unit="tags", origin="calculated",
           paths=("videos.snippet.tags",), dependencies=("metadata",), evidence=("videos.tags",),
           normalization="cardinality_of_stored_tags_v1; uncollected_tags_unknown; empty_array_zero",
           bounds=("0..500",), use_class="descriptive_analytics"),
    *(_field("video." + name, "signed_decimal", unit="percent_of_views", origin="calculated",
             paths=("videos.statistics." + path, "videos.statistics.viewCount"), clock="counters",
             dependencies=("counters",), operators=("gt", "gte", "lt", "lte") + _STATE_OPS, sortable=False,
             evidence=("videos." + column, "videos.views", "videos.checked_at"),
             normalization=counter + "*100/views_v1; exact_rational_comparison; value_half_even_six_places; zero_or_unknown_views_unavailable",
             bounds=("nonnegative exact rational; value at most six fractional places",), use_class="descriptive_analytics")
      for name, column, path, counter in (("like_rate", "like_count", "likeCount", "likes"),
                                          ("comment_rate", "comment_count", "commentCount", "comments"))),
    # Local calendar date and year of publication in the request's time zone.
    *(_field("video." + name, kind, unit=unit, origin="normalized", paths=("videos.snippet.publishedAt",),
             dependencies=("metadata",), sortable=False, evidence=("videos.published_at",),
             normalization=f"local_{unit}_of_published_at_in_time_zone_v1; outside_0001_9999_unavailable", bounds=(bound,))
      for name, kind, unit, bound in (("published_date", "local_date", "date", "0001-01-01..9999-12-31"),
                                      ("published_year", "uint_decimal", "year", "1..9999"))),
    # Home "Relative view pace": lifetime views per day with the age of
    # the Overview's views/day, publication to the counter observation, and the
    # Home feed's one-hour minimum age. Ungated, as the Home pace always was.
    _field("video.lifetime_views_per_day", "signed_decimal", unit="views_per_day", origin="calculated",
           paths=("videos.statistics.viewCount", "videos.snippet.publishedAt"), clock="counters",
           dependencies=("counters", "metadata"), operators=_STATE_OPS, sortable=False,
           evidence=("videos.views", "videos.checked_at", "videos.published_at"),
           normalization="views*86400/max(checked_at-published_at,3600s)_v1; half_even_six_places; unknown_views_or_publication_unknown",
           bounds=("nonnegative exact decimal, six fractional places at most",)),
    _field("channel.title", "text", paths=("channels.snippet.title",), clock="channel_profile",
           privacy="free_text_may_contain_personal_data", dependencies=("channel_profile",),
           empty="known_empty_string", operators=_EXACT + ("contains_casefold_v1",) + _STATE_OPS,
           evidence=("channels.title",), normalization="preserve_source; casefold_v1_for_matching_and_order"),
    _field("channel.avatar_url", "text_code", paths=("channels.snippet.thumbnails",), clock="channel_profile",
           dependencies=("channel_profile",), operators=_STATE_OPS, sortable=False, max_text_length=2048,
           evidence=("channels.avatar_url",), normalization="stored_https_reference; not_fetched_or_cached"),
    *(_field("channel." + name, "uint_decimal", unit=name, clock="channel_profile",
             paths=("channels.statistics." + path,), dependencies=("channel_profile",),
             evidence=("channels." + name,), normalization="reported_nonnegative_integer_decimal_string; no_unrounding",
             bounds=("wire/query:0.." + str(UINT64_MAX), "existing_storage:0.." + str(POSTGRES_BIGINT_MAX)))
      for name, path in (("subscribers", "subscriberCount"), ("views", "viewCount"), ("video_count", "videoCount"))),
    _field("channel.country_code", "text_code", paths=("channels.snippet.country",), clock="channel_country",
           dependencies=("channel_country",), operators=_EXACT + _STATE_OPS, max_text_length=64,
           evidence=("channels.country_code",), normalization="preserve_source_code"),
    _field("channel.created_at", "utc_instant", unit="instant", origin="normalized",
           paths=("channels.snippet.publishedAt",), clock="channel_profile", dependencies=("channel_profile",),
           evidence=("channels.channel_created_at",), normalization="UTC_RFC3339_microseconds"),
)})


_channel_identity = replace(_field('channel.id','channel_id',origin='operational',
    privacy='public_identity',clock='scope',nullable=False,role='core',granularity='identity',
    modes=('identity',),operators=_EXACT,evidence=('channels.channel_id',),
    normalization='tracked_channel_reference_v1',bounds=('UC plus 22 ASCII URL-safe characters',)),
    source_id=CHANNEL_SOURCE_ID,grain='channel',video_types=(),dependencies=('scope','lifecycle'))
FIELDS = MappingProxyType({**FIELDS,_channel_identity.id:_channel_identity})


def supports_source(field_id, source_id):
    return field_id in FIELDS and source_id in SOURCES and (
        field_id.startswith('channel.') if source_id==CHANNEL_SOURCE_ID else field_id!='channel.id')


def source_field(field_id, source_id):
    if not supports_source(field_id,source_id): raise KeyError(field_id)
    field=FIELDS[field_id]
    if source_id==SOURCE_ID:return field
    return replace(field,source_id=source_id,grain='channel',video_types=(),
        dependencies=tuple(dep for dep in field.dependencies if dep not in {'membership','availability','publication'}))


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


# ---- Presets: one registry of value ranges for filters, facets and summaries ----
# Every preset lives in the checked-in shared file
# application/shared/catalogue-filter-presets.json, which the familiar Library
# (the browser and the Saved v1 check in library_v1) also reads; this section is its Python owner
# and the client receives the same sets through /research/capabilities (presets).
# A bucket is the half-open range [min, max) of a field's whole-number values; an
# open end is None. The buckets of a set are contiguous, in ascending order, and
# cover every value from the first lower bound up, so each known value falls in
# exactly one bucket. A top-level group is the Long-form set and the set of every
# type that does not override it; "by_video_type" overrides a group for one type
# (Shorts and Streams have their own duration sets). A changed edge is a
# new version of the file. Reading that one data file is the only I/O here.
PRESETS_FILE = Path(__file__).resolve().parents[1] / "shared" / "catalogue-filter-presets.json"
# Group -> the registry field its buckets divide.
PRESET_GROUP_FIELDS = MappingProxyType({
    "views": "video.views", "duration": "video.duration_seconds", "likes": "video.likes",
    "comments": "video.comments", "title_length": "video.title_length", "tag_count": "video.tag_count",
})


@dataclass(frozen=True)
class PresetBucket:
    key: str
    label: str
    min: int | None
    max: int | None

    def as_dict(self):
        return {"key": self.key, "label": self.label, "min": self.min, "max": self.max}


def _checked_presets(items):
    """Unique keys; whole nonnegative bounds; only the first bucket opens below and
    only the last above; each bucket starts where the previous one ends."""
    if type(items) is not list or not items or any(type(item) is not dict or set(item) != {"key", "label", "min", "max"}
                                                   for item in items):
        raise RuntimeError("invalid_bucket_registry")
    sets = tuple(PresetBucket(item["key"], item["label"], item["min"], item["max"]) for item in items)
    keys = [bucket.key for bucket in sets]
    if len(set(keys)) != len(keys):
        raise RuntimeError("invalid_bucket_registry")
    for index, bucket in enumerate(sets):
        bounds = [value for value in (bucket.min, bucket.max) if value is not None]
        if (type(bucket.key) is not str or not bucket.key or type(bucket.label) is not str or not bucket.label
                or any(type(value) is not int or value < 0 for value in bounds)
                or (bucket.min is None and index > 0) or ((bucket.max is None) != (index == len(sets) - 1))
                or (bucket.min is not None and bucket.max is not None and bucket.min >= bucket.max)
                or (index > 0 and bucket.min != sets[index - 1].max)):
            raise RuntimeError("invalid_bucket_registry")
    return sets


def _load_presets():
    data = json.loads(PRESETS_FILE.read_text(encoding="utf-8"))
    overrides = data.get("by_video_type", {})
    if type(data.get("version")) is not int or not set(overrides) <= set(VIDEO_TYPES) or any(
            not set(groups) <= set(PRESET_GROUP_FIELDS) for groups in overrides.values()):
        raise RuntimeError("invalid_bucket_registry")
    sets = {group: MappingProxyType({kind: _checked_presets(overrides.get(kind, {}).get(group, data[group]))
                                     for kind in VIDEO_TYPES}) for group in PRESET_GROUP_FIELDS}
    return data["version"], MappingProxyType(sets)


PRESETS_VERSION, PRESET_SETS = _load_presets()


def preset_buckets(group, video_type):
    """The bucket set of a group for one video type; KeyError if unknown."""
    if video_type not in VIDEO_TYPES:
        raise KeyError(video_type)
    return PRESET_SETS[group][video_type]


def describe_presets(fields=None):
    """The client document (in /research/capabilities): every set per video type,
    for the groups whose field is in `fields` (all when None)."""
    return {"version": PRESETS_VERSION, "groups": {
        group: {"field": field, "buckets": {kind: [bucket.as_dict() for bucket in PRESET_SETS[group][kind]]
                                            for kind in VIDEO_TYPES}}
        for group, field in PRESET_GROUP_FIELDS.items() if fields is None or field in fields}}
