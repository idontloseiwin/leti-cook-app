"""Bounded public GitHub release delivery into the existing trusted verifier.

Only the installed source/key supply trust. GitHub metadata chooses immutable
asset IDs; it never supplies an executable, verification key, or arbitrary URL.
Transport injection is a Python test seam, never an environment or CLI option.
"""
from __future__ import annotations

from dataclasses import dataclass
import http.client
import os
from pathlib import Path
import tempfile
import time
from urllib.parse import quote, urljoin, urlsplit

from application.release_version import parse_version
from ops.release_manifest import MAX_ARCHIVE, MAX_MANIFEST, ReleaseError, ReleaseSource, strict_json
from ops.release_verification import MAX_SUMS, verify_release

API_HOST = 'api.github.com'
ASSET_HOST = 'release-assets.githubusercontent.com'
API_ACCEPT = 'application/vnd.github+json'
BINARY_ACCEPT = 'application/octet-stream'
MAX_METADATA = 2 * 1024 * 1024
PAGE_SIZE = 100
MAX_PAGES = 10
MAX_REDIRECTS = 3
TRANSFER_SECONDS = 180
IO_SECONDS = 10
CHUNK_SIZE = 64 * 1024


def _fail(code):
    raise ReleaseError(code)


def _positive(value, maximum=2 ** 63 - 1):
    return type(value) is int and 1 <= value <= maximum


def _source(value):
    if type(value) is not ReleaseSource:
        _fail('release_source_invalid')
    # Validate even a forged/deserialized dataclass before building host paths.
    return ReleaseSource(value.owner, value.repository)


def _api(source):
    source = _source(source)
    return f'https://{API_HOST}/repos/{source.owner}/{source.repository}/releases'


def _version(value):
    try:
        return parse_version(value)
    except ValueError:
        _fail('release_version_invalid')


class GitHubTransport:
    """HTTPS without ambient proxies, credentials, automatic redirects or retries.

    GitHub documents asset GET as either 200 or 302. Its release asset host is
    explicitly listed in the GitHub-hosted-runners network reference. No general
    githubusercontent.com wildcard is trusted here.
    """
    def __init__(self):
        self._deadline = time.monotonic() + TRANSFER_SECONDS

    def _remaining(self):
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            _fail('release_download_timeout')
        return min(IO_SECONDS, remaining)

    def get(self, url, *, accept, limit):
        if accept not in (API_ACCEPT, BINARY_ACCEPT) or not _positive(limit, MAX_ARCHIVE):
            _fail('release_transport_request_invalid')
        original = urlsplit(url)
        if original.hostname != API_HOST:
            _fail('release_download_url_invalid')
        for redirect in range(MAX_REDIRECTS + 1):
            try:
                target = urlsplit(url)
                if (target.scheme != 'https' or target.hostname not in (API_HOST, ASSET_HOST)
                        or target.port not in (None, 443) or target.username is not None
                        or target.password is not None or target.fragment or len(url) > 16384
                        or any(ord(c) < 33 or ord(c) > 126 for c in url)
                        or not target.path.startswith('/')
                        or (target.hostname != API_HOST and accept != BINARY_ACCEPT)):
                    _fail('release_download_url_invalid')
                connection = http.client.HTTPSConnection(target.hostname, timeout=self._remaining())
                try:
                    path = target.path + ('?' + target.query if target.query else '')
                    headers = {'Accept': accept, 'Accept-Encoding': 'identity', 'User-Agent': 'leti-cook-release'}
                    if target.hostname == API_HOST:
                        headers['X-GitHub-Api-Version'] = '2022-11-28'
                    connection.request('GET', path, headers=headers)
                    response = connection.getresponse()
                    if response.status in (301, 302, 303, 307, 308):
                        if redirect == MAX_REDIRECTS or accept != BINARY_ACCEPT:
                            _fail('release_download_redirect_invalid')
                        locations = response.headers.get_all('Location', [])
                        if len(locations) != 1:
                            _fail('release_download_redirect_invalid')
                        url = urljoin(url, locations[0])
                        continue
                    if response.status != 200:
                        code = {401: 'release_access_denied', 403: 'release_access_denied',
                                404: 'release_not_found', 429: 'release_rate_limited'}.get(
                                    response.status, 'release_download_http_error')
                        _fail(code)
                    if response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
                        _fail('release_download_encoding_invalid')
                    lengths = response.headers.get_all('Content-Length', [])
                    if (len(lengths) > 1 or lengths and (not lengths[0].isascii()
                            or not lengths[0].isdecimal() or len(lengths[0]) > 12)):
                        _fail('release_download_length_invalid')
                    expected = int(lengths[0]) if lengths else None
                    if expected is not None and not 0 < expected <= limit:
                        _fail('release_download_too_large')
                    result = bytearray()
                    while True:
                        timeout = self._remaining()
                        if connection.sock is not None:
                            connection.sock.settimeout(timeout)
                        block = response.read1(min(CHUNK_SIZE, limit + 1 - len(result)))
                        self._remaining()
                        if not block:
                            break
                        result.extend(block)
                        if len(result) > limit:
                            _fail('release_download_too_large')
                    if not result or expected is not None and len(result) != expected:
                        _fail('release_download_incomplete')
                    return bytes(result)
                finally:
                    connection.close()
            except ReleaseError:
                raise
            except TimeoutError:
                _fail('release_download_timeout')
            except (OSError, ValueError, http.client.HTTPException):
                _fail('release_download_failed')
        _fail('release_download_redirect_invalid')


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    asset_id: int
    size: int


