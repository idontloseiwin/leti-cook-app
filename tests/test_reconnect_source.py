"""Provider-only reconnect source with a bounded fake transport; no DB/network."""
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from types import SimpleNamespace
import copy
import sys
import unittest
from unittest.mock import Mock

from requests.exceptions import ConnectionError as RequestsConnectionError, Timeout, ChunkedEncodingError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "application"))
from channels.reconnect_source import MAX_RESPONSE_BYTES, ReconnectSource
from channels.preparation import initial_channel_status
from channels.service import add_channel
from collection.channel_metadata import channel_metadata_update_values
from typed_playlist import typed_playlist_id
from youtube_client import YouTubeResponseTooLarge
from youtube_errors import ProviderOutcome, YouTubeAPIError, YouTubeDailyQuotaExceededError, YouTubeRateLimitError

CHANNEL = "UC" + "a" * 22
OTHER = "UC" + "b" * 22
VIDEO = "v" * 11
NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
PROFILE = {
    "id": CHANNEL, "snippet": {"title": "Synthetic channel", "country": "ua",
        "publishedAt": "2020-01-01T00:00:00Z", "thumbnails": {"default": {"url": "https://example.invalid/a"}}},
    "statistics": {"videoCount": "15", "subscriberCount": "42", "viewCount": "90"},
    "contentDetails": {"relatedPlaylists": {"uploads": "UU" + "a" * 22}},
    "brandingSettings": {},
}


def response(payload, status=200, retry=None):
    return SimpleNamespace(status_code=status, json=lambda: payload,
                           headers={} if retry is None else {"Retry-After": retry})


def refusal(status, reason=None, **kwargs):
    return response({"error": {"errors": [{"reason": reason}] if reason else [], **kwargs}}, status)


class ReconnectSourceTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.calls = []
        self.cancel = Event()
        self.answers = []
        self.after = None
        self.source = ReconnectSource(self.request, metadata_update_values=channel_metadata_update_values,
            clock=lambda: self.now, utc_clock=self.utc)

    def utc(self):
        return NOW + timedelta(seconds=self.now - 100)

    def request(self, resource, **kwargs):
        self.calls.append((resource, kwargs))
        answer = self.answers.pop(0)
        self.now += 2
        if self.after:
            self.after(len(self.calls))
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def success(self, *, video=False, counts=(10, 3, 2), profile=None):
        self.answers = ([response({"items": [{"snippet": {"channelId": CHANNEL}}]})] if video else [])
        self.answers += [response({"items": [copy.deepcopy(PROFILE if profile is None else profile)]})]
        self.answers += [response({"pageInfo": {"totalResults": value}}) for value in counts]

    def resolve(self, locator=CHANNEL, deadline=300):
        return self.source(locator, cancelled=self.cancel, deadline=deadline)

    def test_id_profile_and_exact_three_typed_count_reads(self):
        self.success(counts=("10", 3, 2))
        result = self.resolve()
        self.assertIsNone(result.code)
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(self.calls[0][0], "channels")
        self.assertEqual(self.calls[0][1]["params"]["id"], CHANNEL)
        for kind, (endpoint, kwargs) in zip(("long", "short", "stream"), self.calls[1:]):
            self.assertEqual(endpoint, "playlistItems")
            self.assertEqual(kwargs["params"], {"part": "id", "playlistId": typed_playlist_id(CHANNEL, kind),
                                               "maxResults": 1, "fields": "pageInfo(totalResults)"})
        for _, kwargs in self.calls:
            self.assertEqual((kwargs["quota_bucket"], kwargs["quota_units"]), ("general", 1))
            self.assertEqual(kwargs["max_response_bytes"], MAX_RESPONSE_BYTES)
            self.assertIs(kwargs["cancelled"], self.cancel)
            self.assertEqual(kwargs["deadline"], 300)
            self.assertLessEqual(kwargs["timeout_seconds"], 10)
        self.assertEqual(result.channel["initial_inventory_estimate"]["counts"],
                         {"long": 10, "short": 3, "stream": 2})
        self.assertEqual(result.channel["user_input"], "https://www.youtube.com/channel/" + CHANNEL)

    def test_handle_and_video_canonical_locators(self):
        for locator, video in (("https://www.youtube.com/@synthetic/videos?extra=discard", False),
                               ("https://youtu.be/" + VIDEO + "?extra=discard", True)):
            with self.subTest(video=video):
                self.setUp()
                self.success(video=video)
                result = self.resolve(locator)
                self.assertIsNone(result.code)
                self.assertEqual(len(self.calls), 5 if video else 4)
                self.assertEqual(self.calls[0][0], "videos" if video else "channels")
                self.assertEqual(self.calls[0][1]["params"]["id" if video else "forHandle"],
                                 VIDEO if video else "@synthetic")
                self.assertEqual(result.channel["user_input"], "https://www.youtube.com/" +
                                 ("watch?v=" + VIDEO if video else "@synthetic"))

    def test_invalid_or_legacy_input_does_not_send(self):
        for locator, code in (("", "needs_input"), ("https://example.invalid/a", "needs_input"),
                              ("https://www.youtube.com/c/legacy", "unsupported_legacy_channel_link")):
            self.assertEqual(self.resolve(locator).code, code)
        self.assertEqual(self.calls, [])

    def test_cancel_or_expired_before_first_call_is_unsent(self):
        self.cancel.set()
        self.assertFalse(self.resolve().dispatched)
        self.cancel.clear()
        self.assertFalse(self.resolve(deadline=100).dispatched)
        self.assertEqual(self.calls, [])

    def test_cancel_after_each_call_stops_without_presenting_partial_data(self):
        for stop in range(1, 6):
            with self.subTest(stop=stop):
                self.setUp()
                self.success(video=True)
                self.after = lambda count: self.cancel.set() if count == stop else None
                result = self.resolve("https://youtu.be/" + VIDEO)
                self.assertTrue(result.dispatched)
                self.assertEqual(result.code, "provider_unavailable")
                self.assertEqual(len(self.calls), stop)
                self.assertIsNone(result.channel)

    def test_deadline_after_each_call_stops_and_timeout_is_remaining_budget(self):
        for stop in range(1, 6):
            with self.subTest(stop=stop):
                self.setUp()
                self.success(video=True)
                result = self.resolve("https://youtu.be/" + VIDEO, deadline=100 + 2 * stop - 1)
                self.assertEqual(result.code, "provider_unavailable")
                self.assertEqual(len(self.calls), stop)
                self.assertEqual(self.calls[-1][1]["timeout_seconds"], 1)
                self.assertIsNone(result.channel)

    def test_cancel_during_clock_capture_prevents_request(self):
        def utc():
            self.cancel.set()
            return NOW
        self.source.utc_clock = utc
        self.assertFalse(self.resolve().dispatched)
        self.assertEqual(self.calls, [])

    def test_explicit_refusals_at_every_stage_stop_remaining_calls(self):
        cases = [
            (refusal(403, "quotaExceeded"), "provider_quota_exceeded"),
            (response({"error": {"status": "RESOURCE_EXHAUSTED"}}, 403, "7"), "provider_rate_limited"),
            (refusal(400, "API_KEY_INVALID"), "provider_key_rejected"),
            (refusal(403, "SERVICE_DISABLED"), "provider_access_denied"),
            (refusal(503), "provider_unavailable"),
        ]
        for answer, expected in cases:
            for index in range(5):
                with self.subTest(code=expected, stage=index):
                    self.setUp()
                    self.success(video=True)
                    self.answers[index] = answer
                    result = self.resolve("https://youtu.be/" + VIDEO)
                    self.assertEqual(result.code, expected)
                    self.assertEqual(len(self.calls), index + 1)
                    self.assertIsNone(result.channel)
                    if expected == "provider_rate_limited":
                        self.assertEqual(result.retry_seconds, 7)

    def test_typed_transport_refusals_are_preserved(self):
        cases = [
            (ProviderOutcome("daily_quota", "general"), "provider_quota_exceeded", None),
            (ProviderOutcome("rate_limit", "general", retry_after_seconds=0), "provider_rate_limited", 1),
            (ProviderOutcome("key_refused", "general", key_code="provider_key_rejected"), "provider_key_rejected", None),
            (ProviderOutcome("transient", "general", retry_after_seconds=2), "provider_unavailable", 2),
        ]
        for outcome, code, delay in cases:
            with self.subTest(code=code):
                self.setUp()
                self.answers = [outcome]
                result = self.resolve()
                self.assertEqual((result.code, result.retry_seconds), (code, delay))
                self.assertEqual(len(self.calls), 1)

    def test_generic_404_is_never_missing_or_empty(self):
        for index in range(5):
            with self.subTest(stage=index):
                self.setUp()
                self.success(video=True)
                self.answers[index] = refusal(404)
                result = self.resolve("https://youtu.be/" + VIDEO)
                self.assertEqual(result.code, "channel_lookup_unavailable")
                self.assertIsNone(result.channel)
                self.assertEqual(len(self.calls), index + 1)

    def test_exact_absent_typed_playlist_keeps_null_count_and_all_three_sources(self):
        self.success()
        self.answers[2] = refusal(404, "playlistNotFound")
        result = self.resolve()
        estimate = result.channel["initial_inventory_estimate"]
        self.assertEqual(len(self.calls), 4)
        self.assertIsNone(estimate["counts"]["short"])
        self.assertEqual(estimate["statuses"]["short"], "playlist_not_found")
        self.assertEqual(result.channel["initial_long_inventory_status"], "pending")
        self.assertIsNotNone(estimate["checked_at_by_type"]["short"])

    def test_exact_missing_video_channel_and_empty_results(self):
        for video, answer in ((False, refusal(404, "channelNotFound")),
                              (True, refusal(404, "videoNotFound")),
                              (False, response({"items": []})), (True, response({"items": []}))):
            with self.subTest(video=video):
                self.setUp()
                self.answers = [answer]
                self.assertEqual(self.resolve("https://youtu.be/" + VIDEO if video else CHANNEL).code,
                                 "channel_not_found")
                self.assertEqual(len(self.calls), 1)

    def test_zero_list_envelopes_with_or_without_items_stop_before_counts(self):
        for video in (False, True):
            for explicit_items in (False, True):
                for results_per_page in (0, 5):
                    with self.subTest(video=video, explicit_items=explicit_items, results_per_page=results_per_page):
                        self.setUp()
                        payload = {"kind": "youtube#videoListResponse" if video else "youtube#channelListResponse",
                                   "etag": "synthetic", "pageInfo": {"totalResults": 0,
                                                                    "resultsPerPage": results_per_page}}
                        if explicit_items:
                            payload["items"] = []
                        self.answers = [response(payload)]
                        result = self.resolve("https://youtu.be/" + VIDEO if video else CHANNEL)
                        self.assertEqual(result.code, "channel_not_found")
                        self.assertIsNone(result.channel)
                        self.assertEqual([call[0] for call in self.calls], ["videos" if video else "channels"])

    def test_single_results_do_not_require_results_per_page_equal_item_count(self):
        for video in (False, True):
            with self.subTest(video=video):
                self.setUp()
                self.success(video=video)
                for index in range(2 if video else 1):
                    payload = self.answers[index].json()
                    payload.update(kind="youtube#videoListResponse" if video and index == 0
                                   else "youtube#channelListResponse",
                                   pageInfo={"totalResults": 1, "resultsPerPage": 5})
                result = self.resolve("https://youtu.be/" + VIDEO if video else CHANNEL)
                self.assertIsNone(result.code)
                self.assertEqual(result.channel["channel_id"], CHANNEL)
                self.assertEqual(len(self.calls), 5 if video else 4)

    def test_malformed_or_contradictory_list_envelopes_are_not_missing(self):
        for video in (False, True):
            kind = "youtube#videoListResponse" if video else "youtube#channelListResponse"
            zero = {"kind": kind, "pageInfo": {"totalResults": 0, "resultsPerPage": 0}}
            bad = [None, [], {}, {"pageInfo": {"totalResults": 0}}, {"kind": kind},
                   dict(zero, items=None), dict(zero, items={}), dict(zero, pageInfo=None),
                   dict(zero, kind="youtube#playlistListResponse"), dict(zero, error={}),
                   dict(zero, nextPageToken="synthetic"), dict(zero, prevPageToken="synthetic"),
                   dict(zero, items=[PROFILE]),
                   dict(zero, items=[], pageInfo={"totalResults": 1, "resultsPerPage": 0})]
            bad += [dict(zero, pageInfo={"totalResults": value}) for value in (None, True, "0", 0.0, -1, 1)]
            bad += [dict(zero, pageInfo={"totalResults": 0, "resultsPerPage": value})
                    for value in (None, False, "0", -1)]
            for payload in bad:
                with self.subTest(video=video, payload=payload):
                    self.setUp()
                    self.answers = [response(payload)]
                    result = self.resolve("https://youtu.be/" + VIDEO if video else CHANNEL)
                    self.assertEqual(result.code, "channel_lookup_unavailable")
                    self.assertIsNone(result.channel)
                    self.assertEqual(len(self.calls), 1)

    def test_invalid_json_does_not_prove_missing(self):
        self.answers = [SimpleNamespace(status_code=200, headers={},
                                       json=Mock(side_effect=ValueError("synthetic invalid JSON")))]
        self.assertEqual(self.resolve().code, "channel_lookup_unavailable")
        self.assertEqual(len(self.calls), 1)

    def test_requests_transport_errors_at_each_stage_defer_without_partial_data(self):
        for error_type in (Timeout, RequestsConnectionError, ChunkedEncodingError, YouTubeResponseTooLarge):
            for index in range(5):
                with self.subTest(error=error_type.__name__, stage=index):
                    self.setUp()
                    self.success(video=True)
                    self.answers[index] = error_type("private provider URL")
                    result = self.resolve("https://youtu.be/" + VIDEO)
                    self.assertEqual((result.code, result.retry_seconds), ("provider_unavailable", 60))
                    self.assertIsNone(result.channel)
                    self.assertNotIn("private", repr(result))
                    self.assertEqual(len(self.calls), index + 1)

    def test_malformed_count_stops_without_authorizing_manual_or_auto_row(self):
        for count in (None, True, -1, 1.5, " 3", "1e3", "9" * 100, "١", 2147483648):
            with self.subTest(count=count):
                self.setUp()
                self.success(counts=(count, 3, 2))
                result = self.resolve()
                self.assertEqual(result.code, "initial_inventory_count_unavailable")
                self.assertEqual(len(self.calls), 2)
                self.assertIsNone(result.channel)

    def test_malformed_or_wrong_profile_identity_does_not_send_counts(self):
        bad = ({}, {"items": {}}, {"items": [PROFILE, PROFILE]}, {"items": [dict(PROFILE, id=OTHER)]},
               {"items": [dict(PROFILE, id="@not-a-provider-id")]},
               {"items": [dict(PROFILE, statistics=[])]})
        for payload in bad:
            with self.subTest(payload=payload):
                self.setUp()
                self.answers = [response(payload)]
                self.assertEqual(self.resolve().code, "channel_lookup_unavailable")
                self.assertEqual(len(self.calls), 1)

    def test_malformed_video_identity_stops_before_profile(self):
        self.answers = [response({"items": [{"snippet": {"channelId": "@synthetic"}}]})]
        self.assertEqual(self.resolve("https://youtu.be/" + VIDEO).code, "channel_lookup_unavailable")
        self.assertEqual(len(self.calls), 1)

    def test_transport_failures_are_fixed_and_never_retried(self):
        for error, code in ((TimeoutError("private payload"), "provider_unavailable"),
                            (ConnectionError("private url"), "provider_unavailable"),
                            (YouTubeAPIError("private title"), "provider_unavailable"),
                            (YouTubeDailyQuotaExceededError("private key"), "provider_quota_exceeded")):
            with self.subTest(code=code):
                self.setUp()
                self.answers = [error]
                result = self.resolve()
                self.assertEqual(result.code, code)
                self.assertNotIn("private", repr(result))
                self.assertEqual(len(self.calls), 1)


    def test_typed_rate_exception_preserves_bounded_delay_at_each_stage_without_retry(self):
        for delay in (1, 17, 60):
            for index in range(5):
                with self.subTest(delay=delay, stage=index):
                    self.setUp()
                    self.success(video=True)
                    self.answers[index] = YouTubeRateLimitError(retry_after_seconds=delay)
                    result = self.resolve("https://youtu.be/" + VIDEO)
                    self.assertEqual((result.code, result.retry_seconds), ("provider_rate_limited", delay))
                    self.assertEqual(len(self.calls), index + 1)
                    self.assertIsNone(result.channel)

    def test_unknown_transport_bug_is_not_retried_or_converted_to_missing(self):
        self.answers = [RuntimeError("synthetic adapter bug")]
        with self.assertRaises(RuntimeError):
            self.resolve()
        self.assertEqual(len(self.calls), 1)

    def test_source_clocks_are_acquisition_times_and_not_later_queue_time(self):
        self.success()
        result = self.resolve()
        row = copy.deepcopy(result.channel)
        self.assertEqual(row["channel_metadata_checked_at"], NOW.isoformat())
        self.assertEqual(row["added_at"], NOW.isoformat())
        estimate = row["initial_inventory_estimate"]
        self.assertEqual(estimate["checked_at"], (NOW + timedelta(seconds=2)).isoformat())
        self.assertEqual(estimate["checked_at_by_type"], dict(
            zip(("long", "short", "stream"), ((NOW + timedelta(seconds=i)).isoformat() for i in (2, 4, 6)))))
        self.now += 10000  # simulate a coordinator wait; no source stamps are renewed
        self.assertEqual(result.channel, row)

    def test_prepared_rows_equal_existing_add_service_at_threshold_and_channel_total(self):
        for counts, total, expected in (
            ((9999, 0, 0), "9999", "pending"), ((10000, 0, 0), "10000", "pending"),
            ((9999, 1, 1), "10001", "manual_required"), ((2, 0, 0), "10001", "manual_required"),
            ((10001, 0, 0), None, "manual_required"), ((2, 0, 0), None, "pending"),
        ):
            with self.subTest(counts=counts, total=total):
                self.setUp()
                profile = copy.deepcopy(PROFILE)
                if total is None:
                    profile["statistics"].pop("videoCount")
                else:
                    profile["statistics"]["videoCount"] = total
                self.success(counts=counts, profile=profile)
                result = self.resolve()
                row = result.channel
                self.assertEqual(row["initial_long_inventory_status"], expected)
                service_result = add_channel(CHANNEL, deployment_profile="self-hosted",
                    repository=SimpleNamespace(load_channel_record=lambda identity: None),
                    trash_repository=Mock(), lookup_cache=SimpleNamespace(get=lambda *a: (False, None), put=lambda *a: None),
                    channel_lock=lambda *a: nullcontext(), parse_legacy_input=Mock(),
                    get_channel_id_from_video=Mock(), lookup_channel_response=lambda params: response({"items": [profile]}),
                    estimate_initial_inventory=lambda identity: row["initial_inventory_estimate"],
                    metadata_update_values=channel_metadata_update_values, prepare_hydration=Mock(), on_change=Mock(),
                    clock=lambda: NOW, prepare_only=True)
                self.assertEqual(service_result, {"status": "prepared", "channel": row})

    def test_unknown_count_policy_remains_manual_in_shared_legacy_helper(self):
        estimate = dict(scope="all", video_types=["long", "short", "stream"],
                        counts=dict(long=2, short=None, stream=0), statuses=dict(long="ok", short="error", stream="ok"))
        self.assertEqual(initial_channel_status(PROFILE, estimate), "manual_required")

    def test_retry_date_uses_observation_clock_and_daily_wins_rate(self):
        self.answers = [response({"error": {"status": "RESOURCE_EXHAUSTED"}}, 403, "Sat, 10 Oct 2026 12:00:09 GMT")]
        self.assertEqual(self.resolve().retry_seconds, 9)
        self.answers = [response({"error": {"status": "RESOURCE_EXHAUSTED",
                                         "errors": [{"reason": "quotaExceeded"}]}}, 403, "1")]
        self.assertEqual(self.resolve().code, "provider_quota_exceeded")

    def test_invalid_configuration_and_clocks_fail_before_transport(self):
        for timeout in (True, 0, -1, 31, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                ReconnectSource(self.request, metadata_update_values=channel_metadata_update_values, timeout_seconds=timeout)
        for deadline in (True, 0, -1, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                self.resolve(deadline=deadline)
        self.source.utc_clock = lambda: NOW.replace(tzinfo=None)
        with self.assertRaisesRegex(ValueError, "reconnect_source_clock_invalid"):
            self.resolve()
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
