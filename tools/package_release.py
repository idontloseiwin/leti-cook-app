"""Build an exact release from the reviewed source selection; no network or SQL."""
from __future__ import annotations
import argparse
import ast
import hashlib
import importlib.util
import json
from pathlib import Path, PurePosixPath
import re
import sys
sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('release_installer', ROOT / 'install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)

def validate_local_imports(files, root: Path = ROOT):
    """Reject omitted static local dependencies without importing application code.

    External packages come from requirements.lock. Dynamic import targets still
    require runtime acceptance; this guard only claims local literal imports.
    """
    missing = set()
    for target, data in files.items():
        path = PurePosixPath(target)
        if not target.startswith('application/') or path.suffix != '.py':
            continue
        package = list(path.parts[1:-1])
        for node in ast.walk(ast.parse(data, filename=target)):
            names = []
            if isinstance(node, ast.Import):
                names = [item.name for item in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    if node.level > len(package):
                        raise ValueError('invalid_selected_relative_import')
                    prefix = package[:len(package) - node.level + 1]
                else:
                    prefix = []
                module = '.'.join(prefix + ([node.module] if node.module else []))
                names = [module] + [module + '.' + item.name if module else item.name for item in node.names]
            for module in names:
                parts = module.split('.')
                for length in range(1, len(parts) + 1):
                    candidate = root / 'application' / Path(*parts[:length])
                    choices = (candidate / '__init__.py', candidate.with_suffix('.py'))
                    for source in choices:
                        if source.is_file():
                            local = source.relative_to(root).as_posix()
                            if local not in files:
                                missing.add(local)
    if missing:
        raise ValueError('missing_selected_local_import: ' + ', '.join(sorted(missing)))


def package(output: Path, *, root=None):
    root = ROOT if root is None else Path(root)
    output = installer.safe_path(output)
    if output.exists():
        raise ValueError('new_output_directory_required')
    selection = json.loads((root / 'packaging/files.json').read_text(encoding='utf-8'))
    # Source development needs no publisher key. Once the owner pins the
    # canonical public key, every package carries it automatically; signed
    # release verification requires it to match the bootstrap/installed key.
    public_key = 'application/release-signing.pub'
    if (root / public_key).exists():
        selection[public_key] = public_key
    dist_prefix = 'application/frontend/dist-self-hosted/'
    selected_dist = {name for name in selection if name.startswith(dist_prefix)}
    actual_dist = {p.relative_to(root).as_posix() for p in (root / dist_prefix).rglob('*') if p.is_file()}
    if selected_dist != actual_dist:
        raise ValueError('frontend_asset_selection_changed_update_packaging_files')
    files = {}
    for target, source in selection.items():
        for name in (target, source):
            p = PurePosixPath(name)
            if p.is_absolute() or '..' in p.parts or ':' in name or '\\' in name or any(x.startswith('.') for x in p.parts):
                raise ValueError('unsafe_selected_path')
        src = installer.safe_path(root / source)
        if not src.is_file() or src.stat().st_nlink != 1 or src.stat().st_size > installer.MAX_FILE:
            raise ValueError('invalid_selected_source')
        data = src.read_bytes()
        if re.search(rb'AIza[0-9A-Za-z_-]{35}|-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----|gh[pousr]_[A-Za-z0-9]{20,}', data):
            raise ValueError('credential_pattern_in_selected_source')
        files[target] = data
    validate_local_imports(files, root=root)
    if len(files) > installer.MAX_FILES or sum(map(len, files.values())) > installer.MAX_TOTAL:
        raise ValueError('release_size_limit')
    manifest = {'format': installer.FORMAT, 'version': 1, 'fresh_install_only': True,
                'stage': 'self-hosted-release-candidate', 'release_accepted': False,
                'youtube_configuration_included': False,
                'files': {k: hashlib.sha256(v).hexdigest() for k, v in sorted(files.items())}}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=False)
    for name, data in files.items():
        dest = output / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    (output / 'bundle.json').write_text(json.dumps(manifest, indent=2, sort_keys=True)+'\n', encoding='utf-8')
    installer.verify_bundle(output)
    return {'status': 'verified_release_candidate', 'files': len(files), 'release_accepted': False}

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(package(args.output)))