@dataclass(frozen=True)
class ReleaseSelection:
    source: ReleaseSource
    release_id: int
    version: str
    assets: tuple[ReleaseAsset, ...]


def _get(transport, url, *, accept, limit):
    data = transport.get(url, accept=accept, limit=limit)
    if type(data) is not bytes or not 1 <= len(data) <= limit:
        _fail('release_download_size_invalid')
    return data


def _metadata(transport, url):
    return strict_json(_get(transport, url, accept=API_ACCEPT, limit=MAX_METADATA), limit=MAX_METADATA)


def _eligible(value, include_prereleases):
    if (type(value) is not dict or not _positive(value.get('id'))
            or type(value.get('draft')) is not bool or type(value.get('prerelease')) is not bool
            or type(value.get('tag_name')) is not str):
        _fail('release_metadata_invalid')
    tag = value['tag_name']
    if value['draft'] or not tag.startswith('v'):
        return None
    try:
        version = parse_version(tag[1:])
    except ValueError:
        return None
    if not include_prereleases and (value['prerelease'] or version.prerelease):
        return None
    return version


def _asset_limits(version):
    return {f'leti-cook-{version}.tar.gz': MAX_ARCHIVE,
            'release.json': MAX_MANIFEST, 'SHA256SUMS': MAX_SUMS, 'SHA256SUMS.sig': 64}


def _selection(source, value, version):
    assets = value.get('assets')
    if type(assets) is not list or not 4 <= len(assets) <= 100:
        _fail('release_assets_invalid')
    limits, seen_names, seen_ids, result = _asset_limits(version), set(), set(), []
    for asset in assets:
        if (type(asset) is not dict or type(asset.get('name')) is not str
                or not 1 <= len(asset['name']) <= 255 or not _positive(asset.get('id'))
                or asset['name'] in seen_names or asset['id'] in seen_ids):
            _fail('release_assets_invalid')
        seen_names.add(asset['name'])
        seen_ids.add(asset['id'])
        name = asset['name']
        if name not in limits:
            continue
        if asset.get('state') != 'uploaded' or not _positive(asset.get('size'), limits[name]):
            _fail('release_asset_invalid')
        if name == 'SHA256SUMS.sig' and asset['size'] != 64:
            _fail('release_asset_invalid')
        result.append(ReleaseAsset(name, asset['id'], asset['size']))
    if set(limits) != {item.name for item in result}:
        _fail('release_asset_missing')
    return ReleaseSelection(source, value['id'], version, tuple(sorted(result, key=lambda item: item.name)))


