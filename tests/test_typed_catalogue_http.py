"""Offline HTTP acceptance for type-bound catalogue reads, using real builders/cache."""
from contextlib import ExitStack
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "application"))
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from catalogue_browse import CatalogueBrowseCache
from web import explorer
from test_typed_catalogue_builders import TypedCatalogueRepository, NOW

TYPES = ("long", "short", "stream")
BASE = "/channels/channel/explorer/videos"


class HttpRepository(TypedCatalogueRepository):
    hidden = False

    def load_channel(self, channel_id, *, columns):
        row = super().load_channel(channel_id, columns=columns)
        row["is_hidden"] = self.hidden
        return row


class TypedCatalogueHttpTests(unittest.TestCase):
    def client(self, *, count=6, baseline=True, derived=True):
        repository = HttpRepository(count=count)
        cache = CatalogueBrowseCache(clock=lambda: NOW, auto_prune=False)
        stack = self.enterContext(ExitStack())
        factories = []
        for name in ("ChannelExplorerRepository", "ChannelVideoGrowthRepository", "ChannelBaselineRepository"):
            factory = stack.enter_context(patch.object(explorer, name, return_value=repository))
            factories.append(factory)

        # Only replace the clock default. Every request still executes the
        # actual route, validation, builder, recording repository and cache.
        for name in ("build_channel_explorer", "build_channel_video_growth", "build_channel_filter_facets"):
            builder = getattr(explorer, name)

            def at_fixed_time(*args, _builder=builder, **kwargs):
                kwargs.setdefault("now", NOW)
                return _builder(*args, **kwargs)

            stack.enter_context(patch.object(explorer, name, side_effect=at_fixed_time))

        def visible(channel_id):
            if repository.hidden or channel_id != "channel":
                raise HTTPException(404, "Channel not found")
            return {"channel_id": channel_id}

        visibility = Mock(side_effect=visible)
        app = FastAPI()
        app.include_router(explorer.create_explorer_router(
            cache=SimpleNamespace(catalogue=cache),
            library=SimpleNamespace(load_visible_channel_or_404=visibility),
            core=SimpleNamespace(VIDEO_TYPE_LONG="long", VIDEO_TYPE_SHORT="short", VIDEO_TYPE_STREAM="stream"),
            database=object(), CHANNEL_BASELINE_ANALYTICS_ENABLED=baseline,
            CHANNEL_DERIVED_INSIGHTS_ENABLED=derived, CHANNEL_LAUNCH_ANALYTICS_ENABLED=True,
            CHANNEL_TIMELINE_INSIGHTS_ENABLED=False,
        ))
        client = stack.enter_context(TestClient(app))
        return client, repository, factories, visibility

    def ok(self, response):
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_capabilities_distinguish_typed_api_from_unopened_ui_tabs(self):
        client, _, factories, visibility = self.client(baseline=False, derived=False)
        response = client.get("/explorer/capabilities")
        payload = self.ok(response)["catalogue_api"]
        self.assertEqual(payload["video_types"], list(TYPES))
        self.assertTrue(payload["one_type_per_request"])
        self.assertEqual(payload["ui_video_types"], ["long"])
        self.assertEqual(payload["raw"], "available")
        for field in ("filter_facets", "static_video_details", "video_growth"):
            self.assertEqual(payload[field], "unavailable")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertFalse(any(factory.called for factory in factories))
        visibility.assert_not_called()

    def test_three_types_share_filters_sort_and_complete_cursor_paging(self):
        client, repository, _, _ = self.client(count=61)
        params = dict(q="Video", category_id="22", min_views=300,
            catalogue_filters=json.dumps({"version": 1, "bounds": {"min_likes": "20"}}),
            sort_by="views", sort_order="desc")
        for video_type in TYPES:
            for suffix in ("browse", "growth/browse"):
                with self.subTest(video_type=video_type, suffix=suffix):
                    url = f"{BASE}/{video_type}/{suffix}"
                    first_response = client.get(url, params=params)
                    first = self.ok(first_response)
                    self.assertEqual(first_response.headers["cache-control"], "no-store")
                    self.assertEqual(len(first["videos"]), 50)
                    self.assertEqual(first["pagination"]["matched_count"], 59)
                    self.assertEqual(first["video_type"], video_type)
                    self.assertEqual(first["browse"]["video_type"], video_type)
                    self.assertTrue(first["coverage"]["complete"])
                    self.assertTrue(first["coverage"]["catalogue_read_ready"])
                    reads = len(repository.inventory_reads)
                    second = self.ok(client.get(url, params={**params, "cursor": first["browse"]["next_cursor"]}))
                    self.assertEqual(len(repository.inventory_reads), reads, "Cursor replay must not read live inventory")
                    self.assertEqual(second["browse"]["session_id"], first["browse"]["session_id"])
                    self.assertIsNone(second["browse"]["next_cursor"])
                    rows = first["videos"] + second["videos"]
                    self.assertEqual([row["views"] for row in rows], list(range(6100, 200, -100)))
                    self.assertEqual(len({row["video_id"] for row in rows}), 59)
                    prefix = {"long": "l", "short": "h", "stream": "s"}[video_type]
                    self.assertTrue(all(row["video_id"].startswith(prefix) for row in rows))

    def test_direct_raw_and_growth_filter_sort_parity_preserves_recorded_flag(self):
        client, repository, _, _ = self.client()
        for video_type in TYPES:
            for suffix in ("", "/growth"):
                with self.subTest(video_type=video_type, suffix=suffix):
                    payload = self.ok(client.get(f"{BASE}/{video_type}{suffix}", params={
                        "min_views": 300, "sort_by": "views", "sort_order": "desc", "limit": 2, "offset": 1,
                        "catalogue_filters": '{"version":1}'}))
                    self.assertEqual([row["views"] for row in payload["videos"]], [500, 400])
                    self.assertEqual(payload["pagination"]["matched_count"], 4)
                    self.assertTrue(payload["coverage"]["catalogue_read_ready"])
                    if video_type != "long":
                        self.assertFalse(payload["coverage"]["product_exposure_ready"])
                    self.assertFalse(repository.states[video_type]["product_exposure_ready"])

    def test_session_facets_and_details_replay_typed_evidence_without_live_reads(self):
        client, repository, factories, _ = self.client()
        filters = json.dumps({"version": 1, "bounds": {"min_likes": "20"},
            "growth": {"min_views_gained": "200"}})
        for video_type in TYPES:
            with self.subTest(video_type=video_type):
                page = self.ok(client.get(f"{BASE}/{video_type}/growth/browse", params={"catalogue_filters": filters}))
                session = page["browse"]["session_id"]
                calls = sum(factory.call_count for factory in factories)
                facets = self.ok(client.get(f"{BASE}/{video_type}/filter-facets", params={
                    "browse_session": session, "full_catalogue": "true", "catalogue_filters": filters}))
                self.assertEqual(facets["matched_count"], 3)
                self.assertEqual(sum(row["count"] for row in facets["facets"]["views"]), 3)
                self.assertTrue(facets["coverage"]["catalogue_read_ready"])
                video_id = page["videos"][0]["video_id"]
                details = self.ok(client.get(f"{BASE}/{video_type}/{video_id}/static-details",
                    params={"browse_session": session}))
                self.assertEqual(details["video_id"], video_id)
                self.assertEqual(details["browse_session"], session)
                self.assertEqual(sum(factory.call_count for factory in factories), calls)
                self.assertFalse(repository.states[video_type]["product_exposure_ready"])

    def test_live_facets_select_the_typed_repository_for_bounded_and_full_reads(self):
        client, repository, factories, _ = self.client()
        explorer_factory, _, baseline_factory = factories
        # ChannelBaselineRepository is deliberately Long-only. Replacing it
        # with a rich fixture would hide a missing-method failure in production.
        baseline_factory.side_effect = AssertionError("Typed facets must use the typed inventory adapter")
        filters = json.dumps({"version": 1, "bounds": {"min_likes": "20"}})
        for video_type in TYPES:
            for full in ("false", "true"):
                with self.subTest(video_type=video_type, full=full):
                    before = explorer_factory.call_count
                    response = client.get(f"{BASE}/{video_type}/filter-facets", params={
                        "full_catalogue": full, "catalogue_filters": filters})
                    result = self.ok(response)
                    self.assertEqual(explorer_factory.call_count, before + 1)
                    self.assertEqual(repository.inventory_reads[-1], video_type)
                    self.assertEqual(result["matched_count"], 4)
                    self.assertTrue(result["coverage"]["complete"])
                    self.assertTrue(result["coverage"]["catalogue_read_ready"])
        baseline_factory.assert_not_called()

    def test_cursor_facets_and_details_cannot_cross_type_even_on_same_channel(self):
        client, _, factories, _ = self.client(count=61)
        for source_type in TYPES:
            page = self.ok(client.get(f"{BASE}/{source_type}/browse"))
            calls = sum(factory.call_count for factory in factories)
            for target_type in TYPES:
                if target_type == source_type:
                    continue
                with self.subTest(source=source_type, target=target_type):
                    session = page["browse"]["session_id"]
                    requests = (
                        (f"{BASE}/{target_type}/browse", {"cursor": page["browse"]["next_cursor"]}),
                        (f"{BASE}/{target_type}/filter-facets", {"browse_session": session, "full_catalogue": "true"}),
                        (f"{BASE}/{target_type}/{page['videos'][0]['video_id']}/static-details", {"browse_session": session}),
                    )
                    for url, params in requests:
                        response = client.get(url, params=params)
                        self.assertEqual(response.status_code, 400, response.text)
                        self.assertEqual(response.json()["detail"], "catalogue_browse_scope_mismatch")
            self.assertEqual(sum(factory.call_count for factory in factories), calls)

    def test_unknown_type_is_422_before_repository_or_visibility_reads(self):
        client, _, factories, visibility = self.client()
        for suffix, params in (
            ("browse", {}), ("growth", {}), ("growth/browse", {}), ("filter-facets", {}),
            ("h0000000000/static-details", {"browse_session": "0" * 32}),
        ):
            with self.subTest(suffix=suffix):
                response = client.get(f"{BASE}/invalid/{suffix}", params=params)
                self.assertEqual(response.status_code, 422, response.text)
        self.assertFalse(any(factory.called for factory in factories))
        visibility.assert_not_called()

    def test_disabled_analysis_gates_reject_typed_requests_before_database(self):
        client, _, factories, visibility = self.client(baseline=False, derived=False)
        static_filter = json.dumps({"version": 1, "bounds": {"min_tag_count": "1"}})
        for video_type in TYPES:
            for suffix, params in (
                ("growth", {}), ("growth/browse", {}), ("filter-facets", {}),
                ("h0000000000/static-details", {"browse_session": "0" * 32}),
                ("browse", {"catalogue_filters": static_filter}),
            ):
                with self.subTest(video_type=video_type, suffix=suffix):
                    response = client.get(f"{BASE}/{video_type}/{suffix}", params=params)
                    self.assertEqual(response.status_code, 404, response.text)
        self.assertFalse(any(factory.called for factory in factories))
        visibility.assert_not_called()

    def test_hidden_channel_cannot_open_or_replay_typed_catalogue(self):
        client, repository, factories, _ = self.client(count=61)
        pages = {kind: self.ok(client.get(f"{BASE}/{kind}/browse")) for kind in TYPES}
        repository.hidden = True
        for video_type in TYPES:
            page = pages[video_type]
            session = page["browse"]["session_id"]
            for suffix, params in (
                ("", {}), ("/growth", {}), ("/filter-facets", {"full_catalogue": "true"}),
                ("/browse", {}), ("/growth/browse", {}),
                ("/browse", {"cursor": page["browse"]["next_cursor"]}),
                ("/filter-facets", {"browse_session": session, "full_catalogue": "true"}),
                (f"/{page['videos'][0]['video_id']}/static-details", {"browse_session": session}),
            ):
                with self.subTest(video_type=video_type, suffix=suffix, params=params):
                    response = client.get(f"{BASE}/{video_type}{suffix}", params=params)
                    self.assertEqual(response.status_code, 404, response.text)

    def test_typed_session_does_not_open_long_only_history_launch_or_baseline(self):
        client, _, factories, _ = self.client()
        for video_type in ("short", "stream"):
            page = self.ok(client.get(f"{BASE}/{video_type}/growth/browse"))
            video_id = page["videos"][0]["video_id"]
            calls = sum(factory.call_count for factory in factories)
            for suffix in ("history", "launch", "lifetime-baseline"):
                for path_type, expected_status in ((video_type, 404), ("long", 400)):
                    with self.subTest(type=video_type, path_type=path_type, suffix=suffix):
                        response = client.get(f"{BASE}/{path_type}/{video_id}/{suffix}",
                            params={"browse_session": page["browse"]["session_id"]})
                        self.assertEqual(response.status_code, expected_status, response.text)
            self.assertEqual(sum(factory.call_count for factory in factories), calls)


if __name__ == "__main__":
    unittest.main()
