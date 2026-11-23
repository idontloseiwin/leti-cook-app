"""Artifact reproducibility and version/hash consistency, without network or keys."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import package_release, release_archive
from ops.release_manifest import load_source


class ReleaseArchiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        cls.bundle = cls.root / "bundle"
        package_release.package(cls.bundle)
        source_file = cls.root / "release-source.json"
        source_data = json.dumps({"format": "leti-cook-release-source", "format_version": 1,
                                 "owner": "example-owner", "repository": "example-app"}).encode()
        source_file.write_bytes(source_data)
        (cls.bundle / "application/release-source.json").write_bytes(source_data)
        manifest_file = cls.bundle / "bundle.json"
        bundle_manifest = json.loads(manifest_file.read_bytes())
        bundle_manifest["files"]["application/release-source.json"] = hashlib.sha256(source_data).hexdigest()
        manifest_file.write_bytes(release_archive.canonical_json(bundle_manifest))
        cls.source = load_source(source_file)
        cls.images = {name: {"repository": f"ghcr.io/example-owner/example-app-{name}",
                             "digest": "sha256:" + ("1" if name == "backend" else "2") * 64,
                             "platform": "linux/amd64"} for name in ("backend", "db")}

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def build(self, name):
        version = (self.bundle / "application/VERSION").read_text().strip()
        return release_archive.create_release(self.bundle, self.root / name, self.images,
                                              version, source=self.source)

    def test_repeated_builds_ignore_source_metadata_and_have_normalized_members(self):
        first = self.build("first")
        reversed_source = self.root / "reverse-source"
        reversed_source.mkdir()
        for index, file in enumerate(sorted(self.bundle.rglob("*"), reverse=True)):
            if file.is_file():
                copied = reversed_source / file.relative_to(self.bundle)
                copied.parent.mkdir(parents=True, exist_ok=True)
                copied.write_bytes(file.read_bytes())
                os.utime(copied, (1700000000 + index, 1700000000 + index))
        second = release_archive.create_release(reversed_source, self.root / "second", self.images,
                                                first["version"], source=self.source)
        archive = Path(first["archive"])
        self.assertEqual(archive.read_bytes(), Path(second["archive"]).read_bytes())
        header = archive.read_bytes()[:10]
        self.assertEqual(header[3] & 8, 0, "gzip must not include the local filename")
        self.assertEqual(header[4:8], b"\0\0\0\0")
        with tarfile.open(archive, "r:gz") as package:
            entries = package.getmembers()
            self.assertEqual([entry.name for entry in entries], sorted(entry.name for entry in entries))
            for entry in entries:
                self.assertTrue(entry.isfile())
                self.assertEqual((entry.uid, entry.gid, entry.mtime, entry.mode), (0, 0, 0, 0o644))
                self.assertEqual((entry.uname, entry.gname), ("", ""))
            raw_version = package.extractfile("application/VERSION").read().decode().strip()
        manifest = json.loads((archive.parent / "release.json").read_bytes())
        self.assertEqual(manifest["version"], raw_version)
        expected_schema = json.loads((self.bundle / "schema/manifest.json").read_bytes())
        self.assertEqual(manifest["schema_version"], expected_schema["schema_version"])
        self.assertEqual(manifest["package"], {
            "name": archive.name, "size": archive.stat().st_size,
            "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        })
        sums = (archive.parent / "SHA256SUMS").read_text().splitlines()
        self.assertEqual(len(sums), 2)
        for row in sums:
            digest, name = row.split("  ")
            self.assertEqual(digest, hashlib.sha256((archive.parent / name).read_bytes()).hexdigest())
        self.assertFalse(first["signed"])

    def test_bundle_cannot_silently_change_future_update_source(self):
        from ops.release_manifest import ReleaseSource
        output = self.root / "different-source"
        with self.assertRaisesRegex(ValueError, "release_source_mismatch"):
            release_archive.create_release(self.bundle, output, self.images, "0.1.0",
                                            source=ReleaseSource("other-owner", "other-app"))
        self.assertFalse(output.exists())

    def test_invalid_image_policy_does_not_create_output(self):
        images = {**self.images, "backend": {**self.images["backend"], "repository": "ghcr.io/other/evil"}}
        output = self.root / "invalid-policy"
        with self.assertRaises(ValueError):
            release_archive.create_release(self.bundle, output, images, "0.1.0", source=self.source)
        self.assertFalse(output.exists())

    def test_never_overwrites_existing_artifacts(self):
        output = self.root / "occupied"
        output.mkdir()
        (output / "sentinel").write_bytes(b"owner work")
        with self.assertRaisesRegex(ValueError, "new_output_directory_required"):
            self.build("occupied")
        self.assertEqual((output / "sentinel").read_bytes(), b"owner work")


if __name__ == "__main__":
    unittest.main()
