"""Batch 3 behavioral and safety checks; all data is temporary."""
import datetime as dt
import contextlib
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import test_maintenance as fixtures
import cache_cleanup
import diagnostics
import library_actions
import maintenance as core
import recovery
import web_execution


class ManagementPolishTests(unittest.TestCase):
    setUp = fixtures.MaintenanceTests.setUp
    tearDown = fixtures.MaintenanceTests.tearDown
    skill = fixtures.MaintenanceTests.skill
    row = fixtures.MaintenanceTests.row

    def old_plan(self, state='preview'):
        plan = self.m.plan('sync',self.row('b'),self.row('a'),allow_changes=True)
        old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=25)
        plan['created_at'] = old.isoformat()
        self.m.db.execute('update maintenance_plans set created_at=?,state=?,payload=? where id=?',
                          (old.isoformat(),state,json.dumps(plan),plan['id']))
        self.m.db.commit()
        path = self.home / 'plans' / plan['id']
        for child in sorted(path.rglob('*'), key=lambda p:len(p.parts), reverse=True)+[path]:
            os.utime(child,(old.timestamp(),old.timestamp()))
        return plan, path

    def test_config_parse_and_schema_errors_preserve_original(self):
        for data in ('{bad','[]','null','{"platforms":[]}',
                     '{"platforms":{"shared":{"roots":"/tmp"}}}',
                     '{"platforms":{"shared":{"enabled":"false"}}}',
                     '{"scan_schema":true}','{"web":[]}'):
            with self.subTest(data=data):
                self.a.CONFIG_PATH.write_text(data)
                with self.assertRaisesRegex(ValueError,'原配置已保留'):self.a.load_config()
                with self.assertRaises(ValueError):self.a.save_config({'platforms':{}})
                self.assertEqual(self.a.CONFIG_PATH.read_text(),data)

    def test_config_atomic_replace_failure_preserves_original_and_cleans_temp(self):
        before=self.a.CONFIG_PATH.read_bytes()
        with patch.object(self.a.os,'replace',side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):self.a.save_config(self.a.load_config())
        self.assertEqual(self.a.CONFIG_PATH.read_bytes(),before)
        self.assertFalse(list(self.home.glob('.config-*.tmp')))

    def test_invalid_new_config_does_not_overwrite_valid_config(self):
        before=self.a.CONFIG_PATH.read_bytes()
        with self.assertRaises(ValueError):self.a.save_config({'platforms':{'shared':{'roots':[12]}}})
        self.assertEqual(self.a.CONFIG_PATH.read_bytes(),before)

    def test_config_symlink_is_not_replaced_or_written_through(self):
        actual=self.base/'config.json';self.a.CONFIG_PATH.replace(actual)
        self.a.CONFIG_PATH.symlink_to(actual)
        before=actual.read_bytes()
        with self.assertRaisesRegex(ValueError,'未覆盖原配置'):self.a.save_config(self.a.load_config())
        self.assertEqual(actual.read_bytes(),before);self.assertTrue(self.a.CONFIG_PATH.is_symlink())

    def test_missing_config_symlink_target_is_not_created(self):
        self.a.CONFIG_PATH.unlink();actual=self.base/'missing-config.json'
        self.a.CONFIG_PATH.symlink_to(actual)
        with self.assertRaisesRegex(ValueError,'未覆盖原配置'):self.a.load_config()
        self.assertFalse(actual.exists());self.assertTrue(self.a.CONFIG_PATH.is_symlink())

    def test_directory_leaf_rejected_before_registration(self):
        before=self.a.CONFIG_PATH.read_bytes()
        for action in ('directory-preview','directory-add'):
            with self.assertRaisesRegex(ValueError,'上一级 Skills 目录'):
                library_actions.dispatch(self.m,{'action':action,'path':str(self.roots/'a'),'confirmed':True})
        self.assertEqual(self.a.CONFIG_PATH.read_bytes(),before)
        self.assertEqual(diagnostics.classify(ValueError('当前选择的是单个 Skill，请选择它的上一级 Skills 目录'))[0],'directory_is_skill')

    def test_collection_directory_add_remains_manageable(self):
        root=self.base/'collection';(root/'example').mkdir(parents=True)
        (root/'example'/'SKILL.md').write_text('---\nname: example\n---\ntext')
        before=library_actions.preview(self.m,str(root))
        library_actions.dispatch(self.m,{'action':'directory-add','path':str(root),
                                         'fingerprint':before['fingerprint'],'confirmed':True})
        row=self.m.select(str(root/'example'))
        self.assertEqual(self.m.validate_target(row['path']),root/'example')

    def test_cache_clean_requires_confirmation_and_same_snapshot(self):
        plan,path=self.old_plan();info=cache_cleanup.inspect(self.m)
        self.assertEqual(info['count'],1)
        with self.assertRaisesRegex(ValueError,'Confirmation'):cache_cleanup.clean(self.m,info['fingerprint'])
        with self.assertRaisesRegex(ValueError,'已变化'):cache_cleanup.clean(self.m,'invalid',True)
        self.assertTrue(path.exists())
        result=cache_cleanup.clean(self.m,info['fingerprint'],True)
        self.assertEqual(result['completed'],1);self.assertFalse(path.exists())
        self.assertEqual(self.m.get_plan(plan['id'])['state'],'discarded')
        self.assertTrue((self.roots/'a'/'SKILL.md').is_file())
        self.assertTrue((self.roots/'b'/'SKILL.md').is_file())

    def test_cache_clean_keeps_fresh_failed_running_and_unregistered(self):
        paths=[]
        for state in ('failed','running'):paths.append(self.old_plan(state)[1])
        fresh=self.m.plan('delete',self.row('a'));paths.append(self.home/'plans'/fresh['id'])
        orphan=self.home/'plans'/'unregistered';orphan.mkdir();paths.append(orphan)
        self.assertEqual(cache_cleanup.inspect(self.m)['count'],0)
        for path in paths:self.assertTrue(path.exists())

    def test_cache_clean_keeps_recently_modified_old_plan(self):
        _,path=self.old_plan();(path/'new.txt').write_text('recent')
        self.assertEqual(cache_cleanup.inspect(self.m)['count'],0)

    def test_cache_clean_revalidates_changed_files(self):
        _,path=self.old_plan();info=cache_cleanup.inspect(self.m)
        (path/'new.txt').write_text('changed')
        with self.assertRaisesRegex(ValueError,'已变化'):cache_cleanup.clean(self.m,info['fingerprint'],True)
        self.assertTrue(path.exists())

    def test_cache_clean_skips_symlink_and_special_file(self):
        _,path=self.old_plan();outside=self.base/'outside';outside.write_text('preserve')
        link=path/'link';link.symlink_to(outside)
        old=dt.datetime.now().timestamp()-26*3600;os.utime(path,(old,old))
        self.assertEqual(cache_cleanup.inspect(self.m)['count'],0)
        link.unlink();os.mkfifo(path/'pipe');os.utime(path/'pipe',(old,old));os.utime(path,(old,old))
        self.assertEqual(cache_cleanup.inspect(self.m)['count'],0)
        self.assertEqual(outside.read_text(),'preserve')

    def test_cache_clean_rejects_linked_staging_root(self):
        _,path=self.old_plan();base=self.home/'plans';actual=self.home/'real-plans';base.rename(actual)
        base.symlink_to(actual,target_is_directory=True)
        with self.assertRaisesRegex(ValueError,'symlink'):cache_cleanup.inspect(self.m)
        self.assertTrue((actual/path.name).exists())

    def test_cache_clean_does_not_touch_registered_live_skill(self):
        plan,path=self.old_plan()
        config=self.a.load_config();config['platforms']['shared']['roots'].append(str(path))
        self.a.save_config(config)
        self.assertEqual(cache_cleanup.inspect(self.m)['count'],0)
        self.assertTrue(path.exists())

    def test_cache_clean_preserves_interrupted_operation_staging(self):
        plan,path=self.old_plan()
        self.m.operation('running-op','sync','running',{'plan_id':plan['id']})
        self.assertEqual(cache_cleanup.inspect(self.m)['count'],0)
        self.m.db.execute("update maintenance_operations set payload='{' where id='running-op'");self.m.db.commit()
        self.assertTrue(cache_cleanup.inspect(self.m)['blocked']);self.assertTrue(path.exists())

    def test_cache_clean_preserves_failed_operation_even_if_plan_state_is_preview(self):
        plan,path=self.old_plan()
        self.m.operation('failed-op','sync','failed',{'plan_id':plan['id']})
        self.assertEqual(cache_cleanup.inspect(self.m)['count'],0)
        self.assertTrue(path.exists())

    def test_cache_failure_is_partial_and_keeps_backup_and_quarantine(self):
        _,path=self.old_plan();bid=self.m.backup(self.row('a'))['id']
        container,_=self.m.backup_record(bid);info=cache_cleanup.inspect(self.m)
        with patch.object(cache_cleanup.shutil,'rmtree',side_effect=PermissionError('denied')):
            value=cache_cleanup.clean(self.m,info['fingerprint'],True)
        self.assertEqual(value['state'],'partial');self.assertEqual(value['completed'],0)
        self.assertTrue(path.exists());self.assertTrue(container.exists())

    def test_cache_browse_is_read_only_and_does_not_refresh_inventory(self):
        self.old_plan();self.m.inventory()
        with patch.object(core.Manager,'__init__',side_effect=AssertionError('writer created')):
            value=core.api_dispatch(self.a,{'action':'cache-inspect'})
        self.assertEqual(value['count'],1)
        with patch.object(self.m,'inventory',side_effect=AssertionError('full refresh')):
            cleaned=cache_cleanup.clean(self.m,value['fingerprint'],True)
        self.assertEqual(self.m.cached_inventory()['space']['staged_plans'],cleaned['staged_space'])

    def test_cache_cli_uses_same_inspection_and_requires_yes(self):
        _,path=self.old_plan();output=io.StringIO()
        with contextlib.redirect_stdout(output):self.a.main(['cache-inspect'])
        value=json.loads(output.getvalue());self.assertEqual(value['count'],1)
        with contextlib.redirect_stdout(io.StringIO()),self.assertRaises(SystemExit):
            self.a.main(['cache-clean','--fingerprint',value['fingerprint']])
        self.assertTrue(path.exists())
        with contextlib.redirect_stdout(io.StringIO()):
            self.a.main(['cache-clean','--fingerprint',value['fingerprint'],'--yes'])
        self.assertFalse(path.exists())

    def test_native_backup_space_moves_with_recycle_marker(self):
        bid=self.m.backup(self.row('a'))['id'];before=recovery.stored_space(self.m)
        self.m.apply(self.m.plan('trash-backup',backup_id=bid)['id'])
        after=recovery.stored_space(self.m)
        self.assertEqual(after['backups']['bytes'],0)
        self.assertEqual(after['quarantine']['bytes'],before['backups']['bytes'])
        self.assertEqual(self.m.inventory()['space']['quarantine'],after['quarantine'])
        self.m.apply(self.m.plan('restore-backup',backup_id=bid)['id'])
        self.assertEqual(recovery.stored_space(self.m),before)

    def test_uninstall_reference_read_includes_removed_agent_and_protection(self):
        config=self.a.load_config();config['platforms']['codex'].update(enabled=True,roots=[str(self.roots)],discover_defaults=False)
        self.a.save_config(config);self.m.scan()
        shared=self.m.select(str(self.roots/'a'),'shared');codex=self.m.select(str(self.roots/'a'),'codex')
        self.m.protect(codex,True)
        config=self.a.load_config();config['platforms']['codex']['removed']=True;self.a.save_config(config)
        with patch.object(core.Manager,'__init__',side_effect=AssertionError('writer created')):
            refs=core.api_dispatch(self.a,{'action':'uninstall-references','ids':[shared['id']]})['references'][shared['id']]
        self.assertEqual({r['platform'] for r in refs},{'shared','codex'})
        self.assertTrue(next(r for r in refs if r['platform']=='codex')['protected'])
        expected={shared['id']:[{k:r[k] for k in ('id','platform','path')} for r in refs]}
        web_execution.check_uninstall_scope(self.m,{'expected_references':expected},[shared['id']])
        with self.assertRaises(ValueError):self.m.plan('delete',shared)

    def test_reference_batch_matches_writer_for_directories_links_and_root_aliases(self):
        link=self.roots/'ref';link.symlink_to(self.roots/'a',target_is_directory=True)
        alias=self.base/'alias';alias.symlink_to(self.roots,target_is_directory=True)
        config=self.a.load_config();config['platforms']['codex'].update(enabled=True,roots=[str(alias)],discover_defaults=False)
        self.a.save_config(config);self.m.scan()
        rows=[self.m.select(str(path),platform) for path,platform in
              [(self.roots/'a','shared'),(link,'shared'),(alias/'a','codex')]]
        expected={r['id']:self.m.affected(Path(r['path'])) for r in rows}
        with patch.object(self.m,'affected',side_effect=AssertionError('repeated full reference scan')):
            result=web_execution.uninstall_references(self.m,[r['id'] for r in rows])
        for key,refs in result['references'].items():
            self.assertEqual([{field:r[field] for field in ('id','platform','path')} for r in refs],expected[key])


if __name__=='__main__':unittest.main()
