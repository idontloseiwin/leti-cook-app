"""Fresh installation from captured signed releases using the ordinary installer.

The public CLI has one built-in source/key. Python injection belongs to trusted
host callers and offline tests, never to an archive or command-line override.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from types import MappingProxyType

import install
from application.release_version import parse_version
from ops.release_download import fetch_release
from ops.release_images import ensure_release_image
from ops.release_manifest import ROOT, ReleaseError, load_source, strict_json
from ops.release_verification import VerifiedRelease, verify_release

_IMAGE = re.compile(r'sha256:[0-9a-f]{64}')


def _pinned_runner(run):
    """Bind every Docker action to the local socket checked for this invocation."""
    endpoint = os.environ.get('DOCKER_HOST', '')
    context_query = ['docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}']
    if not endpoint or os.environ.get('DOCKER_CONTEXT'):
        endpoint = run(context_query).strip()
    if (not endpoint.startswith('unix:///') or '\x00' in endpoint or '\n' in endpoint
            or len(endpoint) > 4096):
        raise install.InstallError('local_docker_socket_required')

    def pinned(args, **kwargs):
        if args == context_query:
            return endpoint
        if args and args[0] == 'docker':
            args = ['docker', '--host', endpoint, *args[1:]]
        return run(args, **kwargs)
    return pinned


def _architecture(info):
    arch = info.get('Architecture')
    aliases = {'x86_64': 'amd64', 'aarch64': 'arm64', 'armv7l': 'arm', 'armv6l': 'arm', 'armhf': 'arm'}
    if type(arch) is not str or re.fullmatch(r'[a-zA-Z0-9_]+', arch) is None:
        raise install.InstallError('docker_architecture_unknown')
    return aliases.get(arch, arch)


def _image_preparer(verified, *, build_local):
    def prepare(directory, project, info, run):
        architecture = _architecture(info)
        local = build_local or architecture != 'amd64'
        print('stage=' + ('build_images' if local else 'pull_verified_images'), flush=True)
        images = {}
        for service in ('db', 'backend'):
            if local:
                reference = project + '-' + service
                # Each context contains only captured release files. Secrets are
                # generated at the installation root, outside both contexts.
                context = directory / ('database' if service == 'db' else 'application')
                run(['docker', 'build', '--label', 'org.leti-cook.project=' + project,
                     '--tag', reference, str(context)], cwd=directory, timeout=1800)
            else:
                image = verified.manifest['images'][service]
                reference = image['repository'] + '@' + image['digest']
                images[service] = ensure_release_image(reference, run)
                continue
            try:
                details = json.loads(run(['docker', 'image', 'inspect', '--format', '{{json .}}', reference]))
            except (ValueError, TypeError):
                raise install.InstallError('release_image_identity_invalid') from None
            if (type(details) is not dict or type(details.get('Id')) is not str
                    or _IMAGE.fullmatch(details['Id']) is None or details.get('Os') != 'linux'
                    or details.get('Architecture') != architecture
                    or (not local and reference not in (details.get('RepoDigests') or []))):
                raise install.InstallError('release_image_identity_invalid')
            images[service] = details['Id']
        override = {'services': {name: {'image': image} for name, image in images.items()},
                    'volumes': {'database-data': {'name': project + '_database-data'}}}
        install.write_private(directory / 'runtime.compose.json',
                              (json.dumps(override, sort_keys=True) + '\n').encode('ascii'))
        return images
    return prepare


def install_verified_release(verified, directory, port, *, run=install.command,
                             build_local=False, api_key=None):
    """Trusted adapter; VerifiedRelease must come from the signature verifier."""
    if not isinstance(verified, VerifiedRelease) or type(build_local) is not bool:
        raise ReleaseError('verified_release_required')
    if sys.platform != 'linux':
        raise install.InstallError('linux_installer_required')
    files = MappingProxyType({name: data for name, data in verified.files.items() if name != 'bundle.json'})
    manifest = strict_json(verified.files['bundle.json'], limit=256 * 1024)
    return install.install_files(manifest, files, directory, port, run=_pinned_runner(run), api_key=api_key,
                                 source='release', prepare_images=_image_preparer(verified, build_local=build_local),
                                 bundle_bytes=verified.files['bundle.json'])


def main(argv=None):
    parser = argparse.ArgumentParser(description='Install a verified Leti Cook release into a new folder.')
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument('--package', type=Path, help='Local archive beside release.json, SHA256SUMS and SHA256SUMS.sig.')
    selection.add_argument('--version', help='Exact version; otherwise the latest stable release.')
    parser.add_argument('--pre', action='store_true', help='Allow prerelease versions.')
    parser.add_argument('--build-local', action='store_true', help='Build images from the verified source files.')
    parser.add_argument('--check', action='store_true', help='Verify the release without installing or contacting Docker.')
    parser.add_argument('--directory', type=Path, help='New absolute installation folder; otherwise ask.')
    parser.add_argument('--port', type=int, help='Application port; otherwise suggest a free port.')
    args = parser.parse_args(argv)
    try:
        if args.port is not None:
            install.validate_port(args.port)
        source = load_source()
        public_key = ROOT / 'application/release-signing.pub'
        if args.package is None:
            verified = fetch_release(source=source, public_key_path=public_key, version=args.version,
                                     include_prereleases=args.pre)
        else:
            verified = verify_release(args.package, source=source, public_key_path=public_key)
        if parse_version(verified.manifest['version']).prerelease and not args.pre:
            raise ReleaseError('release_prerelease_not_allowed')
        if args.check:
            print(json.dumps({'status': 'verified', 'version': verified.manifest['version'],
                              'source': 'release', 'changes': False}, sort_keys=True))
            return 0
        if not sys.stdin.isatty():
            raise install.InstallError('interactive_terminal_required_for_setup_code')
        info = install.check_docker()
        print('Docker checks passed.')
        install.check_time_sync()
        print('The system clock is synchronised.')
        print('Choose where to install Leti Cook and which port to use.')
        print('Use an absolute folder path without spaces; its parent folder must exist.')
        plan = install.choose_plan(args)
        install.print_plan(plan, info, sum(map(len, verified.files.values())))
        os.umask(0o077)
        key = install.hidden_api_key()
        try:
            result = install_verified_release(verified, plan['directory'], plan['port'],
                                              build_local=args.build_local, api_key=key)
        finally:
            key = None
        print(json.dumps(result, sort_keys=True))
        return 0
    except (EOFError, KeyboardInterrupt):
        print('Stopped by user. Any partial installation is retained.', file=sys.stderr)
        return 130
    except (OSError, ValueError, subprocess.SubprocessError, install.InstallError) as error:
        reason = (str(error) if isinstance(error, (ReleaseError, install.InstallError))
                  and re.fullmatch(r'[a-z0-9_]+', str(error)) else type(error).__name__)
        print('reason=' + reason, file=sys.stderr)
        if reason in install.REASON_HINTS:
            print(install.REASON_HINTS[reason], file=sys.stderr)
        print('Installation stopped. Any new partial installation is retained.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
