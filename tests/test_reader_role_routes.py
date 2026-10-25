"""Fixed native reader-role route manifest; additions cannot silently skip proof.

The executable PostgreSQL/HTTP harness consumes this same manifest. These local
checks prove manifest completeness, not database privileges or runtime success.
"""
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]

# Ordered so a real browse request supplies the four inspector session tokens,
# and the actual mutation requests follow nonmutating catalogue verification.
PROTECTED_ROUTES = (
    "POST /research/queries/execute",  # Native HTTP proof: research_query_pg.py.
    "GET /explorer/channel-library/video-channel",
    "GET /explorer/home-summary",
    "GET /explorer/recent-videos",
    "GET /sidebar-data",
    "GET /channels/{channel_id}/explorer",
    "GET /channels/{channel_id}/explorer/videos/long",
    "GET /channels/{channel_id}/explorer/videos/short",
    "GET /channels/{channel_id}/explorer/videos/stream",
    "GET /channels/{channel_id}/explorer/videos/{video_type}/browse",
    "GET /channels/{channel_id}/explorer/video-growth/browse",
    "GET /channels/{channel_id}/explorer/video-growth",
    "GET /channels/{channel_id}/explorer/videos/{video_type}/growth",
    "GET /channels/{channel_id}/explorer/videos/{video_type}/growth/browse",
    "GET /channels/{channel_id}/explorer/videos/{video_type}/{video_id}/static-details",
    "GET /channels/{channel_id}/explorer/videos/long/{video_id}/lifetime-baseline",
    "GET /channels/{channel_id}/explorer/videos/long/{video_id}/launch",
    "GET /channels/{channel_id}/explorer/videos/long/{video_id}/history",
    "GET /channels/{channel_id}/explorer/videos/{video_type}/filter-facets",
    "GET /channels/{channel_id}/explorer/profile",
    "GET /channels/{channel_id}/explorer/publications",
    "GET /channels/{channel_id}/explorer/format-totals",
    "GET /channels/{channel_id}/explorer/counter-dynamics-28d",
    "GET /channels/{channel_id}/explorer/counter-dynamics",
    "GET /channels/{channel_id}/explorer/timeline",
    "GET /channels/{channel_id}/explorer/baseline",
    "GET /channels/{channel_id}/explorer/videos/long/baseline",
    "GET /channels/{channel_id}/explorer/derived-insights",
    "GET /channels/{channel_id}/explorer/videos/long/derived-insights",
    "GET /channels/{channel_id}/backfill-current-stats",
    "GET /channels/{channel_id}/videos/long/backfill-current-stats",
    "GET /channels/{channel_id}/hydration",
    "GET /system/video-type-reviews",
    "GET /system/video-type-reviews/{case_id}",
    "GET /saved/collections/{collection_id}/members",
    "GET /user-work/deleted-channels/titles",
    "POST /channels/{channel_id}/backfill-current-stats",
    "POST /channels/{channel_id}/videos/long/backfill-current-stats",
    "POST /channels/{channel_id}/hydration",
    "POST /add-channel",
    "POST /starter-channels/add",
    "POST /system/video-type-reviews/{case_id}/decision",
)

AUXILIARY_READS = (
    "GET /system/run-history",
    "GET /system-summary", "GET /system/pipeline-health",
    "GET /saved/views", "GET /saved/collections", "GET /user-work/status",
    "POST /user-work/backup", "GET /user-work/deleted-channels",
    "GET /setup/status", "GET /sidebar-user-data",
    "GET /saved/collections/{collection_id}/member-identities",
)


class ReaderRouteManifestTests(unittest.TestCase):
    def test_every_protected_route_has_a_native_http_case(self):
        inventory = json.loads((ROOT / "tests/fixtures/read_paths.json").read_text(encoding="utf-8"))
        protected = {key for key, value in inventory["routes"].items() if value["verdict"] == "protected"}
        self.assertEqual(len(PROTECTED_ROUTES), len(set(PROTECTED_ROUTES)))
        self.assertEqual(set(PROTECTED_ROUTES), protected)

    def test_user_work_health_and_setup_reads_are_not_omitted(self):
        self.assertEqual(len(AUXILIARY_READS), len(set(AUXILIARY_READS)))
        self.assertTrue({"POST /user-work/backup", "GET /user-work/deleted-channels",
                         "GET /system-summary", "GET /system/pipeline-health",
                         "GET /setup/status"}.issubset(AUXILIARY_READS))

    def test_inspector_sessions_are_built_by_prior_real_browse_route(self):
        browse = PROTECTED_ROUTES.index("GET /channels/{channel_id}/explorer/video-growth/browse")
        inspectors = [i for i, route in enumerate(PROTECTED_ROUTES) if "{video_id}/" in route]
        self.assertEqual(len(inspectors), 4)
        self.assertTrue(all(index > browse for index in inspectors))


if __name__ == "__main__":
    unittest.main()
