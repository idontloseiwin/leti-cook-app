"""Containers keep only the rights their start needs and have a process limit. Source only.

The official PostgreSQL entrypoint starts as root, hands the data folder to
postgres (find/chown, chmod) and switches to it with setpriv
(database/Dockerfile): CHOWN, DAC_OVERRIDE, FOWNER, SETGID and SETUID. The
backend needs none. The installer refuses a resolved compose without these
settings; the Linux CI runs its database with the same rights. No memory limit.
"""
from copy import deepcopy
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import install
from test_direct_pg_installation import PORT, PROJECT, composed

DATABASE = (ROOT / 'database/compose.yaml').read_text(encoding='utf-8')
BACKEND = (ROOT / 'application.compose.yaml').read_text(encoding='utf-8')
CI = (ROOT / 'tools/check_schema_postgres.sh').read_text(encoding='utf-8')
# Linux capability numbers (linux/capability.h).
NUMBERS = {'CHOWN': 0, 'DAC_OVERRIDE': 1, 'FOWNER': 3, 'SETGID': 6, 'SETUID': 7}
DOCKER_DEFAULT_BOUNDING = '00000000a80425fb'


def service_block(text, name):
    """One service's lines, from its four-space key to the next service or top-level key."""
    lines = text.split('\n  ' + name + ':\n', 1)[1].splitlines()
    block = []
    for line in lines:
        if line and not line.startswith('    '):
            break
        block.append(line)
    return '\n'.join(block)


def validate(config):
    install.validate_composed(config, PORT, directory=PurePosixPath('/opt/leti-cook'), project=PROJECT)


