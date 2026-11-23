"""Static frontend delivery and retired-interface responses."""
from fastapi.responses import FileResponse, RedirectResponse
from fastapi import HTTPException, APIRouter


def create_frontend_router(*, FRONTEND_DIST_DIR):
    router = APIRouter()


    def frontend_asset_path(asset_path: str):
        assets_dir = (FRONTEND_DIST_DIR / "assets").resolve()
        requested = (assets_dir / asset_path).resolve()
        if requested != assets_dir and assets_dir not in requested.parents:
            raise HTTPException(status_code=404, detail="Asset not found")
        if not requested.exists() or not requested.is_file():
            raise HTTPException(status_code=404, detail="Asset not found")
        return requested


    @router.get("/app-explorer-v3/assets/{asset_path:path}")
    async def explorer_asset(asset_path: str):
        return FileResponse(frontend_asset_path(asset_path))


    @router.get("/app-explorer-v3")
    @router.get("/app-explorer-v3/{path:path}")
    async def app_explorer_v3_ui(path: str = ""):
        entry = FRONTEND_DIST_DIR / "explorer-v3.html"
        if not entry.is_file():
            raise HTTPException(status_code=404, detail="Explorer V3 frontend build not found")
        return FileResponse(entry)


    @router.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    async def root():
        # The bare address opens the workspace instead of a raw JSON status.
        return RedirectResponse("/app-explorer-v3", status_code=303)


    @router.get("/app-next", include_in_schema=False)
    @router.get("/app-next/{path:path}", include_in_schema=False)
    @router.get("/app-explorer", include_in_schema=False)
    @router.get("/app-explorer/{path:path}", include_in_schema=False)
    @router.get("/app-explorer-v2", include_in_schema=False)
    @router.get("/app-explorer-v2/{path:path}", include_in_schema=False)
    @router.get("/app", include_in_schema=False)
    @router.get("/app/", include_in_schema=False)
    async def retired_legacy_app(path: str = ""):
        raise HTTPException(
            status_code=410,
            detail={"message": "This interface has been retired.", "replacement": "/app-explorer-v3"},
            headers={"Cache-Control": "no-store"},
        )


    return router
