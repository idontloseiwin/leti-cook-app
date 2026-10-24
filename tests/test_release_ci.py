"""Offline OCI identity guards and complete temporary-key release dry signing."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ops.release_manifest import ReleaseError
from tools import package_release, release_ci as ci
from tools.release_archive import canonical_json


def oci_fixture(path, *, architecture="amd64"):
    path = Path(path)
    (path / "blobs" / "sha256").mkdir(parents=True)

    def blob(data, media):
        data = canonical_json(data) if isinstance(data, dict) else data
        digest = hashlib.sha256(data).hexdigest()
        (path / "blobs" / "sha256" / digest).write_bytes(data)
        return {"digest": "sha256:" + digest, "size": len(data), "mediaType": media}

    config = blob({"architecture": architecture, "os": "linux", "rootfs": {"type": "layers", "diff_ids": []}}, ci.OCI_CONFIG)
    layer = blob(b"synthetic-layer-not-executed", "application/vnd.oci.image.layer.v1.tar+gzip")
    manifest = blob({"schemaVersion": 2, "mediaType": ci.OCI_MANIFEST,
                     "config": config, "layers": [layer]}, ci.OCI_MANIFEST)
    (path / "oci-layout").write_bytes(canonical_json({"imageLayoutVersion": "1.0.0"}))
    (path / "index.json").write_bytes(canonical_json({"schemaVersion": 2, "manifests": [manifest]}))
    return manifest["digest"]


class OCIIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.layout = self.root / "oci"
        self.digest = oci_fixture(self.layout)

    def test_digest_is_registry_manifest_not_configuration_id(self):
        self.assertEqual(ci.inspect_oci(self.layout), self.digest)
        manifest = json.loads((self.layout / "blobs/sha256" / self.digest[7:]).read_bytes())
        self.assertNotEqual(self.digest, manifest["config"]["digest"])

    def test_modified_layer_or_unverified_extra_file_refused(self):
        manifest = json.loads((self.layout / "blobs/sha256" / self.digest[7:]).read_bytes())
        layer = self.layout / "blobs/sha256" / manifest["layers"][0]["digest"][7:]
        original = layer.read_bytes()
        layer.write_bytes(b"x" * len(original))
        with self.assertRaisesRegex(ReleaseError, "digest_mismatch"):
            ci.inspect_oci(self.layout)
        layer.write_bytes(original)
        (self.layout / "extra").write_bytes(b"unchecked")
        with self.assertRaisesRegex(ReleaseError, "file_set_invalid"):
            ci.inspect_oci(self.layout)

    def test_nested_index_multiple_manifests_and_wrong_platform_refused(self):
        index_path = self.layout / "index.json"
        index = json.loads(index_path.read_bytes())
        nested = copy.deepcopy(index)
        nested["manifests"][0]["mediaType"] = ci.OCI_INDEX
        index_path.write_bytes(canonical_json(nested))
        with self.assertRaisesRegex(ReleaseError, "descriptor_invalid"):
            ci.inspect_oci(self.layout)
        index["manifests"].append(index["manifests"][0])
        index_path.write_bytes(canonical_json(index))
        with self.assertRaisesRegex(ReleaseError, "index_invalid"):
            ci.inspect_oci(self.layout)
        other = self.root / "arm"
        oci_fixture(other, architecture="arm64")
        with self.assertRaisesRegex(ReleaseError, "platform_invalid"):
            ci.inspect_oci(other)

    def test_foreign_descriptor_url_and_size_boolean_refused(self):
        base = {"mediaType": ci.OCI_MANIFEST, "digest": self.digest, "size": 10}
        for change in ({"urls": ["https://example.invalid/layer"]}, {"size": True}, {"digest": "sha256:../../x"}):
            with self.subTest(change=change), self.assertRaises(ReleaseError):
                ci._descriptor({**base, **change}, {ci.OCI_MANIFEST})

    def test_hardlinked_blob_is_not_accepted(self):
        blob = self.layout / "blobs/sha256" / self.digest[7:]
        os.link(blob, self.root / "second-link")
        with self.assertRaisesRegex(ReleaseError, "blob_invalid"):
            ci.inspect_oci(self.layout)

    def test_image_tag_uses_semver_parser_and_safe_build_suffix(self):
        self.assertEqual(ci.image_tag("0.1.0-rc.1+build.2"), "v0.1.0-rc.1_build.2")
        for value in ("v0.1.0", "0.1.0/next", "0.1.0+" + "x" * 125):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ci.image_tag(value)


class CompleteDryRunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.bundle = cls.root / "source-bundle"
        package_release.package(cls.bundle)
        cls.openssl = shutil.which("openssl")
        if cls.openssl is None and os.name == "nt":
            cls.openssl = "C:/Program Files/Git/usr/bin/openssl.exe"
        if not cls.openssl:
            raise RuntimeError("OpenSSL 3 is required")
        cls.calls = []
        def build(args, **kwargs):
            cls.calls.append((args, kwargs))
            output = args[args.index("--output") + 1]
            path = output.split("dest=", 1)[1].split(",", 1)[0]
            oci_fixture(Path(path))
            return b""
        cls.out = cls.root / "dry"
        cls.result = ci.dry_run(cls.bundle, cls.out, openssl=cls.openssl, run=build)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_complete_existing_archive_signer_and_host_verifier_pass(self):
        self.assertEqual(self.result, {"version": "0.1.0", "signed": True, "verified": True,
                                     "tamper_rejected": True, "registry_roundtrip": False, "fixture_only": True})
        verified = ci._verified(self.out / "release", ci.FIXTURE_SOURCE, self.out / "trusted.pub", self.openssl)
        self.assertEqual(verified.files["application/release-signing.pub"], (self.out / "trusted.pub").read_bytes())
        original = (self.bundle / "application/release-source.json").read_bytes()
        self.assertEqual(original, (ROOT / "application/release-source.json").read_bytes())
        self.assertNotEqual(original, verified.files["application/release-source.json"])
        for file in self.out.rglob("*"):
            if file.is_file():
                self.assertNotIn(b"BEGIN PRIVATE KEY", file.read_bytes(), file.name)

    def test_two_builds_from_fixture_bundle_are_only_external_dry_commands(self):
        self.assertEqual(len(self.calls), 2)
        for (args, options), context in zip(self.calls, ("application", "database")):
            self.assertEqual(args[:5], ["docker", "buildx", "build", "--builder", "leti-cook-release"])
            self.assertIn("--provenance=false", args)
            self.assertIn("--sbom=false", args)
            self.assertEqual(args[-1], str(self.out / "bundle" / context))
            self.assertNotIn("--push", args)
            self.assertGreater(options["timeout"], 0)

    def test_placeholder_source_fails_before_output_and_external_command(self):
        # This is a deliberately unconfigured fixture, independent of the
        # real publishing source selected by the owner.
        fixture = self.root / "placeholder-bundle"
        shutil.copytree(self.bundle, fixture)
        source = fixture / "application/release-source.json"
        source.write_bytes(json.dumps({"format": "leti-cook-release-source", "format_version": 1,
                                      "owner": "OWNER", "repository": "REPOSITORY"}).encode())
        manifest = fixture / "bundle.json"
        data = json.loads(manifest.read_bytes())
        data["files"]["application/release-source.json"] = hashlib.sha256(source.read_bytes()).hexdigest()
        manifest.write_text(json.dumps(data), encoding="utf-8")
        destination = self.root / "must-not-exist"
        with patch.object(ci, "command") as run:
            with self.assertRaisesRegex(ReleaseError, "source_unconfigured"):
                ci.prepare_images(fixture, destination, run=run, source=ci.FIXTURE_SOURCE)
        self.assertFalse(destination.exists())
        run.assert_not_called()

    def test_artifacts_bound_to_same_bundle_as_image_build(self):
        manifest, _ = ci._bundle(self.out / "bundle", ci.FIXTURE_SOURCE)
        manifest["files"]["README.md"] = "0" * 64
        with self.assertRaisesRegex(ReleaseError, "build_bundle_mismatch"):
            ci._assert_build_bundle(self.out / "images", canonical_json(manifest))

    def _publish(self, run):
        return ci.publish_images(self.out / "images", self.out / "release", source=ci.FIXTURE_SOURCE,
                                 public_key_path=self.out / "trusted.pub", openssl=self.openssl, run=run)

    def test_publish_preserves_prepared_digest_and_checks_remote(self):
        calls = []
        remote = {}
        def run(args, **kwargs):
            calls.append(args)
            if args[1] == "inspect":
                ref = args[-1]
                if ref not in remote:
                    raise ci.CommandError(b'Error: reading manifest v0.1.0: manifest unknown')
                return remote[ref]
            self.assertEqual(args[:4], ["skopeo", "copy", "--all", "--preserve-digests"])
            name = "backend" if "release-fixture-backend" in args[-1] else "db"
            layout = self.out / "images" / name
            digest = ci.inspect_oci(layout)
            remote[args[-1]] = (layout / "blobs/sha256" / digest[7:]).read_bytes()
            Path(args[args.index("--digestfile") + 1]).write_text(digest + "\n", encoding="ascii")
            return b""
        result = self._publish(run)
        self.assertEqual(set(result), {"backend", "db"})
        self.assertEqual(sum(args[1] == "copy" for args in calls), 2)
        self.assertTrue(all(args[0] == "skopeo" for args in calls))
        calls.clear()
        self._publish(run)
        self.assertFalse(any(args[1] == "copy" for args in calls))

    def test_existing_different_tag_refused_before_any_copy(self):
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            return b"different manifest"
        with self.assertRaisesRegex(ReleaseError, "existing_image_conflict"):
            self._publish(run)
        self.assertTrue(all(args[1] == "inspect" for args in calls))

    def test_auth_network_errors_never_treated_as_missing(self):
        for reason in (b'Error: unauthorized', b'Error: connection refused', b'Error: name unknown.example: timeout'):
            calls = []
            def run(args, **kwargs):
                calls.append(args)
                raise ci.CommandError(reason)
            with self.subTest(reason=reason), self.assertRaises(ci.CommandError):
                self._publish(run)
            self.assertEqual(len(calls), 1)

    def test_wrong_copy_digest_prevents_success(self):
        def run(args, **kwargs):
            if args[1] == "inspect":
                raise ci.CommandError(b'Error: manifest unknown')
            Path(args[args.index("--digestfile") + 1]).write_text("sha256:" + "0" * 64, encoding="ascii")
            return b""
        with self.assertRaisesRegex(ReleaseError, "pushed_image_mismatch"):
            self._publish(run)

    def test_invalid_signature_refuses_before_any_registry_command(self):
        signature = self.out / "release" / "SHA256SUMS.sig"
        before = signature.read_bytes()
        signature.write_bytes(bytes([before[0] ^ 1]) + before[1:])
        try:
            with patch.object(ci, "command") as run:
                with self.assertRaisesRegex(ReleaseError, "signature_invalid"):
                    self._publish(run)
                run.assert_not_called()
        finally:
            signature.write_bytes(before)

    def test_registry_roundtrip_pulls_same_digest_and_cleans_only_created_container(self):
        verified = ci._verified(self.out / "release", ci.FIXTURE_SOURCE, self.out / "trusted.pub", self.openssl)
        calls, remote = [], {}
        def run(args, **kwargs):
            calls.append(args)
            if args[:2] == ["docker", "port"]:
                return b"127.0.0.1:54321\n"
            if args[0] == "docker":
                return b""
            if args[1] == "inspect":
                if args[-1] not in remote:
                    raise ci.CommandError(b"Error: manifest unknown")
                return remote[args[-1]]
            if args[-1].startswith("oci:"):
                name = "backend" if "fixture-backend" in args[-2] else "db"
                self.assertIn("@" + verified.manifest["images"][name]["digest"], args[-2])
                shutil.copytree(self.out / "images" / name, args[-1][4:])
                return b""
            name = "backend" if "fixture-backend" in args[-1] else "db"
            digest = verified.manifest["images"][name]["digest"]
            remote[args[-1]] = (self.out / "images" / name / "blobs/sha256" / digest[7:]).read_bytes()
            Path(args[args.index("--digestfile") + 1]).write_text(digest, encoding="ascii")
            return b""
        with patch("urllib.request.build_opener") as opener:
            opener.return_value.open.return_value.__enter__.return_value.status = 200
            ci.registry_roundtrip(self.out / "images", verified, "registry:3.0.0@sha256:" + "1" * 64, run=run)
        create = calls[0]
        name = create[create.index("--name") + 1]
        self.assertEqual(calls[-1], ["docker", "rm", "--force", "--volumes", name])
        self.assertEqual(create[create.index("--publish") + 1], "127.0.0.1::5000")
        pulls = [args for args in calls if args[:2] == ["skopeo", "copy"] and args[-1].startswith("oci:")]
        self.assertEqual(len(pulls), 2)
        self.assertTrue(all("--preserve-digests" in args for args in pulls))
        self.assertTrue((self.out / "release" / "install-release.py").is_file())

    def test_non_loopback_registry_and_floating_image_refused(self):
        verified = ci._verified(self.out / "release", ci.FIXTURE_SOURCE, self.out / "trusted.pub", self.openssl)
        with self.assertRaisesRegex(ReleaseError, "fixture_registry_invalid"):
            ci._copy_images(self.out / "images", verified, run=lambda *a, **kw: b"", registry="example.org:5000")
        with self.assertRaisesRegex(ReleaseError, "registry_image_unpinned"):
            ci.registry_roundtrip(self.out / "images", verified, "registry:3.0.0", run=lambda *a, **kw: b"")


if __name__ == "__main__":
    unittest.main()
