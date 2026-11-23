"""An abandoned database transaction is ended after 10 minutes. Source only.

database/compose.yaml sets the server default, so updates apply it to existing
installations and a rollback restores the old command. It must stay well above
every limit the product sets for its own transactions, which take precedence.
"""
from pathlib import Path, PurePosixPath
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import install
from ops.update_host import runtime_override
from test_direct_pg_installation import PORT, PROJECT, composed

COMPOSE = (ROOT / 'database/compose.yaml').read_text(encoding='utf-8')
SETTING = 'idle_in_transaction_session_timeout'
UNITS = {'ms': 0.001, 's': 1, 'min': 60, 'h': 3600}


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


def seconds(value):
    match = re.fullmatch(r'(\d+)(ms|s|min|h)', value)
    if not match:
        raise ValueError(value)
    return int(match[1]) * UNITS[match[2]]


class DatabaseIdleTransactionTests(unittest.TestCase):
    def server_default(self):
        command = database_command()
        self.assertEqual(command[0], 'postgres')
        self.assertTrue(all(flag == '-c' for flag in command[1::2]), command)
        values = [value.split('=', 1) for value in command[2::2]]
        self.assertEqual(len(values), len(dict(values)))
        return dict(values)[SETTING]

    def test_compose_sets_ten_minutes_for_every_session_that_sets_nothing(self):
        self.assertEqual(self.server_default(), '10min')

    def test_limit_stays_above_the_products_own_transaction_limits(self):
        from ops.schema_upgrade import STEP_TIMEOUT_SECONDS
        default = seconds(self.server_default())
        # Every application transaction sets its own, shorter local limit first.
        driver = (ROOT / 'application/db/base/postgres.py').read_text(encoding='utf-8')
        local = re.findall(r"set_config\('" + SETTING + r"', '(\w+)', true\)", driver)
        self.assertEqual(local, ['60s'])
        self.assertGreaterEqual(default, 5 * seconds(local[0]))
        # Schema steps set theirs for the session.
        schema = (ROOT / 'ops/schema_upgrade.py').read_text(encoding='utf-8')
        self.assertIn("f\"SET " + SETTING + "='{STEP_TIMEOUT_SECONDS}s';\\n\"", schema)
        self.assertGreaterEqual(default, 5 * STEP_TIMEOUT_SECONDS)

    def test_the_application_role_inherits_the_default(self):
        # The role keeps only its statement bound; no role or session option overrides this one.
        bootstrap = (ROOT / 'database/bootstrap.sh').read_text(encoding='utf-8')
        self.assertEqual(re.findall(r'alter role authenticator set (\w+)', bootstrap), ['statement_timeout'])
        import manage
        for options in (manage.SCAN_SESSION_OPTIONS, manage.REDACT_SESSION_OPTIONS):
            self.assertNotIn(SETTING, options)
        credentials = (ROOT / 'ops/self_hosted_credentials.py').read_text(encoding='utf-8')
        self.assertNotIn(SETTING, credentials)

    def test_installer_and_update_override_accept_the_command(self):
        config = composed()
        config['services']['db']['command'] = database_command()
        install.validate_composed(config, PORT, directory=PurePosixPath('/opt/leti-cook'), project=PROJECT)
        # An update swaps only images and the data volume, so the new compose brings the setting.
        override = runtime_override({'db': 'sha256:' + '1' * 64, 'backend': 'sha256:' + '2' * 64},
                                    PROJECT + '_database-data')
        self.assertEqual(override['services']['db'], {'image': 'sha256:' + '1' * 64})


if __name__ == '__main__':
    unittest.main()
