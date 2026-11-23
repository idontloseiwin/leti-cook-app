"""Channel page reads that stay outside the shared research backend.

The browser extension maps a video to its channel; the header reads the stored
channel profile and the list proof of its unlisted note. Library, Overview,
Inspector and the workshop read through /research/*.
"""
from db import ChannelExplorerRepository, FrontendRepository
from channel_explorer import (
    ChannelExplorerNotFoundError,
    build_channel_list_inventory,
    build_channel_profile,
)
from uuid import UUID
from typing import Literal
from fastapi import HTTPException, APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field
from collection_formats import shown_formats
from db.inventory.manual_check import request_check, ManualCheckRefused
from saved_work import SavedWorkError, validate_mutation_headers
from starlette.responses import Response
import re


class ManualCheckCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    epoch: int = Field(strict=True, ge=1, le=2**63-1)
    previous: UUID | None
    request_id: UUID


def create_explorer_router(*, database):
    router = APIRouter()

    @router.get("/explorer/channel-library/video-channel")
    def explorer_channel_library_video_channel(video_id: str, response: Response):
        response.headers["Cache-Control"] = "no-store"
        if not re.fullmatch(r"[a-zA-Z0-9_-]{11}", video_id):
            raise HTTPException(status_code=422, detail="Invalid YouTube video ID")

        repository = FrontendRepository(database)
        channel_id = repository.load_channel_id_for_video(video_id)
        channel_rows = repository.load_channel(channel_id) if channel_id else []
        visible_channel_id = (
            channel_id
            if channel_rows and not channel_rows[0].get("is_hidden")
            else None
        )
        return {
            "contract_version": 1,
            "status": "matched" if visible_channel_id else "not_tracked",
            "video_id": video_id,
            "channel_id": visible_channel_id,
            "source": {
                "kind": "stored_video_channel_mapping",
                "youtube_requests": False,
                "database_writes": False,
            },
        }


    @router.get("/channels/{channel_id}/explorer/profile")
    def channel_explorer_profile(channel_id: str):
        try:
            return build_channel_profile(
                ChannelExplorerRepository(database),
                channel_id,
            )
        except ChannelExplorerNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error


    # The header's unlisted note: list counts and the profile's public video count only.
    @router.get("/channels/{channel_id}/explorer/list-inventory")
    def channel_explorer_list_inventory(channel_id: str):
        try:
            return build_channel_list_inventory(ChannelExplorerRepository(database), channel_id)
        except ChannelExplorerNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error


    @router.post("/channels/{channel_id}/explorer/list-check/{video_type}")
    def confirm_list_check(channel_id: str, video_type: Literal["long","short","stream"],
                           command: ManualCheckCommand, request: Request):
        recheck = getattr(request.state, "owner_access_recheck", None)
        if getattr(request.state, "installation_access", False) is not True or not callable(recheck):
            raise HTTPException(401, detail="authentication_required")
        try:
            validate_mutation_headers(request.headers, request.url.scheme)
        except SavedWorkError:
            raise HTTPException(403, detail="write_guard") from None
        if recheck() is None:
            raise HTTPException(403, detail="owner_access_required")
        if not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", channel_id):
            raise HTTPException(422, detail="invalid_channel")
        if video_type not in shown_formats():
            raise HTTPException(409, detail="format_off")
        try:
            return request_check(database, channel_id, video_type, command.epoch,
                                 str(command.previous) if command.previous else None, str(command.request_id))
        except ManualCheckRefused as error:
            raise HTTPException(error.status, detail=error.reason) from error

    return router
