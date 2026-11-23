"""Closed release metadata shared by the offline writer and trusted verifier."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re

from application.release_version import parse_version
from ops.bundle_contract import MAX_TOTAL

ROOT = Path(__file__).resolve().parents[1]
MAX_ARCHIVE = MAX_TOTAL + 1024 * 1024
MAX_MANIFEST = 64 * 1024
HASH = re.compile(r"[0-9a-f]{64}")
SCHEMA_HEADER = b"-- leti-cook self-hosted clean schema; format=1; application-data=none"


class ReleaseError(ValueError):
    """A bounded public failure code, without input payloads or credentials."""

    @property
    def code(self):
        return str(self)


def _keys(value, expected, code):
    if type(value) is not dict or set(value) != set(expected):
        raise ReleaseError(code)


def _integer(value, minimum, maximum):
    return type(value) is int and minimum <= value <= maximum


def strict_json(data: bytes, *, limit=MAX_MANIFEST):
    if type(data) is not bytes or not 1 <= len(data) <= limit:
        raise ReleaseError("release_json_size_invalid")

    def object_pairs(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ReleaseError("release_json_duplicate_key")
            value[key] = item
        return value

    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=object_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise ReleaseError("release_json_invalid") from None


@dataclass(frozen=True)
class ReleaseSource:
    owner: str
    repository: str

    def __post_init__(self):
        if self.owner == "OWNER" or self.repository == "REPOSITORY":
            raise ReleaseError("release_source_unconfigured")
        if (type(self.owner) is not str or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", self.owner)
                or type(self.repository) is not str
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", self.repository)
                or self.repository in {".", ".."}):
            raise ReleaseError("release_source_invalid")

    def image_repository(self, name):
        if name not in {"backend", "db"}:
            raise ReleaseError("release_image_invalid")
        return f"ghcr.io/{self.owner.lower()}/{self.repository.lower()}-{name}"


def parse_source(data):
    value = strict_json(data, limit=16384)
    _keys(value, {"format", "format_version", "owner", "repository"}, "release_source_invalid")
    if value["format"] != "leti-cook-release-source" or type(value["format_version"]) is not int or value["format_version"] != 1:
        raise ReleaseError("release_source_invalid")
    return ReleaseSource(value["owner"], value["repository"])


def load_source(path=None):
    target = Path(path) if path is not None else ROOT / "application/release-source.json"
    try:
        with target.open("rb") as stream:
            return parse_source(stream.read(16385))
    except OSError:
        raise ReleaseError("release_source_unavailable") from None


def validate_manifest(value, source=None):
    source = load_source() if source is None else source
    if not isinstance(source, ReleaseSource):
        raise ReleaseError("release_source_invalid")
    _keys(value, {"format", "format_version", "version", "schema_version", "minimum_upgrade_version", "package", "images"},
          "release_manifest_invalid")
    if (value["format"] != "leti-cook-release" or type(value["format_version"]) is not int
            or value["format_version"] != 1 or not _integer(value["schema_version"], 1, 1000000)):
        raise ReleaseError("release_manifest_invalid")
    try:
        version = parse_version(value["version"])
        minimum = parse_version(value["minimum_upgrade_version"])
    except ValueError:
        raise ReleaseError("release_version_invalid") from None
    if minimum > version:
        raise ReleaseError("release_minimum_newer_than_version")
    package = value["package"]
    _keys(package, {"name", "sha256", "size"}, "release_package_invalid")
    if (package["name"] != f"leti-cook-{version}.tar.gz"
            or type(package["sha256"]) is not str or HASH.fullmatch(package["sha256"]) is None
            or not _integer(package["size"], 1, MAX_ARCHIVE)):
        raise ReleaseError("release_package_invalid")
    _keys(value["images"], {"backend", "db"}, "release_images_invalid")
    images = {}
    for name, item in value["images"].items():
        _keys(item, {"repository", "digest", "platform"}, "release_image_invalid")
        if (item["repository"] != source.image_repository(name) or item["platform"] != "linux/amd64"
                or type(item["digest"]) is not str or re.fullmatch(r"sha256:[0-9a-f]{64}", item["digest"]) is None):
            raise ReleaseError("release_image_invalid")
        images[name] = dict(item)
    return {**value, "package": dict(package), "images": images}


def serialize_manifest(value, source=None):
    return (json.dumps(validate_manifest(value, source), sort_keys=True, indent=2, ensure_ascii=True) + "\n").encode("ascii")


def read_schema_version(schema_bytes, manifest):
    """Read explicit clean-schema metadata, never infer a version from function bodies."""
    if (type(schema_bytes) is not bytes or schema_bytes.splitlines()[0:1] != [SCHEMA_HEADER]
            or type(manifest) is not dict or manifest.get("format") != "leti-cook-self-hosted-schema"
            or type(manifest.get("version")) is not int or manifest["version"] != 1
            or not _integer(manifest.get("schema_version"), 1, 1000000)
            or manifest.get("fresh_database_only") is not True
            or type(manifest.get("postgres_major")) is not int or manifest["postgres_major"] != 17
            or type(manifest.get("api_data_rows")) is not int or manifest["api_data_rows"] != 0
            or type(manifest.get("user_work_rows")) is not int or manifest["user_work_rows"] != 0
            or type(manifest.get("files")) is not dict
            or manifest["files"].get("schema.sql") != hashlib.sha256(schema_bytes).hexdigest()):
        raise ReleaseError("release_schema_invalid")
    return manifest["schema_version"]