class ContainerLimitTests(unittest.TestCase):
    def test_database_service_keeps_only_the_entrypoint_rights(self):
        db = service_block(DATABASE, 'db')
        self.assertIn('\n    cap_drop: [ALL]\n', db + '\n')
        added = re.search(r'\n    cap_add: \[([A-Z_, ]+)\]\n', db + '\n')[1].split(', ')
        self.assertEqual(added, install.CONTAINER_CAPABILITIES['db'])
        self.assertEqual(sorted(added), sorted(NUMBERS))
        self.assertIn('\n    security_opt: [no-new-privileges:true]\n', db + '\n')
        self.assertEqual(int(re.search(r'\n    pids_limit: (\d+)\n', db + '\n')[1]),
                         install.CONTAINER_PROCESS_LIMITS['db'])
        self.assertIn('setpriv', db)
        for absent in ('mem_limit', 'memswap_limit', 'mem_reservation', 'deploy:', 'privileged', 'cpus'):
            self.assertNotIn(absent, db)
        # The capabilities serve the official entrypoint's root phase and the privilege drop.
        recipe = (ROOT / 'database/Dockerfile').read_text(encoding='utf-8')
        self.assertIn('--reuid=postgres --regid=postgres --init-groups', recipe)

    def test_backend_service_has_no_capabilities_and_a_process_limit(self):
        backend = service_block(BACKEND, 'backend')
        self.assertIn('\n    cap_drop: [ALL]\n', backend + '\n')
        self.assertNotIn('cap_add', backend)
        self.assertIn('\n    security_opt: [no-new-privileges:true]\n', backend + '\n')
        self.assertEqual(int(re.search(r'\n    pids_limit: (\d+)\n', backend + '\n')[1]),
                         install.CONTAINER_PROCESS_LIMITS['backend'])
        for absent in ('mem_limit', 'memswap_limit', 'mem_reservation', 'deploy:', 'privileged'):
            self.assertNotIn(absent, backend)
        self.assertEqual(install.CONTAINER_CAPABILITIES['backend'], [])

    def test_installer_refuses_each_lost_or_widened_setting(self):
        validate(composed())
        reordered = composed()
        reordered['services']['db']['cap_add'].reverse()
        validate(reordered)
        rights, processes = 'container_rights_not_limited', 'container_process_limit_required'
        cases = [
            (lambda s: s['db'].pop('cap_drop'), rights),
            (lambda s: s['db'].update(cap_drop=['NET_RAW']), rights),
            (lambda s: s['db']['cap_add'].append('SYS_ADMIN'), rights),
            (lambda s: s['db']['cap_add'].append('CHOWN'), rights),
            (lambda s: s['db']['cap_add'].remove('FOWNER'), rights),
            (lambda s: s['db'].update(cap_add=['ALL']), rights),
            (lambda s: s['backend'].update(cap_add=['NET_BIND_SERVICE']), rights),
            (lambda s: s['backend'].pop('cap_drop'), rights),
            (lambda s: s['db'].pop('security_opt'), rights),
            (lambda s: s['db'].update(security_opt=['no-new-privileges:false']), rights),
            (lambda s: s['db']['security_opt'].append('seccomp=unconfined'), rights),
            (lambda s: s['backend'].update(security_opt=[]), rights),
            (lambda s: s['db'].update(privileged=True), rights),
            (lambda s: s['db'].pop('pids_limit'), processes),
            (lambda s: s['db'].update(pids_limit=-1), processes),
            (lambda s: s['db'].update(pids_limit=0), processes),
            (lambda s: s['db'].update(pids_limit=4096), processes),
            (lambda s: s['db'].update(pids_limit='512'), processes),
            (lambda s: s['backend'].pop('pids_limit'), processes),
            (lambda s: s['backend'].update(pids_limit=2048), processes),
        ]
        for index, (mutate, code) in enumerate(cases):
            with self.subTest(case=index, code=code):
                config = deepcopy(composed())
                mutate(config['services'])
                with self.assertRaisesRegex(install.InstallError, '^' + code + '$'):
                    validate(config)

    def test_linux_ci_runs_its_database_with_the_same_rights(self):
        # Options end at the bare "--" (followed by the command, if any).
        create = re.split(r'\n  --(?:\n| )', CI.split('schema_ci_create postgres ', 1)[1], maxsplit=1)[0]
        self.assertIn('--cap-drop ALL ', create)
        self.assertEqual(re.findall(r'--cap-add (\w+)', create), install.CONTAINER_CAPABILITIES['db'])
        self.assertIn('--security-opt no-new-privileges:true', create)
        # A stricter process ceiling than the installation's proves the headroom.
        self.assertLessEqual(int(re.search(r'--pids-limit (\d+)', create)[1]), install.CONTAINER_PROCESS_LIMITS['db'])
        mask = sum(1 << NUMBERS[name] for name in install.CONTAINER_CAPABILITIES['db'])
        self.assertIn('= %016x\n' % mask, CI)

    def test_ci_bounding_set_check_accepts_only_the_reduced_container(self):
        bash = str(Path('C:/Program Files/Git/bin/bash.exe')) if os.name == 'nt' else shutil.which('bash')
        if not bash or not Path(bash).exists():
            raise RuntimeError('Bash is required to run the CI capability check')
        lines = [line.strip().replace("'\\''", "'") for line in CI.splitlines()
                 if 'CapBnd:' in line or 'NoNewPrivs:' in line]
        self.assertEqual(len(lines), 2)
        mask = '%016x' % sum(1 << NUMBERS[name] for name in install.CONTAINER_CAPABILITIES['db'])
        for bounding, no_new, accepted in ((mask, '1', True), (DOCKER_DEFAULT_BOUNDING, '1', False),
                                           (mask, '0', False), ('000001ffffffffff', '1', False)):
            with self.subTest(bounding=bounding, no_new=no_new), tempfile.TemporaryDirectory() as temp:
                status = Path(temp) / 'status'
                status.write_text(f'Name:\tpostgres\nCapBnd:\t{bounding}\nNoNewPrivs:\t{no_new}\n', newline='\n')
                script = 'set -eu\n' + '\n'.join(line.replace('/proc/1/status', status.as_posix()) for line in lines)
                result = subprocess.run([bash, '-c', script], capture_output=True, text=True, timeout=20,
                                        env=dict(os.environ, MSYS_NO_PATHCONV='1', MSYS2_ARG_CONV_EXCL='*'))
                self.assertEqual(result.returncode == 0, accepted, result.stderr)


if __name__ == '__main__':
    unittest.main()
