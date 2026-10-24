"""Guard the agreed dependency direction after collection extraction."""
import ast
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
APPLICATION = ROOT / "application"


def imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    result = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
        elif isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
    return result


class CollectionBoundaryTests(unittest.TestCase):
    def test_scenarios_cannot_import_entrypoints_or_reverse_channel_layer(self):
        forbidden = {"server", "sync_service", "core", "channels", "runtime", "web", "os", "dotenv"}
        for path in sorted((APPLICATION / "collection").glob("*.py")):
            self.assertFalse({name for name in imports(path) if name.split(".")[0] in forbidden}, str(path))
        for path in sorted((APPLICATION / "channels").glob("*.py")):
            for name in imports(path):
                self.assertNotIn(name.split(".")[0], {"server", "sync_service", "core", "runtime", "web"}, str(path))
                if name.split(".")[0] == "collection":
                    self.assertEqual(name, "collection.api", str(path))

    def test_repositories_and_raw_providers_never_import_application_scenarios(self):
        for path in sorted((APPLICATION / "db").rglob("*.py")):
            self.assertFalse({name for name in imports(path) if name.split(".")[0] in
                {"channels", "collection", "server", "sync_service", "runtime", "web"}}, str(path))
        for name in ("youtube_errors", "youtube_playlist", "youtube_channels", "youtube_video_profiles",
                     "youtube_source_fields", "youtube_batch_stats", "youtube_response_trace"):
            path = APPLICATION / (name + ".py")
            self.assertFalse({module for module in imports(path) if module.split(".")[0] in
                {"db", "collection", "channels", "server", "sync_service", "runtime", "core", "web", "os", "dotenv"}}, str(path))

    def test_collection_policy_is_below_repositories_and_scenarios(self):
        path=APPLICATION / "collection_policy.py"
        self.assertFalse({name for name in imports(path) if name.split(".")[0] in
            {"db","channels","collection","server","sync_service","runtime","core","web","os"}})
        manifest=json.loads((ROOT / "packaging/files.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest.get("application/collection_policy.py"),"application/collection_policy.py")

    def test_reconnect_response_contract_has_no_workflow_or_database_dependencies(self):
        path = APPLICATION / 'channel_reconnect_outcome.py'
        self.assertFalse({name for name in imports(path) if name.split('.')[0] in
            {'db', 'channels', 'collection', 'server', 'runtime', 'core', 'web', 'os'}})
        manifest = json.loads((ROOT / 'packaging/files.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest.get('application/channel_reconnect_outcome.py'),
                         'application/channel_reconnect_outcome.py')

    def test_entrypoints_do_not_use_retired_facade(self):
        for relative in ("server.py", "auto_sync.py", "scripts/run_operator_job_worker.py"):
            self.assertNotIn("sync_service", imports(APPLICATION / relative), relative)
        for path in sorted((APPLICATION / "collection").glob("*.py")):
            self.assertNotIn("_legacy_", path.read_text(encoding="utf-8"), str(path))

    def test_http_modules_never_import_the_composition_root(self):
        for path in sorted((APPLICATION / "web").glob("*.py")):
            self.assertFalse({name for name in imports(path) if name.split(".")[0] in
                              {"server", "core", "sync_service"}}, str(path))

    def test_all_extracted_modules_are_in_the_installation_package(self):
        manifest = json.loads((ROOT / "packaging/files.json").read_text(encoding="utf-8"))
        paths = [path for folder in ("collection", "channels", "runtime", "web")
                 for path in (APPLICATION / folder).glob("*.py")]
        paths += [APPLICATION / (name + ".py") for name in
                  ("youtube_errors", "youtube_playlist", "youtube_channels", "youtube_video_profiles", "youtube_response_trace")]
        for path in paths:
            relative = path.relative_to(ROOT).as_posix()
            self.assertEqual(manifest.get(relative), relative)


if __name__ == "__main__":
    unittest.main()
