"""Prepare OCI images once, sign their identity, then publish those same bytes.

This is CI tooling, not a general registry client. Production names and trust
come only from the canonical release configuration. The separate dry-run path
uses a temporary fixture bundle and a loopback registry, never GHCR.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time
import uuid

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import install
from application.release_version import parse_version, parse_version_file
from ops.release_manifest import ReleaseError, ReleaseSource, load_source, parse_source, strict_json
from ops.release_verification import _capture, verify_release
from tools.release_archive import canonical_json, create_release
from tools.sign_release import sign_release

NAMES = ("backend", "db")
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_CONFIG = "application/vnd.oci.image.config.v1+json"
OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_LAYERS = {"application/vnd.oci.image.layer.v1.tar", "application/vnd.oci.image.layer.v1.tar+gzip"}
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
MAX_JSON = 1024 * 1024
MAX_BLOB = 2 * 1024**3
MAX_OCI = 4 * 1024**3
FIXTURE_SOURCE = ReleaseSource("leti-cook-ci", "release-fixture")


class CommandError(ReleaseError):
    def __init__(self, stderr):
        super().__init__("release_command_failed")
        self.stderr = stderr


def command(args, *, timeout=600, capture=False):
    result = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.PIPE if capture else None, timeout=timeout, check=False)
    if result.returncode:
        # The caller may classify one documented registry error. Do not print
        # credential files, remote bodies, or signing material in error output.
        raise CommandError((result.stderr or b"")[-16384:])
    if capture and len(result.stdout) > MAX_JSON:
        raise ReleaseError("release_command_output_too_large")
    return result.stdout if capture else b""


def _new_directory(path):
    path = Path(path).absolute()
    if path.exists() or path.is_symlink():
        raise ReleaseError("new_output_directory_required")
    path.mkdir(mode=0o700, parents=False)
    return path


def _write(path, data):
    with Path(path).open("xb") as stream:
        stream.write(data)


def _json(path):
    return strict_json(_capture(path, MAX_JSON), limit=MAX_JSON)


def image_tag(version):
    version = str(parse_version(version))
    tag = "v" + version.replace("+", "_")
    if len(tag) > 128:
        raise ReleaseError("release_image_tag_too_long")
    return tag


def _descriptor(value, allowed):
    if (type(value) is not dict or value.get("mediaType") not in allowed
            or type(value.get("digest")) is not str or not DIGEST.fullmatch(value["digest"])
            or type(value.get("size")) is not int or not 0 < value["size"] <= MAX_BLOB
            or set(value) - {"mediaType", "digest", "size", "annotations", "platform"}):
        raise ReleaseError("release_oci_descriptor_invalid")
    if "platform" in value and value["platform"] != {"architecture": "amd64", "os": "linux"}:
        raise ReleaseError("release_oci_platform_invalid")
    return value


def inspect_oci(path):
    """Hash the complete selected manifest graph; reject indexes/foreign layers.

    Buildx's outer layout index must contain exactly one image manifest. There
    is no architecture selection at publish time and no Docker image-ID proxy.
    """
    path = Path(path).absolute()
    if _json(path / "oci-layout") != {"imageLayoutVersion": "1.0.0"}:
        raise ReleaseError("release_oci_layout_invalid")
    index = _json(path / "index.json")
    if (type(index) is not dict or index.get("schemaVersion") != 2
            or type(index["schemaVersion"]) is not int
            or index.get("mediaType", OCI_INDEX) != OCI_INDEX
            or type(index.get("manifests")) is not list or len(index["manifests"]) != 1
            or set(index) - {"schemaVersion", "mediaType", "manifests", "annotations"}):
        raise ReleaseError("release_oci_index_invalid")
    selected = _descriptor(index["manifests"][0], {OCI_MANIFEST})
    descriptors = {}

    def blob(descriptor, *, as_json=False):
        digest, size = descriptor["digest"], descriptor["size"]
        old = descriptors.get(digest)
        if old is not None and old != size:
            raise ReleaseError("release_oci_descriptor_invalid")
        descriptors[digest] = size
        if sum(descriptors.values()) > MAX_OCI:
            raise ReleaseError("release_oci_too_large")
        filename = path / "blobs" / "sha256" / digest[7:]
        for part in (filename, *filename.parents):
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 1024:
                raise ReleaseError("release_oci_linked_input")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        with os.fdopen(os.open(filename, flags), "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size != size:
                raise ReleaseError("release_oci_blob_invalid")
            hasher = hashlib.sha256()
            remaining = size
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ReleaseError("release_oci_blob_invalid")
                hasher.update(chunk)
                remaining -= len(chunk)
            if stream.read(1):
                raise ReleaseError("release_oci_input_changed")
            actual = hasher.hexdigest()
            after = os.fstat(stream.fileno())
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                raise ReleaseError("release_oci_input_changed")
        if actual != digest[7:]:
            raise ReleaseError("release_oci_digest_mismatch")
        return _json(filename) if as_json else None

    manifest = blob(selected, as_json=True)
    if (type(manifest) is not dict or manifest.get("schemaVersion") != 2
            or type(manifest["schemaVersion"]) is not int or manifest.get("mediaType") != OCI_MANIFEST
            or set(manifest) - {"schemaVersion", "mediaType", "config", "layers", "annotations"}
            or type(manifest.get("layers")) is not list or not 1 <= len(manifest["layers"]) <= 128):
        raise ReleaseError("release_oci_manifest_invalid")
    config = blob(_descriptor(manifest.get("config"), {OCI_CONFIG}), as_json=True)
    if type(config) is not dict or (config.get("os"), config.get("architecture")) != ("linux", "amd64"):
        raise ReleaseError("release_oci_platform_invalid")
    for layer in manifest["layers"]:
        blob(_descriptor(layer, OCI_LAYERS))
    # Extra files cannot hide unchecked content in the prepared artifact.
    expected = {"oci-layout", "index.json"} | {"blobs/sha256/" + digest[7:] for digest in descriptors}
    found = set()
    for directory, dirs, files in os.walk(path, followlinks=False):
        for entry in dirs + files:
            child = Path(directory) / entry
            info = child.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 1024:
                raise ReleaseError("release_oci_linked_input")
            if entry in files:
                found.add(child.relative_to(path).as_posix())
    if found != expected:
        raise ReleaseError("release_oci_file_set_invalid")
    return selected["digest"]


def _bundle(bundle, source):
    manifest, files = install.verify_bundle(Path(bundle))
    if parse_source(files["application/release-source.json"]) != source:
        raise ReleaseError("release_source_mismatch")
    image_tag(parse_version_file(files["application/VERSION"]))
    return manifest, files


def prepare_images(bundle, output, *, builder="leti-cook-release", source=None, run=command):
    source = load_source() if source is None else source
    bundle_manifest, _ = _bundle(bundle, source)
    bundle_digest = hashlib.sha256(canonical_json(bundle_manifest)).hexdigest()
    if type(builder) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", builder) is None:
        raise ReleaseError("release_builder_invalid")
    output = _new_directory(output)
    images = {}
    for name, context in (("backend", "application"), ("db", "database")):
        destination = output / name
        if "," in str(destination) or (os.name != "nt" and ":" in str(destination)):
            raise ReleaseError("release_oci_path_invalid")
        run(["docker", "buildx", "build", "--builder", builder, "--platform", "linux/amd64", "--provenance=false", "--sbom=false",
             "--output", f"type=oci,dest={destination},tar=false,oci-mediatypes=true,compression=gzip",
             str(Path(bundle).absolute() / context)], timeout=1800)
        images[name] = {"repository": source.image_repository(name), "digest": inspect_oci(destination),
                        "platform": "linux/amd64"}
    current, _ = _bundle(bundle, source)
    if hashlib.sha256(canonical_json(current)).hexdigest() != bundle_digest:
        raise ReleaseError("release_build_bundle_changed")
    _write(output / "images.json", canonical_json(images))
    _write(output / "build.json", canonical_json({"format": "leti-cook-oci-build", "version": 1,
                                                 "bundle_sha256": bundle_digest}))
    return images


def prepared_images(prepared, *, source):
    value = _json(Path(prepared) / "images.json")
    expected = {name: {"repository": source.image_repository(name), "digest": inspect_oci(Path(prepared) / name),
                       "platform": "linux/amd64"} for name in NAMES}
    if value != expected:
        raise ReleaseError("release_prepared_images_mismatch")
    return expected


def _assert_build_bundle(prepared, bundle_bytes):
    receipt = _json(Path(prepared) / "build.json")
    expected = {"format": "leti-cook-oci-build", "version": 1,
                "bundle_sha256": hashlib.sha256(bundle_bytes).hexdigest()}
    if receipt != expected or type(receipt.get("version")) is not int:
        raise ReleaseError("release_build_bundle_mismatch")


def build_artifacts(bundle, prepared, output, minimum_upgrade_version, *, source=None):
    source = load_source() if source is None else source
    manifest, _ = _bundle(bundle, source)
    _assert_build_bundle(prepared, canonical_json(manifest))
    return create_release(bundle, output, prepared_images(prepared, source=source),
                          minimum_upgrade_version, source=source)


def _verified(directory, source, key, openssl):
    metadata = _json(Path(directory) / "release.json")
    # validate_manifest closes the package basename before it becomes a path.
    from ops.release_manifest import validate_manifest
    metadata = validate_manifest(metadata, source)
    return verify_release(Path(directory) / metadata["package"]["name"], public_key_path=key,
                          source=source, openssl=openssl)


def _remote_manifest(reference, *, run, authfile=None, insecure=False, missing_ok=False):
    args = ["skopeo", "inspect", "--raw"]
    if authfile is not None:
        args += ["--authfile", str(authfile)]
    if insecure:
        args += ["--tls-verify=false"]
    try:
        return run(args + ["docker://" + reference], timeout=180, capture=True)
    except CommandError as error:
        # skopeo has no structured status output. Only explicit registry missing
        # manifest/name errors permit a first push; auth/TLS/network errors stop.
        text = error.stderr.decode("utf-8", "replace").lower().strip()
        if missing_ok and re.search(r": (?:manifest unknown|name unknown)(?:: [^\n]*)?[\"]?$", text):
            return None
        raise


def _copy_images(prepared, release, *, run, authfile=None, registry=None):
    images = release.manifest["images"]
    tag = image_tag(release.manifest["version"])
    insecure = registry is not None
    if registry is not None and re.fullmatch(r"127\.0\.0\.1:[1-9][0-9]{0,4}", registry) is None:
        raise ReleaseError("release_fixture_registry_invalid")
    references = {name: (images[name]["repository"] if registry is None else f"{registry}/fixture-{name}")
                  + ":" + tag for name in NAMES}
    exists = {}
    # Check both names before any registry mutation.
    for name, reference in references.items():
        existing = _remote_manifest(reference, run=run, authfile=authfile, insecure=insecure, missing_ok=True)
        exists[name] = existing is not None
        if existing is not None and "sha256:" + hashlib.sha256(existing).hexdigest() != images[name]["digest"]:
            raise ReleaseError("release_existing_image_conflict")
    for name, reference in references.items():
        if not exists[name]:
            with tempfile.TemporaryDirectory(prefix="leti-cook-image-push-") as temporary:
                digestfile = Path(temporary) / "digest"
                args = ["skopeo", "copy", "--all", "--preserve-digests", "--digestfile", str(digestfile)]
                if authfile is not None:
                    args += ["--authfile", str(authfile)]
                if insecure:
                    args += ["--dest-tls-verify=false"]
                run(args + ["oci:" + str(Path(prepared).absolute() / name), "docker://" + reference], timeout=1800)
                if _capture(digestfile, 80).strip().decode("ascii") != images[name]["digest"]:
                    raise ReleaseError("release_pushed_image_mismatch")
        remote = _remote_manifest(reference, run=run, authfile=authfile, insecure=insecure)
        if "sha256:" + hashlib.sha256(remote).hexdigest() != images[name]["digest"]:
            raise ReleaseError("release_remote_image_mismatch")
    return {name: {"reference": references[name], "digest": images[name]["digest"]} for name in NAMES}


def publish_images(prepared, release_directory, *, authfile=None, source=None,
                   public_key_path=None, openssl="openssl", run=command):
    source = load_source() if source is None else source
    release = _verified(release_directory, source, public_key_path, openssl)
    _assert_build_bundle(prepared, release.files["bundle.json"])
    if prepared_images(prepared, source=source) != release.manifest["images"]:
        raise ReleaseError("release_signed_images_mismatch")
    return _copy_images(prepared, release, run=run, authfile=authfile)


def _fixture_bundle(bundle, output, public_key):
    manifest, files = install.verify_bundle(Path(bundle))
    files = dict(files)
    files["application/release-source.json"] = canonical_json({
        "format": "leti-cook-release-source", "format_version": 1,
        "owner": FIXTURE_SOURCE.owner, "repository": FIXTURE_SOURCE.repository})
    files["application/release-signing.pub"] = public_key
    manifest["files"] = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    target = _new_directory(output)
    for name, data in files.items():
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        _write(path, data)
    _write(target / "bundle.json", canonical_json(manifest))
    install.verify_bundle(target)
    return target


def registry_roundtrip(prepared, release, image, *, run=command):
    if re.fullmatch(r"registry:[0-9]+\.[0-9]+\.[0-9]+@sha256:[0-9a-f]{64}", image) is None:
        raise ReleaseError("release_registry_image_unpinned")
    _assert_build_bundle(prepared, release.files["bundle.json"])
    if prepared_images(prepared, source=FIXTURE_SOURCE) != release.manifest["images"]:
        raise ReleaseError("release_signed_images_mismatch")
    name = "leti-cook-release-ci-" + uuid.uuid4().hex
    created = False
    try:
        run(["docker", "run", "--detach", "--name", name, "--label", "org.leti-cook.release-ci=true",
             "--memory", "256m", "--cpus", "1", "--pids-limit", "64", "--publish", "127.0.0.1::5000",
             "--env", "REGISTRY_STORAGE_DELETE_ENABLED=false", "--mount", "type=volume,destination=/var/lib/registry",
             image], timeout=180)
        created = True
        port = run(["docker", "port", name, "5000/tcp"], timeout=30, capture=True).decode("ascii").strip()
        if re.fullmatch(r"127\.0\.0\.1:[1-9][0-9]{0,4}", port) is None:
            raise ReleaseError("release_fixture_registry_invalid")
        # Readiness needs no external network. Connection failure before the
        # registry starts is bounded; publication itself is never blindly retried.
        import urllib.request
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for attempt in range(20):
            try:
                with opener.open(f"http://{port}/v2/", timeout=2) as response:
                    if response.status == 200:
                        break
            except OSError:
                pass
            if attempt == 19:
                raise ReleaseError("release_fixture_registry_unready")
            time.sleep(0.25)
        result = _copy_images(prepared, release, run=run, registry=port)
        with tempfile.TemporaryDirectory(prefix="leti-cook-registry-proof-") as temporary:
            for item, metadata in release.manifest["images"].items():
                pulled = Path(temporary) / item
                reference = f"{port}/fixture-{item}@{metadata['digest']}"
                run(["skopeo", "copy", "--all", "--preserve-digests", "--src-tls-verify=false",
                     "docker://" + reference, "oci:" + str(pulled)], timeout=1800)
                if inspect_oci(pulled) != metadata["digest"]:
                    raise ReleaseError("release_fixture_roundtrip_mismatch")
        # Repeat is an identity check; no copy is needed for existing same bytes.
        if _copy_images(prepared, release, run=run, registry=port) != result:
            raise ReleaseError("release_fixture_roundtrip_mismatch")
        return result
    finally:
        if created:
            run(["docker", "rm", "--force", "--volumes", name], timeout=60)


def dry_run(bundle, output, *, builder="leti-cook-release", registry_image=None, run=command, openssl="openssl"):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    output = _new_directory(output)
    fixture = _fixture_bundle(bundle, output / "bundle", public)
    _write(output / "trusted.pub", public)
    prepared = output / "images"
    prepare_images(fixture, prepared, builder=builder, source=FIXTURE_SOURCE, run=run)
    version = parse_version_file((fixture / "application/VERSION").read_bytes())
    result = build_artifacts(fixture, prepared, output / "release", version, source=FIXTURE_SOURCE)
    with tempfile.TemporaryDirectory(prefix="leti-cook-ci-key-") as temporary:
        private = Path(temporary) / "key.pem"
        fd = os.open(private, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
        sign_release(output / "release", private, source=FIXTURE_SOURCE)
    verified = _verified(output / "release", FIXTURE_SOURCE, output / "trusted.pub", openssl)
    signature = output / "release" / "SHA256SUMS.sig"
    original = signature.read_bytes()
    try:
        signature.write_bytes(bytes([original[0] ^ 1]) + original[1:])
        try:
            _verified(output / "release", FIXTURE_SOURCE, output / "trusted.pub", openssl)
        except ReleaseError as error:
            if str(error) != "release_signature_invalid":
                raise
        else:
            raise ReleaseError("release_fixture_tamper_accepted")
    finally:
        signature.write_bytes(original)
    from tools.build_release_bootstrap import build_bootstrap
    build_bootstrap(fixture, output / "release" / "install-release.py")
    if registry_image is not None:
        registry_roundtrip(prepared, verified, registry_image, run=run)
    return {"version": result["version"], "signed": True, "verified": True, "tamper_rejected": True,
            "registry_roundtrip": registry_image is not None, "fixture_only": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare-images")
    prepare.add_argument("--bundle", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--builder", required=True)
    artifacts = commands.add_parser("build-artifacts")
    artifacts.add_argument("--bundle", type=Path, required=True)
    artifacts.add_argument("--prepared", type=Path, required=True)
    artifacts.add_argument("--output", type=Path, required=True)
    artifacts.add_argument("--minimum-upgrade-version", required=True)
    publish = commands.add_parser("publish-images")
    publish.add_argument("--prepared", type=Path, required=True)
    publish.add_argument("--release-directory", type=Path, required=True)
    publish.add_argument("--authfile", type=Path)
    dry = commands.add_parser("dry-run")
    dry.add_argument("--bundle", type=Path, required=True)
    dry.add_argument("--output", type=Path, required=True)
    dry.add_argument("--registry-image", required=True)
    dry.add_argument("--builder", required=True)
    args = parser.parse_args()
    try:
        if args.command == "prepare-images":
            result = prepare_images(args.bundle, args.output, builder=args.builder)
        elif args.command == "build-artifacts":
            result = build_artifacts(args.bundle, args.prepared, args.output, args.minimum_upgrade_version)
        elif args.command == "publish-images":
            result = publish_images(args.prepared, args.release_directory, authfile=args.authfile)
        else:
            result = dry_run(args.bundle, args.output, builder=args.builder, registry_image=args.registry_image)
        print(json.dumps(result, sort_keys=True))
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Release CI step failed: {type(error).__name__}.\n")


if __name__ == "__main__":
    main()
