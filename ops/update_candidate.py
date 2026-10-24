"""Capture and prepare an update tree without Docker, SQL or candidate imports.

Only an already authenticated VerifiedRelease is accepted. This module does not
stop the installation or replace it: the journal owner performs those phases.
Errors contain fixed codes, never private environment values or file contents.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import hashlib
import importlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
from types import MappingProxyType
import uuid

from application.release_version import parse_version, parse_version_file
from ops.bundle_contract import FORMAT, MAX_FILE, MAX_FILES, MAX_TOTAL, REQUIRED_BUNDLE_FILES
from ops.release_manifest import ReleaseError, parse_source, strict_json, validate_manifest
from ops.release_verification import VerifiedRelease
from ops.schema_manifest import SchemaPlan, validate_schema_bundle

_ROOT = Path(__file__).resolve().parents[1]
_HASH = re.compile(r"[0-9a-f]{64}")
_PROJECT = re.compile(r"leti-cook-[0-9a-f]{12}")
_COMPATIBILITY = "application/update-compatibility.json"
_CHANNEL_ADD_OWNER = "application/db/user_work/reconnect_admission.py"
_CONFIGS = frozenset(("config/config.ini", "config/collection-schedules.ini"))
_GENERATED = frozenset(("installation.env", "backend.env", "installation.json", "installed.json"))
_OPTIONAL = frozenset(("runtime.compose.json",))
_PRIVATE = _GENERATED | _OPTIONAL | {"bundle.json"}
_CONFIG_KEYS = {
    "format": frozenset(("version",)),
    "collection": frozenset(("automatic_collection", "interval_minutes", "handoff_grace_seconds",
        "manual_stats_quota_units", "initial_load_concurrency", "youtube_request_concurrency")),
    "limits": frozenset(("metadata_limit", "inventory_page_budget", "stats_limit", "new_video_stats_limit",
        "operator_chunk_size", "inventory_pages_per_chunk", "drain_max_chunks", "drain_max_seconds")),
}
_INSTALL_KEYS = frozenset(("SELF_HOSTED_DB_PROJECT", "LIC_HTTP_PORT", "LIC_UPDATE_MARKER_HOST_DIRECTORY",
    "LIC_UPDATE_CIPHER_HOST_DIRECTORY", "SELF_HOSTED_SCHEMA_PATH", "SELF_HOSTED_SCHEMA_SHA256",
    "POSTGRES_PASSWORD", "AUTHENTICATOR_PASSWORD"))
_BACKEND_FIXED = {
    "LIC_DEPLOYMENT_PROFILE": "self-hosted", "PYTHON_DOTENV_DISABLED": "1",
    "LIC_DATABASE_TRANSPORT": "postgres", "LIC_HOT_RUNTIME_URL": "http://127.0.0.1:8002/internal/hot-runtime",
    "LIC_AUTH_DATABASE": "/var/lib/leti-cook/access/owner.sqlite3",
    "LIC_SETUP_DIRECTORY": "/var/lib/leti-cook/configuration", "LIC_SETTINGS_FILE": "/etc/leti-cook/config.ini",
    "LIC_UPDATE_MARKER_DIRECTORY": "/run/leti-cook-update/marker",
    "LIC_UPDATE_CIPHER_DIRECTORY": "/run/leti-cook-update/cipher",
    "OPERATOR_JOB_WAKE_PATH": "/var/lib/leti-cook/operator-jobs.wake",
    "OPERATOR_JOBS_MODE": "durable", "YOUTUBE_QUOTA_PROJECT_ID": "self-hosted",
}
_BACKEND_FLAGS = frozenset(("DATABASE_VIDEO_COLLECTION_ENABLED", "DATABASE_CHANNEL_DISCOVERY_ENABLED",
    "TYPED_PLAYLIST_INVENTORY_ENABLED", "SHORT_VIDEO_COLLECTION_ENABLED", "COMPLETED_STREAM_COLLECTION_ENABLED",
    "YOUTUBE_BATCH_STATS_ENABLED", "CHANNEL_BASELINE_ANALYTICS_ENABLED", "CHANNEL_DERIVED_INSIGHTS_ENABLED"))
_BACKEND_VARIABLE = frozenset(("LIC_DATABASE_DSN", "LIC_DATABASE_ID", "LIC_HOT_RUNTIME_SECRET", "LIC_PUBLIC_ORIGIN"))


class CandidateError(ReleaseError):
    """A fixed, nonsecret preflight or preparation failure code."""


@dataclass(frozen=True, slots=True)
class CandidatePlan:
    installed: Path
    project: str
    database_id: str
    port: int
    current_version: str
    target_version: str
    installed_state: Mapping
    installed_schema: SchemaPlan
    schema: SchemaPlan
    files: Mapping[str, bytes] = field(repr=False)
    installed_fingerprint: str
    installed_directory_identity: tuple[int, int]


@dataclass(frozen=True, slots=True)
class PreparedCandidate:
    path: Path
    fingerprint: str
    directory_identity: tuple[int, int]


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _json(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _readonly(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: _readonly(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_readonly(item) for item in value)
    return value


def _plain(value):
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _name(name):
    if (type(name) is not str or not 1 <= len(name) <= 255 or not name.isascii()
            or any(ord(c) < 33 or ord(c) > 126 for c in name) or "\\" in name or ":" in name):
        raise CandidateError("update_file_name_invalid")
    path = PurePosixPath(name)
    if path.is_absolute() or str(path) != name or any(part.startswith(".") for part in path.parts):
        raise CandidateError("update_file_name_invalid")
    return name


def _safe_path(value, *, existing=True):
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise CandidateError("update_path_invalid")
    for item in (*reversed(path.parents), path):
        try:
            info = item.lstat()
        except FileNotFoundError:
            if item == path and not existing:
                continue
            raise CandidateError("update_path_missing") from None
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 1024:
            raise CandidateError("update_path_link")
        if not stat.S_ISDIR(info.st_mode):
            raise CandidateError("update_path_not_directory")
        # Windows focused tests exercise the byte/identity contract. Production
        # is Linux root; there is no caller flag that disables ownership checks.
        if os.name != "nt" and (info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022):
            # A system sticky /tmp is an acceptable ancestor, never the root or
            # its direct parent. All operation directories remain root-owned.
            if not (item not in (path, path.parent) and info.st_uid == 0 and info.st_mode & stat.S_ISVTX):
                raise CandidateError("update_path_permissions")
    if os.name != "nt" and os.geteuid() != 0:
        raise CandidateError("update_root_required")
    return path


def _file(path, *, private=False):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode) or getattr(before, "st_file_attributes", 0) & 1024:
            raise CandidateError("update_file_link")
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            first = os.fstat(stream.fileno())
            if (not stat.S_ISREG(first.st_mode) or first.st_nlink != 1 or first.st_size > MAX_FILE
                    or (first.st_dev, first.st_ino) != (before.st_dev, before.st_ino)):
                raise CandidateError("update_file_invalid")
            if os.name != "nt" and (first.st_uid != 0 or stat.S_IMODE(first.st_mode) & (0o077 if private else 0o022)):
                raise CandidateError("update_file_permissions")
            data = stream.read(MAX_FILE + 1)
            last = os.fstat(stream.fileno())
        after = path.lstat()
    except OSError:
        raise CandidateError("update_file_unreadable") from None
    # Windows 3.12 reports birth time in lstat.ctime but change time in fstat;
    # Linux uses change time for both and retains that additional race check.
    fields = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns,
                            value.st_ctime_ns if os.name != "nt" else 0)
    if fields(first) != fields(last) or fields(last) != fields(after) or len(data) != first.st_size:
        raise CandidateError("update_file_changed")
    return data


def _capture_tree(directory):
    root = _safe_path(directory)
    first = root.stat()
    files, directories, total = {}, set(), 0
    pending = [root]
    while pending:
        folder = pending.pop()
        try:
            entries = sorted(folder.iterdir(), key=lambda p: p.name)
        except OSError:
            raise CandidateError("update_tree_unreadable") from None
        for entry in entries:
            name = _name(entry.relative_to(root).as_posix())
            info = entry.lstat()
            if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 1024:
                _safe_path(entry)
                directories.add(name)
                if len(directories) > MAX_FILES:
                    raise CandidateError("update_tree_too_large")
                pending.append(entry)
            else:
                files[name] = _file(entry, private=name in _PRIVATE)
                total += len(files[name])
                if len(files) > MAX_FILES + len(_GENERATED) + len(_OPTIONAL) + 1 or total > MAX_TOTAL + 1024 * 1024:
                    raise CandidateError("update_tree_too_large")
    implied = {str(parent) for name in files for parent in PurePosixPath(name).parents if str(parent) != "."}
    if directories != implied:
        raise CandidateError("update_unknown_directory")
    last = root.stat()
    if (first.st_dev, first.st_ino) != (last.st_dev, last.st_ino):
        raise CandidateError("update_tree_changed")
    return files, (first.st_dev, first.st_ino)


def _fingerprint(files):
    digest = hashlib.sha256()
    for name, data in sorted(files.items()):
        digest.update(name.encode("ascii") + b"\0" + str(len(data)).encode("ascii") + b"\0")
        digest.update(hashlib.sha256(data).digest())
    return digest.hexdigest()


def _bundle(files, *, installed=False):
    try:
        value = strict_json(files["bundle.json"], limit=256 * 1024)
    except (KeyError, ValueError):
        raise CandidateError("update_bundle_invalid") from None
    if (type(value) is not dict or set(value) != {"format", "version", "fresh_install_only", "stage",
            "release_accepted", "youtube_configuration_included", "files"} or value["format"] != FORMAT
            or type(value["version"]) is not int or value["version"] != 1
            or value["fresh_install_only"] is not True or value["release_accepted"] is not False
            or value["youtube_configuration_included"] is not False or value["stage"] != "self-hosted-release-candidate"):
        raise CandidateError("update_bundle_invalid")
    hashes = value["files"]
    if (type(hashes) is not dict or not 10 <= len(hashes) <= MAX_FILES
            or not REQUIRED_BUNDLE_FILES | {_COMPATIBILITY} <= set(hashes)):
        raise CandidateError("update_bundle_files_invalid")
    expected = set(hashes) | {"bundle.json"} | (_GENERATED if installed else set())
    actual = set(files) - (_OPTIONAL if installed else set())
    if expected != actual or set(hashes) & (_GENERATED | _OPTIONAL | {"bundle.json"}):
        raise CandidateError("update_unknown_file")
    for name, expected_hash in hashes.items():
        _name(name)
        data = files[name]
        if (type(expected_hash) is not str or _HASH.fullmatch(expected_hash) is None
                or type(data) is not bytes or len(data) > MAX_FILE):
            raise CandidateError("update_bundle_files_invalid")
        if not (installed and name in _CONFIGS) and _digest(data) != expected_hash:
            raise CandidateError("update_shipped_file_modified")
    if sum(map(len, files.values())) > MAX_TOTAL + 1024 * 1024:
        raise CandidateError("update_tree_too_large")
    return value


def _compatibility(data):
    value = strict_json(data)
    expected = {"format": "leti-cook-update-compatibility", "format_version": 1,
        "application_state_version": 1, "config_format_version": 3}
    if (type(value) is not dict or set(value) != set(expected) | {"schedules_format_version"}
            or any(type(value[k]) is not type(v) or value[k] != v for k, v in expected.items())
            or type(value["schedules_format_version"]) is not int
            or value["schedules_format_version"] not in (2, 3, 4)):
        raise CandidateError("update_compatibility_unsupported")
    return value


def channel_add_owner_present(directory):
    """Read the complete verified installed tree; file absence alone is not proof."""
    files, _ = _capture_tree(directory)
    manifest = _bundle(files, installed=True)
    _compatibility(files[_COMPATIBILITY])
    return _CHANNEL_ADD_OWNER in manifest['files']


def _trusted_parsers():
    """Reuse the installed host's stdlib-only parsers, never candidate modules."""
    application = _ROOT / "application"
    old_path = sys.path[:]
    try:
        sys.path.insert(0, str(application))
        modules = []
        for name in ("config_migrations", "collection_schedule_model", "self_hosted_settings", "collection_schedule"):
            module = importlib.import_module(name)
            if Path(module.__file__).resolve() != (application / (name + ".py")).resolve():
                raise CandidateError("update_trusted_parser_mismatch")
            modules.append(module)
    finally:
        sys.path[:] = old_path
    return modules[0], modules[2], modules[3]


