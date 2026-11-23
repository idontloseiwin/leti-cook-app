"""Strict wire shapes for review, not an executable query validator.

The validator must additionally resolve registry fields/operators, enforce
budgets before recursive parsing, authorize source/scope, and bind server clocks.
These models perform no reads, SQL, permission checks or endpoint registration.
"""
from __future__ import annotations

from datetime import date, datetime
import re
from typing import Annotated, Literal, Union
from zoneinfo import ZoneInfo

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator, model_validator, model_serializer

from . import growth
from .registry import FIELDS, OperatorId, UINT64_MAX, CHANNEL_SOURCE_ID, supports_source

MAX_BODY_BYTES = 64 * 1024
MAX_DEPTH = 8
MAX_LEAVES = 128
MAX_LIST_VALUES = 1000
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_CURSOR_CHARS = 16 * 1024  # Includes a full 1000-code-point Unicode sort key.
# A frozen result holds every matching row; larger results are refused, never
# truncated. 20,000 videos of one channel fit with room to spare.
MAX_FROZEN_ROWS = 50_000
# Catalogue filter time zone: UTC or an IANA Area/Location.
TIME_ZONE_PATTERN = r"(?:UTC|[A-Za-z]+(?:/[A-Za-z0-9_+-]+){1,2})"


def time_zone(value):
    """An IANA zone name that loads on this server; never a fixed offset."""
    if type(value) is not str or len(value) > 64 or re.fullmatch(TIME_ZONE_PATTERN, value) is None:
        raise ValueError("unknown_time_zone")
    try:
        ZoneInfo(value)
    except Exception:  # Not found, a directory or an unreadable tzdata entry.
        raise ValueError("unknown_time_zone") from None
    return value


TimeZoneName = Annotated[StrictStr, AfterValidator(time_zone)]

