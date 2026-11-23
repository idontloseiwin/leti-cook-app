"""Every backend package is pinned by version and hash; tool versions agree. Source only: no network."""
from pathlib import Path
import json
import re
import unittest

ROOT=Path(__file__).resolve().parents[1]
APP=ROOT/'application'

def pins(text):
    return {m.group(1).lower():m.group(2) for m in re.finditer(r'^([A-Za-z0-9._-]+)(?:\[[A-Za-z0-9._,-]+\])?==([^\s\\;]+)',text,re.M)}

AUDIT_LOCK=ROOT/'tools/requirements-audit.lock'

def lock_blocks(path=APP/'requirements.lock'):
    text=path.read_text(encoding='utf-8')
    return re.findall(r'^([A-Za-z0-9._-]+)==(\S+)((?:\s*\\\n\s+--hash=sha256:[0-9a-f]{64})+)',text,re.M)

class LockTests(unittest.TestCase):
    def test_dependency_extras_do_not_hide_pins(self):
        self.assertEqual(pins('psycopg[binary]==3.3.6\npsycopg-pool==3.3.3'),
                         {'psycopg': '3.3.6', 'psycopg-pool': '3.3.3'})

    def test_psycopg_binary_matches_driver(self):
        locked=pins((APP/'requirements.lock').read_text(encoding='utf-8'))
        self.assertEqual(locked['psycopg-binary'], locked['psycopg'])

    def test_top_level_pins_match_the_lock(self):
        top=pins((APP/'requirements.txt').read_text(encoding='utf-8'))
        locked=pins((APP/'requirements.lock').read_text(encoding='utf-8'))
        self.assertTrue(top)
        for name,version in top.items():
            self.assertEqual(locked.get(name),version,f'{name}: regenerate requirements.lock (CONTRIBUTING.md)')

    def test_every_locked_package_has_hashes(self):
        blocks=lock_blocks()
        self.assertEqual(len(blocks),len(pins((APP/'requirements.lock').read_text(encoding='utf-8'))))
        for name,_,hashes in blocks:
            self.assertGreaterEqual(hashes.count('--hash=sha256:'),1,name)

    def test_audited_minimums(self):
        locked=pins((APP/'requirements.lock').read_text(encoding='utf-8'))
        version=lambda name:tuple(int(part) for part in locked[name].split('.')[:2])
        self.assertGreaterEqual(version('cryptography'),(50,0))
        self.assertGreaterEqual(version('requests'),(2,33))
        self.assertGreaterEqual(tuple(map(int,locked['certifi'].split('.'))),(2026,10,19))

    def test_image_and_ci_install_only_the_hashed_lock(self):
        dockerfile=(APP/'Dockerfile').read_text(encoding='utf-8')
        self.assertIn('COPY requirements.lock ./requirements.lock',dockerfile)
        self.assertIn('pip install --no-cache-dir --require-hashes --no-deps -r requirements.lock',dockerfile)
        self.assertNotIn('requirements.txt',dockerfile)
        workflow=(ROOT/'.github/workflows/package.yml').read_text(encoding='utf-8')
        self.assertIn('pip install --require-hashes --no-deps -r application/requirements.lock',workflow)
        self.assertIn('"application/requirements.lock": "application/requirements.lock"',
                      (ROOT/'packaging/files.json').read_text(encoding='utf-8'))
        import install
        self.assertIn('application/requirements.lock', install.REQUIRED_BUNDLE_FILES)

class DevelopmentOnlyTests(unittest.TestCase):
    def test_python_dotenv_is_not_shipped(self):
        for name in ('requirements.txt','requirements.lock'):
            self.assertNotIn('python-dotenv',(APP/name).read_text(encoding='utf-8'),name)
        core=(APP/'core.py').read_text(encoding='utf-8')
        self.assertNotRegex(core,r'(?m)^from dotenv import')
        self.assertIn('    from dotenv import load_dotenv\nexcept ImportError:',core)

