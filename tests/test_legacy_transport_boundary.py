"""Shipped runtime has no stored-function RPC adapter, and none is packaged or imported."""
import ast,json,sys,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'application')]
from db.base import postgres as runtime

class LegacyBoundaryTests(unittest.TestCase):
 def test_runtime_does_not_export_or_attach_legacy_api(self):
  for name in ('FunctionArgument','FunctionSpec'):self.assertFalse(hasattr(runtime,name))
  for name in ('call','call_function'):self.assertFalse(hasattr(runtime.PostgresDatabase,name))
  self.assertFalse((ROOT/'application/db/function_contracts.py').exists())
 def test_historical_evidence_excluded_from_shipping_and_installer(self):
  manifest=json.loads((ROOT/'packaging/files.json').read_text(encoding='utf8'))
  for name in manifest:
   self.assertNotIn('legacy_pg_transport',name);self.assertNotIn('legacy_function_contracts',name);self.assertNotIn('schema24_oracle',name);self.assertNotIn('legacy_transport93',name)
  import install
  self.assertNotIn('application/db/function_contracts.py',install.REQUIRED_BUNDLE_FILES)
  self.assertIn('application/db/base/operation_contracts.py',install.REQUIRED_BUNDLE_FILES)
 def test_application_import_graph_cannot_reach_tools_adapter(self):
  for path in (ROOT/'application').rglob('*.py'):
   for node in ast.walk(ast.parse(path.read_bytes())):
    if isinstance(node,ast.ImportFrom):self.assertFalse(any(word in (node.module or '') for word in ('legacy_pg_transport','legacy_function_contracts','function_contracts')),str(path))
if __name__=='__main__':unittest.main()
