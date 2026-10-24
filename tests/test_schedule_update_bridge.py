"""Opt-in offline proof using an exact, separately held old trusted updater.

Set LIC_TEST_OLD_UPDATER_ROOT to the reviewed pre-v4 source checkout. No old
implementation is re-created here: both subprocesses import their staged
installed tree. This proves candidate preflight/preparation only, not signing,
the update journal, Docker activation, or a shipped compatibility release.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
FORMAT = 'leti-cook-owner-install'
SCHEDULE = 'config/collection-schedules.ini'
COMPATIBILITY = 'application/update-compatibility.json'
EXTRAS = {'application/config_migrations.py', 'application/self_hosted_settings.py',
          'schema/configuration-seeds.sql'}


def encoded(value):
    return (json.dumps(value, sort_keys=True) + '\n').encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def repack(files):
    files['bundle.json'] = encoded({'format': FORMAT, 'version': 1,
        'fresh_install_only': True, 'stage': 'self-hosted-release-candidate',
        'release_accepted': False, 'youtube_configuration_included': False,
        'files': {name: digest(data) for name, data in files.items() if name != 'bundle.json'}})
    return files


def capture_source(root):
    # Read the finite literal contract; do not execute the prospective source.
    tree = ast.parse((root / 'ops/bundle_contract.py').read_bytes())
    assignment = next(node for node in tree.body if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == 'REQUIRED_BUNDLE_FILES'
                for target in node.targets))
    assert isinstance(assignment.value, ast.Call)
    assert isinstance(assignment.value.func, ast.Name) and assignment.value.func.id == 'frozenset'
    names = set(ast.literal_eval(assignment.value.args[0])) | EXTRAS
    manifest = json.loads((root / 'schema/manifest.json').read_bytes())
    names |= {'schema/' + name for name in manifest['files']}
    assert len(names) <= 768
    files = {}
    for name in sorted(names):
        path = PurePosixPath(name)
        assert not path.is_absolute() and '..' not in path.parts
        data = (root / name).read_bytes()
        assert len(data) <= 50 * 1024 * 1024
        files[name] = data
    assert sum(map(len, files.values())) <= 128 * 1024 * 1024
    files['application/release-source.json'] = encoded({'format': 'leti-cook-release-source',
        'format_version': 1, 'owner': 'example-owner', 'repository': 'example-app'})
    return files


def write_tree(root, files):
    root.mkdir(mode=0o700)
    for name, data in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(0o600 if name == 'bundle.json' else 0o644)


def captured_bundle(root):
    raw = (root / 'bundle.json').read_bytes()
    names = json.loads(raw)['files']
    assert len(names) <= 768
    return {'bundle.json': raw, **{name: (root / name).read_bytes() for name in names}}


def worker(phase, root, target, bridge, prepared):
    # All imported product code comes from this process's trusted installed root.
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(root))
    from unittest.mock import patch
    import install
    import ops.update_candidate as updater
    from ops.release_verification import VerifiedRelease
    from ops.schema_manifest import validate_schema_bundle
    assert updater._ROOT.resolve() == root.resolve()

    def forbid_external(event, args):
        if event in {'socket.connect', 'subprocess.Popen', 'os.system'}:
            raise AssertionError('offline_bridge_external_operation_refused')
    sys.addaudithook(forbid_external)

    def release(files, version, minimum):
        return VerifiedRelease({'format': 'leti-cook-release', 'format_version': 1,
            'version': version, 'schema_version': validate_schema_bundle(files).schema_version,
            'minimum_upgrade_version': minimum,
            'package': {'name': f'leti-cook-{version}.tar.gz', 'sha256': '0' * 64, 'size': 1},
            'images': {name: {'repository': f'ghcr.io/example-owner/example-app-{name}',
                'digest': 'sha256:' + '1' * 64, 'platform': 'linux/amd64'}
                for name in ('backend', 'db')}}, files)

    def rejected(files, code):
        try:
            updater.preflight_candidate(root, release(files, '0.1.2', '0.1.0'))
        except updater.CandidateError as error:
            assert str(error) == code, str(error)
        else:
            raise AssertionError('old_updater_unexpectedly_accepted_v4')

    if phase == 'old':
        old = captured_bundle(root)
        schema = validate_schema_bundle(old)
        project = 'leti-cook-0123456789ab'
        with patch.object(install, 'safe_path', return_value=PurePosixPath('/opt/leti-cook-test')):
            host, backend = install.installation_settings(Path('/opt/leti-cook-test'),
                8123, schema.schema_sha256, project)
        for name in ('LIC_UPDATE_MARKER_HOST_DIRECTORY', 'LIC_UPDATE_CIPHER_HOST_DIRECTORY', 'SELF_HOSTED_SCHEMA_PATH'):
            host[name] = host[name].replace('/opt/leti-cook-test', root.as_posix())
        private = {'installation.env': ''.join(k + '=' + v + '\n' for k, v in host.items()).encode(),
            'backend.env': ''.join(k + '=' + v + '\n' for k, v in backend.items()).encode()}
        identity = {'project': project, 'port': 8123, 'database_id': backend['LIC_DATABASE_ID'],
            'version': '0.1.0', 'source': 'source'}
        private['installation.json'] = encoded({'format': FORMAT, **identity, 'status': 'preparing',
            'bundle_sha256': digest(old['bundle.json'])})
        private['installed.json'] = encoded({**identity, 'status': 'owner_setup_ready', 'collection_started': False})
        for name, data in private.items():
            (root / name).write_bytes(data)
            (root / name).chmod(0o600)
        owner = b'# Owner comment retained byte for byte\r\n' + old[SCHEDULE].replace(
            b'age_0_1d_seconds = 3600', b'age_0_1d_seconds = 1800').replace(b'\r\n', b'\n').replace(b'\n', b'\r\n')
        (root / SCHEDULE).write_bytes(owner)
        target_files = captured_bundle(target)
        rejected(target_files, 'update_compatibility_unsupported')
        disguised = dict(target_files)
        metadata = json.loads(disguised[COMPATIBILITY])
        metadata['schedules_format_version'] = 3
        disguised[COMPATIBILITY] = encoded(metadata)
        rejected(repack(disguised), 'update_config_invalid')

        bridge_files = captured_bundle(bridge)
        sentinel = dict(bridge_files)
        sentinel['application/collection_schedule.py'] = b"raise AssertionError('candidate parser executed')\n"
        sentinel_plan = updater.preflight_candidate(root, release(repack(sentinel), '0.1.1', '0.1.0'))
        assert sentinel_plan.files[SCHEDULE] == owner
        assert Path(sys.modules['collection_schedule'].__file__).resolve() == (root / 'application/collection_schedule.py').resolve()
        assert sys.modules['collection_schedule'].FORMAT_VERSION == 3
        plan = updater.preflight_candidate(root, release(bridge_files, '0.1.1', '0.1.0'))
        result = updater.prepare_candidate(plan, prepared)
        assert (result.path / SCHEDULE).read_bytes() == owner
        assert (result.path / 'backend.env').read_bytes() == private['backend.env']
        print(json.dumps({'old_rejects_v4': True, 'old_rejects_disguised_v4': True,
            'old_accepts_v3_bridge': True, 'candidate_parser_not_executed': True,
            'owner_schedule_sha256': digest(owner)}))
    else:
        assert phase == 'bridge'
        owner = (root / SCHEDULE).read_bytes()
        files = captured_bundle(target)
        files['application/collection_schedule.py'] = b"raise AssertionError('candidate parser executed')\n"
        plan = updater.preflight_candidate(root, release(repack(files), '0.1.2', '0.1.1'))
        assert sys.modules['collection_schedule'].FORMAT_VERSION == 4
        assert Path(sys.modules['collection_schedule'].__file__).resolve() == (root / 'application/collection_schedule.py').resolve()
        assert plan.files[SCHEDULE] == owner
        result = updater.prepare_candidate(plan, prepared)
        assert (result.path / SCHEDULE).read_bytes() == owner
        print(json.dumps({'bridge_accepts_v4': True, 'candidate_parser_not_executed': True,
            'owner_schedule_sha256': digest(owner)}))


@unittest.skipUnless(os.environ.get('LIC_TEST_OLD_UPDATER_ROOT'), 'requires reviewed pre-v4 trusted baseline')
class ScheduleUpdateBridgeTests(unittest.TestCase):
    def test_actual_old_host_then_prepared_bridge_preserve_owner_settings(self):
        baseline = Path(os.environ['LIC_TEST_OLD_UPDATER_ROOT']).resolve(strict=True)
        with tempfile.TemporaryDirectory(prefix='leti-cook-schedule-bridge-') as directory:
            base = Path(directory).resolve()
            base.chmod(0o700)
            installed, target, bridge, prepared = (base / name for name in ('installed', 'target', 'bridge', 'prepared'))
            old = capture_source(baseline)
            old['application/VERSION'] = b'0.1.0\n'
            self.assertEqual(json.loads(old[COMPATIBILITY])['schedules_format_version'], 3)
            new = capture_source(ROOT)
            new['application/VERSION'] = b'0.1.2\n'
            self.assertEqual(json.loads(new[COMPATIBILITY])['schedules_format_version'], 4)
            transitional = dict(new)
            transitional['application/VERSION'] = b'0.1.1\n'
            transitional[SCHEDULE] = old[SCHEDULE]
            metadata = json.loads(transitional[COMPATIBILITY])
            metadata['schedules_format_version'] = 3
            transitional[COMPATIBILITY] = encoded(metadata)
            for path, files in ((installed, old), (target, new), (bridge, transitional)):
                write_tree(path, repack(files))

            def run(phase, destination):
                completed = subprocess.run([sys.executable, '-I', '-B', str(Path(__file__).resolve()),
                    '--worker', phase, str(installed), str(target), str(bridge), str(destination)],
                    capture_output=True, text=True, timeout=45, check=False)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                return json.loads(completed.stdout)

            first = run('old', prepared)
            # Simulated activation in this test's private temporary directory.
            # The product coordinator/journal and running installation are untouched.
            retired = base / 'retired'
            for path in (installed, prepared, retired):
                self.assertEqual(path.resolve().parent, base)
            installed.rename(retired)
            prepared.rename(installed)
            second = run('bridge', base / 'prepared-final')
            self.assertTrue(first['old_rejects_v4'])
            self.assertTrue(first['old_rejects_disguised_v4'])
            self.assertTrue(first['old_accepts_v3_bridge'])
            self.assertTrue(second['bridge_accepts_v4'])
            self.assertEqual(first['owner_schedule_sha256'], second['owner_schedule_sha256'])


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--worker':
        worker(sys.argv[2], *(Path(value).resolve() for value in sys.argv[3:]))
    else:
        unittest.main()