class ToolVersionTests(unittest.TestCase):
    def test_nvmrc_matches_ci(self):
        node=(APP/'frontend/.nvmrc').read_text(encoding='utf-8').strip()
        for workflow in ('package.yml','audit.yml'):
            self.assertIn(f"node-version: '{node}'",(ROOT/'.github/workflows'/workflow).read_text(encoding='utf-8'),workflow)

    def test_updates_and_audits_are_automated(self):
        dependabot=(ROOT/'.github/dependabot.yml').read_text(encoding='utf-8')
        for ecosystem in ('pip','npm','docker','github-actions'):
            self.assertRegex(dependabot,rf'package-ecosystem: {ecosystem}\n',ecosystem)
        for directory in ('/application', '/database'):
            self.assertIn(f'package-ecosystem: docker\n    directory: {directory}\n', dependabot)
        audit=(ROOT/'.github/workflows/audit.yml').read_text(encoding='utf-8')
        self.assertIn('pip-audit -r application/requirements.lock --require-hashes --disable-pip',audit)
        self.assertIn('audit --omit=dev --audit-level=high',audit)

class AuditToolTests(unittest.TestCase):
    """The audit job installs pip-audit only by hash and also audits the frontend build tools."""
    def workflow(self):
        return (ROOT/'.github/workflows/audit.yml').read_text(encoding='utf-8')

    def test_audit_tool_is_installed_only_from_its_hashed_lock(self):
        installs=[line.strip() for line in self.workflow().splitlines() if re.search(r'\bpip install\b',line)]
        self.assertEqual(installs,['python -m pip install --require-hashes --no-deps -r tools/requirements-audit.lock'])
        self.assertIn('python -m pip check',self.workflow())
        self.assertIn('pip-audit -r tools/requirements-audit.lock --require-hashes --disable-pip',self.workflow())

    def test_audit_lock_pins_every_package_with_hashes(self):
        declared=pins((ROOT/'tools/requirements-audit.txt').read_text(encoding='utf-8'))
        self.assertEqual(declared,{'pip-audit':'2.10.1'})
        text=AUDIT_LOCK.read_text(encoding='utf-8')
        locked=pins(text)
        self.assertEqual(locked['pip-audit'],declared['pip-audit'])
        blocks=lock_blocks(AUDIT_LOCK)
        self.assertEqual(len(blocks),len(locked))
        for name,_,hashes in blocks:
            self.assertGreaterEqual(hashes.count('--hash=sha256:'),1,name)
        # --no-deps installs nothing else, so the lock holds the whole tool, pip included.
        for name in ('pip','pip-api','requests','cyclonedx-python-lib','cachecontrol','rich'):
            self.assertIn(name,locked)
        self.assertNotRegex(text,r'(?m)^[A-Za-z0-9._-]+\[')
        self.assertIn('--generate-hashes',text.split('\n',2)[1])

    def test_shared_packages_match_the_backend_lock(self):
        backend=pins((APP/'requirements.lock').read_text(encoding='utf-8'))
        locked=pins(AUDIT_LOCK.read_text(encoding='utf-8'))
        for name in sorted(set(locked)&set(backend)):
            self.assertEqual(locked[name],backend[name],f'{name}: refresh with --constraint application/requirements.lock')

    def test_audit_tool_is_not_shipped(self):
        self.assertNotIn('requirements-audit',(ROOT/'packaging/files.json').read_text(encoding='utf-8'))

    def test_frontend_build_tools_are_audited_at_critical(self):
        steps=re.findall(r'npm --prefix application/frontend audit[^\n]*',self.workflow())
        self.assertEqual(steps,['npm --prefix application/frontend audit --omit=dev --audit-level=high',
                                'npm --prefix application/frontend audit --audit-level=critical'])
        # Without --omit=dev the audit covers vite and its bundler, which build the shipped web app.
        package=json.loads((APP/'frontend/package.json').read_text(encoding='utf-8'))
        self.assertIn('vite',package['devDependencies'])
        locked=json.loads((APP/'frontend/package-lock.json').read_text(encoding='utf-8'))['packages']
        self.assertTrue(locked['node_modules/vite'].get('dev'))

if __name__=='__main__':unittest.main()
