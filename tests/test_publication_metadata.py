"""Waiting profile metadata keeps optional fields and rejects poison rows early."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "application"), str(Path(__file__).resolve().parent)]
import test_initial_video_parallel as initial
import test_recurring_general_profiles as profiles
from test_publication_lifecycle import upcoming
from test_video_source_clocks import GENERAL, claim
from collection.video_payloads import build_video_identity_payload, build_video_metadata_payload
from video_collection_service import _parse_provider_timestamp


def complete_optional_profile(identity):
    item = upcoming(identity)
    item["snippet"].update(tags=["one", "два"], categoryId=" 22 ",
                           defaultAudioLanguage=" en ", defaultLanguage=" en-US ")
    item["topicDetails"] = {"topicCategories": ["https://en.wikipedia.org/wiki/Film"] * 2}
    item["contentDetails"]["caption"] = "true"
    return item


OPTIONAL_FIELDS = {
    "tags": ["one", "два"], "category_id": "22",
    "youtube_topic_categories": ["https://en.wikipedia.org/wiki/Film"],
    "default_audio_language": "en", "default_language": "en-US", "caption_available": True,
}
BAD_OPTIONAL_FIELDS = (
    ("snippet", "tags", "raw-sensitive-provider-value", "tags_invalid", "snippet.tags"),
    ("snippet", "tags", [1], "tags_invalid", "snippet.tags"),
    ("snippet", "tags", ["tag"] * 501, "tags_invalid", "snippet.tags"),
    ("snippet", "tags", ["x" * 25601], "tags_invalid", "snippet.tags"),
    ("snippet", "tags", ["\x00"], "tags_invalid", "snippet.tags"),
    ("snippet", "tags", ["\ud800"], "tags_invalid", "snippet.tags"),
    ("snippet", "categoryId", {}, "category_id_invalid", "snippet.categoryId"),
    (None, "topicDetails", [], "topic_details_invalid", "topicDetails"),
    ("topicDetails", "topicCategories", "raw-sensitive-provider-value", "topic_categories_invalid", "topicDetails.topicCategories"),
    ("topicDetails", "topicCategories", ["raw-sensitive-provider-value"], "topic_categories_invalid", "topicDetails.topicCategories"),
    ("snippet", "defaultAudioLanguage", [], "default_audio_language_invalid", "snippet.defaultAudioLanguage"),
    ("snippet", "defaultLanguage", "x" * 65, "default_language_invalid", "snippet.defaultLanguage"),
    ("snippet", "defaultLanguage", "\x00", "default_language_invalid", "snippet.defaultLanguage"),
    ("contentDetails", "caption", "raw-sensitive-provider-value", "caption_invalid", "contentDetails.caption"),
)


class PublicationMetadataTests(unittest.TestCase):
    def setUp(self):
        self.h = profiles.RecurringGeneralProfileTests("runTest")
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)

    def test_waiting_all_types_preserve_normalized_optional_metadata(self):
        kinds = ("long", "short", "stream", "unknown")
        self.h.items = [complete_optional_profile(kind) for kind in kinds]
        result, calls = self.h.process([claim(kind, kind, "identity_refresh" if kind == "unknown" else "metadata") for kind in kinds])
        self.assertEqual((result["publication_waiting"], result["updated"], result["errors"]), (4, 0, 0))
        self.assertEqual(calls, ["general"])
        for kind in kinds:
            operation, row = self.h.rows()[kind]
            self.assertEqual(operation, "lifecycle")
            for key, value in OPTIONAL_FIELDS.items():
                self.assertEqual(row[key], value, (kind, key))
            self.assertIsNone(row["duration"])
            self.assertNotIn("views", row)
        self.h.stats.assert_not_called()

    def test_identity_requires_duration_and_waiting_opt_in_still_rejects_invalid_duration(self):
        item = complete_optional_profile("identity")
        channel = item["snippet"]["channelId"]
        for value in (None, "absent"):
            with self.subTest(value=value):
                if value is None:
                    item["contentDetails"]["duration"] = None
                else:
                    item["contentDetails"].pop("duration", None)
                with self.assertRaisesRegex(ValueError, "duration_missing"):
                    build_video_identity_payload(channel, item, GENERAL.isoformat(), parse_dt=_parse_provider_timestamp)
                row = build_video_metadata_payload(channel, item, GENERAL.isoformat(),
                    parse_dt=_parse_provider_timestamp, allow_missing_duration=True)
                self.assertIsNone(row["duration"])
        for duration in ("PT0.5S", " PT1M", 10, ""):
            with self.subTest(duration=duration):
                item["contentDetails"]["duration"] = duration
                with self.assertRaisesRegex(ValueError, "duration_invalid"):
                    build_video_metadata_payload(channel, item, GENERAL.isoformat(),
                        parse_dt=_parse_provider_timestamp, allow_missing_duration=True)
        item["contentDetails"]["duration"] = "PT1M"
        row = build_video_identity_payload(channel, item, GENERAL.isoformat(), parse_dt=_parse_provider_timestamp)
        for key, value in OPTIONAL_FIELDS.items():
            self.assertEqual(row[key], value)

    def test_bad_optional_field_is_one_diagnosed_refusal_and_other_items_complete(self):
        for publication in ("upcoming", "none"):
            for container, field, value, reason, phase in BAD_OPTIONAL_FIELDS:
                with self.subTest(publication=publication, field=field, reason=reason, value_type=type(value).__name__):
                    self.h.repo.writes.clear()
                    bad = complete_optional_profile("bad")
                    bad["snippet"]["liveBroadcastContent"] = publication
                    if publication == "none":
                        bad["contentDetails"]["duration"] = "PT1M"
                        bad["statistics"] = {"viewCount": "4"}
                    target = bad if container is None else bad.setdefault(container, {})
                    target[field] = deepcopy(value)
                    self.h.items = [bad, complete_optional_profile("waiting"), initial.video("ready")]
                    with patch("collection.video_trace.event") as observed:
                        result, calls = self.h.process([claim("bad"), claim("waiting"), claim("ready")])
                    self.assertEqual(calls, ["general"])
                    self.assertEqual((result["errors"], result["publication_waiting"], result["updated"]), (1, 1, 1))
                    self.assertFalse(result["settlement_unknown"])
                    self.assertEqual(self.h.rows()["bad"][0], "failure")
                    diagnostics = [call for call in observed.call_args_list if call.args[:2] == ("video_validation", "failed")]
                    self.assertEqual(len(diagnostics), 1)
                    self.assertEqual(diagnostics[0].kwargs["reason"], reason)
                    self.assertEqual(diagnostics[0].kwargs["phase"], phase)
                    self.assertNotIn("raw-sensitive-provider-value", json.dumps(diagnostics[0].kwargs))
                    self.assertNotIn("detail", diagnostics[0].kwargs)
        self.h.stats.assert_not_called()

    def test_initial_atomic_batch_keeps_waiting_fields_and_one_bad_item_does_not_abort_plan(self):
        bad = complete_optional_profile("bad")
        bad["snippet"]["tags"] = [None]
        repo = initial.AtomicRepository()
        collector = initial.service(repo, Mock(return_value=[bad, complete_optional_profile("waiting"), initial.video("ready")]))
        result = initial.run(collector, ["bad", "waiting", "ready"])
        self.assertEqual(len(repo.commits), 1)
        rows = {row["video_id"]: (command["operation"], row) for command in repo.commits[0] for row in command["rows"]}
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows["bad"][0], "fail_batch")
        self.assertEqual(rows["waiting"][0], "complete_stream_lifecycle")
        for key, value in OPTIONAL_FIELDS.items():
            self.assertEqual(rows["waiting"][1][key], value)
        self.assertEqual((result["errors"], result["publication_waiting"], result["updated"]), (1, 1, 1))
        self.assertFalse(result["settlement_unknown"])
        collector.forbidden.assert_not_called()

    def test_retained_typed_route_rejects_bad_optional_without_aborting_good_waiting_item(self):
        self.h.repo.fail_typed = Mock(side_effect=lambda kind, rows: self.h.repo.record("typed_failure", rows))
        for kind in ("short", "stream"):
            with self.subTest(kind=kind):
                self.h.repo.writes.clear()
                bad = complete_optional_profile("bad")
                bad["contentDetails"]["caption"] = []
                self.h.collector.fetch_full = Mock(return_value=[bad, complete_optional_profile("good")])
                result = self.h.collector.process_typed_claims(
                    [claim("bad", kind), claim("good", kind)], video_type=kind, reason="optional_metadata_test")
                self.assertEqual((result["errors"], result["publication_waiting"]), (1, 1))
                self.assertFalse(result["settlement_unknown"])
                self.assertEqual(self.h.rows()["bad"][0], "typed_failure")
                for key, value in OPTIONAL_FIELDS.items():
                    self.assertEqual(self.h.rows()["good"][1][key], value)


if __name__ == "__main__":
    unittest.main()
