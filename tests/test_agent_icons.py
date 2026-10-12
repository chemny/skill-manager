import json
from pathlib import Path
import re
import sys
import unittest
from urllib.parse import unquote
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from skill_locations import BUILTIN_AGENTS


class AgentIconTests(unittest.TestCase):
    def test_every_preset_has_bundled_brand_mark(self):
        page = (Path(__file__).resolve().parents[1] / 'assets/maintenance.html').read_text()
        brands = json.loads(re.search(r'const agentBrandIcons=(.*);', page)[1])
        existing = {'codex', 'claude_code', 'hermes', 'openclaw'}
        self.assertEqual(set(BUILTIN_AGENTS), existing | set(brands))
        for agent, url in brands.items():
            with self.subTest(agent=agent):
                self.assertTrue(url.startswith('data:image/svg+xml,'))
                root = ET.fromstring(unquote(url.split(',', 1)[1]))
                self.assertEqual(root.tag, '{http://www.w3.org/2000/svg}svg')
                self.assertIn('viewBox', root.attrib)
                for element in root.iter():
                    self.assertNotIn(element.tag.rsplit('}', 1)[-1], ('script', 'foreignObject', 'image'))
                    self.assertFalse(any(key.lower().startswith('on') for key in element.attrib))

    def test_no_generic_circle_fallback(self):
        page = (Path(__file__).resolve().parents[1] / 'assets/maintenance.html').read_text()
        render = next(line for line in page.splitlines() if line.startswith('const agentIcon='))
        self.assertNotIn('<circle', render)
        self.assertIn('agent-initials', render)
        self.assertIn('title=', render)
