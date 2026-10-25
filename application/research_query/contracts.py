"""Strict wire shapes for review, not an executable query validator.

The validator must additionally resolve registry fields/operators, enforce
budgets before recursive parsing, authorize source/scope, and bind server clocks.
These models perform no reads, SQL, permission checks or endpoint registration.
"""
from __future__ import annotations

from datetime import datetime
import re
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator, model_validator

from .registry import FIELDS, OperatorId, UINT64_MAX

MAX_BODY_BYTES = 64 * 1024
MAX_DEPTH = 8
MAX_LEAVES = 128
MAX_LIST_VALUES = 1000
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_CURSOR_CHARS = 16 * 1024  # Includes a full 1000-code-point Unicode sort key.

VideoType = Literal["long", "short", "stream"]
FieldId = Annotated[StrictStr, Field(min_length=1, max_length=96, pattern=r"^[a-z][a-z0-9_.]*$")]
VideoId = Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9_-]{11}$")]
ChannelId = Annotated[StrictStr, Field(pattern=r"^UC[A-Za-z0-9_-]{22}$")]
FolderId = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")]
Instant = Annotated[StrictStr, Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")]
LiteralValue = Union[StrictStr, StrictBool, list[StrictStr]]


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Source(WireModel):
    id: Literal["public.videos.current.v1"]


class ChannelScope(WireModel):
    kind: Literal["channel"]
    id: ChannelId


class FolderScope(WireModel):
    kind: Literal["folder"]
    id: FolderId
    include_descendants: Literal[False] = False  # Folders are currently flat.


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
    timezone: Literal["UTC"] = "UTC"  # IANA local-date conversion is a later operator.


class CoverageRequest(WireModel):
    mode: Literal["available", "require_complete"] = "available"
    required_fields: Annotated[list[FieldId], Field(max_length=50)] = Field(default_factory=list)


class Projection(WireModel):
    missing_capability: Literal["error"] = "error"


class PageRequest(WireModel):
    size: Annotated[StrictInt, Field(ge=1, le=200)] = 50
    after: Annotated[StrictStr, Field(min_length=1, max_length=MAX_CURSOR_CHARS)] | None = None


class QueryRequest(WireModel):
    query_version: StrictInt
    source: Source
    scope: Annotated[Union[ChannelScope, FolderScope], Field(discriminator="kind")]
    video_types: Annotated[list[VideoType], Field(min_length=1, max_length=1)]
    select: Annotated[list[FieldId], Field(min_length=1, max_length=50)]
    where: Expression | None = None
    order_by: Annotated[list[Sort], Field(min_length=1, max_length=3)]
    time: Time = Field(default_factory=Time)
    coverage: CoverageRequest = Field(default_factory=CoverageRequest)
    projection: Projection = Field(default_factory=Projection)
    consistency: Literal["current"] = "current"
    page: PageRequest = Field(default_factory=PageRequest)

    @field_validator("query_version")
    @classmethod
    def version_supported(cls, value):
        if value != 1:
            raise ValueError("unsupported_version")
        return value


class Cell(WireModel):
    value: StrictStr | StrictBool | None
    state: Literal["known", "unknown", "expired", "unavailable"]
    provenance_ref: Annotated[StrictStr, Field(min_length=1, max_length=96)]
    observed_at: Instant | None
    expires_at: Instant | None

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


class Provenance(WireModel):
    origin: Literal["youtube_reported", "normalized", "operational"]
    source_paths: Annotated[list[StrictStr], Field(max_length=8)]
    method: Annotated[StrictStr, Field(min_length=1, max_length=96)]
    definition_epoch: Annotated[StrictStr, Field(max_length=96)] | None


class Row(WireModel):
    id: VideoId
    cells: dict[FieldId, Cell]


class Coverage(WireModel):
    status: Literal["complete", "partial", "unknown"]
    # Counts refer to authorized scope, not global hidden/private objects.
    channels_in_scope: Annotated[StrictInt, Field(ge=0)]
    channels_complete: Annotated[StrictInt, Field(ge=0)]
    eligible_count: Annotated[StrictInt, Field(ge=0)] | None = None
    reasons: dict[StrictStr, Annotated[StrictInt, Field(ge=0)]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def scope_counts(self):
        if self.channels_complete > self.channels_in_scope:
            raise ValueError("invalid_coverage_counts")
        if self.status == "complete" and self.channels_complete != self.channels_in_scope:
            raise ValueError("incomplete_channel_scope")
        return self


class DefinitionReference(WireModel):
    id: Annotated[StrictStr, Field(min_length=1, max_length=96)]
    version: Annotated[StrictInt, Field(ge=1)]
    revision: Annotated[StrictStr, Field(min_length=1, max_length=96)]


class Context(WireModel):
    source_id: Literal["public.videos.current.v1"]
    source_schema_version: Annotated[StrictInt, Field(ge=1)]
    registry_version: Annotated[StrictInt, Field(ge=1)]
    lifecycle_profile: DefinitionReference
    preferences_revision: Annotated[StrictStr, Field(min_length=1, max_length=96)]
    type_policy: DefinitionReference
    evaluated_at: Instant
    valid_before: Instant
    consistency: Literal["current"]

    @model_validator(mode="after")
    def lifetime(self):
        if datetime.fromisoformat(self.evaluated_at) >= datetime.fromisoformat(self.valid_before):
            raise ValueError("result_already_expired")
        return self


class PageResult(WireModel):
    has_more: StrictBool
    next_cursor: Annotated[StrictStr, Field(min_length=1, max_length=MAX_CURSOR_CHARS)] | None

    @model_validator(mode="after")
    def cursor_matches_continuation(self):
        if self.has_more != (self.next_cursor is not None):
            raise ValueError("cursor_continuation_mismatch")
        return self


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
        for row in self.rows:
            if set(row.cells) != set(self.columns):
                raise ValueError("result_column_mismatch")
            if any(cell.provenance_ref not in self.provenance for cell in row.cells.values()):
                raise ValueError("missing_provenance_reference")
            for field_id, cell in row.cells.items():
                if not FIELDS[field_id].nullable and cell.state != "known":
                    raise ValueError("missing_required_identity")
                if cell.state != "known":
                    continue
                if type(cell.value) is not str:
                    raise ValueError("unsupported_result_value_type")
                if FIELDS[field_id].value_type == "uint_decimal":
                    uint_decimal(cell.value)
                elif field_id == "video.id":
                    if cell.value != row.id:
                        raise ValueError("result_identity_mismatch")
                elif field_id == "video.channel_id":
                    if re.fullmatch(r"UC[A-Za-z0-9_-]{22}", cell.value) is None:
                        raise ValueError("invalid_channel_identity")
                elif field_id == "video.type" and cell.value not in {"long", "short", "stream"}:
                    raise ValueError("invalid_video_type")
                elif field_id == "video.title" and len(cell.value) > 1000:
                    raise ValueError("title_exceeds_storage_bound")
                elif field_id == "video.published_at":
                    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z", cell.value) is None:
                        raise ValueError("invalid_publication_instant")
                    datetime.fromisoformat(cell.value)
                if datetime.fromisoformat(cell.observed_at) > datetime.fromisoformat(self.context.evaluated_at):
                    raise ValueError("future_source_observation")
                if datetime.fromisoformat(cell.expires_at) < datetime.fromisoformat(self.context.valid_before):
                    raise ValueError("result_outlives_source")
        return self


ERROR_STATUS = {
    "invalid_query": 422, "unsupported_version": 422, "unsupported_source": 422,
    "unsupported_field": 422, "unsupported_operator": 422, "unsupported_video_types": 422,
    "field_disabled": 422, "dependency_disabled": 422, "policy_denied": 403,
    "grant_required": 403, "source_unavailable": 404, "incomplete_source": 409,
    "cursor_expired": 409, "cursor_context_changed": 409, "query_budget_exceeded": 422,
    "query_busy": 429, "backend_unavailable": 503,
}


class QueryError(WireModel):
    code: StrictStr
    path: Annotated[StrictStr, Field(max_length=256)] | None = None

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
