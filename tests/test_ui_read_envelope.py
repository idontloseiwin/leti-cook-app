"""A failed finite-JSON envelope is not a source-age expiry observation."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "application"))
from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.testclient import TestClient
from hot_storage import register_read_deadline
from web.middleware import HttpReadScopeMiddleware, install_runtime_middleware
import telemetry
from telemetry_contract import build_event


class UiReadEnvelopeTests(unittest.TestCase):
    def test_invalid_envelope_and_actual_expiry_have_distinct_safe_codes(self):
        now = datetime.now(timezone.utc)
        for expired in (False, True):
            with self.subTest(expired=expired):
                events = []
                app = FastAPI()
                with patch("web.middleware.selected_deployment_profile", return_value="operator"):
                    install_runtime_middleware(app, core=SimpleNamespace())
                app.add_middleware(HttpReadScopeMiddleware)

                @app.get("/explorer/home-summary")
                async def read():
                    register_read_deadline(now if expired else now + timedelta(minutes=1))
                    return Response("PRIVATE_INVALID_JSON", media_type="application/json")

                with patch("web.middleware.hot_read_now", return_value=now), \
                     patch.object(telemetry, "is_enabled", return_value=True), \
                     patch.object(telemetry, "emit_event",
                         side_effect=lambda *a, **kw: events.append(build_event(*a, **kw))):
                    result = TestClient(app).get("/explorer/home-summary")
                self.assertEqual(result.status_code, 503)
                reasons = {row.get("reason") for row in events}
                self.assertIn("source_expired" if expired else "invalid_response_envelope", reasons)
                self.assertNotIn("invalid_response_envelope" if expired else "source_expired", reasons)
                self.assertNotIn("PRIVATE_INVALID_JSON", repr(events))
                self.assertEqual(events[-1]["http_status"], 503)


if __name__ == "__main__":
    unittest.main()
