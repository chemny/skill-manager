import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_maintenance as fixtures
from component_ownership import Ownership, CODEX_DISTRIBUTIONS
from skill_locations import default_agent_roots, BUILTIN_AGENTS


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {'HOME':str(self.home)}, clear=True)
        self.env.start()
        self.commands = patch('component_ownership.shutil.which', return_value=None)
        self.commands.start()

    def tearDown(self):
        self.commands.stop()
        self.env.stop()
        self.tmp.cleanup()

    def skill(self, path):
        path.mkdir(parents=True)
        (path / 'SKILL.md').write_text('---\nname: skill-creator\nbuiltin: true\n---\nExample')
        return path

    def test_codex_distribution_and_same_name_user_copy(self):
        system = self.home / '.codex/skills/.system'
        self.skill(system / 'skill-creator')
        (system / '.codex-system-skills.marker').write_text(next(iter(CODEX_DISTRIBUTIONS)))
        policy = Ownership()
        item = policy.classify(system / 'skill-creator')
        self.assertEqual(item['kind'], 'system')
        self.assertTrue(item['read_only'])
        self.assertTrue(item['version'])
        user = self.skill(self.home / '.agents/skills/skill-creator')
        self.assertEqual(policy.classify(user)['kind'], 'user')
        self.assertFalse(policy.classify(user)['read_only'])

    def test_unmatched_distribution_is_not_claimed_builtin(self):
        system = self.home / '.codex/skills/.system'
        self.skill(system / 'skill-creator')
        (system / '.codex-system-skills.marker').write_text('unverified-release')
        item = Ownership().classify(system / 'skill-creator')
        self.assertEqual(item['kind'], 'package_unverified')
        self.assertTrue(item['read_only'])

    def test_directory_names_and_self_claims_are_not_ownership(self):
        policy = Ownership()
        for relative in ('repo/.codex/plugins/cache/my-skill', '.hermes/hermes-agent/skills/custom',
                         '.agents/skills/skill-creator', '.claude/skills/.system/custom'):
            self.assertEqual(policy.classify(self.skill(self.home / relative))['kind'], 'user')

    def test_plugin_manifest_and_native_install_version(self):
        root = self.home / '.claude/plugins/cache/vendor/plugin/1.2.0'
        skill = self.skill(root / 'skills/my-skill')
        manifest = root / '.claude-plugin/plugin.json'
        manifest.parent.mkdir()
        manifest.write_text(json.dumps({'name':'plugin', 'version':'1.2.0'}))
        registry = self.home / '.claude/plugins/installed_plugins.json'
        registry.write_text(json.dumps({'plugins':{'plugin@vendor':[{'installPath':str(root), 'version':'1.2.0'}]}}))
        item = Ownership().classify(skill)
        self.assertEqual(item['kind'], 'plugin')
        self.assertEqual(item['component_id'], 'plugin@vendor')
        self.assertTrue(item['installed'])
        self.assertTrue(item['read_only'])
        registry.write_text(json.dumps({'plugins':{'plugin@vendor':[{'installPath':str(root), 'version':'2.0.0'}]}}))
        self.assertFalse(Ownership().classify(skill)['installed'])
        self.assertEqual(Ownership().classify(self.skill(self.home / '.agents/skills/plugin'))['kind'], 'user')

    def test_codex_plugin_is_not_system_and_cache_without_manifest_unknown(self):
        root = self.home / '.codex/plugins/cache/official/plugin/1.0'
        skill = self.skill(root / 'skills/example')
        self.assertEqual(Ownership().classify(skill)['kind'], 'package_unverified')
        (root / '.codex-plugin').mkdir()
        (root / '.codex-plugin/plugin.json').write_text(json.dumps({'name':'example', 'version':'1.0'}))
        self.assertEqual(Ownership().classify(skill)['kind'], 'plugin')
        self.assertIsNone(Ownership().classify(skill)['installed'])

    def test_user_link_can_be_removed_without_deleting_plugin(self):
        target = self.skill(self.home / '.codex/plugins/cache/vendor/plugin/1.0/skills/example')
        link = self.home / 'example'
        link.symlink_to(target, target_is_directory=True)
        self.assertFalse(Ownership().classify(link)['read_only'])
        self.assertTrue(Ownership().classify(target)['read_only'])

    def test_native_extension_is_not_a_system_skill(self):
        for agent, folder, manifest in (('gemini', '.gemini', 'gemini-extension.json'),
                                        ('qwen', '.qwen', 'qwen-extension.json')):
            root = self.home / folder / 'extensions/example'
            target = self.skill(root / 'skills/review')
            (root / manifest).write_text(json.dumps({'name':'example', 'version':'2.1.0'}))
            item = Ownership().classify(target)
            self.assertEqual((item['kind'], item['agent']), ('plugin', agent))
            self.assertEqual(item['version'], '2.1.0')
            self.assertTrue(item['read_only'])

    def test_executable_bound_package_and_user_copy(self):
        package = self.home / 'npm/node_modules/@qwen-code/qwen-code'
        skill = self.skill(package / 'packages/core/src/skills/bundled/review')
        executable = package / 'bin/qwen.js'
        executable.parent.mkdir()
        executable.write_text('')
        (package / 'package.json').write_text(json.dumps({'name':'@qwen-code/qwen-code', 'version':'1.2.3'}))
        with patch('component_ownership.shutil.which', side_effect=lambda cmd: str(executable) if cmd=='qwen' else None):
            item = Ownership().classify(skill)
        self.assertEqual((item['kind'], item['version']), ('system', '1.2.3'))
        self.assertEqual(Ownership().classify(skill)['kind'], 'package_unverified')

    def test_malformed_native_metadata_does_not_break_inventory(self):
        root = self.home / '.claude/plugins/cache/vendor/plugin/1.0'
        target = self.skill(root / 'skills/review')
        (root / '.claude-plugin').mkdir()
        (root / '.claude-plugin/plugin.json').write_text(json.dumps({'name':'plugin'}))
        (self.home / '.claude/plugins/installed_plugins.json').write_text(json.dumps({'plugins':{'plugin@vendor':[{'installPath':[]}, {'installPath':str(root)}]}}))
        self.assertFalse(Ownership().classify(target)['installed'])
        (root / 'package.json').write_text(json.dumps({'name':[]}))
        self.assertEqual(Ownership().classify(target)['kind'], 'plugin')
        (self.home / 'package.json').write_text(json.dumps({'name':[]}))
        user = self.skill(self.home / 'user-review')
        self.assertEqual(Ownership().classify(user)['kind'], 'user')