def _config(files, *, template=False):
    try:
        migrations, settings, schedules = _trusted_parsers()
        parser = migrations.parse_config_bytes(files["config/config.ini"])
        # A verified template can add optional settings; the installed file is
        # preserved instead of adopting those values. Never import candidate
        # parsers. Format metadata and the owner's local file remain strict.
        if set(parser.sections()) != set(_CONFIG_KEYS) or any(
                set(parser[section]) - keys for section, keys in _CONFIG_KEYS.items()
                if not template or section == "format"):
            raise CandidateError("update_unknown_config_key")
        settings.read_settings_bytes(files["config/config.ini"])
        schedules.parse_schedule_file(files["config/collection-schedules.ini"])
    except CandidateError:
        raise
    except (KeyError, ValueError):
        raise CandidateError("update_config_invalid") from None


def _env(data):
    if type(data) is not bytes or len(data) > 16384:
        raise CandidateError("update_environment_invalid")
    try:
        lines = data.decode("ascii").splitlines()
    except UnicodeError:
        raise CandidateError("update_environment_invalid") from None
    values = {}
    for line in lines:
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise CandidateError("update_environment_invalid")
        key, value = line.split("=", 1)
        if (re.fullmatch(r"[A-Z][A-Z0-9_]*", key) is None or key in values or not value
                or any(ord(c) < 33 or ord(c) > 126 or c in "$#'\"`\\" for c in value)):
            raise CandidateError("update_environment_invalid")
        values[key] = value
    return values


