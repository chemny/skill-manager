import json
import unittest
from unittest.mock import patch
import test_maintenance as fixtures
import maintenance as core
import recovery
import task_scopes


class WebExecutionTests(unittest.TestCase):
    setUp = fixtures.MaintenanceTests.setUp
    tearDown = fixtures.MaintenanceTests.tearDown
    skill = fixtures.MaintenanceTests.skill
    row = fixtures.MaintenanceTests.row

    def execute(self, operation, **data):
        return core.dispatch(self.m, {'action':'execute','operation':operation,'confirmed':True,**data})

    def no_pending(self):
        self.assertFalse(self.m.db.execute("select id from maintenance_plans where state in ('preview','selection')").fetchone())

    def test_uninstall_executes_and_is_recoverable_without_pending(self):
        row = self.row('a'); before = core.manifest(self.roots/'a')
        result = self.execute('delete', target=row['id'])
        self.assertFalse((self.roots/'a').exists())
        self.assertEqual(result['state'], 'succeeded')
        bid = result['backups'][0]['id']
        self.assertTrue(self.m.verify_backup(bid)['valid'])
        self.execute('restore', id=bid)
        self.assertEqual(core.manifest(self.roots/'a'), before)
        self.no_pending()

    def test_backup_recycling_and_restore_are_direct(self):
        bid = self.m.backup(self.row('a'))['id']
        self.execute('trash-backup', id=bid)
        self.assertTrue(next(b for b in self.m.backups(False)['backups'] if b['id']==bid)['trashed_backup'])
        self.execute('restore-backup', id=bid)
        self.assertEqual(next(b for b in self.m.backups(False)['backups'] if b['id']==bid)['type'], 'backup')
        self.no_pending()

    def test_install_selected_content_executes_without_another_approval(self):
        source = self.base/'incoming'; source.mkdir()
        (source/'SKILL.md').write_text('---\nname: incoming\ndescription: fixture\nversion: 1.0\n---\ncontent')
        selection = core.dispatch(self.m, {'action':'install-local-identify','path':str(source),'target_root':str(self.roots)})
        result = self.execute('install', id=selection['id'], remote_path=selection['candidates'][0]['remote_path'])
        self.assertEqual(result['state'], 'succeeded')
        self.assertTrue((self.roots/'incoming'/'SKILL.md').is_file())
        self.no_pending()

    def test_failed_execution_does_not_leave_an_approval_item(self):
        row = self.row('a')
        with patch.object(self.m, 'apply', side_effect=ValueError('fixture failure')):
            with self.assertRaisesRegex(ValueError, 'fixture failure'): self.execute('delete', target=row['id'])
        self.assertTrue((self.roots/'a'/'SKILL.md').is_file())
        self.no_pending()

    def test_batch_delete_skips_protected_and_cleans_staging(self):
        self.m.protect(self.row('b'), True)
        result = core.dispatch(self.m, {'action':'execute-library-many','operation':'delete',
                                       'ids':[self.row('a')['id'],self.row('b')['id']],'confirmed':True})
        self.assertEqual(result['completed'], 1)
        self.assertEqual(len(result['skipped']), 1)
        self.assertFalse((self.roots/'a').exists())
        self.assertTrue((self.roots/'b').exists())
        self.no_pending()

    def test_batch_update_accepts_newer_remote_with_local_edits_and_backup(self):
        row = self.row('a')
        self.m.bind_source(row, 'https://github.com/example/repo', path='a')
        (self.roots/'a'/'notes').write_text('local work')
        remote = self.base/'remote'; remote.mkdir()
        (remote/'SKILL.md').write_text('---\nname: a\nversion: 2.0\n---\nnew release')
        with patch.object(self.m, 'remote', return_value=(remote,self.m.source(row))):
            self.m.check(row)
            result = core.dispatch(self.m, {'action':'execute-library-many','operation':'update',
                                           'ids':[row['id']],'confirmed':True})
        self.assertEqual(result['completed'], 1)
        self.assertTrue(self.m.verify_backup(result['results'][0]['backups'][0]['id'])['valid'])
        self.assertEqual((self.roots/'a'/'notes').read_text(), 'local work')
        self.no_pending()

    def test_empty_trash_does_not_touch_retained_or_regular_backups(self):
        ids = [self.m.backup(self.row('a'))['id'] for _ in range(3)]
        for bid in ids[:2]: self.execute('trash-backup', id=bid)
        recovery.retain(self.m, ids[1], True)
        result = core.dispatch(self.m, {'action':'empty-trash','confirmed':True})
        self.assertEqual(result['completed'], 1)
        records = {b['id']:b for b in self.m.backups(False)['backups']}
        self.assertEqual(records[ids[0]]['state'], 'purged')
        self.assertEqual(records[ids[1]]['state'], 'available')
        self.assertEqual(records[ids[2]]['type'], 'backup')
        self.no_pending()

    def test_explicit_request_and_scoped_write_are_required(self):
        row = self.row('a')
        with self.assertRaises(ValueError): core.dispatch(self.m, {'action':'execute','operation':'delete','target':row['id']})
        scope = task_scopes.describe(self.a, {'action':'execute','operation':'delete','target':row['id']})
        self.assertTrue(scope['write']); self.assertFalse(scope['global'])
        self.assertEqual(scope['operation'], 'delete')
        self.assertIn(str(self.roots/'a'), scope['resources'])
        self.assertIn('execute', task_scopes.COMMITS)

    def test_batch_restore_runs_without_an_approval_item(self):
        bids = [self.execute('delete', target=self.row(name)['id'])['backups'][0]['id'] for name in ('a','b')]
        result = core.dispatch(self.m, {'action':'execute-restore-many','entries':[{'id':bid} for bid in bids],
                                       'confirmed':True})
        self.assertEqual(result['completed'], 2)
        self.assertTrue((self.roots/'a'/'SKILL.md').is_file())
        self.assertTrue((self.roots/'b'/'SKILL.md').is_file())
        self.no_pending()

    def test_sync_runs_directly_and_backs_up_overwritten_changes(self):
        row = self.row('a')
        result = self.execute('sync', target=row['id'], source=self.row('b')['id'], allow_local_changes=True)
        self.assertEqual(result['state'], 'succeeded')
        self.assertTrue(self.m.verify_backup(result['backups'][0]['id'])['valid'])
        self.assertEqual((self.roots/'a'/'SKILL.md').read_text(), (self.roots/'b'/'SKILL.md').read_text())
        self.no_pending()

    def test_uninstall_requires_unchanged_selected_references(self):
        row = self.row('a')
        expected = {row['id']:self.m.affected(self.roots/'a')}
        link = self.roots/'alias';link.symlink_to(self.roots/'a', target_is_directory=True)
        self.m.scan()
        with self.assertRaisesRegex(ValueError,'associations changed'):
            self.execute('delete', target=row['id'], expected_references=expected)
        self.assertTrue((self.roots/'a'/'SKILL.md').is_file())
        self.assertTrue(link.is_symlink())
        self.no_pending()

    def test_batch_uninstall_validates_all_scopes_before_deleting(self):
        a,b = self.row('a'),self.row('b')
        expected = {a['id']:self.m.affected(self.roots/'a'),b['id']:[]}
        with self.assertRaisesRegex(ValueError,'associations changed'):
            core.dispatch(self.m, {'action':'execute-library-many','operation':'delete','ids':[a['id'],b['id']],
                                   'expected_references':expected,'confirmed':True})
        self.assertTrue((self.roots/'a'/'SKILL.md').is_file())
        self.assertTrue((self.roots/'b'/'SKILL.md').is_file())
        self.no_pending()

    def test_uninstall_selected_link_keeps_shared_content(self):
        link = self.roots/'alias';link.symlink_to(self.roots/'a', target_is_directory=True)
        self.m.scan();row = self.m.select(str(link))
        result = self.execute('delete', target=row['id'], expected_references={row['id']:self.m.affected(link)})
        self.assertEqual(result['state'],'succeeded')
        self.assertFalse(link.exists())
        self.assertTrue((self.roots/'a'/'SKILL.md').is_file())
        self.no_pending()

    def test_reference_change_during_staging_is_not_implicitly_authorized(self):
        row=self.row('a');expected={row['id']:self.m.affected(self.roots/'a')}
        original=self.m.plan
        def changed(*args,**kwargs):
            (self.roots/'late-alias').symlink_to(self.roots/'a',target_is_directory=True)
            self.m.scan()
            return original(*args,**kwargs)
        with patch.object(self.m,'plan',changed):
            with self.assertRaisesRegex(ValueError,'associations changed'):
                self.execute('delete',target=row['id'],expected_references=expected)
        self.assertTrue((self.roots/'a'/'SKILL.md').is_file())
        self.no_pending()

    def shared_root(self):
        alias=self.base/'claude-skills';alias.symlink_to(self.roots,target_is_directory=True)
        config=self.a.load_config()
        config['platforms']['codex']={'enabled':True,'discover_defaults':False,'roots':[str(self.roots)]}
        config['platforms']['claude_code']={'enabled':True,'discover_defaults':False,'roots':[str(alias)]}
        self.a.save_config(config);self.m.scan()
        return alias

    def test_shared_root_requires_real_target_and_preserves_root_link(self):
        alias=self.shared_root();before=core.manifest(self.roots/'a')
        entry=self.m.select(str(alias/'a'),'claude_code')
        with self.assertRaisesRegex(ValueError,'shared symlink'):
            self.execute('delete',target=entry['id'],expected_references={entry['id']:self.m.affected(alias/'a')})
        row=self.m.select(str(self.roots/'a'),'codex')
        result=self.execute('delete',target=row['id'],expected_references={row['id']:self.m.affected(self.roots/'a')})
        self.assertEqual(result['state'],'succeeded')
        self.assertFalse((alias/'a').exists())
        self.assertTrue(alias.is_symlink())
        self.assertTrue((alias/'b'/'SKILL.md').is_file())
        bid=result['backups'][0]['id'];self.assertTrue(self.m.verify_backup(bid)['valid'])
        self.execute('restore',id=bid)
        self.assertEqual(core.manifest(alias/'a'),before)
        self.no_pending()

    def test_shared_root_real_target_respects_alias_protection(self):
        alias=self.shared_root()
        self.m.protect(self.m.select(str(alias/'a'),'claude_code'),True)
        row=self.m.select(str(self.roots/'a'),'codex')
        with self.assertRaisesRegex(ValueError,'protected'):
            self.execute('delete',target=row['id'],expected_references={row['id']:self.m.affected(self.roots/'a')})
        self.assertTrue((alias/'a'/'SKILL.md').is_file())
        self.no_pending()

    def test_shared_uninstall_refreshes_link_health_without_rescanning(self):
        self.shared_root()
        link=self.roots/'alias';link.symlink_to(self.roots/'a',target_is_directory=True)
        self.m.scan();entry=self.m.select(str(link),'shared');row=self.m.select(str(self.roots/'a'),'codex')
        result=self.execute('delete',target=row['id'],expected_references={row['id']:self.m.affected(self.roots/'a')})
        refreshed={r['id']:r for r in self.m.inventory(ids=[row['id']])['items']}
        self.assertTrue(refreshed[entry['id']]['present'])
        self.assertEqual(refreshed[entry['id']]['health'],'broken-link')
        self.execute('restore',id=result['backups'][0]['id'])
        refreshed={r['id']:r for r in self.m.inventory(ids=[row['id']])['items']}
        self.assertEqual(refreshed[entry['id']]['health'],'ok')
        self.assertTrue((link/'SKILL.md').is_file())
        self.no_pending()
