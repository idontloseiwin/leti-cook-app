"""Actual trusted-checkout package -> bootstrap -> signer -> OpenSSL chain.

Every source file is captured in a temporary checkout. The only private key is
freshly generated in memory; no GitHub, registry, Docker or network is involved.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import install
from application.release_version import parse_version_file
from ops.release_manifest import ReleaseError, ReleaseSource
from tools import package_release, release_workflow
from tools.build_release_bootstrap import build_bootstrap
from tools.package_extension import FILES as EXTENSION_FILES, package_extension
from tools.release_archive import canonical_json, create_release


class ReleaseSigningChainTests(unittest.TestCase):
    def test_actual_signer_accepts_only_unchanged_trusted_checkout_artifacts(self):
        openssl = shutil.which("openssl")
        if openssl is None and os.name == "nt":
            candidate = Path("C:/Program Files/Git/usr/bin/openssl.exe")
            if candidate.is_file():
                openssl = str(candidate)
        self.assertIsNotNone(openssl, "Real OpenSSL 3 is required for the signing chain")
        with tempfile.TemporaryDirectory(prefix="leti-cook-signing-chain-") as temporary:
            root = Path(temporary)
            original_bundle = root / "captured"
            package_release.package(original_bundle)
            _, files = install.verify_bundle(original_bundle)
            selection = json.loads((ROOT / "packaging/files.json").read_bytes())
            checkout = root / "checkout"
            checkout.mkdir()
            # Reconstruct the actual package selection, including source aliases
            # such as install.sh <- packaging/install.sh, without importing it.
            for target, source_path in selection.items():
                path = checkout / source_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(files[target])
            (checkout / "packaging/files.json").write_bytes(canonical_json(selection))
            # The trusted checkout also owns the separate extension artifacts.
            for name in EXTENSION_FILES:
                path = checkout / "extensions/chromium" / name
                data = (ROOT / "extensions/chromium" / name).read_bytes()
                if path.exists():
                    self.assertEqual(path.read_bytes(), data, name)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
            key = Ed25519PrivateKey.generate()
            private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()).decode("ascii")
            public = key.public_key().public_bytes(serialization.Encoding.PEM,
                                                   serialization.PublicFormat.SubjectPublicKeyInfo)
            source = ReleaseSource("signing-chain-owner", "signing-chain-app")
            (checkout / "application/release-signing.pub").write_bytes(public)
            (checkout / "application/release-source.json").write_bytes(canonical_json({
                "format": "leti-cook-release-source", "format_version": 1,
                "owner": source.owner, "repository": source.repository}))
            version = parse_version_file(files["application/VERSION"])
            prepared = root / "bundle"
            package_release.package(prepared, root=checkout)
            images = {name: {"repository": source.image_repository(name),
                             "digest": "sha256:" + ("a" if name == "backend" else "b") * 64,
                             "platform": "linux/amd64"} for name in ("backend", "db")}
            release = root / "release"
            artifacts = create_release(prepared, release, images, version, source=source)
            bootstrap = build_bootstrap(checkout, release / "install-release.py")
            package_extension(release, root=checkout)
            signature = release / "SHA256SUMS.sig"
            key_directories = []
            real_temporary = tempfile.TemporaryDirectory

            def temporary_directory(*args, **kwargs):
                result = real_temporary(*args, **kwargs)
                if kwargs.get("prefix") == "leti-cook-sign-":
                    key_directories.append(Path(result.name))
                return result

            key_environment = {"LETI_COOK_RELEASE_SIGNING_KEY": private}
            # Production helper invokes 'openssl' itself: keep its real command
            # and give Windows the installed OpenSSL directory, not a fake.
            environment = {"PATH": str(Path(openssl).parent) + os.pathsep + os.environ.get("PATH", "")}
            with patch.dict(os.environ, environment), \
                    patch.object(release_workflow.tempfile, "TemporaryDirectory", side_effect=temporary_directory):
                for path, expected in (
                        (checkout / "README.md", "release_workflow_source_mismatch"),
                        (Path(artifacts["archive"]), "release_workflow_source_mismatch"),
                        (bootstrap, "release_workflow_bootstrap_mismatch"),
                        (checkout / "extensions/chromium/content.js", "release_workflow_extension_mismatch"),
                        (release / "extension-files.json", "release_workflow_extension_mismatch")):
                    with self.subTest(changed=path.name):
                        original = path.read_bytes()
                        path.write_bytes(original + b"\nchanged after preparation\n")
                        try:
                            with self.assertRaisesRegex(ReleaseError, "^" + expected + "$"):
                                release_workflow.sign_from_environment(checkout, release, environ=key_environment)
                            self.assertFalse(signature.exists())
                            self.assertEqual(key_directories, [], "No private-key file before checkout checks")
                        finally:
                            path.write_bytes(original)
                verified = release_workflow.sign_from_environment(checkout, release, environ=key_environment)
            self.assertEqual(signature.stat().st_size, 64)
            self.assertEqual(verified.manifest["version"], version)
            self.assertEqual(verified.files["application/VERSION"], (checkout / "application/VERSION").read_bytes())
            self.assertEqual(verified.files["application/release-signing.pub"], public)
            self.assertEqual(verified.manifest["images"], images)
            self.assertEqual(len(key_directories), 1)
            self.assertFalse(key_directories[0].exists(), "Temporary private key must be deleted after success")
            for name, data in verified.files.items():
                self.assertNotIn(private.encode("ascii"), data, name)
            self.assertEqual((ROOT / "application/release-source.json").read_bytes(),
                             (original_bundle / "application/release-source.json").read_bytes())


if __name__ == "__main__":
    unittest.main()