def _receipts(files, directory, schema):
    installation = strict_json(files["installation.json"])
    state = strict_json(files["installed.json"])
    if (type(installation) is not dict or set(installation) != {"format", "project", "port", "status", "database_id", "version", "source", "bundle_sha256"}
            or installation["format"] != FORMAT or installation["status"] != "preparing"
            or installation["bundle_sha256"] != _digest(files["bundle.json"])
            or type(state) is not dict or set(state) != {"status", "project", "port", "database_id", "version", "source", "collection_started"}
            or state["status"] != "owner_setup_ready" or type(state["collection_started"]) is not bool):
        raise CandidateError("update_installation_receipt_invalid")
    for name in ("project", "port", "database_id", "version", "source"):
        if installation[name] != state[name] or type(installation[name]) is not type(state[name]):
            raise CandidateError("update_installation_identity_mismatch")
    project, port, identity = state["project"], state["port"], state["database_id"]
    if (type(project) is not str or _PROJECT.fullmatch(project) is None
            or type(port) is not int or not 1024 <= port <= 65535 or state["source"] not in ("source", "release")
            or state["version"] != parse_version_file(files["application/VERSION"])):
        raise CandidateError("update_installation_identity_invalid")
    try:
        parsed = uuid.UUID(identity)
        if not parsed.int or str(parsed) != identity:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise CandidateError("update_installation_identity_invalid") from None
    host, backend = _env(files["installation.env"]), _env(files["backend.env"])
    if set(host) != _INSTALL_KEYS or set(backend) != set(_BACKEND_FIXED) | _BACKEND_FLAGS | _BACKEND_VARIABLE:
        raise CandidateError("update_unknown_environment_key")
    expected = {"SELF_HOSTED_DB_PROJECT": project, "LIC_HTTP_PORT": str(port),
        "LIC_UPDATE_MARKER_HOST_DIRECTORY": directory.as_posix() + ".update/marker",
        "LIC_UPDATE_CIPHER_HOST_DIRECTORY": directory.as_posix() + ".update/cipher",
        "SELF_HOSTED_SCHEMA_PATH": (directory / "schema/schema.sql").as_posix(),
        "SELF_HOSTED_SCHEMA_SHA256": schema.schema_sha256}
    if any(host[key] != value for key, value in expected.items()):
        raise CandidateError("update_environment_identity_mismatch")
    if any(re.fullmatch(r"[A-Za-z0-9_-]{32,128}", host[key]) is None for key in ("POSTGRES_PASSWORD", "AUTHENTICATOR_PASSWORD")):
        raise CandidateError("update_environment_invalid")
    expected_backend = {**_BACKEND_FIXED, "LIC_DATABASE_ID": identity,
        "LIC_PUBLIC_ORIGIN": f"http://127.0.0.1:{port}",
        "LIC_DATABASE_DSN": "postgresql://authenticator:" + host["AUTHENTICATOR_PASSWORD"] + "@db:5432/leti-cook"}
    if (any(backend[key] != value for key, value in expected_backend.items())
            or re.fullmatch(r"[A-Za-z0-9_-]{32,128}", backend["LIC_HOT_RUNTIME_SECRET"]) is None
            or any(backend[key] not in ("true", "false", "1", "0") for key in _BACKEND_FLAGS)):
        raise CandidateError("update_environment_invalid")
    if "runtime.compose.json" in files:
        _override(files["runtime.compose.json"], project)
    return installation, state


