"""Contract checks for one confirmed page and its dependent acquisitions."""
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "application"))
from provider_sources.channel_search import ChannelSearch, SEARCH, GENERAL, STATS
from provider_sources.service import AcquisitionService, ServiceRefused
from provider_sources.transport import Exchange


CHANNEL = "UC" + "a" * 22
FIRST, SECOND = "a" * 11, "b" * 11
SPEC = {"mode": "topic", "q": "miniatures", "stats": True}


def hit(vid):
    return {"id": {"kind": "youtube#video", "videoId": vid},
            "snippet": {"title": "Miniature", "channelId": CHANNEL, "channelTitle": "Studio"}}


def stats(videos):
    return {"items": [{"id": v, "statistics": {"viewCount": "0"}} for v in videos],
            "summary": {"requestedVideoCount": len(videos), "succeededVideoCount": len(videos),
                        "failedVideoCount": 0, "failedVideoIds": []}}


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.bodies = []
        self.calls = []
        self.now = 100
        plan_kind = {"youtube.search.list": "youtube#searchListResponse", "youtube.channels.list": "youtube#channelListResponse",
                     "youtube.videos.batchGetStats": "youtube#batchGetStatsResponse"}
        def send(plan, params, **kwargs):
            self.calls.append(plan.method_id)
            status, body = self.bodies.pop(0)
            if status == 200:
                body = {"kind": plan_kind[plan.method_id], **body}
            return Exchange(status, json.dumps(body).encode(), None, None, None)
        provider = AcquisitionService(attempt_recorder=None, ledger_reader=lambda *a, **k: ([], False),
                                      send=send, monotonic=lambda: self.now)
        self.search = ChannelSearch(provider, lambda keys: dict.fromkeys(keys, "tracked"))
        self.addCleanup(patch.stopall)
        patch("hot_runtime.require_hot_runtime_ready").start()

    def preview(self, spec, owner="A"):
        return self.search.preview(json.dumps(spec).encode(), owner)

    def run_action(self, ticket, owner="A"):
        body = {"action_id": ticket["action_id"], "confirmed_cost": ticket["max_cost"]}
        return self.search.execute(json.dumps(body).encode(), owner, threading.Event(), lambda: owner)

    def test_partial_retry_does_not_repeat_search_and_more_is_explicit(self):
        ticket = self.preview(SPEC)
        self.assertEqual(ticket["max_cost"], {SEARCH: 1, GENERAL: 1, STATS: 1})
        self.assertFalse(self.calls)
        self.bodies = [(200, {"items": [hit(FIRST), hit(SECOND)], "nextPageToken": "next"}),
                       (200, {"items": [{"id": CHANNEL, "snippet": {"title": "Studio"},
                                         "statistics": {"hiddenSubscriberCount": True, "subscriberCount": "9"}}]}),
                       (500, {"error": {"code": 500}})]
        result = self.run_action(ticket)
        self.assertEqual(len(result["hits"]), 2)
        self.assertEqual(result["library"], {CHANNEL: "tracked"})
        self.assertIsNone(result["profiles"][CHANNEL]["subscribers"])
        self.assertTrue(result["more"] and result["retry"])
        self.assertEqual(self.run_action(ticket), result)
        retry = self.preview({"parent": ticket["action_id"], "action": "retry"})
        self.assertEqual(retry["max_cost"], {STATS: 1})
        self.bodies = [(200, stats([FIRST, SECOND]))]
        fixed = self.run_action(retry)
        self.assertFalse(fixed["retry"])
        self.assertEqual(fixed["views"][FIRST], 0)
        self.bodies = [(200, {"items": []})]
        next_ticket = self.preview({"parent": retry["action_id"], "action": "more"})
        self.assertEqual(next_ticket, self.preview({"parent": ticket["action_id"], "action": "more"}))
        self.assertEqual(fixed["search_step"], result["search_step"])
        last = self.run_action(next_ticket)
        self.assertFalse(last["more"])
        self.assertEqual(self.calls, ["youtube.search.list", "youtube.channels.list", "youtube.videos.batchGetStats",
                                     "youtube.videos.batchGetStats", "youtube.search.list"])

    def test_bad_rows_and_empty_distinguished_and_cost_receipt_bound_to_owner(self):
        ticket = self.preview(SPEC)
        self.assertRaises(ServiceRefused, self.run_action, ticket, "B")
        self.bodies = [(200, {"items": [{"id": {"kind": "unknown", "videoId": FIRST}}]})]
        page = self.run_action(ticket)
        self.assertEqual(page["steps"][SEARCH]["rejected"], 1)
        self.assertEqual(self.calls, ["youtube.search.list"])
        self.now += 601
        self.assertRaises(ServiceRefused, self.run_action, ticket)
        self.assertRaises(ServiceRefused, self.preview, {"mode": "channel", "q": "x", "stats": True})

    def test_raw_position_survives_an_unreadable_hit(self):
        spec = {"mode": "channel", "q": "studio", "stats": False}
        self.bodies = [(200, {"items": [{"id": {}}, {"id": {"kind": "youtube#channel", "channelId": CHANNEL}}]}),
                       (200, {"items": []})]
        page = self.run_action(self.preview(spec))
        self.assertEqual(page["hits"][0]["position"], 2)
        self.assertEqual(page["hits"][0]["channel"], CHANNEL)
        self.assertEqual(page["search_step"]["rejected"], 1)

    def test_empty_refusal_and_cancelled_are_distinct_and_never_enrich(self):
        self.bodies = [(200, {"items": []}), (500, {"error": {"code": 500}})]
        empty = self.run_action(self.preview(SPEC))
        refused = self.run_action(self.preview(SPEC))
        self.assertIsNone(empty["search_step"]["stop"])
        self.assertTrue(refused["search_step"]["stop"])
        ticket = self.preview(SPEC)
        event = threading.Event()
        event.set()
        cancelled = self.search.execute(json.dumps({"action_id": ticket["action_id"],
                                        "confirmed_cost": ticket["max_cost"]}).encode(), "A", event, lambda: "A")
        self.assertEqual(cancelled["search_step"]["stop"], "cancelled")
        self.assertEqual(len(self.calls), 2)

    def test_restart_and_exhausted_local_daily_caps_send_nothing(self):
        ticket = self.preview(SPEC)
        fresh = ChannelSearch(self.search.provider, None)
        with self.assertRaises(ServiceRefused):
            fresh.execute(json.dumps({"action_id": ticket["action_id"], "confirmed_cost": ticket["max_cost"]}).encode(),
                          "A", threading.Event(), lambda: "A")
        for bucket in (SEARCH, GENERAL):
            cap = self.search.provider.caps[bucket]
            with patch.object(cap, '_reader', return_value=([('used', cap.limit)], False)):
                with self.assertRaises(ServiceRefused) as caught:
                    self.run_action(ticket)
                self.assertEqual(caught.exception.code, "daily_cap_reached")
        self.assertFalse(self.calls)

    def test_double_click_and_lost_response(self):
        ticket = self.preview(SPEC)
        entered, release = threading.Event(), threading.Event()
        def failing_send(*args, **kwargs):
            entered.set()
            release.wait(3)
            raise RuntimeError("unknown")
        self.search.provider.send = failing_send
        errors = []
        def first():
            try:
                self.run_action(ticket)
            except RuntimeError as exc:
                errors.append(str(exc))
        thread = threading.Thread(target=first)
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            with self.assertRaises(ServiceRefused) as error:
                self.run_action(ticket)
            self.assertEqual(error.exception.code, "search_action_running")
        finally:
            release.set()
            thread.join(4)
        self.assertEqual(errors, ["unknown"])
        with self.assertRaises(ServiceRefused) as error:
            self.run_action(ticket)
        self.assertEqual(error.exception.code, "search_action_uncertain")
