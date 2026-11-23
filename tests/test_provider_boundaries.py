"""Static boundaries of the source layer. Source text only: no import of the app."""
import ast
import functools
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
APPLICATION = ROOT / 'application'
SKIP = {'frontend', 'node_modules', '__pycache__'}

# Mode A: stored-data owners. The research backend and the Library's remaining
# channel reads.
STORED_DATA_PACKAGES = ('research_query', 'db/reads')
STORED_DATA_MODULES = (
    'web/research.py', 'web/research_fields.py', 'web/explorer.py', 'web/library.py', 'web/home.py',
    'channel_explorer.py', 'saved_collections.py', 'saved_views.py',
)
PROVIDER_MODULES = ('youtube_client', 'provider_runtime', 'provider_admission', 'core',
                    'provider_sources.transport', 'provider_sources.acquire')


def module_name(path):
    relative = path.relative_to(APPLICATION).with_suffix('')
    parts = relative.parts[:-1] if relative.name == '__init__' else relative.parts
    return '.'.join(parts)


def module_path(name):
    base = APPLICATION.joinpath(*name.split('.'))
    for candidate in (base.with_suffix('.py'), base / '__init__.py'):
        if candidate.is_file():
            return candidate
    return None


@functools.lru_cache(maxsize=None)
def imported_names(path):
    """Every imported module name, including lazy imports inside functions."""
    name = module_name(path)
    package = name if path.name == '__init__.py' else name.rpartition('.')[0]
    result = set()
    for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split('.') if package else []
                base = base[:len(base) - (node.level - 1)] if node.level > 1 else base
                module = '.'.join(base + ([node.module] if node.module else []))
            else:
                module = node.module or ''
            result.add(module)
            result.update(f'{module}.{alias.name}' for alias in node.names)
    return result


def closure(start):
    """Application modules reachable from ``start`` through imports (packages included)."""
    seen, stack = {}, [start]
    while stack:
        current = stack.pop()
        if current in seen or module_path(current) is None:
            continue
        seen[current] = module_path(current)
        for name in imported_names(seen[current]):
            parts = name.split('.')
            stack.extend('.'.join(parts[:index]) for index in range(1, len(parts) + 1))
    return set(seen)


def application_files():
    for path in sorted(APPLICATION.rglob('*.py')):
        if not SKIP & set(path.relative_to(APPLICATION).parts):
            yield path


def stored_data_entry_modules():
    names = [module_name(APPLICATION / relative) for relative in STORED_DATA_MODULES]
    for package in STORED_DATA_PACKAGES:
        names.extend(module_name(path) for path in sorted((APPLICATION / package).glob('*.py')))
    return names


class ModeABoundaryTests(unittest.TestCase):
    def test_stored_data_modules_cannot_reach_the_provider_even_transitively(self):
        entries = stored_data_entry_modules()
        self.assertGreaterEqual(len(entries), 40)  # Guard against silently skipping read modules.
        for entry in entries:
            with self.subTest(entry=entry):
                self.assertIsNotNone(module_path(entry), entry)
                reached = closure(entry)
                self.assertEqual(sorted(name for name in reached if name in PROVIDER_MODULES), [])

    def test_the_closure_helper_sees_lazy_and_relative_imports(self):
        self.assertIn('youtube_client', closure('youtube_batch_stats'))
        self.assertIn('provider_runtime', closure('youtube_client'))  # imported inside a function
        self.assertIn('research_query.validation', closure('research_query.service'))

    def test_query_service_runs_its_entry_points_stored_data_only(self):
        tree = ast.parse((APPLICATION / 'research_query/service.py').read_text(encoding='utf-8'))
        service = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'QueryService')
        guarded = {node.name for node in service.body if isinstance(node, ast.FunctionDef)
                   and any(isinstance(item, ast.Name) and item.id == 'stored_data_only' for item in node.decorator_list)}
        # Select all matching and the frozen dependent reads too
        self.assertEqual(guarded, {'execute', 'execute_graph', 'execute_operation',
                                   'frozen_ids', 'frozen_reference', 'frozen_member'})


class SourceLayerBoundaryTests(unittest.TestCase):
    def test_only_the_layer_transport_imports_the_shared_client(self):
        direct = sorted(path.relative_to(APPLICATION).as_posix() for path in (APPLICATION / 'provider_sources').rglob('*.py')
                        if 'youtube_client' in imported_names(path))
        self.assertEqual(direct, ['provider_sources/transport.py'])

    def test_the_preview_cannot_reach_the_transport(self):
        for entry in ('provider_sources.plan', 'provider_sources.selection', 'provider_sources.wire',
                      'provider_sources.registry', 'provider_sources.credentials', 'provider_sources.results',
                      'provider_sources.families.policy'):
            with self.subTest(entry=entry):
                reached = closure(entry)
                self.assertEqual(sorted(name for name in reached if name in PROVIDER_MODULES
                                        or name in ('provider_sources.parse', 'provider_sources.families.hooks')), [])

    def test_the_layer_never_writes_storage_or_imports_research(self):
        for path in (APPLICATION / 'provider_sources').rglob('*.py'):
            names = imported_names(path)
            with self.subTest(path=path.name):
                self.assertFalse({name for name in names if name.split('.')[0] in ('research_query', 'db', 'core', 'web')})
        self.assertNotIn('provider_sources', {name.split('.')[0] for path in application_files()
                                              if path.parent.name == 'research_query' for name in imported_names(path)})


class TransportOwnershipTests(unittest.TestCase):
    def test_only_the_shared_transport_sends_http_get_to_the_api(self):
        callers = []
        for path in application_files():
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'get'
                        and isinstance(node.func.value, ast.Name) and node.func.value.id == 'requests'):
                    callers.append(path.relative_to(APPLICATION).as_posix())
        self.assertEqual(callers, ['youtube_client.py'])

    def test_api_hosts_appear_only_in_the_transport_and_the_clock_probe(self):
        found = {}
        url = re.compile(r'https?://[A-Za-z0-9.-]*googleapis\.com(?:/\S*)?')
        for path in application_files():
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                # URL literals only; prose in docstrings may name the probe.
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and url.fullmatch(node.value):
                    found.setdefault(path.relative_to(APPLICATION).as_posix(), set()).add(node.value)
        self.assertEqual(set(found), {'youtube_client.py', 'time_trust.py'})
        self.assertEqual(found['time_trust.py'], {'https://www.googleapis.com/'})
        self.assertTrue(all(re.fullmatch(r'https://www\.googleapis\.com/(upload/)?youtube/v3', value)
                            or value in ('https://youtubeanalytics.googleapis.com/v2',
                                         'https://youtubereporting.googleapis.com/v1') # bases
                            for value in found['youtube_client.py']), found['youtube_client.py'])


if __name__ == '__main__':
    unittest.main()
