"""The fast path catches broken documentation without fetching external links."""
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import check_docs


class DocsCheckTests(unittest.TestCase):
    def test_links_anchors_images_references_and_code_examples(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'docs').mkdir()
            (root / 'docs/INSTALLATION.md').write_text('# Setup\n## Same\n## Same\n<a id="custom"></a>\n')
            (root / 'README.md').write_text('''# Home
[install](docs/INSTALLATION.md#setup)
[again](docs/INSTALLATION.md#same-1)
<a href="docs/INSTALLATION.md#custom">custom</a>
[reference][guide]
[guide]: docs/INSTALLATION.md#same
`[example](missing.md)`
```md
[example](also-missing.md)
```
[external](https://example.invalid/not-fetched)
''')
            errors, count = check_docs.check(root)
            self.assertEqual(errors, [])
            self.assertEqual(count, 4)
            (root / 'docs/INSTALLATION.md').write_text('# Changed\n')
            errors, _ = check_docs.check(root)
            self.assertEqual(len(errors), 4)

    def test_missing_images_escape_and_undefined_reference_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'README.md').write_text('![image](missing.png)\n[x](../outside.md)\n')
            errors, _ = check_docs.check(root)
            self.assertEqual(len(errors), 2)
            (root / 'README.md').write_text('[link][missing]\n')
            self.assertIn('Undefined link reference', check_docs.check(root)[0][0])

    def test_encoded_spaces_nested_parentheses_and_headings(self):
        self.assertEqual(check_docs.destination('docs/file(one).md#heading)'), 'docs/file(one).md#heading')
        self.assertEqual(check_docs.destination('<docs/with spaces.md> "title")'), 'docs/with spaces.md')
        self.assertIn('using-the-workspace', check_docs.anchors('## Using the workspace\n'))
        self.assertIn('from-source-development', check_docs.anchors('## From source (development)\n'))
        self.assertIn('configuration', check_docs.anchors('Configuration\n=============\n'))
        self.assertNotIn('example', check_docs.anchors('```md\n# Example\n```\n'))

    def test_current_public_documentation(self):
        errors, count = check_docs.check(ROOT)
        self.assertEqual(errors, [])
        self.assertGreater(count, 20)


if __name__ == '__main__':
    unittest.main()
