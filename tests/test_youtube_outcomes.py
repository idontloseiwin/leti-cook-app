"""Additive F6 error classification. Existing consumer semantics stay unchanged."""
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "application"))
from youtube_errors import (
    bounded_retry_after, classify_youtube_outcome, provider_refusal_code,
    is_youtube_daily_quota_exhausted,
)


def body(*reasons, status=None, code=None):
    return {"error": {"errors": [{"reason": value} for value in reasons], "status": status, "code": code}}


class OutcomeTests(unittest.TestCase):
    def outcome(self, status, payload=None, **kwargs):
        return classify_youtube_outcome(status, payload, quota_bucket="general", **kwargs)

    def test_five_distinct_outcomes_and_request_refusals(self):
        cases = [
            (403, body("quotaExceeded"), "daily_quota"),
            (429, body("dailyLimitExceeded", "rateLimitExceeded"), "daily_quota"),
            (403, body("rateLimitExceeded"), "rate_limit"),
            (403, body("userRateLimitExceeded"), "rate_limit"),
            (429, {}, "rate_limit"),
            (403, body(status="RESOURCE_EXHAUSTED"), "rate_limit"),
            (401, {}, "key_refused"),
            (400, body("API_KEY_INVALID"), "key_refused"),
            (403, body("accessNotConfigured"), "key_refused"),
            (404, body("playlistNotFound"), "resource_missing"),
            (None, None, "transient"),
            (503, {}, "transient"),
            (408, {}, "transient"),
            (400, body("invalidParameter"), None),
            (403, body("playlistItemsNotAccessible"), None),
            (200, body("quotaExceeded"), None),
        ]
        for status, payload, kind in cases:
            with self.subTest(status=status, payload=payload):
                outcome = self.outcome(status, payload)
                self.assertEqual(outcome.kind if outcome else None, kind)

    def test_key_reason_precedence_and_safe_details(self):
        value = self.outcome(400, {"error": {"details": [{"reason": "API_KEY_INVALID"}]}})
        self.assertEqual(value.key_code, "provider_key_rejected")
        value = self.outcome(403, body("accessNotConfigured", "quotaExceeded"))
        self.assertEqual((value.kind, value.key_code), ("key_refused", "provider_access_denied"))
        self.assertIsNone(value.retry_after_seconds)

    def test_buckets_are_separate_and_generic_404_does_not_prove_playlist_absence(self):
        value = classify_youtube_outcome(403, body("quotaExceeded"), quota_bucket="video_batch_get_stats")
        self.assertEqual((value.kind, value.quota_bucket), ("daily_quota", "video_batch_get_stats"))
        self.assertIsNone(self.outcome(404, {}).resource)
        self.assertEqual(self.outcome(404, body("playlistNotFound")).resource, "playlist")
        self.assertIsNone(self.outcome(404, body("playlistNotFound", "videoNotFound")).resource)

    def test_output_never_copies_source_payload_or_message(self):
        secret = "synthetic-secret-url-token"
        value = self.outcome(429, {"error": {"message": secret, "errors": [{"reason": secret}]}})
        self.assertNotIn(secret, repr(asdict(value)))
        self.assertEqual(set(asdict(value)), {"kind", "quota_bucket", "retry_after_seconds", "key_code", "resource"})

    def test_malformed_payload_is_bounded_and_does_not_raise(self):
        for payload in (None, [], "raw", {"error": None}, {"error": {"errors": "bad"}},
                        {"error": {"details": [None, {"reason": {}}, {"reason": []}, {"reason": "x"*500}]}}):
            with self.subTest(payload=payload):
                self.assertEqual(self.outcome(429, payload).kind, "rate_limit")

    def test_retry_after_is_pure_bounded_and_requires_clock_for_date(self):
        now = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)
        cases = [("0", 0), ("17", 17), ("999999", 60), ("-1", None), ("1.5", None),
                 ("x"*129, None), (None, None), (True, None),
                 ("Sat, 10 Oct 2026 12:00:20 GMT", 20),
                 ("Sat, 10 Oct 2026 11:00:00 GMT", 0)]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(bounded_retry_after(value, now=now), expected)
        self.assertIsNone(bounded_retry_after("Sat, 10 Oct 2026 12:00:20 GMT"))
        self.assertIsNone(bounded_retry_after("Sat, 10 Oct 2026 12:00:20 GMT", now=now.replace(tzinfo=None)))
        self.assertEqual(self.outcome(429, {}, retry_after="5").retry_after_seconds, 5)
        self.assertIsNone(self.outcome(403, body("quotaExceeded"), retry_after="5").retry_after_seconds)

    def test_invalid_caller_contract_is_rejected(self):
        for status in (True, "429", 99, 600):
            with self.subTest(status=status), self.assertRaises(ValueError):
                self.outcome(status)
        for bucket in ("unknown", None, [], True):
            with self.subTest(bucket=bucket), self.assertRaises(ValueError):
                classify_youtube_outcome(429, {}, quota_bucket=bucket)

    def test_daily_predicate_does_not_convert_transient_rate_to_daily(self):
        self.assertEqual(provider_refusal_code(403, body("rateLimitExceeded")), "provider_unavailable")
        self.assertFalse(is_youtube_daily_quota_exhausted(
            RuntimeError("reason=rateLimitExceeded"), is_batch_stats_quota_error=lambda _: False))


if __name__ == "__main__":
    unittest.main()
