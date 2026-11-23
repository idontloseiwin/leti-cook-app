"""Sign checked release checksum bytes with an owner-provided Ed25519 PEM key.

This release-tool dependency is already pinned in the development/CI lock. The
host verifier itself uses only the standard library and OpenSSL 3.
"""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from ops.release_manifest import (
    MAX_ARCHIVE, MAX_MANIFEST, ReleaseError, load_source, strict_json, validate_manifest,
)
from ops.release_verification import MAX_SUMS, _capture, _checksums


def sign_release(directory, private_key_path, *, source=None):
    """Create only SHA256SUMS.sig; never overwrite existing signatures."""
    source = load_source() if source is None else source
    directory = Path(directory)
    metadata = _capture(directory / "release.json", MAX_MANIFEST)
    manifest = validate_manifest(strict_json(metadata), source)
    name = manifest["package"]["name"]
    archive = _capture(directory / name, MAX_ARCHIVE)
    sums = _capture(directory / "SHA256SUMS", MAX_SUMS)
    hashes = _checksums(sums, name)
    digest = hashlib.sha256(archive).hexdigest()
    if (hashlib.sha256(metadata).hexdigest() != hashes["release.json"] or digest != hashes[name]
            or digest != manifest["package"]["sha256"] or len(archive) != manifest["package"]["size"]):
        raise ReleaseError("release_digest_mismatch")
    key_bytes = _capture(private_key_path, 4096)
    try:
        key = serialization.load_pem_private_key(key_bytes, password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError()
    except (ValueError, TypeError):
        raise ReleaseError("release_signing_key_invalid") from None
    signature = key.sign(sums)
    destination = directory / "SHA256SUMS.sig"
    try:
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
        with os.fdopen(fd, "wb") as stream:
            stream.write(signature)
    except OSError:
        raise ReleaseError("release_signature_output_unavailable") from None
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--private-key-file", required=True, type=Path)
    args = parser.parse_args()
    try:
        sign_release(args.directory, args.private_key_file)
        print("Release checksum signature created.")
    except (ValueError, OSError):
        parser.exit(1, "Release signing failed. No signing key was printed.\n")


if __name__ == "__main__":
    main()
