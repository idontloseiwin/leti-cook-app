"""Temporary auth stores and real ASGI middleware; no installation or network."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
from agent_access import issue, check, digest, READ_ROUTES
from db.agent_access import AgentAccessRepository, MAX_KEYS
from db.self_hosted_auth import OwnerAuthRepository, AuthStoreError


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = int(time.time())
        self.owner = OwnerAuthRepository.initialize(Path(self.directory.name) / 'owner.sqlite3', 'dummy', self.now)
        self.owner.issue_setup('setup', self.now)
        self.assertEqual(self.owner.register('owner', lambda _: True, lambda: 'password-hash', 'session', self.now), 'ok')
        self.metadata, self.token = issue(self.owner, 'My agent', 1, clock=lambda: self.now)
        self.store = AgentAccessRepository(self.owner.path)

    def test_hash_only_distinct_ids_revocation_and_expiry(self):
        self.assertEqual(check(self.owner, self.token, clock=lambda: self.now), 'agent.' + self.metadata['id'])
        self.assertNotIn(self.token.encode(), self.store.path.read_bytes())
        self.assertNotIn('digest', json.dumps(self.store.list(self.owner.agent_binding(), self.now)))
        self.assertIsNone(check(self.owner, self.token, clock=lambda: self.now + 86400))
        self.assertIsNone(check(self.owner, self.token, clock=lambda: self.now - 1))
        second, token = issue(self.owner, 'Second', clock=lambda: self.now)
        self.assertNotEqual(second['id'], self.metadata['id'])
        self.assertTrue(self.store.revoke(self.metadata['id']))
        self.assertIsNone(check(self.owner, self.token, clock=lambda: self.now))
        self.assertIsNotNone(check(self.owner, token, clock=lambda: self.now))

    def test_password_reset_invalidates_all_keys(self):
        self.owner.reset_password('replacement-hash')
        self.assertIsNone(check(self.owner, self.token, clock=lambda: self.now))

    def test_absent_or_corrupt_store_never_recreated_by_request(self):
        self.store.path.unlink()
        self.assertIsNone(check(self.owner, self.token))
        self.assertFalse(self.store.path.exists())
        self.store.path.write_bytes(b'broken')
        with self.assertRaises(AuthStoreError): check(self.owner, self.token)
        self.assertEqual(self.store.path.read_bytes(), b'broken')

    def test_key_limit_and_invalid_options(self):
        for _ in range(MAX_KEYS - 1): issue(self.owner, 'named')
        with self.assertRaises(AuthStoreError): issue(self.owner, 'too many')
        for name, days in [('', 1), ('x\n', 1), ('x', 0), ('x', 366), ('x', True)]:
            with self.assertRaises(AuthStoreError): issue(self.owner, name, days)
        for token in ['', 'owner-cookie', self.token + 'x', 'lic_ro_' + 'я' * 43]:
            self.assertIsNone(check(self.owner, token))

    def test_path_links_rejected(self):
        if os.name == 'nt': return  # Windows symlink privileges are not required by this suite.
        self.store.path.unlink()
        self.store.path.symlink_to(self.owner.path)
        with self.assertRaises(AuthStoreError): check(self.owner, self.token)

    def test_owner_cli_list_revoke_never_prints_token(self):
        import contextlib
        import io
        from ops.self_hosted_agent import main
        out = io.StringIO()
        with patch.dict(os.environ, LIC_AUTH_DATABASE=str(self.owner.path)), contextlib.redirect_stdout(out):
            self.assertEqual(main(['list']), 0)
            self.assertEqual(main(['revoke', '--key-id', self.metadata['id']]), 0)
        self.assertNotIn(self.token, out.getvalue())
        self.assertIn(self.metadata['id'], out.getvalue())

    def test_create_displays_secret_only_on_controlling_terminal(self):
        import contextlib
        import io
        from ops import self_hosted_agent
        class Terminal(io.StringIO):
            def isatty(self): return True
            def close(self): pass
        tty, out = Terminal(), io.StringIO()
        with patch.dict(os.environ, LIC_AUTH_DATABASE=str(self.owner.path)), \
                patch.object(sys.stdin, 'isatty', return_value=True), \
                patch.object(self_hosted_agent, 'open', return_value=tty, create=True), \
                contextlib.redirect_stdout(out):
            self.assertEqual(self_hosted_agent.main(['create','--name','New']), 0)
        secret = tty.getvalue().splitlines()[-1]
        self.assertTrue(secret.startswith('lic_ro_'))
        self.assertNotIn(secret, out.getvalue())
        self.assertEqual(check(self.owner, secret), 'agent.' + json.loads(out.getvalue())['id'])

    def test_owner_cli_names_unfinished_access_setup_by_exit_status(self):
        # Manage.py reads only this status, never the helper's output.
        import contextlib
        import importlib.util
        import io
        from ops import self_hosted_agent
        spec = importlib.util.spec_from_file_location('lic_manage_agent_status', ROOT/'manage.py')
        manage = importlib.util.module_from_spec(spec); spec.loader.exec_module(manage)
        self.assertEqual(self_hosted_agent.EXIT_OWNER_ACCESS_NOT_CONFIGURED, manage.HELPER_REFUSED_STATUS)
        pending = OwnerAuthRepository.initialize(Path(self.directory.name) / 'pending.sqlite3', 'dummy', self.now)
        for argv in (['list'], ['revoke', '--key-id', self.metadata['id']], ['create', '--name', 'New']):
            with self.subTest(argv=argv):
                out, err = io.StringIO(), io.StringIO()
                with patch.dict(os.environ, LIC_AUTH_DATABASE=str(pending.path)), \
                        patch.object(sys.stdin, 'isatty', return_value=True), \
                        contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    status = self_hosted_agent.main(argv)
                self.assertEqual(status, self_hosted_agent.EXIT_OWNER_ACCESS_NOT_CONFIGURED)
                self.assertEqual(out.getvalue(), '')
                self.assertEqual(err.getvalue(), 'Agent access command refused: owner access is not configured yet.\n')
        err = io.StringIO()
        with patch.dict(os.environ, LIC_AUTH_DATABASE=str(self.owner.path)), contextlib.redirect_stderr(err):
            self.assertEqual(self_hosted_agent.main(['revoke', '--key-id', 'not-a-key-id']), 1)
        self.assertIn('Agent access command refused. Check the name', err.getvalue())
        self.assertTrue(self.store.list(self.owner.agent_binding(), self.now))

    def test_manage_preserves_tty_for_create_and_scripts_for_list(self):
        import importlib.util
        from types import SimpleNamespace
        spec = importlib.util.spec_from_file_location('lic_manage_agent_test', ROOT/'manage.py')
        manage = importlib.util.module_from_spec(spec); spec.loader.exec_module(manage)
        for action, interactive in [('create', True), ('list', False), ('revoke', False)]:
            args = SimpleNamespace(action='agent-key-'+action,name='Research',days=30,key_id=self.metadata['id'])
            with patch.object(manage, 'check_docker'), patch.object(manage, 'compose_prefix', return_value=['docker','compose']), \
                    patch.object(manage, 'command', return_value='') as command:
                self.assertEqual(manage._dispatch(args, Path(self.directory.name), 'synthetic'), 0)
                self.assertEqual(command.call_args.kwargs['interactive'], interactive)
                self.assertEqual('-T' in command.call_args.args[0], not interactive)

    def test_exact_routes_do_not_grant_owner_and_recheck_rejects_revocation(self):
        import httpx
        from self_hosted_auth import SelfHostedAuthMiddleware
        from starlette.responses import JSONResponse
        captured = []
        async def inner(scope, receive, send):
            captured.append(scope['state'])
            await JSONResponse({'ok': True})(scope, receive, send)
        origin = 'http://127.0.0.1:8002'
        app = SelfHostedAuthMiddleware(inner, repository=self.owner, origin=origin, clock=lambda: self.now)
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=origin,
                                         headers={'Authorization': 'Bearer ' + self.token, 'Origin': origin}) as client:
                for method, route in READ_ROUTES:
                    response = await client.request(method, route)
                    self.assertEqual(response.status_code, 200, route)
                    self.assertNotIn('installation_access', captured[-1])
                    self.assertNotIn('owner_access_recheck', captured[-1])
                    self.assertTrue(captured[-1]['research_access_recheck']())
                for method, route in [('GET','/auth/session'), ('POST','/research/fields/preferences'),
                                      ('GET','/telemetry'), ('POST','/channels'), ('POST','/provider-sources/execute'),
                                      ('POST','/research/queries/frozen-ids'), ('DELETE','/research/queries/execute')]:
                    self.assertEqual((await client.request(method, route)).status_code, 403, route)
                saved_check = captured[-1]['research_access_recheck']
                self.store.revoke(self.metadata['id'])
                self.assertIsNone(saved_check())
                self.assertEqual((await client.get('/research/capabilities')).status_code, 401)
                with self.owner.transaction() as db: db.execute("UPDATE owner SET access_mode='local'")
                self.assertEqual((await client.get('/research/capabilities')).status_code, 401)
                self.assertEqual((await client.get('/research/capabilities', headers={'Authorization':'Bearer wrong'})).status_code, 401)
        asyncio.run(run())


if __name__ == '__main__': unittest.main()
