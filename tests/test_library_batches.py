import unittest
from pathlib import Path
from unittest.mock import patch
import test_maintenance as fixtures
import maintenance as core
import library_batches as batches

class LibraryBatchTests(unittest.TestCase):
    setUp=fixtures.MaintenanceTests.setUp
    tearDown=fixtures.MaintenanceTests.tearDown
    skill=fixtures.MaintenanceTests.skill
    row=fixtures.MaintenanceTests.row

    def test_batch_uninstall_preview_and_recoverable_apply(self):
        v=batches.preview(self.m,'delete',[self.row('a')['id'],self.row('b')['id']])
        self.assertEqual(len(v['plans']),2);self.assertTrue((self.roots/'a').exists())
        with self.assertRaisesRegex(ValueError,'Confirmation'):batches.apply(self.m,[p['id'] for p in v['plans']],'delete',False)
        r=batches.apply(self.m,[p['id'] for p in v['plans']],'delete',True)
        self.assertEqual(r['completed'],2);self.assertFalse((self.roots/'a').exists())
        backups=self.m.backups()['backups'];self.assertEqual(len(backups),2)
        self.assertTrue(all(b['type']=='quarantine' and b['verified'] for b in backups))
        self.m.apply(self.m.plan('restore',backup_id=backups[0]['id'])['id'])
        self.assertTrue(Path(backups[0]['original_path']).exists())

    def test_protected_items_are_explained_and_not_uninstalled(self):
        self.m.protect(self.row('b'),True)
        v=batches.preview(self.m,'delete',[self.row('a')['id'],self.row('b')['id']])
        self.assertEqual(len(v['plans']),1);self.assertIn('受保护',v['skipped'][0]['reason'])
        batches.apply(self.m,[p['id'] for p in v['plans']],'delete',True)
        self.assertTrue((self.roots/'b'/'SKILL.md').is_file())

    def test_preflight_checks_all_targets_before_first_write(self):
        v=batches.preview(self.m,'delete',[self.row('a')['id'],self.row('b')['id']])
        (self.roots/'b'/'SKILL.md').write_text('changed')
        with self.assertRaisesRegex(ValueError,'Target changed'):batches.apply(self.m,[p['id'] for p in v['plans']],'delete',True)
        self.assertTrue((self.roots/'a').is_dir());self.assertFalse(self.m.backups()['backups'])

    def test_protection_change_blocks_entire_batch(self):
        v=batches.preview(self.m,'delete',[self.row('a')['id'],self.row('b')['id']])
        self.m.protect(self.row('b'),True)
        with self.assertRaisesRegex(ValueError,'protected'):batches.apply(self.m,[p['id'] for p in v['plans']],'delete',True)
        self.assertTrue((self.roots/'a').is_dir())

    def test_failure_stops_and_reports_completed_and_remaining(self):
        v=batches.preview(self.m,'delete',[self.row('a')['id'],self.row('b')['id']]);apply=self.m.apply
        def failing(pid):
            if pid==v['plans'][1]['id']:raise ValueError('temporary failure')
            return apply(pid)
        with patch.object(self.m,'apply',side_effect=failing):r=batches.apply(self.m,[p['id'] for p in v['plans']],'delete',True)
        self.assertEqual(r['state'],'partial');self.assertEqual(r['completed'],1)
        self.assertEqual(r['remaining'],[v['plans'][1]['id']]);self.assertTrue((self.roots/'b').is_dir())

    def test_duplicate_ids_and_foreign_plan_actions(self):
        v=batches.preview(self.m,'delete',[self.row('a')['id']]*2)
        self.assertEqual(len(v['plans']),1)
        with self.assertRaisesRegex(ValueError,'失效'):batches.apply(self.m,[v['plans'][0]['id']],'update',True)
        self.assertTrue((self.roots/'a').is_dir())

    def test_update_requires_checked_update_state(self):
        v=batches.preview(self.m,'update',[self.row('a')['id']])
        self.assertEqual(v['plans'],[]);self.assertIn('先检查更新',v['skipped'][0]['reason'])

    def test_alias_uninstall_does_not_remove_shared_content(self):
        alias=self.roots/'alias';alias.symlink_to(self.roots/'a',target_is_directory=True);self.m.scan()
        v=batches.preview(self.m,'delete',[self.m.select(str(alias))['id']])
        batches.apply(self.m,[p['id'] for p in v['plans']],'delete',True)
        self.assertFalse(core.exists(alias));self.assertTrue((self.roots/'a'/'SKILL.md').is_file())

    def test_cancel_many_discards_only_unapplied_previews(self):
        v=batches.preview(self.m,'delete',[self.row('a')['id'],self.row('b')['id']])
        self.m.apply(v['plans'][0]['id'])
        r=batches.dispatch(self.m,{'action':'discard-previews','ids':[p['id'] for p in v['plans']]})
        self.assertEqual(r['discarded'],[v['plans'][1]['id']]);self.assertEqual(r['skipped'],[v['plans'][0]['id']])
        self.assertEqual(self.m.plans()['plans'],[]);self.assertTrue((self.roots/'b').is_dir())

    def test_batch_update_uses_staged_content_and_creates_backups(self):
        import json
        for name in ('a','b'):
            row=self.row(name);self.m.bind_source(row,'https://github.com/example/skills','main',name)
            files=core.manifest(self.roots/name)
            self.m.db.execute('update maintenance_items set baseline=?,last_check=? where id=?',(json.dumps(files),json.dumps({'status':'update_available','local_fingerprint':core.fingerprint(files)}),row['id']))
        self.m.db.commit()
        remote=self.base/'remote';remote.mkdir()
        (remote/'SKILL.md').write_text('---\nname: updated\nversion: 2.0\n---\nnew content')
        with patch.object(self.m,'remote',return_value=(remote,{'provider':'github','url':'https://github.com/example/skills','path':'','ref':'main','commit':'fixed'})):
            v=batches.preview(self.m,'update',[self.row('a')['id'],self.row('b')['id']])
        self.assertEqual(len(v['plans']),2)
        (remote/'SKILL.md').write_text('changed after preview')
        r=batches.apply(self.m,[p['id'] for p in v['plans']],'update',True)
        self.assertEqual(r['completed'],2)
        self.assertIn('new content',(self.roots/'a'/'SKILL.md').read_text())
        self.assertNotIn('changed after preview',(self.roots/'b'/'SKILL.md').read_text())
        self.assertEqual(len(self.m.backups()['backups']),2)
