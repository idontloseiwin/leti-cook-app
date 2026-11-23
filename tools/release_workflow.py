"""Small trusted CI gates; no publication is performed by offline tests."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.release_version import parse_version, read_version
from ops.release_manifest import MAX_ARCHIVE, ReleaseError, load_source
from ops.release_verification import _capture, _public_key, verify_release
from tools.build_release_bootstrap import bootstrap_bytes
from tools.package_extension import verify_extension_artifacts


def command(args):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=120, check=True)
    except (OSError, subprocess.SubprocessError):
        raise ReleaseError('release_workflow_command_failed') from None
    if len(result.stdout) > 1024 * 1024:
        raise ReleaseError('release_workflow_output_invalid')
    return result.stdout.strip()


def preflight(root, *, ref, repository, commit, run=command):
    root = Path(root)
    version = read_version(root / 'application/VERSION')
    source = load_source(root / 'application/release-source.json')
    if repository != source.owner + '/' + source.repository or ref != 'refs/tags/v' + version:
        raise ReleaseError('release_workflow_identity_mismatch')
    if type(commit) is not str or re.fullmatch('[0-9a-f]{40}', commit) is None:
        raise ReleaseError('release_workflow_commit_invalid')
    if run(['git', '-C', str(root), 'rev-parse', 'HEAD']) != commit:
        raise ReleaseError('release_workflow_checkout_mismatch')
    key = _capture(root / 'application/release-signing.pub', 4096)
    _public_key(key)
    return {'version': version, 'tag': 'v' + version, 'repository': repository,
            'commit': commit, 'prerelease': bool(parse_version(version).prerelease)}


def verify_checkout_artifacts(root, directory):
    """Rebuild from the trusted tag checkout before allowing its signing key."""
    import install
    from tools.package_release import package
    from tools.release_archive import _write_archive, canonical_json
    root, directory = Path(root), Path(directory)
    version = read_version(root / 'application/VERSION')
    with tempfile.TemporaryDirectory(prefix='leti-cook-release-check-') as temporary:
        bundle = Path(temporary) / 'bundle'
        package(bundle, root=root)
        manifest, files = install.verify_bundle(bundle)
        archive = Path(temporary) / ('leti-cook-' + version + '.tar.gz')
        _write_archive({**files, 'bundle.json': canonical_json(manifest)}, archive)
        if _capture(directory / archive.name, MAX_ARCHIVE) != _capture(archive, MAX_ARCHIVE):
            raise ReleaseError('release_workflow_source_mismatch')
    if _capture(directory / 'install-release.py', 4 * 1024 * 1024) != bootstrap_bytes(root):
        raise ReleaseError('release_workflow_bootstrap_mismatch')
    verify_extension_artifacts(root, directory)


def sign_from_environment(root, directory, *, environ=None):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from tools.sign_release import sign_release
    environ = os.environ if environ is None else environ
    raw = environ.get('LETI_COOK_RELEASE_SIGNING_KEY', '')
    if not isinstance(raw, str) or not 1 <= len(raw) <= 4096:
        raise ReleaseError('release_signing_secret_missing')
    try:
        secret = raw.encode('ascii')
        key = serialization.load_pem_private_key(secret, password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError()
        public = key.public_key().public_bytes(serialization.Encoding.PEM,
                                               serialization.PublicFormat.SubjectPublicKeyInfo)
    except (ValueError, TypeError, UnicodeError):
        raise ReleaseError('release_signing_key_invalid') from None
    root, directory = Path(root), Path(directory)
    trusted = root / 'application/release-signing.pub'
    if _capture(trusted, 4096) != public:
        raise ReleaseError('release_signing_key_mismatch')
    source = load_source(root / 'application/release-source.json')
    verify_checkout_artifacts(root, directory)
    # Outside the artifact tree and removed even if signing/verification fails.
    with tempfile.TemporaryDirectory(prefix='leti-cook-sign-') as temporary:
        path = Path(temporary) / 'private.pem'
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(secret)
        sign_release(directory, path, source=source)
    version = read_version(root / 'application/VERSION')
    return verify_release(directory / ('leti-cook-' + version + '.tar.gz'),
                          public_key_path=trusted, source=source)


def create_draft(root, directory, *, ref, repository, commit, run=command):
    root, directory = Path(root), Path(directory)
    identity = preflight(root, ref=ref, repository=repository, commit=commit, run=run)
    archive = directory / ('leti-cook-' + identity['version'] + '.tar.gz')
    verified = verify_release(archive, public_key_path=root / 'application/release-signing.pub',
                              source=load_source(root / 'application/release-source.json'))
    if verified.manifest['version'] != identity['version']:
        raise ReleaseError('release_workflow_version_mismatch')
    if _capture(directory / 'install-release.py', 4 * 1024 * 1024) != bootstrap_bytes(root):
        raise ReleaseError('release_workflow_bootstrap_mismatch')
    extension_assets = verify_extension_artifacts(root, directory)
    remote = run(['gh', 'api', 'repos/' + repository + '/commits/' + identity['tag'], '--jq', '.sha'])
    if remote != commit:
        raise ReleaseError('release_workflow_remote_tag_changed')
    # `create` refuses an existing release; no upload --clobber or edit fallback.
    args = ['gh', 'release', 'create', identity['tag'], '--repo', repository,
            '--target', commit, '--verify-tag', '--draft', '--title', 'Leti Cook ' + identity['version'],
            '--generate-notes', '--notes-file', str(root / '.github/RELEASE_TEMPLATE.md')]
    if identity['prerelease']:
        args.append('--prerelease')
    args += [str(directory / name) for name in
             (archive.name, 'release.json', 'SHA256SUMS', 'SHA256SUMS.sig', 'install-release.py')]
    args += [str(directory / name) for name in extension_assets]
    run(args)
    return {'draft_created': True, 'tag': identity['tag']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('preflight', 'sign', 'draft'))
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--directory', type=Path)
    args = parser.parse_args()
    try:
        identity = {name: os.environ.get('GITHUB_' + variable, '') for name, variable in
                    (('ref', 'REF'), ('repository', 'REPOSITORY'), ('commit', 'SHA'))}
        if args.action == 'preflight':
            result = preflight(args.root, **identity)
        elif args.directory is None:
            raise ReleaseError('release_directory_required')
        elif args.action == 'sign':
            preflight(args.root, **identity)
            sign_from_environment(args.root, args.directory)
            result = {'signed_and_verified': True}
        else:
            result = create_draft(args.root, args.directory, **identity)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ValueError, OSError, KeyError):
        print('Release workflow stopped. No credentials or raw command output are shown.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
