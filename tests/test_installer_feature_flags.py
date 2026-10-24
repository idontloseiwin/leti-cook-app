"""Feature flags written by the installer are the names the application reads. Source only: no install or server."""
import ast
import asyncio
import unittest
from http_test_support import ROOT, cache_stub, library_stub, endpoint
from types import SimpleNamespace
from starlette.responses import Response
from web.explorer import create_explorer_router
SERVER=ROOT/'application/server.py'

def installer_backend_environment():
    """The literal backend.env mapping built by install.py."""
    for node in ast.walk(ast.parse((ROOT/'install.py').read_text(encoding='utf-8'))):
        if (isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='backend' for t in node.targets)
                and isinstance(node.value,ast.Dict)):
            return {k.value:v.value for k,v in zip(node.value.keys,node.value.values)
                    if isinstance(k,ast.Constant) and isinstance(v,ast.Constant)}
    raise AssertionError('install.py backend environment not found')

def application_string_literals():
    literals=set()
    for path in (ROOT/'application').rglob('*.py'):
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if isinstance(node,ast.Constant) and isinstance(node.value,str):
                literals.add(node.value)
    return literals

def capabilities(environment):
    """The composition parses these four flags and passes them into the real router."""
    names = ('CHANNEL_BASELINE_ANALYTICS_ENABLED', 'CHANNEL_DERIVED_INSIGHTS_ENABLED',
             'CHANNEL_LAUNCH_ANALYTICS_ENABLED', 'CHANNEL_TIMELINE_INSIGHTS_ENABLED')
    assignments = {node.targets[0].id: node.value for node in ast.parse(SERVER.read_text(encoding='utf-8')).body
                   if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)}
    flags = {}
    for name in names:
        expected = ast.parse(f'os.getenv({name!r}, "false").strip().lower() in {{"1", "true", "yes", "on"}}').body[0].value
        if ast.dump(assignments[name]) != ast.dump(expected):
            raise AssertionError(f'Flag parser changed: {name}')
        flags[name] = environment.get(name, 'false').strip().lower() in {'1', 'true', 'yes', 'on'}
    router = create_explorer_router(cache=cache_stub(), library=library_stub(), core=SimpleNamespace(),
                                    database=None, **flags)
    return asyncio.run(endpoint(router, 'channel_explorer_capabilities')(Response()))['channel_explorer']

class InstallerFeatureFlagTests(unittest.TestCase):
    def test_every_flag_the_installer_writes_is_read_by_the_application(self):
        literals=application_string_literals()
        flags=[name for name in installer_backend_environment() if name.endswith('_ENABLED')]
        self.assertTrue(flags)
        self.assertEqual([name for name in flags if name not in literals],[])

    def test_installed_capabilities_enable_baseline_and_derived_insights_only(self):
        states=capabilities(installer_backend_environment())
        available={name for name,state in states.items() if state=='available'}
        self.assertEqual(available,{'raw','baseline','filter_facets','static_video_details',
                                    'measured_change','video_growth','video_history'})
        self.assertEqual((states['timeline'],states['video_launch']),('unavailable','unavailable'))

    def test_without_the_flags_the_analytics_stay_unavailable(self):
        states=capabilities({})
        self.assertEqual({name for name,state in states.items() if state=='available'},{'raw'})

if __name__=='__main__':unittest.main()
