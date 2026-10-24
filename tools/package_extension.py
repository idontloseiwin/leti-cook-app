"""Build the separate unpacked Chromium ZIP from a fixed source selection."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import zipfile

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ops.release_manifest import ReleaseError
from ops.release_verification import _capture

FILES = (
    'LICENSE', 'README.md', 'background.js', 'bridge.js', 'content.css',
    'content.js', 'icons/icon16.png', 'icons/icon32.png', 'icons/icon48.png',
    'icons/icon128.png', 'manifest.json', 'options.css', 'options.html',
    'options.js', 'shared.js',
)


def extension_artifacts(root=ROOT):
    """Return deterministic captured assets; never include untracked files."""
    source = Path(root) / 'extensions/chromium'
    files = {name: _capture(source / name, 1024 * 1024) for name in FILES}
    manifest = json.loads(files['manifest.json'])
    version = manifest.get('version')
    if (manifest.get('manifest_version') != 3 or not isinstance(version, str)
            or re.fullmatch(r'(?:0|[1-9][0-9]{0,4})(?:\.(?:0|[1-9][0-9]{0,4})){0,3}', version) is None
            or any(int(part) > 65535 for part in version.split('.'))):
        raise ReleaseError('extension_manifest_invalid')
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(files.items()):
            entry = zipfile.ZipInfo('leti-cook-chromium/' + name, (1980, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, content)
    inventory = {'format': 'leti-cook-extension-files', 'version': version,
                 'files': {name: hashlib.sha256(content).hexdigest()
                           for name, content in sorted(files.items())}}
    assets = {
        f'leti-cook-chromium-{version}.zip': data.getvalue(),
        'extension-files.json': (json.dumps(inventory, indent=2, sort_keys=True) + '\n').encode('utf-8'),
    }
    assets['SHA256SUMS-extension'] = ''.join(
        f'{hashlib.sha256(content).hexdigest()}  {name}\n'
        for name, content in sorted(assets.items())
    ).encode('ascii')
    return assets


def package_extension(output, *, root=ROOT):
    assets = extension_artifacts(root)
    output = Path(output)
    if any((output / name).exists() for name in assets):
        raise ReleaseError('extension_output_exists')
    output.mkdir(parents=True, exist_ok=True)
    for name, content in assets.items():
        with (output / name).open('xb') as stream:
            stream.write(content)
    return list(assets)


def verify_extension_artifacts(root, directory):
    assets = extension_artifacts(root)
    for name, content in assets.items():
        if _capture(Path(directory) / name, 20 * 1024 * 1024) != content:
            raise ReleaseError('release_workflow_extension_mismatch')
    return list(assets)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps({'assets': package_extension(args.output)}))
