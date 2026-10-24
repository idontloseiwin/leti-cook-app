"""Pure payload boundaries, without bootstrap.

No application bootstrap is executed, database opened or provider contacted.
"""

import copy
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
APPLICATION = ROOT / "application"
sys.path.insert(0, str(APPLICATION))
from collection import video_payloads as payloads
from youtube_source_fields import clean_optional_text, parse_dt as PARSE_DT

NOW = "2026-09-27T10:00:00+00:00"
VIDEO = "video000001"
CHANNEL = "UC" + "a" * 22
TOPIC = "https://en.wikipedia.org/wiki/Film"
BUILDERS = ("build_video_stat_payload", "build_video_collection_metadata_payload")
HELPERS = {"youtube_topic_categories", "youtube_optional_text_field", "youtube_caption_available",
           "youtube_stream_actual_end_at", "clean_optional_text", "build_video_counter_payload"}
CONSTANTS = {"MAX_YOUTUBE_TOPIC_CATEGORIES", "MAX_YOUTUBE_TOPIC_CATEGORY_URL_LENGTH",
             "YOUTUBE_TOPIC_CATEGORY_URL_RE", "VIDEO_TITLE_MAX_CHARS", "VIDEO_TAGS_MAX_COUNT",
             "VIDEO_TAGS_MAX_BYTES"}


class Absent(Exception):
    pass


class Invalid(Exception):
    pass


