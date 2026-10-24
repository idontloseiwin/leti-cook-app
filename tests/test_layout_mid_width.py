"""The updated shell stays a grid at every width (tester finding T-03). Source only."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
STYLES = ROOT / 'application/frontend/src/explorer'


class ShellGridTests(unittest.TestCase):
    def test_the_updated_shell_is_a_grid_outside_any_width_rule(self):
        # styles.css turns the older shell into a block at 900px; the updated
        # shell must override that everywhere, or the page slides under the rail.
        redesign = (STYLES / 'redesign.css').read_text(encoding='utf-8')
        base = re.match(r'\.explorer-app\.is-updated \{(.*?)\n\}', redesign, re.S).group(1)
        self.assertRegex(base, r'\n  display: grid;')
        self.assertIn('@media (max-width: 900px) {\n  .explorer-app {\n    display: block;',
                      (STYLES / 'styles.css').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
