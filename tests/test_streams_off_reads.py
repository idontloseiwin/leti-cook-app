"""Reads refuse a turned-off format and keep the others as they were."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "application"))
sys.path.insert(0, str(ROOT / "tests"))

from test_research_query_contract import query  # noqa: E402
from research_query.operation_kinds import OPERATION_KINDS  # noqa: E402
from research_query.contracts import ERROR_STATUS  # noqa: E402
from research_query.validation import AccessContext, QueryRejected, canonical, capabilities, validate  # noqa: E402
from research_query.registry import CHANNEL_SOURCE_ID  # noqa: E402
import research_mcp  # noqa: E402

ALL = AccessContext("local-test", "owner-1", enabled=True)
OFF = replace(ALL, disabled_video_types=frozenset({"stream"}))


def with_type(kind):
    return canonical(query(video_types=[kind]))


class ResearchRefusalTests(unittest.TestCase):
    def test_turned_off_format_is_refused_with_its_own_code(self):
        with self.assertRaises(QueryRejected) as caught:
            validate(with_type("stream"), OFF)
        self.assertEqual((caught.exception.code, caught.exception.path, caught.exception.status),
                         ("format_turned_off", "video_types", 422))
        self.assertEqual(ERROR_STATUS["format_turned_off"], 422)
        self.assertIn("format_turned_off", research_mcp.ERROR_CODES)

    def test_long_and_short_validate_byte_identically(self):
        for kind in ("long", "short"):
            with self.subTest(kind=kind):
                before, after = validate(with_type(kind), ALL), validate(with_type(kind), OFF)
                self.assertEqual(before.encoded, after.encoded)
                self.assertEqual(before.fingerprint, after.fingerprint)
                self.assertEqual(before.shape_hash, after.shape_hash)
        # With every format on, the context fingerprint equals the one without format settings.
        self.assertEqual(ALL.fingerprint(), replace(ALL, disabled_video_types=frozenset()).fingerprint())
        self.assertNotEqual(ALL.fingerprint(), OFF.fingerprint())
        validate(with_type("stream"), ALL)

    def test_capabilities_name_the_turned_off_format(self):
        on, off = capabilities(ALL), capabilities(OFF)
        self.assertNotIn("turned_off_video_types", on)
        self.assertEqual(on["video_types"], ["long", "short", "stream"])
        self.assertEqual(off["video_types"], ["long", "short"])
        self.assertEqual(off["turned_off_video_types"], ["stream"])
        self.assertEqual({k: v for k, v in off.items() if k not in ("video_types", "turned_off_video_types", "observation_age_limits")},
                         {k: v for k, v in on.items() if k not in ("video_types", "observation_age_limits")})
        self.assertEqual(capabilities(OFF, CHANNEL_SOURCE_ID)["turned_off_video_types"], ["stream"])

    def test_context_rejects_invalid_format_sets(self):
        for bad in ({"video"}, {"long", "short", "stream"}, ["stream"]):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    replace(ALL, disabled_video_types=bad if isinstance(bad, list) else frozenset(bad))

    def test_header_total_of_a_turned_off_format_is_not_shown(self):
        item = dict(status="ready", views="10", reason_code=None, scanned_inventory_count=1, canonical_inventory_count=1,
                    known_view_count=1, missing_view_count=0, excluded={}, expires_at=None)
        result = dict(list_limit=1, formats=dict(long=dict(item), short=dict(item), stream=dict(item)))
        # Only the header totals name formats; no other operation has the hook.
        self.assertEqual([name for name, kind in OPERATION_KINDS.items() if kind.turned_off is not None],
                         ["channel.format_totals.v1"])
        changed = OPERATION_KINDS["channel.format_totals.v1"].turned_off(result, frozenset({"stream"}))
        self.assertEqual(changed["formats"]["long"], item)
        self.assertEqual(changed["formats"]["stream"]["status"], "turned_off")
        self.assertEqual(changed["formats"]["stream"]["reason_code"], "format_turned_off")
        self.assertIsNone(changed["formats"]["stream"]["views"])
        self.assertEqual(set(changed["formats"]["stream"]), set(item))


class WebContextTests(unittest.TestCase):
    def test_research_context_follows_the_installation_file(self):
        import collection_formats
        with patch.object(collection_formats, "_installation_formats", return_value=("long", "short")):
            self.assertEqual(collection_formats.disabled_formats(collection_formats.shown_formats({})), ("stream",))
        with patch.object(collection_formats, "_installation_formats", return_value=None):
            self.assertEqual(collection_formats.disabled_formats(collection_formats.shown_formats({})), ())


if __name__ == "__main__":
    unittest.main()
