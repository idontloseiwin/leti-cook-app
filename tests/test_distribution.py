"""Offline distribution-boundary regressions."""
import ast
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'application'))
from deployment_profile import selected_deployment_profile
import youtube_categories

class DistributionTests(unittest.TestCase):
    def test_profile_never_falls_back_to_private(self):
        self.assertEqual(selected_deployment_profile({'LIC_DEPLOYMENT_PROFILE':'self-hosted'}),'self-hosted')
        for values in ({},{'LIC_DEPLOYMENT_PROFILE':'private'},{'LIC_DEPLOYMENT_PROFILE':'unknown'}):
            with self.assertRaises(RuntimeError):selected_deployment_profile(values)

    def test_category_lookup_uses_managed_store(self):
        calls=[]
        class Store:
            def category_name(self,category,language):
                calls.append((category,language));return 'fresh-label'
        module=types.ModuleType('youtube_category_reference')
        module.managed_reference_store=lambda:Store()
        with patch.dict(sys.modules,{'youtube_category_reference':module}):
            self.assertEqual(youtube_categories.youtube_video_category_name('27','en'),'fresh-label')
        self.assertEqual(calls,[('27','en')])

    def test_removed_modules_are_not_imported(self):
        imports=[]
        for path in (ROOT/'application').rglob('*.py'):
            if 'node_modules' in path.parts:continue
            tree=ast.parse(path.read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if isinstance(node,ast.Import):imports.extend(alias.name for alias in node.names)
                elif isinstance(node,ast.ImportFrom):imports.append(node.module or '')
        for name in imports:
            self.assertFalse(name.startswith(('owner_studio','db.owner_studio','db.saved_rollout','telemetry_legacy_routes','adaptive_counter_cadence','channel_sync_pipeline','deep_inventory')),name)
        for name in ('db/saved_rollout.py','adaptive_counter_cadence.py','channel_sync_pipeline.py','deep_inventory.py'):
            self.assertFalse((ROOT/'application'/name).exists())

if __name__=='__main__':unittest.main()
