"""Offline checks across the integrated ASGI limits and backup routes."""
import asyncio
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from pydantic import BaseModel
from starlette.responses import JSONResponse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "application"))
from request_limits import RequestBodyLimitMiddleware
from user_work_service import create_user_work_router


class Payload(BaseModel):
    text: str


class GuardIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_streamed_body_limit_with_real_fastapi_model_parsing(self):
        app = FastAPI()
        called = []

        @app.post("/model")
        def model(value: Payload):
            called.append(value.text)
            return {"ok": True}

        app.add_middleware(RequestBodyLimitMiddleware, default_limit=32)

        async def chunks():
            for chunk in [b'{"text":"', b'x' * 24, b'"}']:
                yield chunk

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            response = await client.post("/model", content=chunks(), headers={"Content-Type": "application/json"})
            self.assertEqual((response.status_code, response.json(), called), (413, {"code": "request_too_large"}, []))
            good = await client.post("/model", json={"text": "small"})
            self.assertEqual((good.status_code, called), (200, ["small"]))

    async def test_backup_preview_serialization_stays_off_the_event_loop(self):
        class Repository:
            def status(self):
                return {"can_restore": True, "channel_mappings": [], "user_channel_refs": [], "folder_ids": [], "pending": [],
                        "reconnect": {"snapshot": "0" * 64,
                            "counts": dict.fromkeys(("idle", "queued", "running", "waiting", "blocked"), 0), "excluded_hidden": 0}}

        document = {
            "format": "let-it-cook-user-work", "version": 2, "created_at": "2026-09-25T00:00:00+00:00",
            "user_work": {"folders": [], "labels": [], "channels": [], "collections": [], "reviews": []},
            "browser_preferences": {"catalogue_density": "large", "bookmarked_channel_refs": [], "collapsed_folder_ids": []},
        }
        app = FastAPI()
        app.include_router(create_user_work_router(Repository()))
        app.add_middleware(RequestBodyLimitMiddleware)
        loop_thread = threading.get_ident()
        rendered_on_loop = []
        render = JSONResponse.render

        def record_render(response, value):
            if isinstance(value, dict) and "backup" in value:
                rendered_on_loop.append(threading.get_ident() == loop_thread)
            return render(response, value)

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            with patch.object(JSONResponse, "render", record_render):
                response = await client.post("/user-work/restore/preview", content=json.dumps(document),
                                             headers={"Content-Type": "application/json", "X-Saved-Work": "1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["backup"], {**document, "format": "leti-cook-user-work"})
        self.assertEqual(rendered_on_loop, [False])


if __name__ == "__main__":
    unittest.main()
