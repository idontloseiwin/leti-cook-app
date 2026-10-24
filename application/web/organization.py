"""Folder and label HTTP adapters."""
from db.user_work.channel_labels import ChannelLabelRepository
from db import ChannelOrganizationRepository
from fastapi import HTTPException, APIRouter
from saved_work import SavedWorkError, saved_name
from datetime import datetime, timezone
from channels.service import user_work_limit_reached
import uuid
from .models import FolderAssign, FolderRequest, LabelAssignRequest, LabelCreateRequest
from .constants import LABEL_KEY_RE, LABEL_NAME_MAX_LENGTH, LEGACY_VIRAL_DISABLED_DETAIL
from .common import has_control_characters, normalize_label_name


def create_organization_router(*, cache, library, database):
    router = APIRouter()
    clear_frontend_read_cache = cache.clear_frontend_read_cache
    load_folders = library.load_folders
    require_visible_channel_user_work = library.require_visible_channel_user_work

    @router.post("/recalculate-viral", deprecated=True)
    async def recalculate_viral():
        raise HTTPException(status_code=410, detail=LEGACY_VIRAL_DISABLED_DETAIL)


    @router.post("/labels")
    def create_channel_label(req: LabelCreateRequest):
        name = normalize_label_name(req.name)
        if not name:
            raise HTTPException(status_code=400, detail="Label name is required")
        if len(name) > LABEL_NAME_MAX_LENGTH:
            raise HTTPException(status_code=400, detail="Label name must be 40 characters or fewer")
        if has_control_characters(name):
            raise HTTPException(status_code=400, detail="Label name cannot contain control characters")

        repository = ChannelLabelRepository(database)
        existing = next(
            (row for row in repository.load_labels()
             if normalize_label_name(row.get("name")).casefold() == name.casefold()),
            None,
        )
        if existing:
            # The same name reuses the label; the flag lets the page say so.
            return {**existing, "created": False}

        row = {
            "label_key": f"custom:{uuid.uuid4()}",
            "name": name,
            "label_type": "custom",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            created = repository.create_label(row)
        except Exception as error:
            if user_work_limit_reached(error):
                raise HTTPException(status_code=413, detail="The workspace holds its limit of 1,000 labels. "
                                    "Remove unused labels before adding more.") from None
            existing = next(
                (item for item in repository.load_labels()
                 if normalize_label_name(item.get("name")).casefold() == name.casefold()),
                None,
            )
            if not existing:
                raise
            # Another request created the same name first.
            return {**existing, "created": False}
        clear_frontend_read_cache()
        return {**created, "created": True}


    def _existing_label_or_404(label_key, repository):
        labels = repository.load_labels() if LABEL_KEY_RE.fullmatch(label_key) else []
        if not any(row.get("label_key") == label_key for row in labels):
            raise HTTPException(status_code=404, detail="Label not found")
        return labels


    @router.patch("/labels/{label_key}")
    def rename_channel_label(label_key: str, req: LabelCreateRequest):
        name = normalize_label_name(req.name)
        if not name:
            raise HTTPException(status_code=400, detail="Label name is required")
        if len(name) > LABEL_NAME_MAX_LENGTH:
            raise HTTPException(status_code=400, detail="Label name must be 40 characters or fewer")
        if has_control_characters(name):
            raise HTTPException(status_code=400, detail="Label name cannot contain control characters")
        repository = ChannelLabelRepository(database)
        labels = _existing_label_or_404(label_key, repository)
        if any(row.get("label_key") != label_key and normalize_label_name(row.get("name")).casefold() == name.casefold()
               for row in labels):
            raise HTTPException(status_code=409, detail={"code": "label_name_taken",
                                                         "message": "Another label already has this name."})
        # The preset Monetized label is renamed and deleted like any other.
        repository.rename_label(label_key, name)
        clear_frontend_read_cache()
        row = next(row for row in labels if row.get("label_key") == label_key)
        return {**row, "name": name}


    @router.delete("/labels/{label_key}")
    def delete_channel_label(label_key: str):
        repository = ChannelLabelRepository(database)
        _existing_label_or_404(label_key, repository)
        repository.delete_label(label_key)
        clear_frontend_read_cache()
        return {"status": "deleted", "label_key": label_key}


    @router.post("/channels/{channel_id}/labels/{label_key}")
    def assign_channel_label(
        channel_id: str,
        label_key: str,
        req: LabelAssignRequest,
    ):
        if not LABEL_KEY_RE.fullmatch(label_key):
            raise HTTPException(status_code=404, detail="Label not found")
        require_visible_channel_user_work(channel_id)
        repository = ChannelLabelRepository(database)
        if not any(row.get("label_key") == label_key for row in repository.load_labels()):
            raise HTTPException(status_code=404, detail="Label not found")
        if req.enabled:
            try:
                repository.assign_label(channel_id, label_key)
            except Exception as error:
                if not user_work_limit_reached(error):
                    raise
                raise HTTPException(status_code=413, detail="The workspace holds its limit of 100,000 label "
                                    "assignments. Remove labels from channels before adding more.") from None
        else:
            repository.unassign_label(channel_id, label_key)
        clear_frontend_read_cache(channel_id)
        return {"status": "ok", "enabled": req.enabled}


    @router.post("/folders")
    def create_folder(req: FolderRequest):
        # The user-work backup exports folder names with this same rule.
        try:
            folder_name = saved_name(req.name or "")
        except SavedWorkError:
            raise HTTPException(status_code=400, detail="Folder name must contain 1 to 80 characters, without control characters") from None

        existing_folders = load_folders()
        for folder in existing_folders:
            if str(folder.get("name") or "").strip().lower() == folder_name.lower():
                # The same name reuses the folder; the flag lets the page say so.
                return {"id": folder.get("id"), "name": folder.get("name"), "created": False}

        folder_id = str(uuid.uuid4())
        try:
            ChannelOrganizationRepository(database).create_folder({
                "id": folder_id,
                "name": folder_name,
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
        except Exception as error:
            if not user_work_limit_reached(error):
                raise
            raise HTTPException(status_code=413, detail="The workspace holds its limit of 1,000 folders. "
                                "Remove unused folders before adding more.") from None
        clear_frontend_read_cache()
        return {"id": folder_id, "name": folder_name, "created": True}


    def _existing_folder_or_404(folder_id):
        try:
            folder_id = str(uuid.UUID(folder_id))
        except ValueError:
            raise HTTPException(status_code=404, detail="Folder not found") from None
        folders = load_folders()
        if not any(str(folder.get("id")) == folder_id for folder in folders):
            raise HTTPException(status_code=404, detail="Folder not found")
        return folder_id, folders


    @router.patch("/folders/{folder_id}")
    def rename_folder(folder_id: str, req: FolderRequest):
        try:
            folder_name = saved_name(req.name or "")
        except SavedWorkError:
            raise HTTPException(status_code=400, detail="Folder name must contain 1 to 80 characters, without control characters") from None
        folder_id, folders = _existing_folder_or_404(folder_id)
        if any(str(folder.get("id")) != folder_id and str(folder.get("name") or "").strip().lower() == folder_name.lower()
               for folder in folders):
            raise HTTPException(status_code=409, detail={"code": "folder_name_taken",
                                                         "message": "Another folder already has this name."})
        ChannelOrganizationRepository(database).rename_folder(folder_id, folder_name)
        clear_frontend_read_cache()
        return {"id": folder_id, "name": folder_name}


    @router.delete("/folders/{folder_id}")
    def delete_folder(folder_id: str):
        # Its channels stay and move to no folder.
        folder_id, _ = _existing_folder_or_404(folder_id)
        ChannelOrganizationRepository(database).delete_folder(folder_id)
        clear_frontend_read_cache()
        return {"status": "deleted", "id": folder_id}


    @router.post("/channels/{channel_id}/folder")
    def assign_folder(channel_id: str, req: FolderAssign):
        require_visible_channel_user_work(channel_id)
        val = req.folder_id if req.folder_id else None
        if val is not None and not any(str(folder.get("id")) == val for folder in load_folders()):
            raise HTTPException(status_code=404, detail="Folder not found")
        ChannelOrganizationRepository(database).assign_folder(channel_id, val)
        clear_frontend_read_cache(channel_id)
        return {"status": "ok"}


    return router
