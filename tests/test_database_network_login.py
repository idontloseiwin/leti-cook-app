"""Only the application's login reaches PostgreSQL over the network. Source only.

database/Dockerfile writes the connection rules into the image and
database/compose.yaml selects them, so an update and its rollback change them
together. These tests run the Dockerfile's own command, check each rule against
the connections an installation makes, and check that the installer, the
update override and the Linux CI use the same file.
"""
import ipaddress
import json
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
from ops.update_host import runtime_override
from test_direct_pg_installation import PORT, PROJECT, composed

RECIPE = (ROOT / 'database/Dockerfile').read_text(encoding='utf-8')
COMPOSE = (ROOT / 'database/compose.yaml').read_text(encoding='utf-8')
DIRECTORY = '/usr/local/share/leti-cook'
HBA_FILE = DIRECTORY + '/pg_hba.conf'
EXPECTED_RULES = [
    ('local', 'all', 'all', None, 'trust'),
    ('host', 'all', 'all', '127.0.0.1/32', 'scram-sha-256'),
    ('host', 'all', 'all', '::1/128', 'scram-sha-256'),
    ('host', 'leti-cook', 'authenticator', 'all', 'scram-sha-256'),
    ('host', 'all', 'all', 'all', 'reject'),
]
# Addresses a container on the installation's Docker network may have.
NETWORK = ('172.18.0.3', '10.0.0.7', '192.168.1.20', 'fd00::5', 'fe80::1')


def run_instructions():
    """Each RUN instruction with Docker's backslash continuations kept for the shell."""
    blocks, current = [], None
    for line in RECIPE.splitlines():
        if current is None:
            if not line.startswith('RUN '):
                continue
            current = [line[4:]]
        else:
            current.append(line)
        if not line.endswith('\\'):
            blocks.append('\n'.join(current))
            current = None
    return blocks


def parse_rules(text):
    rules = []
    for line in text.splitlines():
        fields = line.split()
        if not fields or fields[0].startswith('#'):
            continue
        if fields[0] == 'local':
            rules.append((fields[0], fields[1], fields[2], None, ' '.join(fields[3:])))
        else:
            rules.append((fields[0], fields[1], fields[2], fields[3], ' '.join(fields[4:])))
    return rules


def method(rules, kind, database, user, address=None):
    """PostgreSQL's first-match rule; a connection no line matches is refused."""
    for rule_kind, rule_database, rule_user, rule_address, auth in rules:
        if rule_kind != kind or rule_database not in ('all', database) or rule_user not in ('all', user):
            continue
        if kind == 'host' and rule_address != 'all' and \
                ipaddress.ip_address(address) not in ipaddress.ip_network(rule_address):
            continue
        return auth
    return 'reject'


def database_command():
    """The db service's command list, read from compose.yaml without a YAML parser."""
    block = COMPOSE.split('\n    command:\n', 1)[1]
    values = []
    for line in block.splitlines():
        if not line.startswith('      '):
            break
        item = line.strip()
        if item.startswith('- '):
            values.append(item[2:])
    return values


class DatabaseNetworkLoginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bash = str(Path('C:/Program Files/Git/bin/bash.exe')) if os.name == 'nt' else shutil.which('bash')
        if not cls.bash or not Path(cls.bash).exists():
            raise RuntimeError('Bash is required to run the Dockerfile command')

    def written_rules(self):
        """Run the Dockerfile's own instruction into a temporary folder."""
        blocks = [block for block in run_instructions() if 'pg_hba.conf' in block]
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].count(DIRECTORY), 3)
        with tempfile.TemporaryDirectory(prefix='pg-hba-test-') as temp:
            target = Path(temp) / 'share'
            command = blocks[0].replace(DIRECTORY, target.as_posix())
            result = subprocess.run([self.bash, '-c', command], capture_output=True, text=True, timeout=20,
                                    env=dict(os.environ, MSYS_NO_PATHCONV='1', MSYS2_ARG_CONV_EXCL='*'))
            self.assertEqual(result.returncode, 0, result.stderr)
            written = (target / 'pg_hba.conf').read_bytes()
            if os.name != 'nt':
                self.assertEqual((target / 'pg_hba.conf').stat().st_mode & 0o777, 0o644)
                self.assertEqual(target.stat().st_mode & 0o777, 0o755)
        return written

    def test_image_writes_exactly_the_reviewed_rules(self):
        written = self.written_rules()
        self.assertTrue(written.endswith(b'\n'))
        self.assertNotIn(b'\r', written)
        self.assertEqual(parse_rules(written.decode('ascii')), EXPECTED_RULES)

    def test_superuser_is_refused_from_every_network_address(self):
        rules = parse_rules(self.written_rules().decode('ascii'))
        for address in NETWORK:
            for database in ('leti-cook', 'postgres', 'template1'):
                for user in ('postgres', 'anon', 'service_role', 'lic_reader', 'someone'):
                    with self.subTest(address=address, database=database, user=user):
                        self.assertEqual(method(rules, 'host', database, user, address), 'reject')
                # The application's login reaches only its own database.
                expected = 'scram-sha-256' if database == 'leti-cook' else 'reject'
                self.assertEqual(method(rules, 'host', database, 'authenticator', address), expected)

    def test_local_maintenance_and_health_paths_still_sign_in(self):
        rules = parse_rules(self.written_rules().decode('ascii'))
        # Bootstrap, schema steps, check-secrets and redact-secrets: the local socket.
        self.assertEqual(method(rules, 'local', 'leti-cook', 'postgres'), 'trust')
        # The health check: pg_isready -h 127.0.0.1 inside the container.
        for address in ('127.0.0.1', '::1'):
            self.assertEqual(method(rules, 'host', 'leti-cook', 'postgres', address), 'scram-sha-256')
        self.assertNotIn('replication', {rule[1] for rule in rules})
        self.assertFalse([rule for rule in rules if rule[0] == 'host' and rule[4] == 'trust'])
        self.assertEqual(rules[-1], ('host', 'all', 'all', 'all', 'reject'))

    def test_compose_selects_the_image_file_and_keeps_the_health_check(self):
        command = database_command()
        self.assertEqual(command[0], 'postgres')
        self.assertTrue(all(flag == '-c' for flag in command[1::2]), command)
        settings = [value.split('=', 1)[0] for value in command[2::2]]
        self.assertEqual(len(settings), len(set(settings)))
        self.assertIn('hba_file=' + HBA_FILE, command[2::2])
        self.assertIn("pg_isready -h 127.0.0.1 -U postgres -d leti-cook", COMPOSE)
        self.assertIn('POSTGRES_INITDB_ARGS: --auth-host=scram-sha-256', COMPOSE)

    def test_superuser_tools_use_the_local_socket_and_the_runtime_uses_its_login(self):
        from ops.schema_upgrade import PSQL_ARGUMENTS
        from ops.self_hosted_credentials import runtime_settings
        self.assertIn('--host=/var/run/postgresql', PSQL_ARGUMENTS)
        self.assertIn('--username=postgres', PSQL_ARGUMENTS)
        bootstrap = (ROOT / 'database/bootstrap.sh').read_text(encoding='utf-8')
        self.assertEqual(bootstrap.count('psql --no-psqlrc'), 2)
        self.assertNotIn('--host', bootstrap)
        manage = (ROOT / 'manage.py').read_text(encoding='utf-8')
        self.assertEqual(manage.count("'--username', 'postgres'"), 2)
        self.assertNotIn("'--host'", manage)
        self.assertIsNone(re.search(r'(?i)postgres(?:ql)?://postgres[:@]|user=postgres', manage + bootstrap))
        dsn = runtime_settings('a' * 64)['LIC_DATABASE_DSN']
        self.assertRegex(dsn, r'^postgresql://authenticator:[^@]+@db:5432/leti-cook$')

    def test_installer_and_update_override_accept_the_command(self):
        config = composed()
        config['services']['db']['command'] = database_command()
        install.validate_composed(config, PORT, directory=PurePosixPath('/opt/leti-cook'), project=PROJECT)
        # An update only swaps images and the data volume; the rules file comes with the image.
        override = runtime_override({'db': 'sha256:' + '1' * 64, 'backend': 'sha256:' + '2' * 64},
                                    PROJECT + '_database-data')
        self.assertEqual(override['services']['db'], {'image': 'sha256:' + '1' * 64})

    def test_no_new_shipped_file_and_no_unhashed_build_input(self):
        manifest = json.loads((ROOT / 'packaging/files.json').read_text(encoding='utf-8'))
        self.assertEqual(sorted(name for name in manifest if name.startswith('database/')),
                         ['database/Dockerfile', 'database/bootstrap.sh', 'database/compose.yaml'])
        self.assertIsNone(re.search(r'(?m)^(COPY|ADD|ARG)\b', RECIPE))
        # The privilege-dropping instruction stays last; its own test extracts it.
        self.assertIn('setpriv', run_instructions()[-1])

    def test_linux_ci_starts_its_database_with_the_same_rules(self):
        script = (ROOT / 'tools/check_schema_postgres.sh').read_text(encoding='utf-8')
        self.assertEqual(script.count('-- postgres -c hba_file=' + HBA_FILE + '\n'), 1)
        self.assertIn("== '%s|%d|0' ]]" % (HBA_FILE, len(EXPECTED_RULES)), script)
        self.assertLess(script.index('pg_hba_file_rules'), script.index('bash "$root/tools/check_schema_ci_suite.sh"'))


if __name__ == '__main__':
    unittest.main()