class PresetTests(unittest.TestCase):
    def test_nested_paths_and_windows(self):
        env = {'HOME':'/fixture-home'}
        self.assertEqual(default_agent_roots('pi', 'darwin', env), ['/fixture-home/.pi/agent/skills'])
        self.assertEqual(default_agent_roots('opencode', 'darwin', env), ['/fixture-home/.config/opencode/skills'])
        self.assertEqual(default_agent_roots('opencode', 'win32', {'USERPROFILE':r'C:\fixture-home', 'XDG_CONFIG_HOME':r'D:\Config'}), [r'D:\Config\opencode\skills'])
        self.assertEqual(default_agent_roots('pi', 'win32', {'USERPROFILE':r'C:\fixture-home', 'PI_CODING_AGENT_DIR':r'D:\Pi'}), [r'D:\Pi\skills'])
        for key in BUILTIN_AGENTS:
            self.assertTrue(default_agent_roots(key, 'darwin', env)[0].startswith('/fixture-home/'))
            self.assertTrue(default_agent_roots(key, 'win32', {'USERPROFILE':r'C:\fixture-home'})[0].startswith('C:\\fixture-home\\'))
        self.assertNotIn('deepseek', BUILTIN_AGENTS)


class MutationTests(unittest.TestCase):
    setUp = fixtures.MaintenanceTests.setUp
    tearDown = fixtures.MaintenanceTests.tearDown
    skill = fixtures.MaintenanceTests.skill
    row = fixtures.MaintenanceTests.row

    def test_metadata_and_cached_builtin_do_not_block_uninstall(self):
        skill = self.roots / 'a'
        (skill / 'SKILL.md').write_text('---\nname: a\nbuiltin: true\ntype: builtin\n---\nUser Skill')
        self.m.scan()
        self.assertEqual(self.row('a')['source_type'], 'local')
        self.m.db.execute("update capabilities set source_type='builtin' where id=?", (self.row('a')['id'],))
        self.m.db.commit()
        item = next(i for i in self.m.inventory()['items'] if i['name']=='a')
        self.assertEqual(item['source_type'], 'local')
        self.assertEqual(item['source_readiness'], 'source_unknown')
        value = self.m.apply(self.m.plan('delete', self.row('a'))['id'])
        self.assertFalse(skill.exists())
        self.assertTrue(value['backups'])

    def test_guard_without_item_blocks_unresolved_cache(self):
        with patch.dict(os.environ, {'CODEX_HOME':str(self.base / 'codex')}):
            root = self.base / 'codex/plugins/cache/vendor/example/1.0'
            target = root / 'skills/example'
            target.mkdir(parents=True)
            (target / 'SKILL.md').write_text('---\nname: example\n---\nPlugin')
            self.a.update_source('add', 'codex', str(root / 'skills'))
            self.m.scan()
            with self.assertRaisesRegex(ValueError, '缓存'):
                self.m.validate_target(target)
            with self.assertRaises(ValueError):
                self.m.plan('delete', self.row(str(target)))
            self.assertTrue((target / 'SKILL.md').is_file())
