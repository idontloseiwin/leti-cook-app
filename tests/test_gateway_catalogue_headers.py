"""Offline capacity regression for real catalogue batches through the DB gateway."""
import ast
from pathlib import Path
import re
import unittest
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]


def literal(path, name):
    tree = ast.parse(path.read_text(encoding='utf-8'))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(name)


class GatewayCatalogueHeadersTests(unittest.TestCase):
    def test_full_video_batches_fit_response_header_buffer(self):
        source = ROOT / 'application/channel_explorer.py'
        count = literal(source, 'DB_BATCH_SIZE')
        ids = [f'v{i:010d}' for i in range(count)]
        config = (ROOT / 'database/gateway.conf').read_text()
        match = re.search(r'^\s*proxy_buffer_size\s+(\d+)([kKmM]?);', config, re.M)
        capacity = int(match[1]) * {'': 1, 'k': 1024, 'm': 1024**2}[match[2].lower()] if match else 4096
        for name in ('FORMAT_TOTAL_VIDEO_COLUMNS', 'VIDEO_COLUMNS'):
            with self.subTest(columns=name):
                query = urlencode({'select': literal(source, name), 'video_id': 'in.(' + ','.join(ids) + ')'})
                # Reserve 1 KiB for the status line and other PostgREST headers.
                header_bytes = len(('Content-Location: /videos?' + query + '\r\n').encode()) + 1024
                self.assertLessEqual(header_bytes, capacity)


if __name__ == '__main__':
    unittest.main()
