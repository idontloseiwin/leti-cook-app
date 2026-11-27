"""Fixed native reader-role route manifest; additions cannot silently skip proof.

The executable PostgreSQL/HTTP harness tests/reader_role_http_pg.py consumes this
same manifest: each route is a native case there (its transactions read back as
lic_reader, read only) or is named there with the reason it is not run. These local
checks prove manifest completeness, not database privileges or runtime success.
"""
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]

# The actual mutation requests follow the nonmutating reads.
PROTECTED_ROUTES = (
    "POST /research/alerts/preview", "GET /research/alerts/inbox",
    "POST /research/alerts/rules", "PATCH /research/alerts/rules/{rule_id}",
    "POST /research/alerts/inbox/{item_id}/read",
    "POST /research/queries/execute",  # Native HTTP proof: research_query_pg.py.
    "POST /research/queries/frozen-ids",  # Native HTTP proof: research_frozen_pg.py.
    "POST /research/operations/execute",  # Exercises the research operation under the reader role.
    "POST /research/graphs/execute",  # Same harness: graph/direct parity with lic_reader.
    "GET /explorer/channel-library/video-channel",
    "GET /explorer/home-summary",
    "GET /sidebar-data",
    "GET /channels/{channel_id}/explorer/profile",
    "GET /channels/{channel_id}/explorer/list-inventory",
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
    # Native reader-role proof: provider_sources_pg.py (ledger read as lic_reader, fake provider).
    # Fresh provider values only; a harness must never send this route to YouTube.
    "POST /sources/acquisitions/execute",
    "POST /sources/channel-search/execute",  # Real role and Library membership with a synthetic provider.
)

# Routes without YouTube data whose reads still run as lic_reader. Each is a native case in
# reader_role_http_pg.py too, so a relation that role cannot read fails there and not on an
# installation (system/websub read public.websub_* and answered 500 on every call).
READER_ROLE_READS = (
    "GET /research/alerts/rules",
    "GET /system/websub",  # WebSub totals through readable.websub_health.
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

    def test_reader_role_reads_are_classified_routes_without_youtube_data(self):
        routes = json.loads((ROOT / "tests/fixtures/read_paths.json").read_text(encoding="utf-8"))["routes"]
        self.assertEqual(len(READER_ROLE_READS), len(set(READER_ROLE_READS)))
        self.assertEqual(set(READER_ROLE_READS) & set(PROTECTED_ROUTES), set())
        for route in READER_ROLE_READS:
            self.assertEqual(routes[route]["verdict"], "no_youtube_data", route)

    def test_user_work_health_and_setup_reads_are_not_omitted(self):
        self.assertEqual(len(AUXILIARY_READS), len(set(AUXILIARY_READS)))
        self.assertTrue({"POST /user-work/backup", "GET /user-work/deleted-channels",
                         "GET /system-summary", "GET /system/pipeline-health",
                         "GET /setup/status"}.issubset(AUXILIARY_READS))


if __name__ == "__main__":
    unittest.main()
