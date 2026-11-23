"""Feature flags written by the installer are the names the application reads. Source only: no install or server."""
import ast
import unittest
from unittest.mock import patch
from http_test_support import ROOT

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

def updater_backend_keys():
    """The backend.env key groups the updater checks in an installed tree."""
    groups = {}
    for node in ast.parse((ROOT/'ops/update_candidate.py').read_text(encoding='utf-8')).body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and node.targets[0].id in (
                '_BACKEND_FLAGS', '_BACKEND_OPTIONAL_FLAGS'):
            groups[node.targets[0].id] = {item.value for item in ast.walk(node.value)
                                          if isinstance(item, ast.Constant) and isinstance(item.value, str)}
    return groups

def allowed_policies(environment):
    """Policies of the research operations the flags allow (web/research.py reads them per request)."""
    from research_query.operations import OPERATIONS
    from web.research import allowed_operations
    with patch.dict('os.environ', {k: v for k, v in environment.items() if k.endswith('_ENABLED')}, clear=True):
        return {OPERATIONS[operation].policy for operation in allowed_operations()}

UNGATED = {'source_observation', 'channel_header', 'home_feed'}

class InstallerFeatureFlagTests(unittest.TestCase):
    def test_every_flag_the_installer_writes_is_read_by_the_application(self):
        literals=application_string_literals()
        flags=[name for name in installer_backend_environment() if name.endswith('_ENABLED')]
        self.assertTrue(flags)
        self.assertEqual([name for name in flags if name not in literals],[])

    def test_installed_capabilities_enable_baseline_and_derived_insights_only(self):
        self.assertEqual(allowed_policies(installer_backend_environment()),
                         UNGATED | {'descriptive_analytics', 'channel_history', 'additional_analytics'})

    def test_installed_research_operations_include_channel_history(self):
        # Research operations follow the flags a new installation writes; channel
        # history is base analytics, not the channel page's separate timeline flag.
        from research_query.operations import OPERATIONS
        from web.research import POLICY_FLAGS, allowed_operations
        environment=installer_backend_environment()
        self.assertNotIn('CHANNEL_TIMELINE_INSIGHTS_ENABLED',environment)
        self.assertEqual(POLICY_FLAGS[OPERATIONS['channel.observed_history.v1'].policy],('CHANNEL_BASELINE_ANALYTICS_ENABLED',))
        self.assertEqual(environment['CHANNEL_BASELINE_ANALYTICS_ENABLED'],'1')
        flags={name:value for name,value in environment.items() if name.endswith('_ENABLED')}
        with patch.dict('os.environ',flags,clear=True):
            self.assertIn('channel.observed_history.v1',allowed_operations())
        with patch.dict('os.environ',{'CHANNEL_TIMELINE_INSIGHTS_ENABLED':'1'},clear=True):
            self.assertNotIn('channel.observed_history.v1',allowed_operations())

    def test_without_the_flags_the_analytics_stay_unavailable(self):
        self.assertEqual(allowed_policies({}), UNGATED)

    def test_the_timeline_flag_is_accepted_but_enables_nothing(self):
        # It enabled only the removed channel Timeline route. An
        # installation may carry it; the updater keeps it (below), nothing reads it.
        written = installer_backend_environment()
        for base in ({}, written):
            with self.subTest(installed=bool(base)):
                self.assertEqual(allowed_policies({**base, 'CHANNEL_TIMELINE_INSIGHTS_ENABLED': '1'}), allowed_policies(base))
        self.assertNotIn('CHANNEL_TIMELINE_INSIGHTS_ENABLED', application_string_literals())
        self.assertEqual(allowed_policies({**written, 'CHANNEL_LAUNCH_ANALYTICS_ENABLED': '1'}) - allowed_policies(written),
                         {'launch_analytics'})

    def test_every_analytics_switch_the_application_reads_survives_updates(self):
        # Launch and timeline stay off unless the owner adds them; an update keeps them.
        keys, written = updater_backend_keys(), installer_backend_environment()
        self.assertEqual({name for name in written if name.endswith('_ENABLED')}, keys['_BACKEND_FLAGS'])
        self.assertEqual(keys['_BACKEND_OPTIONAL_FLAGS'],
                         {'CHANNEL_LAUNCH_ANALYTICS_ENABLED', 'CHANNEL_TIMELINE_INSIGHTS_ENABLED',
                          'LIC_SUPPORT_JOURNAL_ENABLED'})
        self.assertFalse(keys['_BACKEND_OPTIONAL_FLAGS'] & set(written))
        read = ('CHANNEL_BASELINE_ANALYTICS_ENABLED', 'CHANNEL_DERIVED_INSIGHTS_ENABLED',
                'CHANNEL_LAUNCH_ANALYTICS_ENABLED', 'CHANNEL_TIMELINE_INSIGHTS_ENABLED')
        self.assertEqual([name for name in read if name not in keys['_BACKEND_FLAGS'] | keys['_BACKEND_OPTIONAL_FLAGS']], [])

    def test_support_journal_switch_is_optional_and_does_not_change_research_permissions(self):
        written=installer_backend_environment()
        self.assertNotIn('LIC_SUPPORT_JOURNAL_ENABLED',written)
        self.assertIn('LIC_SUPPORT_JOURNAL_ENABLED',application_string_literals())
        for value in ('true','false'):
            self.assertEqual(allowed_policies({**written,'LIC_SUPPORT_JOURNAL_ENABLED':value}),
                             allowed_policies(written))

if __name__=='__main__':unittest.main()
