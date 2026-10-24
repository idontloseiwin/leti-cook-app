"""Verify a signed release offline without executing or extracting candidate code.

Only captured immutable bytes leave this module. Installation code must consume
those bytes rather than reopen the candidate archive after verification.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import gzip
import hashlib
import io
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tarfile
import tempfile
from types import MappingProxyType
from typing import Mapping

from application.release_version import parse_version_file
from ops.bundle_contract import FORMAT, MAX_FILE, MAX_FILES, MAX_TOTAL, REQUIRED_BUNDLE_FILES
from ops.release_manifest import (
    HASH, MAX_ARCHIVE, MAX_MANIFEST, ROOT, ReleaseError, ReleaseSource,
    load_source, parse_source, strict_json, validate_manifest,
)
from ops.schema_manifest import validate_schema_bundle

MAX_BUNDLE_MANIFEST = 256 * 1024
MAX_TAR = MAX_TOTAL + 1024 * 1024
MAX_SUMS = 2048


@dataclass(frozen=True)
class VerifiedRelease:
    manifest: Mapping
    files: Mapping[str, bytes]


def _readonly(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _readonly(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_readonly(item) for item in value)
    return value


def _capture(path, maximum):
    """Bounded capture of a regular unlinked input; no later candidate reread."""
    path = Path(path).absolute()
    try:
        for component in (path, *path.parents):
            info = component.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 1024:
                raise ReleaseError("release_linked_input")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        with os.fdopen(os.open(path, flags), "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not 1 <= before.st_size <= maximum:
                raise ReleaseError("release_input_invalid")
            data = stream.read(maximum + 1)
            after = os.fstat(stream.fileno())
            if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                    != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                    or len(data) != before.st_size):
                raise ReleaseError("release_input_changed")
            return data
    except OSError:
        raise ReleaseError("release_input_unavailable") from None


def _public_key(data):
    """Accept only the Ed25519 SubjectPublicKeyInfo PEM, never a candidate key."""
    try:
        lines = data.decode("ascii").splitlines()
        if not 3 <= len(lines) <= 5 or lines[0] != "-----BEGIN PUBLIC KEY-----" or lines[-1] != "-----END PUBLIC KEY-----":
            raise ValueError()
        der = base64.b64decode("".join(lines[1:-1]), validate=True)
        if len(der) != 44 or der[:12] != bytes.fromhex("302a300506032b6570032100"):
            raise ValueError()
    except (UnicodeError, ValueError):
        raise ReleaseError("release_public_key_invalid") from None


def _verify_signature(sums, signature, public_key, openssl):
    if len(signature) != 64:
        raise ReleaseError("release_signature_invalid")
    _public_key(public_key)
    try:
        version = subprocess.run([str(openssl), "version"], stdin=subprocess.DEVNULL,
                                 capture_output=True, timeout=5, check=False)
        if version.returncode or re.match(rb"OpenSSL 3\.[0-9]+\.[0-9]+(?:\s|$)", version.stdout) is None:
            raise ReleaseError("release_openssl3_required")
        with tempfile.TemporaryDirectory(prefix="leti-cook-verify-") as temporary:
            directory = Path(temporary)
            directory.chmod(0o700)
            for name, data in (("SHA256SUMS", sums), ("signature", signature), ("trusted.pub", public_key)):
                fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as target:
                    target.write(data)
            result = subprocess.run([
                str(openssl), "pkeyutl", "-verify", "-pubin", "-rawin",
                "-inkey", str(directory / "trusted.pub"), "-in", str(directory / "SHA256SUMS"),
                "-sigfile", str(directory / "signature"),
            ], stdin=subprocess.DEVNULL, capture_output=True, timeout=10, check=False)
            if result.returncode:
                raise ReleaseError("release_signature_invalid")
    except (OSError, subprocess.SubprocessError):
        raise ReleaseError("release_signature_check_unavailable") from None


def _checksums(data, package_name):
    try:
        rows = data.decode("ascii").splitlines(keepends=True)
    except UnicodeError:
        raise ReleaseError("release_checksums_invalid") from None
    hashes = {}
    for row in rows:
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9_.+-]+)\n", row)
        if match is None or match[2] in hashes:
            raise ReleaseError("release_checksums_invalid")
        hashes[match[2]] = match[1]
    if len(rows) != 2 or list(hashes) != sorted(hashes) or set(hashes) != {"release.json", package_name}:
        raise ReleaseError("release_checksums_invalid")
    return hashes


def _path(name):
    if type(name) is not str or not 1 <= len(name) <= 255:
        raise ReleaseError("release_archive_path_invalid")
    path = PurePosixPath(name)
    if (not path.parts or path.is_absolute() or path.as_posix() != name or "\\" in name or ":" in name
            or any(part.startswith(".") for part in path.parts)
            or any(ord(char) < 32 or ord(char) > 126 for char in name)):
        raise ReleaseError("release_archive_path_invalid")
    return name


def _archive_files(data):
    # Read with a hard decompressed ceiling. Metadata records are not delegated
    # to tarfile's permissive streaming parser: only normalized USTAR files pass.
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data), mode="rb") as stream:
            raw = stream.read(MAX_TAR + 1)
        if len(raw) > MAX_TAR or len(raw) % 512:
            raise ReleaseError("release_archive_size_invalid")
        files, offset = {}, 0
        while offset + 512 <= len(raw):
            block = raw[offset:offset + 512]
            if block == bytes(512):
                if len(raw) - offset < 1024 or any(raw[offset:]):
                    raise ReleaseError("release_archive_trailing_data")
                return files
            if (block[257:263] != b"ustar\0" or block[263:265] != b"00"
                    or block[156:157] != tarfile.REGTYPE):
                raise ReleaseError("release_archive_member_invalid")
            member = tarfile.TarInfo.frombuf(block, "utf-8", "strict")
            name = _path(member.name)
            if (name in files or len(files) >= MAX_FILES + 1 or not 0 <= member.size <= MAX_FILE
                    or (member.uid, member.gid, member.mtime, member.mode) != (0, 0, 0, 0o644)
                    or member.uname or member.gname or member.linkname):
                raise ReleaseError("release_archive_member_invalid")
            start = offset + 512
            end = start + member.size
            offset = start + ((member.size + 511) // 512) * 512
            if offset > len(raw) or any(raw[end:offset]):
                raise ReleaseError("release_archive_member_invalid")
            files[name] = raw[start:end]
        raise ReleaseError("release_archive_incomplete")
    except (OSError, EOFError, tarfile.TarError, UnicodeError):
        raise ReleaseError("release_archive_invalid") from None


def _bundle(files, release, source):
    if "bundle.json" not in files:
        raise ReleaseError("release_bundle_missing")
    bundle = strict_json(files["bundle.json"], limit=MAX_BUNDLE_MANIFEST)
    keys = {"format", "version", "fresh_install_only", "stage", "release_accepted", "youtube_configuration_included", "files"}
    if (type(bundle) is not dict or set(bundle) != keys or bundle["format"] != FORMAT
            or type(bundle["version"]) is not int or bundle["version"] != 1
            or bundle["fresh_install_only"] is not True or bundle["release_accepted"] is not False
            or bundle["youtube_configuration_included"] is not False
            or bundle["stage"] != "self-hosted-release-candidate"):
        raise ReleaseError("release_bundle_invalid")
    hashes = bundle["files"]
    if (type(hashes) is not dict or not 10 <= len(hashes) <= MAX_FILES
            or set(files) != set(hashes) | {"bundle.json"} or not REQUIRED_BUNDLE_FILES <= set(hashes)
            or sum(len(files[name]) for name in hashes) > MAX_TOTAL):
        raise ReleaseError("release_bundle_files_invalid")
    for name, expected in hashes.items():
        _path(name)
        if (type(expected) is not str or HASH.fullmatch(expected) is None
                or hashlib.sha256(files[name]).hexdigest() != expected):
            raise ReleaseError("release_bundle_digest_mismatch")
        if name.endswith((".env", ".pem", ".key", ".sqlite3")) or "node_modules" in name or "/owner_studio/" in name:
            raise ReleaseError("release_bundle_private_file")
    if parse_version_file(files["application/VERSION"]) != release["version"]:
        raise ReleaseError("release_version_mismatch")
    schema_plan = validate_schema_bundle(files)
    if schema_plan.schema_version != release["schema_version"]:
        raise ReleaseError("release_schema_version_mismatch")
    if parse_source(files["application/release-source.json"]) != source:
        raise ReleaseError("release_source_mismatch")


def verify_release(package_path, *, public_key_path=None, source=None, openssl="openssl"):
    """Verify captured assets using the installed trust root, before any install action.

    Source/key arguments are trusted dependency injection for host code and tests;
    no archive-supplied key, code, URL or executable is ever used for verification.
    """
    source = load_source() if source is None else source
    if not isinstance(source, ReleaseSource):
        raise ReleaseError("release_source_invalid")
    package_path = Path(package_path)
    archive = _capture(package_path, MAX_ARCHIVE)
    metadata = _capture(package_path.parent / "release.json", MAX_MANIFEST)
    sums = _capture(package_path.parent / "SHA256SUMS", MAX_SUMS)
    signature = _capture(package_path.parent / "SHA256SUMS.sig", 64)
    key = _capture(public_key_path if public_key_path is not None else ROOT / "application/release-signing.pub", 4096)
    _verify_signature(sums, signature, key, openssl)
    hashes = _checksums(sums, package_path.name)
    if (hashlib.sha256(archive).hexdigest() != hashes[package_path.name]
            or hashlib.sha256(metadata).hexdigest() != hashes["release.json"]):
        raise ReleaseError("release_digest_mismatch")
    release = validate_manifest(strict_json(metadata), source)
    if (release["package"]["name"] != package_path.name or release["package"]["size"] != len(archive)
            or release["package"]["sha256"] != hashes[package_path.name]):
        raise ReleaseError("release_package_mismatch")
    files = _archive_files(archive)
    _bundle(files, release, source)
    if files.get("application/release-signing.pub") != key:
        raise ReleaseError("release_signing_key_changed")
    return VerifiedRelease(_readonly(release), MappingProxyType(files))