VideoType = Literal["long", "short", "stream"]
FieldId = Annotated[StrictStr, Field(min_length=1, max_length=96, pattern=r"^[a-z][a-z0-9_.]*$")]
VideoId = Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9_-]{11}$")]
ChannelId = Annotated[StrictStr, Field(pattern=r"^UC[A-Za-z0-9_-]{22}$")]
FolderId = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")]
CollectionId = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")]
LabelId = Annotated[StrictStr, Field(pattern=r"^(?:monetized|custom:[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})$")]
Instant = Annotated[StrictStr, Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")]
LiteralValue = Union[StrictStr, StrictBool, list[StrictStr]]


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Source(WireModel):
    id: Literal["public.videos.current.v1", "public.channels.current.v1"]


class ChannelScope(WireModel):
    kind: Literal["channel"]
    id: ChannelId


class FolderScope(WireModel):
    kind: Literal["folder"]
    id: FolderId
    include_descendants: Literal[False] = False  # Folders are currently flat.


class LabelScope(WireModel):
    kind: Literal["label"]
    id: LabelId


class CollectionScope(WireModel):
    kind: Literal["collection"]
    id: CollectionId


class LibraryScope(WireModel):
    """Every visible channel of the owner's library: not hidden, not in the
    channel trash (both are channel_collection_state.is_hidden). No id."""
    kind: Literal["library"]

    @property
    def id(self):
        return None


Scope = Annotated[Union[ChannelScope, FolderScope, LabelScope, CollectionScope, LibraryScope], Field(discriminator="kind")]


class Predicate(WireModel):
    field: FieldId
    op: OperatorId
    value: LiteralValue | None = None

    @model_validator(mode="after")
    def value_shape(self):
        if self.op in {"is_known", "is_unknown"}:
            if "value" in self.model_fields_set:
                raise ValueError("unary_predicate_has_no_value")
        elif self.value is None:
            raise ValueError("predicate_value_required")
        return self


class All(WireModel):
    all: Annotated[list["Expression"], Field(min_length=1, max_length=MAX_LEAVES)]


class AnyOf(WireModel):
    any: Annotated[list["Expression"], Field(min_length=1, max_length=MAX_LEAVES)]


class Not(WireModel):
    operand: "Expression" = Field(alias="not")


Expression = Union[Predicate, All, AnyOf, Not]
for _node in (All, AnyOf, Not):
    _node.model_rebuild()


class Sort(WireModel):
    field: FieldId
    direction: Literal["asc", "desc"]
    nulls: Literal["last"] = "last"


class Time(WireModel):
    mode: Literal["current"] = "current"
    # The viewer's zone for local dates, years and "today". It is part of
    # the query fingerprint and echoed as context.time_zone. Predicates on UTC
    # instants do not depend on it; local-date operators read it from here.
    timezone: TimeZoneName = "UTC"


class CoverageRequest(WireModel):
    mode: Literal["available", "require_complete"] = "available"
    required_fields: Annotated[list[FieldId], Field(max_length=50)] = Field(default_factory=list)


class Projection(WireModel):
    missing_capability: Literal["error"] = "error"


class PageRequest(WireModel):
    size: Annotated[StrictInt, Field(ge=1, le=200)] = 50
    # A current continuation, or a frozen next/previous cursor.
    after: Annotated[StrictStr, Field(min_length=1, max_length=MAX_CURSOR_CHARS)] | None = None
    # Frozen results only, without `after`: the first page returned is the one
    # that contains this row. Not part of the query fingerprint.
    anchor_id: Annotated[StrictStr, Field(pattern=r"^(?:[A-Za-z0-9_-]{11}|UC[A-Za-z0-9_-]{22})$")] | None = None

    @model_serializer(mode="wrap")
    def omit_absent_anchor(self, handler):
        result = handler(self)
        if self.anchor_id is None:
            result.pop("anchor_id", None)
        return result


class ObservationOptions(WireModel):
    # Versioned, bounded common parameters; a single policy for the whole scope.
    version: Literal[1] = 1
    window_seconds: Literal[3600, 86400] = 3600
    tolerance_seconds: Annotated[StrictInt, Field(ge=0, le=7200)] = 1800
    age_seconds: Literal[86400] = 86400
    age_tolerance_seconds: Annotated[StrictInt, Field(ge=0, le=7200)] = 7200

    @model_validator(mode='after')
    def bounded_window(self):
        if self.tolerance_seconds > self.window_seconds:
            raise ValueError('tolerance_exceeds_window')
        return self


class QueryRequest(WireModel):
    query_version: StrictInt
    source: Source
    scope: Scope
    video_types: Annotated[list[VideoType], Field(min_length=1, max_length=1)] | None = None
    select: Annotated[list[FieldId], Field(min_length=1, max_length=50)]
    where: Expression | None = None
    order_by: Annotated[list[Sort], Field(min_length=1, max_length=3)]
    time: Time = Field(default_factory=Time)
    observations: ObservationOptions = Field(default_factory=ObservationOptions)
    coverage: CoverageRequest = Field(default_factory=CoverageRequest)
    projection: Projection = Field(default_factory=Projection)
    # current: every page is a new read of current data (default).
    # frozen: the complete matching population is read once and kept in this
    # process for a bounded time; pages, totals and anchors come from it.
    consistency: Literal["current", "frozen"] = "current"
    page: PageRequest = Field(default_factory=PageRequest)

    @model_validator(mode='after')
    def source_shape(self):
        if self.source.id==CHANNEL_SOURCE_ID:
            if 'video_types' in self.model_fields_set or 'observations' in self.model_fields_set or self.scope.kind=='collection':
                raise ValueError('channel_source_has_no_video_options')
        elif self.video_types is None:
            raise ValueError('video_type_required')
        return self

    @model_validator(mode='after')
    def frozen_anchor(self):
        anchor = self.page.anchor_id
        if anchor is not None:
            if self.consistency != 'frozen' or self.page.after is not None:
                raise ValueError('anchor_requires_new_frozen_result')
            pattern = r'UC[A-Za-z0-9_-]{22}' if self.source.id == CHANNEL_SOURCE_ID else r'[A-Za-z0-9_-]{11}'
            if re.fullmatch(pattern, anchor) is None:
                raise ValueError('anchor_source_identity_mismatch')
        return self

    @model_serializer(mode='wrap')
    def serialize_source(self,handler):
        result=handler(self)
        if self.source.id==CHANNEL_SOURCE_ID:
            result.pop('video_types',None);result.pop('observations',None)
        return result

    @field_validator("query_version")
    @classmethod
    def version_supported(cls, value):
        if value != 1:
            raise ValueError("unsupported_version")
        return value


class ObservationEvidence(WireModel):
    method: Literal['video.observed_change.v1', 'video.observed_age.v1']
    version: Literal[1] = 1
    from_at: Instant | None = None
    to_at: Instant | None = None
    actual_seconds: StrictStr | None = None
    definition_epoch: StrictStr | None = None
    reason: Literal['known','missing_endpoint','missing_boundary','unknown_definition','incompatible_definition','missing_age_observation']

    @model_validator(mode='after')
    def measured_interval(self):
        from decimal import Decimal
        if self.actual_seconds is not None and not re.fullmatch(r'[0-9]+(?:\.[0-9]{1,6})?',self.actual_seconds):
            raise ValueError('invalid_measured_interval')
        if self.reason == 'known':
            if self.from_at is None or self.to_at is None or self.actual_seconds is None:
                raise ValueError('missing_observation_evidence')
            elapsed=datetime.fromisoformat(self.to_at)-datetime.fromisoformat(self.from_at)
            expected=Decimal(elapsed.days*86400+elapsed.seconds)+Decimal(elapsed.microseconds)/1000000
            if expected <= 0 or Decimal(self.actual_seconds)!=expected:
                raise ValueError('observation_interval_mismatch')
            if self.method=='video.observed_change.v1' and not self.definition_epoch:
                raise ValueError('missing_metric_definition')
        return self


UIntText = Annotated[StrictStr, Field(pattern=r"^(?:0|[1-9][0-9]{0,19})$")]
Epoch = Annotated[StrictStr, Field(pattern=r"^[a-zA-Z0-9_.:-]{1,96}$")]


class GrowthEvidence(WireModel):
    """One row of video.observed_growth_28d.v1, shared by all its cells.

    from_* is the start snapshot (also kept when epochs differ); to_* is the
    current counter observation, from observed_end_at/observed_current_views.
    """
    method: Literal['video.observed_growth_28d.v1']
    version: Literal[1] = 1
    status: Literal['ready', 'partial', 'started_today', 'waiting_next_snapshot', 'unavailable']
    reason: Annotated[StrictStr, Field(max_length=64)] | None
    from_at: Instant | None
    to_at: Instant | None
    from_views: UIntText | None
    to_views: UIntText | None
    actual_interval_seconds: UIntText | None
    boundary_distance_seconds: Annotated[StrictStr, Field(pattern=r"^-?(?:0|[1-9][0-9]{0,6})$")] | None
    tracked_days: UIntText | None
    definition_epoch: Epoch | None
    from_definition_epoch: Epoch | None
    to_definition_epoch: Epoch | None
    time_zone: Annotated[StrictStr, Field(min_length=1, max_length=64)]

    @model_validator(mode='after')
    def legacy_integrity(self):
        problem = growth.evidence_problem(self.model_dump())
        if problem:
            raise ValueError(problem)
        return self


class Cell(WireModel):
    value: StrictStr | StrictBool | Annotated[list[StrictStr], Field(max_length=500)] | None
    state: Literal["known", "unknown", "expired", "unavailable"]
    provenance_ref: Annotated[StrictStr, Field(min_length=1, max_length=96)]
    observed_at: Instant | None
    expires_at: Instant | None
    observation: Annotated[Union[ObservationEvidence, GrowthEvidence], Field(discriminator='method')] | None = None

    @model_validator(mode="after")
    def no_hidden_payload(self):
        if (self.state == "known") != (self.value is not None):
            raise ValueError("cell_state_value_mismatch")
        for value in (self.observed_at, self.expires_at):
            if value is not None:
                datetime.fromisoformat(value.replace("Z", "+00:00"))
        if self.state == "known" and (self.observed_at is None or self.expires_at is None):
            raise ValueError("known_cell_requires_source_clocks")
        if self.observed_at and self.expires_at:
            if datetime.fromisoformat(self.observed_at) >= datetime.fromisoformat(self.expires_at):
                raise ValueError("invalid_source_interval")
        return self


class TypeRule(WireModel):
    id: Literal['long_short_overlap_duration']
    version: Annotated[StrictInt, Field(ge=1)]
    max_short_seconds: Annotated[StrictInt, Field(ge=1, le=86400)]


class Provenance(WireModel):
    origin: Literal["youtube_reported", "normalized", "operational", "calculated"]
    source_paths: Annotated[list[StrictStr], Field(max_length=8)]
    method: Annotated[StrictStr, Field(min_length=1, max_length=96)]
    definition_epoch: Annotated[StrictStr, Field(max_length=96)] | None
    type_rule: TypeRule | None = None

    @model_validator(mode='after')
    def applied_rule_only(self):
        if (self.method == 'automatic_long_short_v1') != (self.type_rule is not None):
            raise ValueError('invalid_type_rule_provenance')
        return self


class Row(WireModel):
    id: Union[VideoId, ChannelId]
    cells: dict[FieldId, Cell]


class Coverage(WireModel):
    status: Literal["complete", "partial", "unknown"]
    # Chosen by the server from the query scope. Channel counts retain their
    # inventory meaning even when completeness is assessed per saved member.
    basis: Literal["channel_inventory", "collection_members", "channel_profiles"] = "channel_inventory"
    # Counts refer to authorized scope, not global hidden/private objects.
    channels_in_scope: Annotated[StrictInt, Field(ge=0)]
    channels_complete: Annotated[StrictInt, Field(ge=0)]
    eligible_count: Annotated[StrictInt, Field(ge=0)] | None = None
    reasons: dict[StrictStr, Annotated[StrictInt, Field(ge=0)]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def scope_counts(self):
        if self.channels_complete > self.channels_in_scope:
            raise ValueError("invalid_coverage_counts")
        if self.status == "complete" and self.basis in {"channel_inventory", "channel_profiles"} and self.channels_complete != self.channels_in_scope:
            raise ValueError("incomplete_channel_scope")
        if self.status == "complete" and any(self.reasons.values()):
            raise ValueError("complete_scope_has_limitations")
        return self


class DefinitionReference(WireModel):
    id: Annotated[StrictStr, Field(min_length=1, max_length=96)]
    version: Annotated[StrictInt, Field(ge=1)]
    revision: Annotated[StrictStr, Field(min_length=1, max_length=96)]


class Context(WireModel):
    source_id: Literal["public.videos.current.v1", "public.channels.current.v1"]
    source_schema_version: Annotated[StrictInt, Field(ge=1)]
    registry_version: Annotated[StrictInt, Field(ge=1)]
    lifecycle_profile: DefinitionReference
    preferences_revision: Annotated[StrictStr, Field(min_length=1, max_length=96)]
    type_policy: DefinitionReference
    evaluated_at: Instant
    valid_before: Instant
    consistency: Literal["current", "frozen"]
    # Oldest video age admitted because the query uses an observed change window;
    # 0: no video is refreshed that often. Null when no change window narrowed it.
    observation_age_limit_seconds: Annotated[StrictInt, Field(ge=0)] | None = None
    # The request's time zone (time.timezone), UTC unless the viewer named one.
    time_zone: TimeZoneName = "UTC"
    # Frozen results only: an opaque reference to the whole frozen result, for
    # the matching-ID export and dependent reads. Never present for current.
    frozen_ref: Annotated[StrictStr, Field(min_length=1, max_length=MAX_CURSOR_CHARS)] | None = None

    @model_validator(mode="after")
    def lifetime(self):
        if datetime.fromisoformat(self.evaluated_at) >= datetime.fromisoformat(self.valid_before):
            raise ValueError("result_already_expired")
        if (self.consistency == "frozen") != (self.frozen_ref is not None):
            raise ValueError("frozen_reference_mismatch")
        return self

    @model_serializer(mode="wrap")
    def omit_current_reference(self, handler):
        result = handler(self)
        if self.frozen_ref is None:
            result.pop("frozen_ref", None)
        return result


class PageResult(WireModel):
    has_more: StrictBool
    next_cursor: Annotated[StrictStr, Field(min_length=1, max_length=MAX_CURSOR_CHARS)] | None
    # Frozen results only; omitted from current pages.
    previous_cursor: Annotated[StrictStr, Field(min_length=1, max_length=MAX_CURSOR_CHARS)] | None = None
    start_index: Annotated[StrictInt, Field(ge=0, le=MAX_FROZEN_ROWS)] | None = None
    total_count: Annotated[StrictInt, Field(ge=0, le=MAX_FROZEN_ROWS)] | None = None
    anchor_id: Annotated[StrictStr, Field(pattern=r"^(?:[A-Za-z0-9_-]{11}|UC[A-Za-z0-9_-]{22})$")] | None = None

    @model_validator(mode="after")
    def cursor_matches_continuation(self):
        if self.has_more != (self.next_cursor is not None):
            raise ValueError("cursor_continuation_mismatch")
        if self.start_index is None:
            if self.previous_cursor is not None or self.total_count is not None or self.anchor_id is not None:
                raise ValueError("frozen_page_fields_without_frozen_result")
        elif (self.total_count is None or self.start_index > self.total_count
              or (self.previous_cursor is None) != (self.start_index == 0)):
            raise ValueError("frozen_page_position_mismatch")
        return self

    @model_serializer(mode="wrap")
    def omit_frozen_position(self, handler):
        result = handler(self)
        if self.start_index is None:
            for key in ("previous_cursor", "start_index", "total_count", "anchor_id"):
                result.pop(key, None)
        return result


class QueryResult(WireModel):
    query_version: StrictInt
    query_fingerprint: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    context: Context
    columns: Annotated[list[FieldId], Field(min_length=1, max_length=50)]
    provenance: dict[StrictStr, Provenance]
    rows: Annotated[list[Row], Field(max_length=200)]
    coverage: Coverage
    page: PageResult
    warnings: Annotated[list[StrictStr], Field(max_length=32)] = Field(default_factory=list)

    @model_validator(mode="after")
    def row_shape(self):
        if self.query_version != 1:
            raise ValueError("unsupported_version")
        if len(set(self.columns)) != len(self.columns):
            raise ValueError("duplicate_result_column")
        if len({row.id for row in self.rows}) != len(self.rows):
            raise ValueError("duplicate_result_identity")
        if any(column not in FIELDS for column in self.columns):
            raise ValueError("unsupported_result_field")
        if any(not supports_source(column,self.context.source_id) for column in self.columns):
            raise ValueError('result_source_field_mismatch')
        frozen = self.context.consistency == "frozen"
        if frozen != (self.page.start_index is not None):
            raise ValueError("page_consistency_mismatch")
        if frozen:
            end = self.page.start_index + len(self.rows)
            if end > self.page.total_count or self.page.has_more != (end < self.page.total_count):
                raise ValueError("frozen_page_bounds_mismatch")
            if self.page.anchor_id is not None and self.page.anchor_id not in {row.id for row in self.rows}:
                raise ValueError("frozen_anchor_outside_page")
        for row in self.rows:
            pattern=r'UC[A-Za-z0-9_-]{22}' if self.context.source_id==CHANNEL_SOURCE_ID else r'[A-Za-z0-9_-]{11}'
            if re.fullmatch(pattern,row.id) is None:raise ValueError('result_source_identity_mismatch')
            if set(row.cells) != set(self.columns):
                raise ValueError("result_column_mismatch")
            if any(cell.provenance_ref not in self.provenance for cell in row.cells.values()):
                raise ValueError("missing_provenance_reference")
            selected_growth = {}
            for field, cell in row.cells.items():
                if isinstance(cell.observation, GrowthEvidence) != (field in growth.FIELDS and cell.observation is not None):
                    raise ValueError("growth_evidence_misplaced")
                if field in growth.FIELDS:
                    selected_growth[field] = (cell.state, cell.value, cell.observation and cell.observation.model_dump())
            if selected_growth and growth.cells_problem(selected_growth):
                raise ValueError("growth_cells_inconsistent")
            for field_id, cell in row.cells.items():
                if not FIELDS[field_id].nullable and cell.state != "known":
                    raise ValueError("missing_required_identity")
                if cell.state != "known":
                    continue
                definition = FIELDS[field_id]
                if definition.value_type == "boolean":
                    if type(cell.value) is not bool:
                        raise ValueError("invalid_boolean_value")
                elif definition.value_type == "text_array":
                    if (type(cell.value) is not list or len(cell.value) > definition.max_items
                            or any(type(v) is not str or "\x00" in v for v in cell.value)
                            or sum(len(v.encode("utf-8")) for v in cell.value) > definition.max_total_bytes):
                        raise ValueError("invalid_array_value")
                elif type(cell.value) is not str:
                    raise ValueError("unsupported_result_value_type")
                if type(cell.value) is str and (len(cell.value) > definition.max_text_length or "\x00" in cell.value):
                    raise ValueError("invalid_text_value")
                if definition.allowed_values is not None and cell.value not in definition.allowed_values:
                    raise ValueError("value_outside_vocabulary")
                if FIELDS[field_id].value_type == "uint_decimal":
                    uint_decimal(cell.value)
                elif FIELDS[field_id].value_type == "signed_decimal":
                    signed_decimal(cell.value)
                elif field_id in {"video.id", "channel.id"}:
                    if cell.value != row.id:
                        raise ValueError("result_identity_mismatch")
                elif field_id == "video.channel_id":
                    if re.fullmatch(r"UC[A-Za-z0-9_-]{22}", cell.value) is None:
                        raise ValueError("invalid_channel_identity")
                elif field_id == "video.type" and cell.value not in {"long", "short", "stream"}:
                    raise ValueError("invalid_video_type")
                elif field_id == "video.title" and len(cell.value) > 1000:
                    raise ValueError("title_exceeds_storage_bound")
                elif definition.value_type == "utc_instant":
                    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z", cell.value) is None:
                        raise ValueError("invalid_publication_instant")
                    datetime.fromisoformat(cell.value)
                elif definition.value_type == "local_date":
                    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", cell.value) is None:
                        raise ValueError("invalid_local_date")
                    date.fromisoformat(cell.value)
                if datetime.fromisoformat(cell.observed_at) > datetime.fromisoformat(self.context.evaluated_at):
                    raise ValueError("future_source_observation")
                if datetime.fromisoformat(cell.expires_at) < datetime.fromisoformat(self.context.valid_before):
                    raise ValueError("result_outlives_source")
        return self


class FrozenIdsContext(WireModel):
    source_id: Literal["public.videos.current.v1", "public.channels.current.v1"]
    evaluated_at: Instant
    valid_before: Instant
    consistency: Literal["frozen"]
    time_zone: TimeZoneName


class FrozenIdsResult(WireModel):
    """Every matching row ID of one frozen result, in its order (select all)."""
    version: Literal[1]
    query_fingerprint: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    context: FrozenIdsContext
    ids: Annotated[list[Union[VideoId, ChannelId]], Field(max_length=MAX_FROZEN_ROWS)]
    total_count: Annotated[StrictInt, Field(ge=0, le=MAX_FROZEN_ROWS)]

    @model_validator(mode="after")
    def complete_unique_identities(self):
        if len(self.ids) != self.total_count or len(set(self.ids)) != len(self.ids):
            raise ValueError("frozen_ids_incomplete")
        pattern = r'UC[A-Za-z0-9_-]{22}' if self.context.source_id == CHANNEL_SOURCE_ID else r'[A-Za-z0-9_-]{11}'
        if any(re.fullmatch(pattern, value) is None for value in self.ids):
            raise ValueError("result_source_identity_mismatch")
        if datetime.fromisoformat(self.context.evaluated_at) >= datetime.fromisoformat(self.context.valid_before):
            raise ValueError("result_already_expired")
        return self


ERROR_STATUS = {
    "invalid_query": 422, "unsupported_version": 422, "unsupported_source": 422,
    "unsupported_field": 422, "unsupported_operator": 422, "unsupported_video_types": 422,
    "field_disabled": 422, "dependency_disabled": 422, "policy_denied": 403,
    "grant_required": 403, "source_unavailable": 404, "incomplete_source": 409,
    "cursor_expired": 409, "cursor_context_changed": 409, "query_budget_exceeded": 422,
    "query_busy": 429, "backend_unavailable": 503, "catalogue_preparing": 503,
    # A video format the owner turned off in config.ini.
    "format_turned_off": 422,
}


# Which bound a query_budget_exceeded refusal reached: more than MAX_FROZEN_ROWS
# rows, the frozen session store's bytes or a server memory setting, the read's
# deadline, a request too large or too complex to read (body, nesting,
# conditions, values, a canvas definition), or an answer too large to send.
# An added key of the same refusal; older clients ignore it. A read its caller
# stopped (the client left) names none: nobody reads that answer.
BUDGET_LIMITS = ("rows", "memory", "time", "request", "response")


class QueryError(WireModel):
    code: StrictStr
    path: Annotated[StrictStr, Field(max_length=256)] | None = None
    limit: Literal["rows", "memory", "time", "request", "response"] | None = None

    @field_validator("code")
    @classmethod
    def known_code(cls, value):
        if value not in ERROR_STATUS:
            raise ValueError("unknown_error_code")
        return value


def uint_decimal(value: str) -> int:
    """Exact wire numeric interpretation; no floating point or coercion."""
    if type(value) is not str or not value.isascii() or not value.isdecimal():
        raise ValueError("invalid_unsigned_decimal")
    if len(value) > 20 or (len(value) > 1 and value.startswith("0")):
        raise ValueError("noncanonical_unsigned_decimal")
    number = int(value)
    if number > UINT64_MAX:
        raise ValueError("unsigned_decimal_out_of_range")
    return number


def signed_decimal(value):
    from decimal import Decimal
    if type(value) is not str or not re.fullmatch(r'-?(?:0|[1-9][0-9]{0,19})(?:\.[0-9]{1,6})?', value):
        raise ValueError('invalid_signed_decimal')
    if abs(Decimal(value)) > UINT64_MAX:
        raise ValueError('signed_decimal_out_of_range')
    return Decimal(value)
