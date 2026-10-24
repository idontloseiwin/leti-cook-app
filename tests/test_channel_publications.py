"""Read-only publication list: bounded observations, no inference or collection."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from http_test_support import cache_stub, endpoint, library_stub
from fastapi import HTTPException
from starlette.responses import Response
from channel_publications import PublicationReadError, build_channel_publications
from db.reads.channel_publications import ChannelPublicationsRepository
from hot_storage import HOT_DATA_WINDOW, HotSourceReadExpired, iso_utc
from web.explorer import create_explorer_router


NOW = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
CHANNEL = "UC" + "x" * 22


def observation(number=1, **changes):
    return dict(video_id=f"{number:011d}", video_type="long", title="Awaited video",
                publication_status="upcoming", scheduled_start_at=None,
                checked_at=iso_utc(NOW), source_expires_at=iso_utc(NOW + HOT_DATA_WINDOW), **changes)


def row(number=1, **changes):
    result = observation(number)
    result.update(changes)
    return result


class ChannelPublicationsTests(unittest.TestCase):
    def test_all_types_and_unconfirmed_type_have_the_same_observation_contract(self):
        repository = Mock()
        repository.load_page.return_value = [row(i, video_type=kind) for i, kind in enumerate(
            ("long", "short", "stream", None), 1)]
        result = build_channel_publications(repository, CHANNEL, now=NOW)
        self.assertEqual([item["video_type"] for item in result["videos"]], ["long", "short", "stream", None])
        self.assertTrue(all(item["scheduled_start_at"] is None for item in result["videos"]))
        self.assertEqual(result["source"], dict(kind="stored_publication_observations", youtube_requests=False, database_writes=False))
        self.assertEqual({call[0] for call in repository.mock_calls}, {"load_page"})

    def test_future_and_past_api_dates_do_not_infer_a_new_state(self):
        repository = Mock()
        for days in (-5, 90):
            repository.load_page.return_value = [row(scheduled_start_at=iso_utc(NOW + timedelta(days=days)))]
            result = build_channel_publications(repository, CHANNEL, now=NOW)["videos"][0]
            self.assertEqual(result["scheduled_start_at"], iso_utc(NOW + timedelta(days=days)))
            self.assertEqual(result["publication_status"], "upcoming")
            self.assertNotIn("published_at", result)

    def test_page_is_bounded_and_cursor_is_last_visible_id(self):
        repository = Mock()
        repository.load_page.return_value = [row(i) for i in range(1, 52)]
        result = build_channel_publications(repository, CHANNEL, now=NOW)
        self.assertEqual(len(result["videos"]), 50)
        self.assertEqual(result["next_cursor"], "00000000050")
        repository.load_page.return_value = [row(51)]
        result = build_channel_publications(repository, CHANNEL, cursor="00000000050", now=NOW)
        self.assertIsNone(result["next_cursor"])
        repository.load_page.assert_called_with(CHANNEL, cursor="00000000050")

    def test_invalid_or_expired_observations_are_not_exposed(self):
        changes = (
            dict(publication_status="completed"), dict(video_type="premiere"),
            dict(checked_at=iso_utc(NOW + timedelta(seconds=1))),
            dict(checked_at=iso_utc(NOW - HOT_DATA_WINDOW - timedelta(seconds=1))),
            dict(source_expires_at=iso_utc(NOW)), dict(source_expires_at=None),
            dict(scheduled_start_at="not a date"), dict(title=["invalid"]),
        )
        for change in changes:
            with self.subTest(change=change):
                repository = Mock()
                repository.load_page.return_value = [row(**change)]
                with self.assertRaises(PublicationReadError):
                    build_channel_publications(repository, CHANNEL, now=NOW)

    def test_repeated_or_backwards_page_ids_are_rejected(self):
        repository = Mock()
        for rows in ([row(2), row(1)], [row(1), row(1)]):
            repository.load_page.return_value = rows
            with self.assertRaises(PublicationReadError):
                build_channel_publications(repository, CHANNEL, now=NOW)

    def test_repository_reads_protected_sources_without_analytics_or_writes(self):
        database = Mock()
        database.read_rows.return_value = [row()]
        repository = ChannelPublicationsRepository(database, read_clock=lambda: NOW)
        self.assertEqual(len(repository.load_page(CHANNEL, cursor="00000000000")), 1)
        call = database.read_rows.call_args
        statement = call.args[1]
        self.assertIn("FROM readable.videos", statement)
        self.assertIn("JOIN readable.video_inventory", statement)
        self.assertIn("v.availability_status IN ('public','unlisted')", statement)
        self.assertIn("WITH waiting AS MATERIALIZED", statement)
        self.assertIn("FROM readable.video_inventory_type_resolution", statement)
        self.assertIn("proof.resolution_status='resolved'", statement)
        self.assertIn("proof.canonical_video_type=waiting.inventory_type", statement)
        self.assertIn("least(waiting.inventory_expires_at,proof.source_expires_at)", statement)
        self.assertNotIn("hot_video_inventory", statement)
        self.assertNotIn("video_snapshots", statement)
        self.assertEqual(call.args[2], (CHANNEL, "00000000000", "00000000000", 51))
        self.assertEqual(call.kwargs["max_rows"], 51)
        self.assertEqual({entry[0] for entry in database.mock_calls}, {"read_rows"})

    def test_read_crossing_its_source_deadline_is_rejected(self):
        database = Mock()
        database.read_rows.return_value = [row(source_expires_at=iso_utc(NOW))]
        with self.assertRaises(HotSourceReadExpired):
            ChannelPublicationsRepository(database, read_clock=lambda: NOW).load_page(CHANNEL)

    def route(self, database, visibility):
        router = create_explorer_router(cache=cache_stub(), core=SimpleNamespace(), database=database,
            library=library_stub(load_visible_channel_or_404=visibility),
            CHANNEL_BASELINE_ANALYTICS_ENABLED=False, CHANNEL_DERIVED_INSIGHTS_ENABLED=False,
            CHANNEL_LAUNCH_ANALYTICS_ENABLED=False, CHANNEL_TIMELINE_INSIGHTS_ENABLED=False)
        return endpoint(router, "channel_explorer_publications")

    def test_hidden_channel_is_rejected_before_publication_read(self):
        database = Mock()
        visibility = Mock(side_effect=HTTPException(status_code=404, detail="Channel not found"))
        with self.assertRaises(HTTPException) as caught:
            self.route(database, visibility)(CHANNEL, Response())
        self.assertEqual(caught.exception.status_code, 404)
        self.assertEqual(database.mock_calls, [])

    def test_read_only_route_does_not_require_analytics_and_never_caches(self):
        database = Mock()
        database.read_rows.return_value = []
        visibility = Mock(return_value={"channel_id": CHANNEL})
        response = Response()
        result = self.route(database, visibility)(CHANNEL, response)
        self.assertEqual(result["videos"], [])
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        visibility.assert_called_once_with(CHANNEL)
        self.assertEqual({entry[0] for entry in database.mock_calls}, {"read_rows"})

    def test_invalid_cursor_does_not_read_database(self):
        database, visibility = Mock(), Mock()
        with self.assertRaises(HTTPException) as caught:
            self.route(database, visibility)(CHANNEL, Response(), cursor="bad cursor")
        self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(database.mock_calls, [])
        visibility.assert_not_called()


if __name__ == "__main__":
    unittest.main()
