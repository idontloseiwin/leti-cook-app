"""Every backend package is pinned by version and hash; tool versions agree. Source only: no network."""
from pathlib import Path
import re
import unittest

ROOT=Path(__file__).resolve().parents[1]
APP=ROOT/'application'

def pins(text):
    return {m.group(1).lower():m.group(2) for m in re.finditer(r'^([A-Za-z0-9._-]+)(?:\[[A-Za-z0-9._,-]+\])?==([^\s\\;]+)',text,re.M)}

def lock_blocks():
    text=(APP/'requirements.lock').read_text(encoding='utf-8')
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

if __name__=='__main__':unittest.main()