def select_release(source, *, version=None, include_prereleases=False, transport=None):
    """Choose metadata without trusting assets or running any installation code."""
    base = _api(source)
    if type(include_prereleases) is not bool:
        _fail('release_selection_invalid')
    client = GitHubTransport() if transport is None else transport
    if version is not None:
        parsed = _version(version)
        if parsed.prerelease and not include_prereleases:
            _fail('release_prerelease_requires_pre')
        candidate = _metadata(client, base + '/tags/' + quote('v' + version, safe=''))
        eligible = _eligible(candidate, include_prereleases)
        if eligible is None or candidate['tag_name'] != 'v' + version:
            _fail('release_requested_version_unavailable')
    else:
        candidates, ids, tags = [], set(), set()
        for page in range(1, MAX_PAGES + 1):
            values = _metadata(client, f'{base}?per_page={PAGE_SIZE}&page={page}')
            if type(values) is not list or len(values) > PAGE_SIZE:
                _fail('release_metadata_invalid')
            for value in values:
                eligible = _eligible(value, include_prereleases)
                if value['id'] in ids or value['tag_name'] in tags:
                    _fail('release_discovery_changed')
                ids.add(value['id'])
                tags.add(value['tag_name'])
                if eligible is not None:
                    candidates.append((eligible, value))
            if len(values) < PAGE_SIZE:
                break
        else:
            _fail('release_discovery_limit_exceeded')
        if not candidates:
            _fail('release_not_found')
        candidates.sort(key=lambda item: item[0], reverse=True)
        if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
            _fail('release_version_ambiguous')
        _, candidate = candidates[0]
        version = candidate['tag_name'][1:]
    # Re-read the selected identity, not /latest or an untrusted metadata URL.
    fixed = _metadata(client, base + '/' + str(candidate['id']))
    if (_eligible(fixed, include_prereleases) is None or fixed['id'] != candidate['id']
            or fixed['tag_name'] != 'v' + version):
        _fail('release_discovery_changed')
    return _selection(source, fixed, version)


def download_verified_release(selection, *, public_key_path, transport=None, openssl='openssl'):
    """Download four fixed assets; only immutable authenticated bytes leave here."""
    if (type(selection) is not ReleaseSelection or not _positive(selection.release_id)
            or type(selection.assets) is not tuple or len(selection.assets) != 4):
        _fail('release_selection_invalid')
    base = _api(selection.source)
    _version(selection.version)
    limits = _asset_limits(selection.version)
    names, ids = set(), set()
    for asset in selection.assets:
        if (type(asset) is not ReleaseAsset or asset.name not in limits or asset.name in names
                or not _positive(asset.asset_id) or asset.asset_id in ids
                or not _positive(asset.size, limits[asset.name])):
            _fail('release_selection_invalid')
        names.add(asset.name)
        ids.add(asset.asset_id)
    client = GitHubTransport() if transport is None else transport
    with tempfile.TemporaryDirectory(prefix='leti-cook-download-') as temporary:
        directory = Path(temporary)
        directory.chmod(0o700)
        for asset in selection.assets:
            data = _get(client, base + '/assets/' + str(asset.asset_id),
                        accept=BINARY_ACCEPT, limit=limits[asset.name])
            if len(data) != asset.size:
                _fail('release_asset_changed')
            descriptor = os.open(directory / asset.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(data)
        release = verify_release(directory / f'leti-cook-{selection.version}.tar.gz',
                                 public_key_path=public_key_path, source=selection.source, openssl=openssl)
        if release.manifest['version'] != selection.version:
            _fail('release_selected_version_mismatch')
        return release


def fetch_release(*, source, public_key_path, version=None, include_prereleases=False,
                  transport=None, openssl='openssl'):
    """Trusted entry for bootstrap and installed updater; no URL override exists."""
    client = GitHubTransport() if transport is None else transport
    selection = select_release(source, version=version, include_prereleases=include_prereleases, transport=client)
    return download_verified_release(selection, public_key_path=public_key_path, transport=client, openssl=openssl)
