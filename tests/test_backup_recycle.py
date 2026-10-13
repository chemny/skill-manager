import json
import sqlite3
import unittest
from unittest.mock import patch
import test_maintenance as fixtures
import maintenance as core
import recovery
import backup_recycle


class BackupRecycleTests(unittest.TestCase):
    setUp = fixtures.MaintenanceTests.setUp
    tearDown = fixtures.MaintenanceTests.tearDown
    skill = fixtures.MaintenanceTests.skill
    row = fixtures.MaintenanceTests.row

    def record(self, bid):
        return next(b for b in self.m.backups(False)['backups'] if b['id']==bid)

    def apply(self, action, bid):
        plan = core.dispatch(self.m, {'action':'preview','operation':action,'id':bid})
        return core.dispatch(self.m, {'action':'apply','id':plan['id'],'confirmed':True})

    def test_native_backup_round_trip_preserves_files_and_installed_skill(self):
        bid = self.m.backup(self.row('a'))['id']
        path, _ = self.m.backup_record(bid)
        files, installed = core.manifest(path), core.manifest(self.roots/'a')
        result = self.apply('trash-backup', bid)
        self.assertEqual(result['destination_view'], 'trash')
        self.assertTrue(self.record(bid)['trashed_backup'])
        self.assertEqual(self.record(bid)['type'], 'quarantine')
        self.assertTrue(self.m.verify_backup(bid)['valid'])
        self.assertEqual(core.manifest(path), files)
        self.apply('restore-backup', bid)
        self.assertEqual(self.record(bid)['type'], 'backup')
        self.assertEqual(core.manifest(path), files)
        self.assertEqual(core.manifest(self.roots/'a'), installed)

    def test_historical_backup_stays_recycled_after_rediscovery(self):
        root = self.base/'old-backups'; root.mkdir()
        core.copy_path(self.roots/'a', root/'a')
        with patch.object(recovery, 'locations', return_value=[root]):
            recovery.discover(self.m)
            bid = next(b['id'] for b in self.m.backups(False)['backups'] if b['external'])
            self.apply('trash-backup', bid)
            result = recovery.discover(self.m)
            self.assertTrue(self.record(bid)['trashed_backup'])
            self.assertEqual(result['summary']['in_trash'], 1)
            self.assertTrue((root/'a'/'SKILL.md').is_file())
            self.apply('restore-backup', bid)
            self.assertEqual(self.record(bid)['type'], 'backup')

    def test_retained_or_in_use_backups_cannot_be_recycled(self):
        bid = self.m.backup(self.row('a'))['id']
        recovery.retain(self.m, bid, True)
        with self.assertRaisesRegex(ValueError, 'retain'): self.m.plan('trash-backup', backup_id=bid)
        recovery.retain(self.m, bid, False)
        plan = self.m.plan('restore', backup_id=bid, allow_changes=True)
        with self.assertRaisesRegex(ValueError, 'pending'): self.m.plan('trash-backup', backup_id=bid)
        self.m.discard_plan(plan['id'])
        self.apply('trash-backup', bid)
        recovery.retain(self.m, bid, True)
        self.apply('restore-backup', bid)
        self.assertTrue(self.record(bid)['retained'])

    def test_files_and_recycle_state_rechecked_at_apply(self):
        bid = self.m.backup(self.row('a'))['id']
        plan = self.m.plan('trash-backup', backup_id=bid)
        path, _ = self.m.backup_record(bid)
        (path/'content'/'notes').write_text('changed after preview')
        with self.assertRaisesRegex(ValueError, 'changed'): self.m.apply(plan['id'])
        self.assertIsNone(backup_recycle.entry(self.m, bid))

    def test_recycle_and_history_are_one_transaction(self):
        bid = self.m.backup(self.row('a'))['id']
        plan = self.m.plan('trash-backup', backup_id=bid)
        self.m.db.execute("create trigger fail_recycle before insert on maintenance_operations when NEW.action='trash-backup' begin select raise(ABORT, 'test failure'); end")
        self.m.db.commit()
        with self.assertRaises(sqlite3.IntegrityError): self.m.apply(plan['id'])
        self.assertIsNone(backup_recycle.entry(self.m, bid))
        self.assertEqual(self.m.get_plan(plan['id'])['state'], 'preview')

    def test_batch_recycle_and_permanent_purge_are_separate(self):
        ids = [self.m.backup(self.row(name))['id'] for name in ('a','b')]
        plans = recovery.preview_purge(self.m, ids, 'trash-backup')
        self.assertFalse(plans['irreversible'])
        result = recovery.apply_purge(self.m, [p['id'] for p in plans['plans']], True)
        self.assertEqual(result['completed'], 2)
        for bid in ids: self.assertTrue(self.record(bid)['trashed_backup'])
        pid = self.m.plan('purge', backup_id=ids[0])['id']
        self.m.apply(pid)
        self.assertEqual(self.record(ids[0])['state'], 'purged')
        self.assertTrue(self.record(ids[1])['trashed_backup'])

    def test_cancel_preview_does_not_recycle(self):
        bid = self.m.backup(self.row('a'))['id']
        plan = self.m.plan('trash-backup', backup_id=bid)
        self.m.discard_plan(plan['id'])
        self.assertEqual(self.record(bid)['type'], 'backup')
        self.assertIsNone(backup_recycle.entry(self.m, bid))

    def test_old_web_delete_requests_also_recycle(self):
        bid = self.m.backup(self.row('a'))['id']
        preview = core.dispatch(self.m, {'action':'preview','operation':'purge','id':bid})
        self.assertEqual(preview['action'], 'trash-backup')
        self.m.discard_plan(preview['id'])
        batch = core.dispatch(self.m, {'action':'preview-purge-many','ids':[bid]})
        self.assertEqual(batch['plans'][0]['action'], 'trash-backup')
        self.m.discard_plan(batch['plans'][0]['id'])
        old = self.m.plan('purge', backup_id=bid)
        for request in ({'action':'apply','id':old['id'],'confirmed':True},
                        {'action':'apply-purge-many','ids':[old['id']],'confirmed':True}):
            with self.assertRaisesRegex(ValueError, '旧预览'): core.dispatch(self.m, request)
        self.assertTrue(self.m.verify_backup(bid)['valid'])
