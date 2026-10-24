"""Build the deterministic trusted release installer; no signing or network I/O.

Trust inputs are the canonical files in the supplied reviewed source tree. Test
fixtures use a separate tree with an ephemeral key, never a runtime override.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
from types import MappingProxyType
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from install import safe_path
from ops.release_manifest import ReleaseError, parse_source
from ops.release_verification import _capture, _public_key

BOOTSTRAP_NAME = 'install-release.py'
MAX_BOOTSTRAP_SOURCE = 2 * 1024 * 1024
# Explicit trusted host closure. No backend imports or candidate installer code
# are loaded while verifying a release; the same reviewed host performs install.
BOOTSTRAP_FILES = (
    'install.py',
    'ops/bundle_contract.py',
    'ops/release_download.py',
    'ops/release_images.py',
    'ops/release_install.py',
    'ops/release_manifest.py',
    'ops/release_verification.py',
    'ops/schema_manifest.py',
    'ops/self_hosted_credentials.py',
    'ops/update_copy_store.py',
    'application/release_version.py',
    'application/schema_release.py',
    'application/update_copy_protocol.py',
    'application/release-source.json',
    'application/release-signing.pub',
)


def bootstrap_files(root):
    root = Path(root)
    source = _capture(root / 'application/release-source.json', 16384)
    parse_source(source)  # In particular, production placeholders never build.
    key = _capture(root / 'application/release-signing.pub', 4096)
    _public_key(key)
    files = {'application/release-source.json': source, 'application/release-signing.pub': key}
    for name in BOOTSTRAP_FILES:
        if name not in files:
            files[name] = _capture(root / name, 256 * 1024)
    if sum(map(len, files.values())) > MAX_BOOTSTRAP_SOURCE:
        raise ReleaseError('release_bootstrap_too_large')
    # Avoid namespace-package fallback to modules in the caller's current folder.
    files['application/__init__.py'] = b''
    files['ops/__init__.py'] = b''
    return MappingProxyType(files)


def bootstrap_bytes(root):
    files = bootstrap_files(root)
    document = json.dumps({name: base64.b64encode(data).decode('ascii') for name, data in files.items()},
                          sort_keys=True, separators=(',', ':')).encode('ascii')
    payload = base64.b85encode(zlib.compress(document, level=9)).decode('ascii')
    digest = hashlib.sha256(document).hexdigest()
    # The embedded bytes are the trust entrypoint, not an untrusted release.
    # Materialize only this fixed closure in a private temporary directory so
    # the independent update-copy helper can copy its own exact source later.
    template = '''#!/usr/bin/env python3
"""Trusted Leti Cook release installer. Obtain this file from a trusted source."""
import sys
if sys.version_info < (3, 10):
    raise SystemExit('Python 3.10 or later is required.')
import os
sys.dont_write_bytecode = True
sys.path[:] = [p for p in sys.path if p and os.path.abspath(p) not in
               {os.getcwd(), os.path.dirname(os.path.abspath(__file__))}]
import base64
import hashlib
import json
from pathlib import Path
import tempfile
import zlib

_PAYLOAD = {payload!r}
_LENGTH = {length}
_SHA256 = {digest!r}

def _main():
    packed = base64.b85decode(_PAYLOAD)
    decoder = zlib.decompressobj()
    raw = decoder.decompress(packed, _LENGTH + 1)
    if (len(raw) != _LENGTH or not decoder.eof or decoder.unused_data
            or hashlib.sha256(raw).hexdigest() != _SHA256):
        raise SystemExit('reason=release_bootstrap_corrupt')
    files = json.loads(raw)
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix='leti-cook-installer-') as temporary:
        root = Path(temporary)
        for name, encoded in files.items():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with target.open('xb') as stream:
                stream.write(base64.b64decode(encoded, validate=True))
        sys.path.insert(0, str(root))
        from ops.release_install import main
        return main()

if __name__ == '__main__':
    raise SystemExit(_main())
'''
    # format() would also interpret the Python set literal in the fixed body.
    return template.replace('{payload!r}', repr(payload)).replace('{length}', str(len(document))).replace(
        '{digest!r}', repr(digest)).encode('ascii')


def build_bootstrap(root: Path, output: Path) -> Path:
    data = bootstrap_bytes(root)
    output = safe_path(output)
    if output.name != BOOTSTRAP_NAME:
        raise ReleaseError('release_bootstrap_filename_invalid')
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o644)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT, help='Reviewed source tree containing canonical release trust files.')
    parser.add_argument('--output', type=Path, required=True, help='New install-release.py file.')
    args = parser.parse_args(argv)
    try:
        build_bootstrap(args.root, args.output)
    except (OSError, ValueError) as error:
        print('reason=' + (error.code if isinstance(error, ReleaseError) else type(error).__name__), file=sys.stderr)
        return 1
    print('status=bootstrap_ready')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
