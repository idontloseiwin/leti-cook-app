"""Typed frozen catalogue generations retain evidence and cannot cross scopes."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "application"))
from catalogue_browse import (CatalogueBrowseCache, CatalogueBrowseError,
    RecordingCatalogueRepository, FrozenCatalogueRepository, normalize_browse_query)

NOW = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)
CHANNEL = "UC" + "a" * 22
CHECKED = (NOW - timedelta(hours=1)).isoformat()


def source(video_type="short", count=61, *, expiry=None):
    ids = [f"v{n:010d}" for n in range(count)]
    expiry = expiry or (NOW + timedelta(minutes=4)).isoformat()
    return {
        "video_type": video_type,
        "channel": {"channel_id": CHANNEL, "is_hidden": False},
        "inventory": [{"video_id": key, "source_checked_at": CHECKED} for key in ids],
        "videos": [{"video_id": key, "channel_id": CHANNEL, "title": key,
                    "views": n, "duration": "PT30S", "checked_at": CHECKED,
                    "metadata_checked_at": CHECKED, "availability_checked_at": CHECKED,
                    "availability_status": "public"} for n, key in enumerate(ids)],
        "inventory_state": {"channel_id": CHANNEL, "video_type": video_type,
            "inventory_status": "completed", "product_exposure_ready": False,
            "source_checked_at": CHECKED, "inventory_completed_at": CHECKED,
            "source_expires_at": expiry},
        "type_resolutions": [{"video_id": key, "canonical_video_type": video_type,
            "resolution_status": "resolved", "automatic_valid": True,
            "source_expires_at": expiry} for key in ids],
        "growth": [{"video_id": key, "status": "waiting_next_snapshot"} for key in ids],
    }


def payload(snapshot):
    return {"video_type": snapshot.get("video_type", "long"),
            "coverage": {"video_type": snapshot.get("video_type", "long"), "scan_mode": "full_catalogue", "truncated": False},
            "filters": {}, "pagination": {}, "videos": deepcopy(snapshot["videos"])}


class TypedSource:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.calls = []
        self.closed = False

    def load_channel(self, channel_id, *, columns):
        self.calls.append(("channel", channel_id))
        return deepcopy(self.snapshot["channel"])

    def iter_inventory_pages(self, channel_id, *, video_type):
        self.calls.append(("inventory", video_type))
        try:
            for row in self.snapshot["inventory"]:
                yield [dict(row)]
        finally:
            self.closed = True

    def load_inventory_state(self, channel_id, *, video_type):
        self.calls.append(("state", video_type))
        return deepcopy(self.snapshot["inventory_state"])

    def load_type_resolutions(self, video_ids, *, video_type):
        self.calls.append(("resolution", video_type))
        return [dict(row) for row in self.snapshot["type_resolutions"] if row["video_id"] in video_ids]

    def load_videos_by_ids(self, video_ids, *, columns):
        return [dict(row) for row in self.snapshot["videos"] if row["video_id"] in video_ids]

    def load_video_view_growth(self, channel_id, video_ids, *, as_of, video_type, recorded_from=None):
        self.calls.append(("growth", video_type, recorded_from))
        return [dict(row) for row in self.snapshot["growth"] if row["video_id"] in video_ids]


class TypedRecordingTests(unittest.TestCase):
    def test_typed_rows_state_resolution_and_growth_replay_without_live_access(self):
        for kind in ("short", "stream"):
            with self.subTest(kind=kind):
                live = TypedSource(source(kind, 2))
                recorder = RecordingCatalogueRepository(live, video_type=kind, capture_growth=True)
                recorder.load_channel(CHANNEL, columns="channel_id")
                recorder.load_inventory_state(CHANNEL, video_type=kind)
                rows = [row for page in recorder.iter_inventory_pages(CHANNEL, video_type=kind) for row in page]
                ids = [row["video_id"] for row in rows]
                recorder.load_type_resolutions(ids, video_type=kind)
                recorder.load_videos_by_ids(ids, columns="video_id")
                recorder.load_video_view_growth(CHANNEL, ids, as_of=NOW, video_type=kind, recorded_from=NOW-timedelta(days=1))
                self.assertTrue(live.closed)
                frozen = FrozenCatalogueRepository(recorder.snapshot)
                calls = list(live.calls)
                live.snapshot["inventory_state"]["product_exposure_ready"] = True
                self.assertFalse(frozen.load_inventory_state(CHANNEL, video_type=kind)["product_exposure_ready"])
                self.assertEqual([r for page in frozen.iter_inventory_pages(CHANNEL, video_type=kind) for r in page], rows)
                self.assertEqual(len(frozen.load_type_resolutions(ids, video_type=kind)), 2)
                self.assertEqual(len(frozen.load_frozen_video_growth(ids)), 2)
                self.assertEqual(live.calls, calls)
                with self.assertRaisesRegex(CatalogueBrowseError, "scope_mismatch"):
                    list(frozen.iter_long_inventory_pages(CHANNEL))
                with self.assertRaisesRegex(CatalogueBrowseError, "scope_mismatch"):
                    recorder.load_type_resolutions(ids, video_type="long")

    def test_abandoned_typed_iterator_closes_underlying_source(self):
        live = TypedSource(source(count=2))
        recorder = RecordingCatalogueRepository(live, video_type="short")
        iterator = recorder.iter_inventory_pages(CHANNEL, video_type="short")
        next(iterator)
        iterator.close()
        self.assertTrue(live.closed)
        self.assertEqual(len(recorder.snapshot["inventory"]), 1)

    def test_legacy_long_adapter_needs_no_new_keyword(self):
        class Legacy:
            def iter_long_inventory_pages(self, channel_id):
                yield [{"video_id": "v0000000000", "source_checked_at": CHECKED}]
            def load_video_view_growth(self, channel_id, video_ids, *, as_of):
                return [{"video_id": video_ids[0]}]
        recorder = RecordingCatalogueRepository(Legacy(), capture_growth=True)
        self.assertEqual(len(list(recorder.iter_long_inventory_pages(CHANNEL))), 1)
        self.assertEqual(len(recorder.load_video_view_growth(CHANNEL, ["v0000000000"], as_of=NOW)), 1)
        self.assertEqual(recorder.snapshot["video_type"], "long")


class TypedBrowseTests(unittest.TestCase):
    def setUp(self):
        self.now = NOW
        self.tick = 0.0
        self.cache = CatalogueBrowseCache(clock=lambda: self.now, monotonic=lambda: self.tick, auto_prune=False)
        self.query = normalize_browse_query("raw", {})

    def begin(self, kind="short", *, snapshot=None, source_name="raw", **kwargs):
        snapshot = deepcopy(snapshot or source(kind))
        query = normalize_browse_query(source_name, {})
        first = self.cache.begin(CHANNEL, source_name, query,
            lambda _now: (payload(snapshot), snapshot), video_type=kind, **kwargs)
        return first, snapshot, query

    def test_page_cursor_and_facet_generation_are_bound_to_type(self):
        for kind in ("long", "short", "stream"):
            first, snapshot, query = self.begin(kind)
            cursor, session = first["browse"]["next_cursor"], first["browse"]["session_id"]
            snapshot["videos"][50]["title"] = "changed after capture"
            resumed = self.cache.resume(CHANNEL, "raw", query, cursor, video_type=kind)
            self.assertEqual(resumed["browse"]["video_type"], kind)
            self.assertEqual(len(resumed["videos"]), 11)
            self.assertNotEqual(resumed["videos"][0]["title"], "changed after capture")
            frozen, created = self.cache.facet_source(session, CHANNEL, query,
                growth_enabled=True, video_type=kind)
            self.assertEqual(frozen.video_type, kind)
            self.assertEqual(created, NOW)
            for other in {"long", "short", "stream"} - {kind}:
                with self.subTest(kind=kind, other=other):
                    with self.assertRaisesRegex(CatalogueBrowseError, "scope_mismatch"):
                        self.cache.resume(CHANNEL, "raw", query, cursor, video_type=other)
                    with self.assertRaisesRegex(CatalogueBrowseError, "scope_mismatch"):
                        self.cache.facet_source(session, CHANNEL, query, growth_enabled=True, video_type=other)
                    with self.assertRaisesRegex(CatalogueBrowseError, "scope_mismatch"):
                        self.cache.validate_inspector_context(session, CHANNEL, growth_enabled=True, video_type=other)
                    with self.assertRaisesRegex(CatalogueBrowseError, "scope_mismatch"):
                        self.cache.video_static_details(session, CHANNEL, "v0000000000", growth_enabled=True, video_type=other)

    def test_long_only_inspectors_cannot_consume_typed_session(self):
        first, _, _ = self.begin()
        session = first["browse"]["session_id"]
        for method in (self.cache.video_launch_context, self.cache.video_lifetime_baseline, self.cache.video_history_context):
            with self.subTest(method=method.__name__):
                with self.assertRaisesRegex(CatalogueBrowseError, "scope_mismatch"):
                    method(session, CHANNEL, "v0000000000", growth_enabled=True)
                with self.assertRaisesRegex(CatalogueBrowseError, "unavailable"):
                    method(session, CHANNEL, "v0000000000", growth_enabled=True, video_type="short")
        with self.assertRaisesRegex(CatalogueBrowseError, "scope_mismatch"):
            self.cache.validate_video_history_context(session, CHANNEL)

    def test_typed_static_context_retains_admission_expiry(self):
        first, _, _ = self.begin()
        result = self.cache.video_static_details(first["browse"]["session_id"], CHANNEL,
            "v0000000000", growth_enabled=True, video_type="short")
        self.assertEqual(result["source_expires_at"], "2026-10-19T12:04:00Z")
        self.assertEqual(result["expires_at"], "2026-10-19T12:04:00Z")

    def test_snapshot_or_payload_type_mismatch_refused(self):
        snapshot = source("short")
        for changed in ("snapshot", "payload"):
            body, saved = payload(snapshot), deepcopy(snapshot)
            (saved if changed == "snapshot" else body)["video_type"] = "long"
            with self.assertRaisesRegex(CatalogueBrowseError, "scope_mismatch"):
                self.cache.begin(CHANNEL, "raw", self.query, lambda _now: (body, saved), video_type="short")
        self.assertEqual(len(self.cache._sessions), 0)

    def test_legacy_snapshot_defaults_to_long_only(self):
        snapshot = source("long")
        del snapshot["video_type"]
        first = self.cache.begin(CHANNEL, "raw", self.query, lambda _now: (payload(snapshot), snapshot))
        self.assertEqual(first["browse"]["video_type"], "long")
        with self.assertRaisesRegex(CatalogueBrowseError, "scope_mismatch"):
            self.cache.resume(CHANNEL, "raw", self.query, first["browse"]["next_cursor"], video_type="short")

    def test_state_and_resolution_expiry_bound_retained_generation(self):
        for location in ("inventory_state", "type_resolutions"):
            snapshot = source()
            proof = snapshot[location][0] if location == "type_resolutions" else snapshot[location]
            proof["source_expires_at"] = (NOW + timedelta(seconds=15)).isoformat()
            first, _, query = self.begin(snapshot=snapshot)
            self.assertEqual(first["browse"]["expires_at"], "2026-10-19T12:00:15Z")
            self.now = NOW + timedelta(seconds=16)
            with self.assertRaisesRegex(CatalogueBrowseError, "session_expired"):
                self.cache.resume(CHANNEL, "raw", query, first["browse"]["next_cursor"], video_type="short")
            self.now = NOW
        for invalid in ("bad", NOW.isoformat(), (NOW-timedelta(seconds=1)).isoformat()):
            snapshot = source()
            snapshot["type_resolutions"][0]["source_expires_at"] = invalid
            with self.assertRaisesRegex(CatalogueBrowseError, "source_expired"):
                self.begin(snapshot=snapshot)

    def test_query_source_gate_invalidation_and_capacity_remain_enforced(self):
        first, _, query = self.begin(source_name="growth")
        session, cursor = first["browse"]["session_id"], first["browse"]["next_cursor"]
        with self.assertRaisesRegex(CatalogueBrowseError, "unavailable"):
            self.cache.facet_source(session, CHANNEL, query, growth_enabled=False, video_type="short")
        with self.assertRaisesRegex(CatalogueBrowseError, "scope_mismatch"):
            self.cache.resume(CHANNEL, "raw", query, cursor, video_type="short")
        with self.assertRaisesRegex(CatalogueBrowseError, "scope_mismatch"):
            self.cache.resume(CHANNEL, "growth", {**query, "q": "other"}, cursor, video_type="short")
        self.cache.invalidate_channel(CHANNEL)
        with self.assertRaisesRegex(CatalogueBrowseError, "session_expired"):
            self.cache.resume(CHANNEL, "growth", query, cursor, video_type="short")
        self.cache.max_session_bytes = 1
        with self.assertRaisesRegex(CatalogueBrowseError, "capacity_exceeded"):
            self.begin()


if __name__ == "__main__":
    unittest.main()
