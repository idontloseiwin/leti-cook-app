"""Offline release trust tests; all private keys and release assets are temporary."""
from __future__ import annotations

import base64
import copy
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from ops.bundle_contract import FORMAT, MAX_FILE, REQUIRED_BUNDLE_FILES
from ops.release_manifest import (
    ReleaseError, ReleaseSource, SCHEMA_HEADER, load_source, parse_source,
    read_schema_version, serialize_manifest, strict_json, validate_manifest,
)
from ops import release_verification as verifier
from tools.release_archive import _write_archive
from tools.sign_release import sign_release


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8")


def source_bytes(owner="example-owner", repository="example-app"):
    return json_bytes({"format": "leti-cook-release-source", "format_version": 1,
                       "owner": owner, "repository": repository})


def manifest():
    return {"format": "leti-cook-release", "format_version": 1,
            "version": "0.1.0", "schema_version": 25, "minimum_upgrade_version": "0.1.0",
            "package": {"name": "leti-cook-0.1.0.tar.gz", "sha256": "0" * 64, "size": 1},
            "images": {name: {"repository": f"ghcr.io/example-owner/example-app-{name}",
                              "digest": "sha256:" + "1" * 64, "platform": "linux/amd64"}
                       for name in ("backend", "db")}}


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.source = parse_source(source_bytes())

    def test_source_is_closed_bounded_and_placeholder_fails(self):
        self.assertEqual(self.source, ReleaseSource("example-owner", "example-app"))
        self.assertEqual(ReleaseSource("Owner", "Repo").image_repository("db"), "ghcr.io/owner/repo-db")
        for data in (source_bytes("OWNER"), source_bytes(repository="REPOSITORY"),
                     source_bytes(repository="../other"), b"{}", source_bytes().replace(b'"format_version": 1', b'"format_version": true'),
                     b"x" * 16385):
            with self.subTest(data=data[:80]), self.assertRaises(ReleaseError):
                parse_source(data)

    def test_duplicate_and_non_json_values_fail(self):
        for data in (b'{"a": 1, "a": 2}', b'{"a":NaN}', b'{"a":Infinity}', b'\xef\xbb\xbf{}'):
            with self.subTest(data=data), self.assertRaises(ReleaseError):
                strict_json(data)

    def test_manifest_closed_fields_bounds_and_trusted_images(self):
        value = manifest()
        self.assertEqual(validate_manifest(value, self.source), value)
        for mutate in (
            lambda v: v.update(extra=1), lambda v: v.update(schema_version=True),
            lambda v: v.update(format_version=True), lambda v: v.update(version="v0.1.0"),
            lambda v: v.update(minimum_upgrade_version="0.2.0"),
            lambda v: v["package"].update(name="../../evil.tar.gz"),
            lambda v: v["package"].update(size=True), lambda v: v["package"].update(sha256="F" * 64),
            lambda v: v["images"]["db"].update(repository="ghcr.io/other/db"),
            lambda v: v["images"]["db"].update(platform="linux/arm64"),
            lambda v: v["images"].update(extra={}),
        ):
            candidate = copy.deepcopy(value)
            mutate(candidate)
            with self.subTest(candidate=candidate), self.assertRaises(ReleaseError):
                validate_manifest(candidate, self.source)

    def test_semver_minimum_and_serialization_are_deterministic(self):
        value = manifest()
        value.update(version="1.0.0-rc.10+build", minimum_upgrade_version="1.0.0-rc.2")
        value["package"]["name"] = "leti-cook-1.0.0-rc.10+build.tar.gz"
        self.assertEqual(serialize_manifest(value, self.source), serialize_manifest(copy.deepcopy(value), self.source))
        value["minimum_upgrade_version"] = "1.0.0"
        with self.assertRaisesRegex(ReleaseError, "release_minimum_newer"):
            validate_manifest(value, self.source)

    def test_validated_manifest_does_not_alias_mutable_input(self):
        value = manifest()
        validated = validate_manifest(value, self.source)
        value["images"]["db"]["repository"] = "changed"
        value["package"]["size"] = 9
        self.assertNotEqual(value, validated)

    def test_schema_version_is_explicit_with_matching_clean_schema_hash(self):
        raw = SCHEMA_HEADER + b"\n"
        schema = {"format": "leti-cook-self-hosted-schema", "version": 1, "schema_version": 25,
                  "fresh_database_only": True, "postgres_major": 17, "api_data_rows": 0,
                  "user_work_rows": 0, "files": {"schema.sql": hashlib.sha256(raw).hexdigest()}}
        self.assertEqual(read_schema_version(raw, schema), 25)
        for mutate in (lambda s: s.pop("schema_version"), lambda s: s.update(schema_version=True),
                       lambda s: s.update(api_data_rows=1), lambda s: s.update(api_data_rows=False),
                       lambda s: s["files"].update({"schema.sql": "0" * 64})):
            changed = copy.deepcopy(schema)
            mutate(changed)
            with self.assertRaisesRegex(ReleaseError, "release_schema_invalid"):
                read_schema_version(raw, changed)


class ReleaseVerificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.openssl = shutil.which("openssl")
        if cls.openssl is None and os.name == "nt":
            candidate = Path("C:/Program Files/Git/usr/bin/openssl.exe")
            if candidate.is_file():
                cls.openssl = str(candidate)
        if cls.openssl is None:
            raise RuntimeError("OpenSSL 3 is required for release signature acceptance tests")
        cls.private_key = Ed25519PrivateKey.generate()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = parse_source(source_bytes())
        self.key_file = self.root / "test-private.pem"
        self.key_file.write_bytes(self.private_key.private_bytes(serialization.Encoding.PEM,
                                  serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        self.public_file = self.root / "trusted.pub"
        self.public_file.write_bytes(self.private_key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
        self.directory = self.root / "assets"
        self.directory.mkdir()
        self.archive = self.directory / "leti-cook-0.1.0.tar.gz"
        self.files = {name: b"fixture" for name in REQUIRED_BUNDLE_FILES}
        self.files["application/VERSION"] = b"0.1.0\n"
        self.files["application/release-source.json"] = source_bytes()
        self.files["application/release-signing.pub"] = self.public_file.read_bytes()
        self.files["install.py"] = b"raise RuntimeError('candidate code must never execute')\n"
        self.files["schema/schema.sql"] = SCHEMA_HEADER + b"\n-- synthetic, never executed\n"
        self.files["schema/baseline/schema.sql"] = self.files["schema/schema.sql"]
        self.files["schema/configuration-seeds.sql"] = b"-- synthetic\n"
        self.files["application/schema-release.json"] = json_bytes({
            "format": "leti-cook-schema-release", "format_version": 1,
            "baseline_id": "leti-cook-release-01", "schema_version": 25,
        })
        schema = {"format": "leti-cook-self-hosted-schema", "version": 1, "schema_version": 25,
                  "baseline_id": "leti-cook-release-01", "baseline_version": 25, "steps": [],
                  "schemas": ["public", "operator_private"], "configuration_seed_counts": {},
                  "fresh_database_only": True, "postgres_major": 17, "api_data_rows": 0, "user_work_rows": 0,
                  "files": {name: hashlib.sha256(self.files["schema/" + name]).hexdigest()
                            for name in ("schema.sql", "baseline/schema.sql", "configuration-seeds.sql")}}
        self.files["schema/manifest.json"] = json_bytes(schema)
        self.refresh_bundle()
        self.metadata = manifest()
        self.write_assets()

    def refresh_bundle(self):
        self.files["bundle.json"] = json_bytes({"format": FORMAT, "version": 1, "fresh_install_only": True,
                                               "stage": "self-hosted-release-candidate", "release_accepted": False,
                                               "youtube_configuration_included": False,
                                               "files": {name: hashlib.sha256(data).hexdigest()
                                                         for name, data in self.files.items() if name != "bundle.json"}})

    def write_assets(self, archive_bytes=None):
        if archive_bytes is None:
            if self.archive.exists():
                self.archive.unlink()
            _write_archive(self.files, self.archive)
        else:
            self.archive.write_bytes(archive_bytes)
        self.metadata["package"].update(size=self.archive.stat().st_size,
                                         sha256=hashlib.sha256(self.archive.read_bytes()).hexdigest())
        (self.directory / "release.json").write_bytes(json_bytes(self.metadata))
        self.resign()

    def resign(self):
        sums = "".join(f"{hashlib.sha256((self.directory / name).read_bytes()).hexdigest()}  {name}\n"
                       for name in sorted((self.archive.name, "release.json"))).encode("ascii")
        (self.directory / "SHA256SUMS").write_bytes(sums)
        (self.directory / "SHA256SUMS.sig").write_bytes(self.private_key.sign(sums))

    def verify(self, **kwargs):
        return verifier.verify_release(self.archive, public_key_path=self.public_file,
                                       source=self.source, openssl=self.openssl, **kwargs)

    def test_real_ed25519_verification_returns_immutable_captured_bytes(self):
        result = self.verify()
        self.assertEqual(result.manifest["version"], "0.1.0")
        self.assertEqual(result.files["application/VERSION"], b"0.1.0\n")
        with self.assertRaises(TypeError):
            result.manifest["package"]["size"] = 0
        with self.assertRaises(TypeError):
            result.files["install.py"] = b"changed"
        self.archive.write_bytes(b"replaced after verification")
        self.assertEqual(result.files["application/VERSION"], b"0.1.0\n")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["assets", "test-private.pem", "trusted.pub"])

    def test_repository_bundle_writer_signer_and_verifier_agree(self):
        from tools import package_release, release_archive
        bundle = self.root / "repository-bundle"
        package_release.package(bundle)
        # This is only a temporary offline fixture; canonical repository
        # OWNER/REPOSITORY stays unconfigured until the owner supplies it.
        source_path = bundle / "application/release-source.json"
        source_path.write_bytes(source_bytes())
        public_path = bundle / "application/release-signing.pub"
        public_path.write_bytes(self.public_file.read_bytes())
        bundle_path = bundle / "bundle.json"
        bundle_manifest = json.loads(bundle_path.read_bytes())
        bundle_manifest["files"]["application/release-signing.pub"] = hashlib.sha256(public_path.read_bytes()).hexdigest()
        bundle_manifest["files"]["application/release-source.json"] = hashlib.sha256(source_path.read_bytes()).hexdigest()
        bundle_path.write_bytes(json_bytes(bundle_manifest))
        output = self.root / "repository-release"
        result = release_archive.create_release(bundle, output, manifest()["images"], "0.1.0", source=self.source)
        sign_release(output, self.key_file, source=self.source)
        verified = verifier.verify_release(result["archive"], public_key_path=self.public_file,
                                           source=self.source, openssl=self.openssl)
        self.assertEqual(set(verified.files), set(bundle_manifest["files"]) | {"bundle.json"})
        expected_schema = json.loads((bundle / "schema/manifest.json").read_bytes())
        self.assertEqual(verified.manifest["schema_version"], expected_schema["schema_version"])
        self.assertEqual(verified.files["application/VERSION"], (ROOT / "application/VERSION").read_bytes())
        for name in bundle_manifest["files"]:
            self.assertEqual(verified.files[name], (bundle / name).read_bytes(), name)

    def test_candidate_cannot_remove_or_rotate_the_installed_trust_key(self):
        trusted = self.files["application/release-signing.pub"]
        for candidate in (None, b"another public key"):
            with self.subTest(candidate=candidate):
                if candidate is None:
                    self.files.pop("application/release-signing.pub", None)
                else:
                    self.files["application/release-signing.pub"] = candidate
                self.refresh_bundle()
                self.write_assets()
                with self.assertRaisesRegex(ReleaseError, "release_signing_key_changed"):
                    self.verify()
        self.files["application/release-signing.pub"] = trusted

    def test_signer_interoperability_and_no_overwrite(self):
        (self.directory / "SHA256SUMS.sig").unlink()
        sign_release(self.directory, self.key_file, source=self.source)
        self.verify()
        with self.assertRaisesRegex(ReleaseError, "release_signature_output_unavailable"):
            sign_release(self.directory, self.key_file, source=self.source)

    def test_untrusted_or_corrupt_signatures_and_missing_key_fail(self):
        signature = self.directory / "SHA256SUMS.sig"
        for data in (b"x" * 64, b"short", Ed25519PrivateKey.generate().sign((self.directory / "SHA256SUMS").read_bytes())):
            signature.write_bytes(data)
            with self.assertRaises(ReleaseError):
                self.verify()
        self.public_file.unlink()
        with self.assertRaises(ReleaseError):
            self.verify()

    def test_tampered_archive_or_metadata_without_resigning_fail(self):
        for path in (self.archive, self.directory / "release.json", self.directory / "SHA256SUMS"):
            original = path.read_bytes()
            path.write_bytes(original + b"\n")
            with self.subTest(path=path.name), self.assertRaises(ReleaseError):
                self.verify()
            path.write_bytes(original)

    def test_recomputed_unsigned_hashes_still_fail(self):
        self.archive.write_bytes(self.archive.read_bytes() + b"x")
        old_signature = (self.directory / "SHA256SUMS.sig").read_bytes()
        self.resign()
        (self.directory / "SHA256SUMS.sig").write_bytes(old_signature)
        with self.assertRaisesRegex(ReleaseError, "release_signature_invalid"):
            self.verify()

    def test_signature_does_not_bypass_metadata_policy(self):
        self.metadata["images"]["db"]["repository"] = "ghcr.io/other/db"
        self.write_assets()
        with self.assertRaisesRegex(ReleaseError, "release_image_invalid"):
            self.verify()

    def test_signed_duplicate_manifest_key_fails(self):
        path = self.directory / "release.json"
        path.write_bytes(path.read_bytes().replace(b'{', b'{"version":"0.1.0",', 1))
        self.resign()
        with self.assertRaises(ReleaseError):
            self.verify()

    def test_signed_wrong_size_and_archive_name_fail(self):
        path = self.directory / "release.json"
        self.metadata["package"]["size"] += 1
        path.write_bytes(json_bytes(self.metadata))
        self.resign()
        with self.assertRaisesRegex(ReleaseError, "release_package_mismatch"):
            self.verify()

    def test_bundle_hash_filelist_and_required_set_are_enforced(self):
        original = dict(self.files)
        for mutate in (lambda f: f.update({"install.py": b"changed"}),
                       lambda f: f.update({"unlisted.txt": b"not listed"}),
                       lambda f: f.pop("ops/release_manifest.py")):
            self.files = dict(original)
            mutate(self.files)
            self.write_assets()
            with self.assertRaises(ReleaseError):
                self.verify()

    def test_even_signed_bundles_cannot_omit_required_file_or_include_private_files(self):
        original = dict(self.files)
        for mutate in (lambda f: f.pop("ops/release_manifest.py"), lambda f: f.update({"secret.key": b"secret"})):
            self.files = dict(original)
            mutate(self.files)
            self.refresh_bundle()
            self.write_assets()
            with self.assertRaises(ReleaseError):
                self.verify()

    def test_payload_version_schema_and_source_must_match(self):
        original = dict(self.files)
        variants = (("application/VERSION", b"0.2.0\n"),
                    ("application/release-source.json", source_bytes(repository="other-app")),
                    ("schema/schema.sql", SCHEMA_HEADER + b"\nchanged"),
                    ("schema/configuration-seeds.sql", b"changed"))
        for name, data in variants:
            self.files = dict(original)
            self.files[name] = data
            self.refresh_bundle()
            self.write_assets()
            with self.subTest(name=name), self.assertRaises(ReleaseError):
                self.verify()

    def test_valid_signature_cannot_hide_inconsistent_schema_release(self):
        original = dict(self.files)
        runtime = json.loads(original["application/schema-release.json"])
        runtime["schema_version"] = 26
        for name, data in (
            ("application/schema-release.json", json_bytes(runtime)),
            ("schema/baseline/schema.sql", original["schema/schema.sql"] + b"\n-- drift\n"),
            ("schema/steps/0026-unlisted.sql", b"SELECT 1;\n"),
        ):
            self.files = dict(original)
            self.files[name] = data
            self.refresh_bundle()
            self.write_assets()
            with self.subTest(name=name), self.assertRaises(ReleaseError):
                self.verify()

    def malicious_archive(self, name="evil", *, kind=tarfile.REGTYPE, size=1, format=tarfile.USTAR_FORMAT, duplicate=False):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w", format=format) as archive:
            entry = tarfile.TarInfo(name)
            entry.type = kind
            entry.size = size if kind == tarfile.REGTYPE else 0
            entry.mode = 0o644
            if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                entry.linkname = "../../target"
            archive.addfile(entry, io.BytesIO(b"x" * size) if entry.size else None)
            if duplicate:
                archive.addfile(entry, io.BytesIO(b"x" * size))
        return gzip.compress(output.getvalue(), mtime=0)

    def test_unsafe_member_types_paths_and_duplicates_fail(self):
        for kwargs in ({"name": "../escape"}, {"name": "/absolute"}, {"name": "a\\b"},
                       {"name": "a//b"}, {"name": "a:stream"}, {"name": ".hidden"}, {"name": "."},
                       {"kind": tarfile.SYMTYPE}, {"kind": tarfile.LNKTYPE}, {"kind": tarfile.DIRTYPE},
                       {"kind": tarfile.FIFOTYPE}, {"duplicate": True},
                       {"name": "a" * 150, "format": tarfile.PAX_FORMAT}):
            self.write_assets(self.malicious_archive(**kwargs))
            with self.subTest(kwargs=kwargs), self.assertRaises(ReleaseError):
                self.verify()

    def test_oversized_decompressed_archive_and_member_fail_bounded(self):
        with patch.object(verifier, "MAX_TAR", 1024):
            with self.assertRaisesRegex(ReleaseError, "release_archive_size_invalid"):
                self.verify()
        with patch.object(verifier, "MAX_FILE", 2):
            with self.assertRaisesRegex(ReleaseError, "release_archive_member_invalid"):
                self.verify()

    def test_truncated_archive_and_hidden_trailing_tar_fail(self):
        original = self.archive.read_bytes()
        for raw in (original[:-5], gzip.compress(gzip.decompress(original) + b"x" * 512, mtime=0)):
            self.write_assets(raw)
            with self.assertRaises(ReleaseError):
                self.verify()

    def test_hardlinked_input_fails_before_signature_tool(self):
        linked = self.root / "hardlink"
        os.link(self.archive, linked)
        with patch.object(verifier, "_verify_signature") as verify:
            with self.assertRaisesRegex(ReleaseError, "release_input_invalid"):
                self.verify()
            verify.assert_not_called()

    def test_signature_tool_unavailable_fails_closed(self):
        with patch.object(verifier.subprocess, "run", side_effect=FileNotFoundError()):
            with self.assertRaisesRegex(ReleaseError, "release_signature_check_unavailable"):
                self.verify()

    def test_non_ed25519_public_key_fails_before_openssl(self):
        self.public_file.write_bytes(b"-----BEGIN PUBLIC KEY-----\n" + base64.b64encode(b"not-ed25519") + b"\n-----END PUBLIC KEY-----\n")
        with patch.object(verifier.subprocess, "run") as run:
            with self.assertRaisesRegex(ReleaseError, "release_public_key_invalid"):
                self.verify()
            run.assert_not_called()

    def test_real_entrypoint_rejects_placeholder_without_capturing_candidate(self):
        # The real repository source is configured; only this fixture is not.
        (self.root / "application").mkdir()
        (self.root / "application/release-source.json").write_bytes(source_bytes(owner="OWNER"))
        with patch("ops.release_manifest.ROOT", self.root), patch.object(verifier, "_capture") as capture:
            with self.assertRaisesRegex(ReleaseError, "release_source_unconfigured"):
                verifier.verify_release(self.archive)
            capture.assert_not_called()


if __name__ == "__main__":
    unittest.main()
