"""Online and offline delivery enter one verified update plan, with read-only checks."""
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ops import updater
from ops.release_manifest import ReleaseError, ReleaseSource
from ops.release_verification import VerifiedRelease


class UpdateDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.root=Path('/opt/leti-cook-test')
        self.layout=SimpleNamespace(installation=self.root, owner=Path('/owned/owner'),
                    project='leti-cook-0123456789ab',database_id='00000000-0000-0000-0000-000000000001')
        self.source=ReleaseSource('example-owner','example-app')
        self.release=VerifiedRelease({'version':'0.2.0'}, {'sentinel':b'captured'})
        self.lines=[]
        for name,value in (('load_source',self.source),('read_version','0.1.0'),
                           ('fetch_release',self.release),('verify_release',self.release)):
            patcher=patch.object(updater,name,return_value=value)
            setattr(self,name,patcher.start())
            self.addCleanup(patcher.stop)
        patcher=patch('ops.update_candidate.preflight_candidate',return_value=object())
        self.preflight=patcher.start()
        self.addCleanup(patcher.stop)
        patcher=patch.object(updater,'Host')
        self.host=patcher.start()
        self.addCleanup(patcher.stop)
        patcher=patch.object(updater,'UpdateEngine')
        self.engine=patcher.start()
        self.addCleanup(patcher.stop)

    def test_online_check_uses_installed_source_and_key_without_preparing_update(self):
        with patch.object(updater.journal,'tree_identity') as tree:
            self.assertEqual(updater.update(self.layout,None,check=True,pre=True,emit=self.lines.append),0)
        self.load_source.assert_called_once_with(self.root/'application/release-source.json')
        self.fetch_release.assert_called_once_with(source=self.source,public_key_path=self.root/'application/release-signing.pub',include_prereleases=True)
        self.verify_release.assert_not_called()
        self.preflight.assert_called_once_with(self.root,self.release,allow_prerelease=True)
        self.host.assert_called_once_with(self.layout,plan=self.preflight.return_value,
                                         release=self.release,build_local=False)
        self.host.return_value.check_readiness.assert_called_once_with()
        self.host.return_value.preflight.assert_not_called()
        self.engine.assert_not_called()
        tree.assert_not_called()
        self.assertIn('Read-only installation checks passed.',self.lines[-2])
        self.assertIn('No update was started.',self.lines[-2])
        self.assertIn('when applying the update.',self.lines[-1])

    def test_online_update_passes_captured_release_into_existing_host_engine(self):
        observed={'old_images':{'db':'db','backend':'backend'},'old_volume':'old','runtime_epoch':'epoch'}
        self.host.return_value.preflight.return_value=observed
        with patch.object(updater.journal,'tree_identity',return_value={'device':1,'inode':2}):
            self.assertEqual(updater.update(self.layout,None,build_local=True,emit=self.lines.append),0)
        self.host.assert_called_once_with(self.layout,plan=self.preflight.return_value,
                                         release=self.release,build_local=True)
        self.engine.return_value.update.assert_called_once_with()
        args,kwargs=self.engine.call_args
        self.assertIs(args[0],self.host.return_value)
        self.assertEqual(args[1]['phase'],'preparing')
        self.assertEqual(args[1]['new_version'],'0.2.0')
        self.assertEqual(args[1]['old_volume'],'old')

    def test_offline_path_does_not_attempt_online_lookup(self):
        path=Path('/download/leti-cook-0.2.0.tar.gz')
        self.assertEqual(updater.update(self.layout,path,check=True,emit=self.lines.append),0)
        self.fetch_release.assert_not_called()
        self.verify_release.assert_called_once_with(path,public_key_path=self.root/'application/release-signing.pub',source=self.source)
        self.host.return_value.check_readiness.assert_called_once_with()
        self.host.return_value.preflight.assert_not_called()
        self.engine.assert_not_called()

    def test_check_surfaces_runtime_or_source_copy_refusal_without_starting_update(self):
        for code in ('update_runtime_not_ready', 'update_copy_window_unconfirmed'):
            with self.subTest(code=code):
                self.host.return_value.check_readiness.side_effect = ValueError(code)
                self.lines.clear()
                with self.assertRaisesRegex(ValueError, '^' + code + '$'):
                    updater.update(self.layout,None,check=True,emit=self.lines.append)
                self.host.return_value.preflight.assert_not_called()
                self.engine.assert_not_called()
                self.assertFalse(any('checks passed' in line for line in self.lines))

    def test_same_version_does_not_start_or_prepare_update(self):
        self.read_version.return_value='0.2.0'
        self.assertEqual(updater.update(self.layout,None,emit=self.lines.append),0)
        self.preflight.assert_not_called()
        self.host.assert_not_called()
        self.engine.assert_not_called()
        self.assertEqual(self.lines,['Already using this release: 0.2.0.'])

    def test_download_signature_or_candidate_failures_cannot_trigger_changes(self):
        for failed in (self.fetch_release,self.preflight):
            with self.subTest(failed=failed):
                failed.side_effect=ReleaseError('release_test_failure')
                with self.assertRaisesRegex(ReleaseError,'release_test_failure'):
                    updater.update(self.layout,None,emit=self.lines.append)
                self.host.assert_not_called()
                self.engine.assert_not_called()
                failed.side_effect=None


if __name__=='__main__':
    unittest.main()
