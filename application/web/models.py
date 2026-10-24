"""HTTP request validation; no runtime resources are constructed here."""
from pydantic import BaseModel, Field, StrictBool
import uuid

class ChannelRequest(BaseModel):
    url: str = Field(max_length=2048)  # the user_channel_input bound


class FolderRequest(BaseModel):
    name: str = Field(max_length=80)  # the folder form's limit


class FolderAssign(BaseModel):
    folder_id: str = Field(max_length=64)


class LabelCreateRequest(BaseModel):
    # Whitespace is collapsed before the 40-character label rule applies.
    name: str = Field(max_length=200)


class LabelAssignRequest(BaseModel):
    # Only JSON true or false: "yes", 1 or "true" must not switch a label.
    enabled: StrictBool


class InitialHydrationRequest(BaseModel):
    expected_items: int | None
    expected_pages: int | None
    expected_quota_units: int | None
    retry_request_id: uuid.UUID | None = None
    confirmed: bool = False