def _override(data, project):
    value = strict_json(data)
    try:
        valid = (type(value) is dict and set(value) == {"services", "volumes"}
            and type(value["services"]) is dict and set(value["services"]) == {"db", "backend"}
            and type(value["volumes"]) is dict and set(value["volumes"]) == {"database-data"}
            and type(value["volumes"]["database-data"]) is dict and set(value["volumes"]["database-data"]) == {"name"})
        for name in ("db", "backend"):
            image = value["services"][name]
            valid = valid and type(image) is dict and set(image) == {"image"} and type(image["image"]) is str and re.fullmatch(r"sha256:[0-9a-f]{64}", image["image"]) is not None
        volume = value["volumes"]["database-data"]["name"]
        valid = valid and type(volume) is str and re.fullmatch(re.escape(project) + r"_(?:database-data|restore_[0-9a-f]{32})", volume) is not None
    except (KeyError, TypeError):
        valid = False
    if not valid:
        raise CandidateError("update_runtime_override_invalid")


def _schema_prefix(installed, candidate):
    if (candidate.baseline_id != installed.baseline_id or candidate.baseline_version != installed.baseline_version
            or candidate.baseline_sha256 != installed.baseline_sha256 or candidate.baseline_sql != installed.baseline_sql
            or candidate.schema_version < installed.schema_version
            or candidate.steps[:len(installed.steps)] != installed.steps):
        raise CandidateError("update_schema_history_changed")
    if candidate.schema_version == installed.schema_version and (candidate.schema_sql != installed.schema_sql or candidate.seed_sql != installed.seed_sql):
        raise CandidateError("update_schema_changed_without_step")


