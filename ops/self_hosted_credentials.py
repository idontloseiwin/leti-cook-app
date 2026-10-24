"""Generate credentials for one NEW local DB stack; never print or overwrite them.

This is a backend service credential, not user login or a YouTube API key.
No connection, Docker operation, database mutation or deployment is performed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import secrets
import stat
import uuid
from urllib.parse import quote


def runtime_settings(authenticator_password: str, *, database_id=None, internal_secret=None) -> dict:
    """Private direct transport settings for the existing non-owner login."""
    if not isinstance(authenticator_password, str) or re.fullmatch(r'[A-Za-z0-9_-]{32,}', authenticator_password) is None:
        raise ValueError('authenticator_password_invalid')
    if (database_id is None) != (internal_secret is None):
        raise ValueError('database_admission_pair_required')
    if database_id is None:
        database_id, internal_secret = str(uuid.uuid4()), secrets.token_urlsafe(32)
    try:
        identity = uuid.UUID(database_id)
        if identity.int == 0 or str(identity) != database_id:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise ValueError('database_identity_invalid') from None
    if type(internal_secret) is not str or re.fullmatch(r'[A-Za-z0-9_-]{32,128}', internal_secret) is None:
        raise ValueError('database_internal_secret_invalid')
    password = quote(authenticator_password, safe='')
    return {
        'LIC_DATABASE_TRANSPORT': 'postgres',
        'LIC_DATABASE_DSN': f'postgresql://authenticator:{password}@db:5432/leti-cook',
        'LIC_DATABASE_ID': database_id,
        'LIC_HOT_RUNTIME_SECRET': internal_secret,
    }


def schema_settings(manifest_path: Path | None, project: str | None) -> dict:
    if manifest_path is None and project is None:
        return {}
    if manifest_path is None or project is None or not re.fullmatch(r"[a-z][a-z0-9_-]{2,62}", project):
        raise ValueError("schema_manifest_and_unique_project_required")
    manifest_path = manifest_path.resolve(strict=True)
    if manifest_path.stat().st_size > 65536:
        raise ValueError("schema_manifest_too_large")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (not isinstance(manifest, dict)
        or manifest.get("format") != "leti-cook-self-hosted-schema"
        or type(manifest.get("version")) is not int or manifest["version"] != 1
        or manifest.get("fresh_database_only") is not True
        or manifest.get("schemas") != ["public", "operator_private"]
        or manifest.get("api_data_rows") != 0 or manifest.get("user_work_rows") != 0):
        raise ValueError("fresh_schema_manifest_required")
    files = manifest.get("files")
    expected = files.get("schema.sql") if isinstance(files, dict) else None
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("schema_digest_required")
    schema = manifest_path.parent / "schema.sql"
    if schema.is_symlink() or not schema.is_file() or schema.stat().st_size > 50 * 1024 * 1024:
        raise ValueError("fresh_schema_file_required")
    content = schema.read_bytes()
    if (content.splitlines()[0:1] != [b"-- leti-cook self-hosted clean schema; format=1; application-data=none"]
        or hashlib.sha256(content).hexdigest() != expected):
        raise ValueError("fresh_schema_digest_mismatch")
    # Compose env files interpolate dollar expressions; refuse ambiguous paths.
    value = schema.as_posix()
    if any(char in value for char in (chr(10), chr(13), "$", "#", chr(34), chr(39))):
        raise ValueError("schema_path_not_env_safe")
    return {"SELF_HOSTED_SCHEMA_PATH": value,
            "SELF_HOSTED_SCHEMA_SHA256": expected,
            "SELF_HOSTED_DB_PROJECT": project}


def port_settings(backend_port: int) -> dict:
    if type(backend_port) is not int or not 1024 <= backend_port <= 65535:
        raise ValueError('backend_port_must_be_integer_1024_to_65535')
    # PostgreSQL has no published port; this is the internal admission endpoint.
    return {'LIC_HOT_RUNTIME_URL': f'http://127.0.0.1:{backend_port}/internal/hot-runtime'}


def create_credentials(directory: Path, *, schema_manifest=None, project=None,
                       backend_port=8002) -> dict:
    ports = port_settings(backend_port)
    settings = schema_settings(schema_manifest, project)
    directory = directory.absolute()
    for item in (directory, *directory.parents):
        if item.is_symlink():
            raise ValueError("credential_directory_must_not_be_a_link")
        if item.exists() and getattr(item.lstat(), "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024):
            raise ValueError("credential_directory_must_not_be_a_reparse_point")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    authenticator_password = secrets.token_hex(32)
    values = {
        "POSTGRES_PASSWORD": secrets.token_hex(32),
        "AUTHENTICATOR_PASSWORD": authenticator_password,
        **runtime_settings(authenticator_password),
        "LIC_DEPLOYMENT_PROFILE": "self-hosted",
    }
    values.update(ports)
    values.update(settings)
    target = directory / ".env"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(target, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write("# Generated for this local installation. Keep server-side; do not commit.\n")
            output.write("# Add your own YOUTUBE_API_KEY separately before provider use.\n")
            output.write("# This file does not enable public access or user authentication.\n")
            for key, value in values.items():
                output.write(key + "=" + value + "\n")
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        # Never retry with new secrets or replace a possibly partial credential file.
        raise RuntimeError("credential_file_write_failed_inspect_locally") from None
    return {"status": "created", "file": str(target), "values_printed": False,
            "permissions": "0600" if os.name != "nt" else "verify_windows_directory_acl",
            "schema_configured": bool(settings), "deployment_performed": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--schema-manifest", type=Path, help="verified generated manifest.json; requires --project")
    parser.add_argument("--project", help="new, unique Compose project name; requires --schema-manifest")
    parser.add_argument("--backend-port", type=int, default=8002, help="loopback backend port for runtime admission (default: 8002)")
    parser.add_argument("--create", action="store_true", help="create a new .env; existing files are refused")
    args = parser.parse_args()
    try:
        port_settings(args.backend_port)
    except ValueError as error:
        parser.error(str(error))
    if not args.create:
        print(json.dumps({"status": "plan", "creates": str(args.directory / ".env"), "deployment_performed": False}))
        return
    try:
        result = create_credentials(args.directory, schema_manifest=args.schema_manifest, project=args.project,
                                    backend_port=args.backend_port)
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(1, "Credential creation refused (" + type(error).__name__ + "). Existing files are not replaced.\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