class FixedClock(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls.fromisoformat(NOW)
        return value.astimezone(tz) if tz else value.replace(tzinfo=None)


def dependencies(flag=False):
    return dict(batch_stats_enabled=flag, parse_dt=PARSE_DT,
                view_count_absent_error=Absent, view_count_invalid_error=Invalid)


def video(**parts):
    value = {"id": VIDEO, "snippet": {"title": "Example", "publishedAt": "2026-09-20T10:00:00Z"},
             "statistics": {"viewCount": "42", "likeCount": "3", "commentCount": "1"},
             "contentDetails": {"duration": "PT3M"}}
    value.update(parts)
    return value



class PayloadTests(unittest.TestCase):
    def test_explicit_dependencies_are_required_and_error_identities_are_preserved(self):
        for name in BUILDERS:
            build = getattr(payloads, name)
            with self.subTest(builder=name), self.assertRaises(TypeError):
                build(CHANNEL, video(), NOW)
            for change in ({"batch_stats_enabled": None}, {"parse_dt": None},
                           {"view_count_absent_error": object}, {"view_count_invalid_error": ValueError()}):
                with self.subTest(builder=name, change=change), self.assertRaises(TypeError):
                    build(CHANNEL, video(), NOW, **(dependencies() | change))
            for value in (None, {}):
                with self.subTest(builder=name, statistics=value), self.assertRaises(Absent) as caught:
                    build(CHANNEL, video(statistics=value), NOW, **dependencies())
                self.assertIs(type(caught.exception), Absent)
            for value in (True, -1, "1.5", "9223372036854775808"):
                with self.subTest(builder=name, views=value), self.assertRaises(Invalid) as caught:
                    build(CHANNEL, video(statistics={"viewCount": value}), NOW, **dependencies())
                self.assertIs(type(caught.exception), Invalid)

    def test_complete_payload_normalizes_optional_fields_without_mutating_provider_item(self):
        item = video(snippet={"title": " Example ", "publishedAt": NOW, "tags": ["one", "two"],
                              "categoryId": 20, "defaultAudioLanguage": " en-US ", "defaultLanguage": " "},
                     statistics={"viewCount": "42", "likeCount": "bad", "commentCount": None},
                     topicDetails={"topicCategories": [" " + TOPIC + " ", TOPIC]},
                     contentDetails={"duration": "PT3M", "caption": " TRUE "},
                     status={"privacyStatus": "unlisted", "uploadStatus": "processed"})
        original = copy.deepcopy(item)
        result = payloads.build_video_collection_metadata_payload(CHANNEL, item, NOW, **dependencies())
        row = result["row"]
        self.assertEqual((row["title"], row["tags"], row["category_id"]), (" Example ", ["one", "two"], "20"))
        self.assertEqual((row["youtube_topic_categories"], row["default_audio_language"],
                          row["default_language"], row["caption_available"]), ([TOPIC], "en-US", None, True))
        self.assertEqual(result["snapshot"], {"video_id": VIDEO, "views": 42, "like_count": None,
                                              "comment_count": None, "recorded_at": NOW})
        self.assertEqual(result["availability_values"], {"availability_status": "unlisted",
                         "privacy_status": "unlisted", "upload_status": "processed", "availability_checked_at": NOW})
        self.assertNotIn("metadata_checked_at", row)
        self.assertEqual(item, original)

    def test_topic_language_and_caption_shape_boundaries(self):
        self.assertEqual(payloads.youtube_topic_categories({}), [])
        self.assertEqual(payloads.youtube_topic_categories({"topicDetails": {"topicCategories": [TOPIC] * 50}}), [TOPIC])
        for details in ([], {"topicCategories": "bad"}, {"topicCategories": [TOPIC] * 51},
                        {"topicCategories": ["https://example.org/wiki/Film"]}, {"topicCategories": [123]}):
            with self.subTest(details=details), self.assertRaises(ValueError):
                payloads.youtube_topic_categories({"topicDetails": details})
        self.assertEqual(payloads.youtube_optional_text_field({"language": "x" * 64}, "language"), "x" * 64)
        for value in ("x" * 65, 123):
            with self.subTest(language=value), self.assertRaises(ValueError):
                payloads.youtube_optional_text_field({"language": value}, "language")
        for value, expected in ((None, None), (False, False), (True, True), (" FALSE ", False), ("true", True)):
            self.assertIs(payloads.youtube_caption_available({"contentDetails": {"caption": value}}), expected)
        for value in (1, [], "yes"):
            with self.subTest(caption=value), self.assertRaises(ValueError):
                payloads.youtube_caption_available({"contentDetails": {"caption": value}})
        self.assertEqual([clean_optional_text(value) for value in (None, " ", " x ", 0)], [None, None, "x", "0"])

    def test_stream_evidence_preserves_timezone_and_stamps_metadata_with_batch_off(self):
        # Future evidence is filtered by the collection service, not this builder.
        for timestamp in ("2026-09-26T20:00:00+0300", "2027-01-01T00:00:00Z"):
            item = video(liveStreamingDetails={"actualEndTime": " " + timestamp + " "})
            built = payloads.build_video_collection_metadata_payload(CHANNEL, item, NOW, **dependencies(False))
            self.assertEqual(built["row"]["stream_actual_end_at"], timestamp)
            self.assertEqual(built["row"]["metadata_checked_at"], NOW)
        for value in ("2026-09-26T20:00:00", "garbage", "", 123):
            with self.subTest(timestamp=value), self.assertRaises(ValueError):
                payloads.youtube_stream_actual_end_at({"liveStreamingDetails": {"actualEndTime": value}}, parse_dt=PARSE_DT)

    def test_counter_payload_keeps_integer_conversion_and_counter_only_shape(self):
        for value, expected in (("42", 42), (4.9, 4), (True, 1)):
            built = payloads.build_video_counter_payload({"id": " " + VIDEO + " ", "views": value,
                                                         "like_count": "unchanged"}, NOW)
            self.assertEqual(built["row"], {"video_id": VIDEO, "views": expected, "checked_at": NOW,
                                            "like_count": "unchanged", "comment_count": None})
            self.assertNotIn("availability_values", built)
        for item in ({"views": 1}, {"id": VIDEO}, {"id": VIDEO, "views": "bad"}):
            with self.subTest(item=item), self.assertRaises(ValueError):
                payloads.build_video_counter_payload(item, NOW)

    def test_process_payload_binding_uses_snapshot_and_explicit_parser_error_identity(self):
        from collection.context import CollectionPolicy
        from collection.settings import CollectionSettings
        from runtime import process_collection as process
        from video_collection_service import YouTubeViewCountAbsent, YouTubeViewCountInvalid

        core = ModuleType("core")
        core.database = object()
        core.API_KEY, core.YOUTUBE_API_TIMEOUT_SECONDS = "fixed-key", 17
        core.YOUTUBE_QUOTA_ATTEMPT_RECORDER = Mock()
        with patch.dict(sys.modules, {"core": core, "sync_service": None}), \
                patch.object(payloads, "build_video_collection_metadata_payload",
                             wraps=payloads.build_video_collection_metadata_payload) as owner:
            for flag in (False, True):
                context = process.bind_process_collection(database=object(),
                    settings=CollectionSettings(True, True, flag), policy=CollectionPolicy(True, True, True, 24))
                core.YOUTUBE_BATCH_STATS_ENABLED = not flag
                _, service = context.video.build_service()
                result = service.build_full_payload(CHANNEL, video(), NOW)
                self.assertIs("metadata_checked_at" in result["row"], flag)
                self.assertIs(owner.call_args.kwargs["batch_stats_enabled"], flag)
                self.assertIs(owner.call_args.kwargs["parse_dt"], PARSE_DT)
                self.assertIs(owner.call_args.kwargs["view_count_absent_error"], YouTubeViewCountAbsent)
                self.assertIs(owner.call_args.kwargs["view_count_invalid_error"], YouTubeViewCountInvalid)
        core.YOUTUBE_QUOTA_ATTEMPT_RECORDER.close.assert_not_called()

    def test_clock_fallback_and_truthy_flags_are_explicit_in_the_pure_owner(self):
        with patch.object(payloads, "datetime", FixedClock):
            for flag in (False, True, 0, "enabled"):
                for now in (None, "", "2020-01-01T03:00:00+03:00"):
                    row = payloads.build_video_stat_payload(CHANNEL, video(), now, **dependencies(flag))["row"]
                    self.assertEqual(row["checked_at"], now or NOW)
                    self.assertIs("metadata_checked_at" in row, bool(flag))
            self.assertEqual(payloads.build_video_counter_payload({"id": VIDEO, "views": 1})["row"]["checked_at"], NOW)

    def test_import_is_independent_of_bootstrap_database_service_and_environment(self):
        code = r'''
import builtins, datetime, os, re, socket, sys, typing
sys.path.insert(0, APPLICATION)
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.split('.')[0] in {'core', 'sync_service', 'server', 'dotenv', 'db', 'runtime',
                             'video_collection_service', 'requests', 'httpx'}:
        raise AssertionError('runtime import: ' + name)
    return original(name, *args, **kwargs)
def forbidden(*args, **kwargs):
    raise AssertionError('environment or network access')
class Environment(dict):
    get = __getitem__ = forbidden
os.environ = Environment()
os.getenv = forbidden
socket.socket.connect = forbidden
builtins.__import__ = guarded
from collection import video_payloads
assert callable(video_payloads.build_video_stat_payload)
'''.replace("APPLICATION", repr(str(APPLICATION)), 1)
        result = subprocess.run([sys.executable, "-I", "-B", "-c", code], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