def preflight_candidate(installed: Path, verified: VerifiedRelease, *, allow_prerelease=False) -> CandidatePlan:
    """Capture a complete safe plan before the coordinator stops any service."""
    if type(verified) is not VerifiedRelease or type(allow_prerelease) is not bool:
        raise CandidateError("update_verified_release_required")
    old, identity = _capture_tree(installed)
    installed = Path(installed)
    _bundle(old, installed=True)
    _compatibility(old[_COMPATIBILITY])
    _config(old)
    old_schema = validate_schema_bundle(old)
    receipt, state = _receipts(old, installed, old_schema)
    files = dict(verified.files)
    _bundle(files)
    _compatibility(files[_COMPATIBILITY])
    if _CHANNEL_ADD_OWNER in old and _CHANNEL_ADD_OWNER not in files:
        raise CandidateError('update_channel_add_owner_removed')
    _config(files, template=True)
    source = parse_source(old["application/release-source.json"])
    release = validate_manifest(_plain(verified.manifest), source=source)
    if parse_source(files["application/release-source.json"]) != source:
        raise CandidateError("update_release_source_changed")
    current = parse_version(state["version"])
    target = parse_version(release["version"])
    if target <= current:
        raise CandidateError("update_version_not_newer")
    if target.prerelease and not allow_prerelease:
        raise CandidateError("update_prerelease_not_allowed")
    if current < parse_version(release["minimum_upgrade_version"]):
        raise CandidateError("update_version_below_minimum")
    if parse_version_file(files["application/VERSION"]) != release["version"]:
        raise CandidateError("update_release_version_mismatch")
    schema = validate_schema_bundle(files)
    if schema.schema_version != release["schema_version"]:
        raise CandidateError("update_release_schema_mismatch")
    _schema_prefix(old_schema, schema)
    # Neither credentials nor state-volume contents are generated or copied.
    # User files keep comments, whitespace, order and their original line ends.
    for name in _CONFIGS | {"backend.env"} | (set(old) & _OPTIONAL):
        files[name] = old[name]
    source_hash = old_schema.schema_sha256.encode("ascii")
    new_hash = schema.schema_sha256.encode("ascii")
    lines = old["installation.env"].splitlines(keepends=True)
    files["installation.env"] = b"".join(line.replace(source_hash, new_hash, 1)
        if line.startswith(b"SELF_HOSTED_SCHEMA_SHA256=") else line for line in lines)
    files["installation.json"] = _json({**receipt, "version": release["version"], "source": "release", "bundle_sha256": _digest(files["bundle.json"])})
    files["installed.json"] = _json({**state, "version": release["version"], "source": "release"})
    # Second full capture detects edits or inode replacement during validation.
    again, again_identity = _capture_tree(installed)
    fingerprint = _fingerprint(old)
    if again_identity != identity or _fingerprint(again) != fingerprint:
        raise CandidateError("update_installation_changed")
    return CandidatePlan(installed, state["project"], state["database_id"], state["port"],
        state["version"], release["version"], _readonly(state), old_schema, schema,
        MappingProxyType(files), fingerprint, identity)


