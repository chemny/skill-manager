import unittest
import test_maintenance as fixtures
import library_actions
from skill_locations import add_preset_agent, agent_catalog, locations


class AgentManagementTests(unittest.TestCase):
    setUp = fixtures.MaintenanceTests.setUp
    tearDown = fixtures.MaintenanceTests.tearDown
    skill = fixtures.MaintenanceTests.skill
    row = fixtures.MaintenanceTests.row

    def test_remove_preserves_files_history_and_other_consumers(self):
        self.a.update_source('add', 'codex', str(self.roots))
        self.a.update_source('add', 'claude_code', str(self.roots))
        self.m.scan()
        before = (self.roots/'a/SKILL.md').read_bytes()
        ids = {r['id'] for r in self.m.r.all_capabilities()}
        self.m.inventory()
        self.a.update_source('remove-agent', 'codex', '')
        self.assertEqual((self.roots/'a/SKILL.md').read_bytes(), before)
        self.assertEqual({r['id'] for r in self.m.r.all_capabilities()}, ids)
        cached = self.m.cached_inventory()
        self.assertTrue(cached['cache']['available'])
        self.assertFalse(any(i['platform']=='codex' for i in cached['items']))
        roots, _ = locations(self.a.load_config())
        self.assertFalse(any(r['platform']=='codex' for r in roots))
        items = self.m.inventory()['items']
        self.assertFalse(any(i['platform']=='codex' for i in items))
        self.assertTrue(all('claude_code' in i['known_agents'] for i in items))
        self.assertTrue(all('codex' not in i['known_agents'] for i in items))
        self.assertFalse(next(a for a in agent_catalog(self.a.load_config()) if a['id']=='codex')['added'])

    def test_readd_uses_presets_and_enables_scan(self):
        self.a.update_source('remove-agent', 'codex', '')
        add_preset_agent(self.a, 'codex')
        settings = self.a.load_config()['platforms']['codex']
        self.assertTrue(settings['enabled'])
        self.assertFalse(settings['removed'])
        self.assertTrue(settings['roots'])
        self.assertTrue(settings['discover_defaults'])

    def test_directory_adoption_associates_selected_agent(self):
        self.a.update_source('add', 'codex', str(self.roots))
        root = self.base/'external'; root.mkdir()
        skill = root/'new'; skill.mkdir(); (skill/'SKILL.md').write_text('---\nname: new\n---\nexternal')
        value = library_actions.preview(self.m, str(root))
        request = {'action':'directory-add','path':str(root),'fingerprint':value['fingerprint'],'confirmed':True,'platform':'codex'}
        result = library_actions.dispatch(self.m, request)
        self.assertEqual(result['platform'], 'codex')
        self.assertIn(str(root), self.a.load_config()['platforms']['codex']['roots'])
        self.assertNotIn(str(root), self.a.load_config()['platforms']['shared']['roots'])
        item = next(i for i in self.m.inventory()['items'] if i['name']=='new')
        self.assertIn('codex', item['known_agents'])
        self.assertTrue((skill/'SKILL.md').is_file())

    def test_unknown_agent_rejected_before_config_change(self):
        value = library_actions.preview(self.m, str(self.roots))
        config = self.a.load_config()
        with self.assertRaises(ValueError):
            library_actions.dispatch(self.m, {'action':'directory-add','path':str(self.roots),'fingerprint':value['fingerprint'],'confirmed':True,'platform':'missing'})
        self.assertEqual(self.a.load_config(), config)

    def test_custom_directory_cannot_be_removed_as_agent(self):
        with self.assertRaises(ValueError):
            self.a.update_source('remove-agent', 'shared', '')
