"""Ensure the release gate detects stale, changed and omitted browser assets."""
from pathlib import Path
import tempfile
import unittest
from tools.check_frontend_build import differences


class FrontendBuildTests(unittest.TestCase):
    def test_same_names_do_not_hide_changed_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            shipped, built = Path(tmp) / "shipped", Path(tmp) / "built"
            shipped.mkdir()
            built.mkdir()
            (shipped / "entry.html").write_bytes(b"old")
            (built / "entry.html").write_bytes(b"new")
            self.assertEqual(differences(shipped, built), ["Different bytes: entry.html"])
            (built / "entry.html").write_bytes(b"old")
            self.assertEqual(differences(shipped, built), [])

    def test_renamed_asset_catches_missing_and_extra_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            shipped, built = Path(tmp) / "shipped", Path(tmp) / "built"
            shipped.mkdir()
            built.mkdir()
            (shipped / "old.js").write_bytes(b"same")
            (built / "new.js").write_bytes(b"same")
            self.assertEqual(differences(shipped, built),
                             ["Missing from source build: old.js", "Not shipped: new.js"])

    def test_empty_missing_or_same_directory_cannot_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(ValueError):
                differences(root, root / "missing")
            with self.assertRaises(ValueError):
                differences(root, root)
            built = root / "built"
            built.mkdir()
            with self.assertRaises(ValueError):
                differences(root, built)
