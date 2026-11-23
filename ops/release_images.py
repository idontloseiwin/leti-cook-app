"""Acquire one exact signed amd64 image; cache failure is never a pull signal."""
from __future__ import annotations

import json
import re

from install import InstallError

_IMAGE = re.compile(r'sha256:[0-9a-f]{64}')
MAX_INSPECT_BYTES = 256 * 1024
MAX_LIST_BYTES = 1024 * 1024
MAX_LIST_ROWS = 4096


def _missing_reference(reference, run):
    repository, digest = reference.rsplit('@', 1)
    # Inspect exit 1 also covers daemon errors. A successful repository-scoped
    # listing must independently confirm absence before any registry request.
    raw = run(['docker', 'image', 'ls', '--all', '--digests', '--no-trunc',
               '--format', '{{json .}}', repository])
    if type(raw) is not str or len(raw.encode('utf-8')) > MAX_LIST_BYTES:
        raise InstallError('release_image_cache_unavailable')
    rows = raw.splitlines()
    if len(rows) > MAX_LIST_ROWS:
        raise InstallError('release_image_cache_unavailable')
    for line in rows:
        try:
            item = json.loads(line)
        except (ValueError, TypeError):
            raise InstallError('release_image_cache_unavailable') from None
        if (type(item) is not dict or item.get('Repository') != repository
                or type(item.get('Digest')) is not str
                or (item['Digest'] != '<none>' and _IMAGE.fullmatch(item['Digest']) is None)):
            raise InstallError('release_image_cache_unavailable')
        if item['Digest'] == digest:
            return False
    return True


def ensure_release_image(reference, run):
    """Return the validated immutable image ID; only a missing image is pulled."""
    inspect = ['docker', 'image', 'inspect', '--format', '{{json .}}', reference]
    try:
        raw = run(inspect)
    except InstallError as error:
        if str(error) != 'command_failed_1':
            raise
        if not _missing_reference(reference, run):
            raise InstallError('release_image_cache_unavailable') from None
        run(['docker', 'pull', '--platform=linux/amd64', reference], timeout=900)
        raw = run(inspect)
    if type(raw) is not str or len(raw.encode('utf-8')) > MAX_INSPECT_BYTES:
        raise InstallError('release_image_identity_invalid')
    try:
        details = json.loads(raw)
    except (ValueError, TypeError):
        raise InstallError('release_image_identity_invalid') from None
    if (type(details) is not dict or type(details.get('Id')) is not str
            or _IMAGE.fullmatch(details['Id']) is None or details.get('Os') != 'linux'
            or details.get('Architecture') != 'amd64'
            or type(details.get('RepoDigests')) is not list
            or any(type(value) is not str for value in details['RepoDigests'])
            or reference not in details['RepoDigests']):
        raise InstallError('release_image_identity_invalid')
    return details['Id']