def _sync_directory(path, *, mode=None):
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if mode is not None:
            os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def prepare_candidate(plan: CandidatePlan, destination: Path) -> PreparedCandidate:
    """Write one new owned tree; never adopt, replace or recursively delete it.

    The coordinator must journal ownership/intent before this call. A failed
    write deliberately leaves the partial tree for journal-guided recovery.
    """
    if type(plan) is not CandidatePlan:
        raise CandidateError("update_plan_required")
    # This public boundary also refuses manually constructed/mutated plans;
    # the dataclass alone is not an authentication or validation mechanism.
    _bundle(plan.files, installed=True)
    _compatibility(plan.files[_COMPATIBILITY])
    _config(plan.files)
    if validate_schema_bundle(plan.files) != plan.schema:
        raise CandidateError("update_plan_schema_mismatch")
    _schema_prefix(plan.installed_schema, plan.schema)
    _, state = _receipts(plan.files, plan.installed, plan.schema)
    if (state["version"], state["project"], state["database_id"], state["port"]) != (
            plan.target_version, plan.project, plan.database_id, plan.port):
        raise CandidateError("update_plan_identity_mismatch")
    current, identity = _capture_tree(plan.installed)
    if identity != plan.installed_directory_identity or _fingerprint(current) != plan.installed_fingerprint:
        raise CandidateError("update_installation_changed")
    destination = _safe_path(destination, existing=False)
    if destination.exists() or destination == plan.installed or plan.installed in destination.parents:
        raise CandidateError("update_new_candidate_required")
    if destination.parent.stat().st_dev != plan.installed.stat().st_dev:
        raise CandidateError("update_same_filesystem_required")
    destination.mkdir(mode=0o700)
    directories = {destination}
    for name, data in sorted(plan.files.items()):
        _name(name)
        target = destination / name
        target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        directories.update(parent for parent in target.parents if parent == destination or destination in parent.parents)
        mode = 0o600 if name in _PRIVATE else 0o644
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            # Creation modes are filtered by the caller's umask. Public image
            # and bind inputs must be readable by the unprivileged backend.
            if os.name != "nt":
                os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
    for directory in sorted(directories, key=lambda p: len(p.parts), reverse=True):
        _sync_directory(directory, mode=0o700 if directory == destination else 0o755)
    _sync_directory(destination.parent)
    captured, identity = _capture_tree(destination)
    if _fingerprint(captured) != _fingerprint(plan.files):
        raise CandidateError("update_candidate_changed")
    return PreparedCandidate(destination, _fingerprint(captured), identity)
