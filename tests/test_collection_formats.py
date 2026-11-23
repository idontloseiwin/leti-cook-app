"""The [collection] formats setting, its default and its readers."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "application"))
sys.path.insert(1, str(ROOT))

import collection_formats  # noqa: E402
from collection_formats import (DEFAULT_FORMATS, FORMATS_ENV, VIDEO_FORMATS, FormatsError,  # noqa: E402
                                collection_formats as pass_formats, disabled_formats, parse_formats,
                                shown_formats)
from self_hosted_settings import (SettingsError, browser_environment, browser_settings,  # noqa: E402
                                  read_settings_bytes, settings_ini, settings_problem)

TEMPLATE = (ROOT / "config/config.ini").read_bytes()
LINE = b"formats = long, short\n"


def with_formats(text):
    return TEMPLATE.replace(LINE, b"formats = " + text + b"\n")


class FormatsSettingTests(unittest.TestCase):
    def test_template_ships_streams_off_and_round_trips(self):
        self.assertEqual(TEMPLATE.count(LINE), 1)
        self.assertIn(b"[format]\nversion = 3\n", TEMPLATE)
        settings = read_settings_bytes(TEMPLATE)
        self.assertEqual(settings["formats"], ["long", "short"])
        self.assertEqual(settings_ini(settings).encode(), TEMPLATE)
        self.assertEqual(browser_environment(settings)[FORMATS_ENV], "long,short")

    def test_missing_line_means_long_and_short(self):
        # An update preserves the installed configuration file.
        old = TEMPLATE.replace(LINE, b"")
        self.assertNotIn(b"formats", old.split(b"[limits]")[0].replace(b"# Video formats", b""))
        self.assertEqual(read_settings_bytes(old)["formats"], ["long", "short"])
        self.assertEqual(read_settings_bytes(old), read_settings_bytes(TEMPLATE))
        self.assertEqual(DEFAULT_FORMATS, ("long", "short"))

    def test_spelling_order_and_spaces_are_normalised(self):
        for text, expected in ((b"long, short, stream", ["long", "short", "stream"]),
                               (b"stream,short,long", ["long", "short", "stream"]),
                               (b"  short ,long  ", ["long", "short"]),
                               (b"short , long", ["long", "short"])):
            with self.subTest(text=text):
                settings = read_settings_bytes(with_formats(text))
                self.assertEqual(settings["formats"], expected)
                self.assertIn("formats = " + ", ".join(expected) + "\n", settings_ini(settings))

    def test_invalid_values_are_refused_with_a_fixed_code(self):
        for text in (b"", b",", b"long,,short", b"Long", b"long, long", b"video", b"long short",
                     b"long;short", b"shorts", b"long, short, stream, long"):
            with self.subTest(text=text):
                with self.assertRaisesRegex(SettingsError, "^invalid_collection_formats$"):
                    read_settings_bytes(with_formats(text))
        message = settings_problem("invalid_collection_formats")
        self.assertIn("long, short and optionally stream", message)
        self.assertNotIn("Long", message)

    def test_long_and_short_are_required(self):
        # Only Streams can be turned off in this release.
        for text in (b"stream", b"long", b"short", b"long, stream", b"short, stream"):
            with self.subTest(text=text):
                with self.assertRaisesRegex(SettingsError, "^collection_formats_require_long_and_short$"):
                    read_settings_bytes(with_formats(text))
        message = settings_problem("collection_formats_require_long_and_short")
        self.assertIn("only stream can be turned off", message)
        base = read_settings_bytes(TEMPLATE)
        for bad in (["long"], ["short", "stream"]):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(SettingsError, "^collection_formats_require_long_and_short$"):
                    browser_settings({**base, "formats": bad})

    def test_browser_settings_validate_and_default(self):
        base = read_settings_bytes(TEMPLATE)
        without = {key: value for key, value in base.items() if key != "formats"}
        self.assertEqual(browser_settings(without)["formats"], ["long", "short"])
        self.assertEqual(browser_settings({**base, "formats": ["stream", "short", "long"]})["formats"], ["long", "short", "stream"])
        for bad in ([], ["long", "long"], ["video"], "long", ("long", 1), None):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(SettingsError, "^invalid_collection_formats$"):
                    browser_settings({**base, "formats": bad})

    def test_parse_formats_contract(self):
        self.assertEqual(parse_formats("long, short, stream"), VIDEO_FORMATS)
        self.assertEqual(disabled_formats(("long", "short")), ("stream",))
        with self.assertRaises(FormatsError):
            parse_formats(None)
        with self.assertRaises(FormatsError):
            parse_formats("long," * 20)


class FormatsReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "config.ini"
        self.path.write_bytes(TEMPLATE)
        if os.name != "nt":
            self.path.chmod(0o644)
        self.env = {"LIC_DEPLOYMENT_PROFILE": "self-hosted", "LIC_SETTINGS_FILE": str(self.path)}
        collection_formats._cache.clear()

    def tearDown(self):
        collection_formats._cache.clear()
        self.temp.cleanup()

    def write(self, raw):
        self.path.write_bytes(raw)
        stamp = self.path.stat().st_mtime_ns + 1_000_000_000
        os.utime(self.path, ns=(stamp, stamp))

    def test_without_installation_settings_every_format_stays(self):
        self.assertEqual(pass_formats({}), VIDEO_FORMATS)
        self.assertEqual(shown_formats({}), VIDEO_FORMATS)
        self.assertEqual(pass_formats({"LIC_DEPLOYMENT_PROFILE": "self-hosted"}), VIDEO_FORMATS)

    def test_installation_file_is_read_and_followed(self):
        self.assertEqual(shown_formats(self.env), ("long", "short"))
        self.assertEqual(pass_formats(self.env), ("long", "short"))
        self.write(with_formats(b"long, short, stream"))
        self.assertEqual(shown_formats(self.env), VIDEO_FORMATS)

    def test_pass_snapshot_wins_for_collection_and_file_wins_for_pages(self):
        # The worker received its snapshot at the start of the pass; the owner
        # turned Streams on meanwhile: pages follow the file at once.
        env = {**self.env, FORMATS_ENV: "long,short"}
        self.write(with_formats(b"long, short, stream"))
        self.assertEqual(pass_formats(env), ("long", "short"))
        self.assertEqual(shown_formats(env), VIDEO_FORMATS)

    def test_invalid_or_missing_file_keeps_streams_hidden(self):
        self.write(with_formats(b"long, video"))
        self.assertEqual(shown_formats(self.env), DEFAULT_FORMATS)
        self.path.unlink()
        self.assertEqual(shown_formats(self.env), DEFAULT_FORMATS)
        self.assertEqual(pass_formats({FORMATS_ENV: "nonsense"}), DEFAULT_FORMATS)


class UpdaterKnowsFormatsTests(unittest.TestCase):
    def test_this_release_accepts_an_installed_file_with_the_key(self):
        from ops.update_candidate import CandidateError, _CONFIG_KEYS, _config
        self.assertIn("formats", _CONFIG_KEYS["collection"])
        schedules = (ROOT / "config/collection-schedules.ini").read_bytes()
        for text in (b"long, short", b"long, short, stream", b" stream , short,long"):
            with self.subTest(text=text):
                _config({"config/config.ini": with_formats(text),
                         "config/collection-schedules.ini": schedules})
        with self.assertRaisesRegex(CandidateError, "^update_config_invalid$"):
            _config({"config/config.ini": with_formats(b"long, video"),
                     "config/collection-schedules.ini": schedules})


if __name__ == "__main__":
    unittest.main()
