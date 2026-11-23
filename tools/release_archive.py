"""Build deterministic release artifacts from an already verified source bundle."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import install
from application.release_version import parse_version, parse_version_file
from ops.release_manifest import ReleaseError, load_source, parse_source, read_schema_version, serialize_manifest


def canonical_json(value):
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("ascii")


def _write_archive(files, destination):
    """Write captured verified bytes; never revisit mutable source paths."""
    with destination.open("xb") as target:
        with gzip.GzipFile(filename="", mode="wb", fileobj=target, mtime=0, compresslevel=9) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
                for name, data in sorted(files.items()):
                    entry = tarfile.TarInfo(name)
                    entry.size = len(data)
                    entry.mode = 0o644
                    entry.uid = entry.gid = entry.mtime = 0
                    entry.uname = entry.gname = ""
                    archive.addfile(entry, io.BytesIO(data))


def create_release(bundle, output, images, minimum_upgrade_version, *, source=None):
    """Return paths to the three unsigned artifacts; signing is a separate step.

    `source` is an explicit internal dependency for offline fixtures. The CLI
    always reads the canonical repository configuration and rejects placeholders.
    No network, Docker, database operation, or signing key is needed here.
    """
    source = load_source() if source is None else source
    manifest, files = install.verify_bundle(bundle)
    if parse_source(files["application/release-source.json"]) != source:
        raise ReleaseError("release_source_mismatch")
    version = parse_version_file(files["application/VERSION"])
    minimum = str(parse_version(minimum_upgrade_version))
    if parse_version(minimum) > parse_version(version):
        raise ValueError("release_minimum_newer_than_version")
    schema_version = read_schema_version(files["schema/schema.sql"], json.loads(files["schema/manifest.json"]))
    captured = {**files, "bundle.json": canonical_json(manifest)}
    output = install.safe_path(output)
    if output.exists():
        raise ValueError("new_output_directory_required")
    archive_name = f"leti-cook-{version}.tar.gz"
    # Validate policy metadata before creating output. The final hash/size follow
    # archive creation, but image references and identity can already be checked.
    metadata = {
        "format": "leti-cook-release", "format_version": 1,
        "version": version, "schema_version": schema_version,
        "minimum_upgrade_version": minimum,
        "package": {"name": archive_name, "sha256": "0" * 64, "size": 1},
        "images": images,
    }
    serialize_manifest(metadata, source)
    output.mkdir(parents=False)
    archive_path = output / archive_name
    _write_archive(captured, archive_path)
    with archive_path.open("rb") as archive_file:
        digest = hashlib.file_digest(archive_file, "sha256").hexdigest()
    metadata["package"].update(sha256=digest, size=archive_path.stat().st_size)
    release_bytes = serialize_manifest(metadata, source)
    (output / "release.json").write_bytes(release_bytes)
    sums = {archive_name: digest, "release.json": hashlib.sha256(release_bytes).hexdigest()}
    (output / "SHA256SUMS").write_bytes("".join(
        f"{sums[name]}  {name}\n" for name in sorted(sums)
    ).encode("ascii"))
    return {"version": version, "archive": str(archive_path), "sha256": digest,
            "files": len(captured), "signed": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--minimum-upgrade-version", required=True)
    parser.add_argument("--backend-digest", required=True)
    parser.add_argument("--db-digest", required=True)
    args = parser.parse_args()
    try:
        source = load_source()
        images = {name: {"repository": source.image_repository(name),
                         "digest": digest, "platform": "linux/amd64"}
                  for name, digest in (("backend", args.backend_digest), ("db", args.db_digest))}
        result = create_release(args.bundle, args.output, images, args.minimum_upgrade_version, source=source)
        print(json.dumps(result, sort_keys=True))
    except (ValueError, OSError, KeyError, UnicodeError, tarfile.TarError) as error:
        parser.exit(1, f"Release artifacts were not completed: {type(error).__name__}.\n")


if __name__ == "__main__":
    main()
