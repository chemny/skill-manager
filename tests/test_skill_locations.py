import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from skill_locations import locations, hermes_locations
import test_maintenance as fixtures


class LocationTests(unittest.TestCase):
    def test_default_conventions_and_disabled_agent(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'HOME':tmp}, clear=True):
            config={'platforms':{'codex':{'roots':['~/.codex/skills']},
                                 'claude_code':{'roots':['~/.claude/skills']},
                                 'hermes':{'roots':['~/.hermes/skills']},
                                 'openclaw':{'roots':['~/.openclaw/skills'],'enabled':False}}}
            roots,warnings=locations(config)
            agents=[r['platform'] for r in roots if r['expanded']==str(Path(tmp)/'.agents/skills')]
            self.assertEqual(agents,['codex','openclaw'])
            self.assertFalse(next(r for r in roots if r['platform']=='openclaw')['enabled'])
            self.assertFalse((Path(tmp)/'.agents').exists())
            self.assertFalse(warnings)

    def test_explicit_roots_do_not_gain_default_consumers(self):
        roots,_=locations({'platforms':{'codex':{'roots':['/custom/skills']},
                                     'hermes':{'roots':[]}}})
        self.assertEqual([r['expanded'] for r in roots],['/custom/skills'])

    def test_common_folder_does_not_imply_every_agent_is_installed(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'HOME':tmp}, clear=True), patch('skill_locations.shutil.which', return_value=None):
            (Path(tmp)/'.agents/skills').mkdir(parents=True)
            roots,_=locations({'platforms':{'openclaw':{'roots':['~/.openclaw/skills']}}})
            common=next(r for r in roots if r['expanded'].endswith('/.agents/skills'))
            self.assertFalse(common['enabled']);self.assertFalse(common['agents'])

    def test_environment_overrides_and_openclaw_isolation(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'CODEX_HOME':tmp+'/codex','CLAUDE_CONFIG_DIR':tmp+'/claude','HERMES_HOME':tmp+'/hermes','OPENCLAW_STATE_DIR':tmp+'/claw'}, clear=True):
            roots,_=locations({'platforms':{a:{'roots':['~/.'+n+'/skills']} for a,n in [('codex','codex'),('claude_code','claude'),('hermes','hermes'),('openclaw','openclaw')]}})
            paths={r['expanded'] for r in roots}
            for name in ('codex','claude','hermes','claw'):
                self.assertIn(tmp+'/'+name+'/skills',paths)
            self.assertFalse(any(r['platform']=='openclaw' and r['expanded'].endswith('/.agents/skills') for r in roots))

    def test_hermes_explicit_external_dirs_and_relative_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp)
            (home/'config.yaml').write_text('skills:\n  external_dirs:\n    - "${SKILLS_TEST_ROOT}/skills" # shared repo\n  create_dir: authored\nother:\n  secret: ignored\n')
            with patch.dict(os.environ,{'SKILLS_TEST_ROOT':tmp}):
                roots,warnings=hermes_locations(home)
            self.assertEqual([str(p) for p,_ in roots],[tmp+'/skills',tmp+'/authored'])
            self.assertFalse(warnings)
            (home/'config.yaml').write_text('skills:\n  external_dirs: *unknown_alias\n')
            roots,warnings=hermes_locations(home)
            self.assertFalse(roots);self.assertTrue(warnings)


class RelationshipTests(unittest.TestCase):
    setUp = fixtures.MaintenanceTests.setUp
    tearDown = fixtures.MaintenanceTests.tearDown
    skill = fixtures.MaintenanceTests.skill
    row = fixtures.MaintenanceTests.row
    def test_two_agents_share_one_storage_and_preserve_history(self):
        config=self.a.load_config()
        alias=self.base/'claude';alias.symlink_to(self.roots,target_is_directory=True)
        config['platforms']['codex']={'enabled':True,'roots':[str(self.roots)]}
        config['platforms']['claude_code']={'enabled':True,'roots':[str(alias)]}
        original_id=self.row(str(self.roots/'a'))['id']
        backup=self.m.backup(self.row(original_id))
        before=self.m.inventory()['space']['installed_unique']
        self.a.save_config(config);self.m.scan()
        inv=self.m.inventory();item=next(i for i in inv['items'] if i['id']==original_id)
        self.assertEqual(item['known_agents'],['claude_code','codex'])
        self.assertTrue(item['shared'])
        self.assertEqual(before,inv['space']['installed_unique'])
        self.assertEqual(item['backup_summary']['latest']['id'],backup['id'])
        self.assertEqual(len(inv['insights']['shared_targets']),2)
        root=next(r for r in self.m.install_roots()['roots'] if r['path']==str(self.roots))
        self.assertEqual(root['agents'],['claude_code','codex'])
        config['platforms']['claude_code']['enabled']=False
        self.a.save_config(config)
        item=next(i for i in self.m.inventory()['items'] if i['id']==original_id)
        self.assertEqual(item['known_agents'],['codex']);self.assertFalse(item['shared'])

    def test_same_name_independent_directories_are_not_shared(self):
        other=self.base/'other';other.mkdir();(other/'a').mkdir()
        (other/'a'/'SKILL.md').write_text('---\nname: a\n---\nSame name')
        config=self.a.load_config();config['platforms']['codex']={'enabled':True,'roots':[str(other)]}
        self.a.save_config(config);self.m.scan()
        inv=self.m.inventory()
        self.assertFalse(inv['shared_targets'])
        self.assertEqual(len({i['real_path'] for i in inv['items'] if i['name']=='a'}),2)

    def test_new_agent_identity_cannot_bypass_original_protection(self):
        original=self.row(str(self.roots/'a'));self.m.protect(original,True)
        config=self.a.load_config();config['platforms']['codex']={'enabled':True,'roots':[str(self.roots)]}
        self.a.save_config(config);self.m.scan()
        codex=self.m.select(str(self.roots/'a'),'codex')
        with self.assertRaisesRegex(ValueError,'protected'):
            self.m.plan('delete',codex)
        self.m.protect(original,False)
        plan=self.m.plan('delete',codex)
        self.m.protect(original,True)
        with self.assertRaisesRegex(ValueError,'protected'):
            self.m.apply(plan['id'])
        self.assertTrue((self.roots/'a'/'SKILL.md').is_file())
