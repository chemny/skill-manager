import json
from pathlib import Path
import unittest
from unittest.mock import patch
import test_maintenance as fixtures
import maintenance
import snapshots


class IncrementalOperationTests(unittest.TestCase):
    setUp = fixtures.MaintenanceTests.setUp
    tearDown = fixtures.MaintenanceTests.tearDown
    skill = fixtures.MaintenanceTests.skill
    row = fixtures.MaintenanceTests.row

    def test_scoped_inventory_avoids_global_usage_and_unrelated_files(self):
        self.m.inventory()
        statements, walked = [], []
        original_size = maintenance.size
        def measure(path, *args):
            walked.append(Path(path))
            return original_size(path, *args)
        self.m.db.set_trace_callback(statements.append)
        with patch.object(self.m.r, 'all_capabilities', side_effect=AssertionError('global inventory')), patch('maintenance.size', side_effect=measure):
            value = self.m.inventory([self.row('a')['id']])
        self.m.db.set_trace_callback(None)
        self.assertEqual([i['name'] for i in value['items']], ['a'])
        self.assertNotIn(self.roots / 'b', walked)
        self.assertFalse(any('update capabilities set usage_' in s.lower() for s in statements))
        self.assertTrue(all('capability_id in' in s for s in statements if s.startswith('select capability_id,occurred_at')))

    def test_deleted_row_is_patched_in_saved_snapshot(self):
        self.m.inventory()
        aid = self.row('a')['id']
        with patch.object(self.m.r, 'refresh_usage_counts', side_effect=AssertionError('global usage write')):
            self.m.apply(self.m.plan('delete', self.row('a'))['id'])
        self.m.inventory([aid])
        cached = {i['id']:i for i in self.m.cached_inventory()['items']}
        self.assertFalse(cached[aid]['present'])
        self.assertTrue(cached[self.row('b')['id']]['present'])

    def test_restore_new_location_registers_only_target_without_scan(self):
        self.m.inventory()
        backup = self.m.backup(self.row('a'))
        target = self.roots / 'restored'
        plan = self.m.plan('restore', backup_id=backup['id'], restore_path=str(target))
        with patch.object(self.m, 'scan', side_effect=AssertionError('full scan')):
            result = self.m.apply(plan['id'])
        row = self.row(str(target))
        self.assertIn(row['id'], result['inventory_ids'])
        self.assertEqual((target / 'SKILL.md').read_bytes(), (self.roots / 'a/SKILL.md').read_bytes())

    def test_restore_shared_root_registers_each_agent(self):
        config = self.a.load_config()
        alias = self.base / 'alias'
        alias.symlink_to(self.roots, target_is_directory=True)
        config['platforms']['codex'] = {'enabled':True, 'roots':[str(self.roots)]}
        config['platforms']['claude_code'] = {'enabled':True, 'roots':[str(alias)]}
        self.a.save_config(config)
        self.m.scan(); self.m.inventory()
        backup = self.m.backup(self.m.select(str(self.roots / 'a'), 'codex'))
        target = self.roots / 'restored'
        with patch.object(self.m, 'scan', side_effect=AssertionError('full scan')):
            result = self.m.apply(self.m.plan('restore', backup_id=backup['id'], restore_path=str(target))['id'])
        scoped = self.m.inventory(result['inventory_ids'])
        self.assertTrue(all(i['known_agents']==['claude_code','codex'] for i in scoped['items']))

    def test_backup_patch_does_not_traverse_any_backup_tree(self):
        a = self.m.backup(self.row('a')); self.m.backup(self.row('b'))
        with patch('maintenance.size', side_effect=AssertionError('recursive backup sizing')):
            result = self.m.backups(verify=False, ids=[a['id']])
        self.assertEqual([b['id'] for b in result['backups']], [a['id']])
        self.assertEqual(result['scope'], [a['id']])

    def test_backup_recycle_updates_recorded_space_without_skill_refresh(self):
        backup = self.m.backup(self.row('a'))
        before = self.m.backups(verify=False)['space']
        self.m.apply(self.m.plan('trash-backup', backup_id=backup['id'])['id'])
        with patch.object(self.m, 'inventory', side_effect=AssertionError('unneeded inventory')), patch('maintenance.size', side_effect=AssertionError('recursive sizing')):
            result = self.m.backups(verify=False, ids=[backup['id']])
        self.assertEqual(result['space']['backups']['allocated_bytes'], 0)
        self.assertEqual(result['space']['quarantine']['allocated_bytes'], before['backups']['allocated_bytes'])
        self.assertTrue(result['backups'][0]['trashed_backup'])

    def test_batch_restore_returns_patch_ids_without_scan(self):
        self.m.inventory()
        backup = self.m.backup(self.row('a'))
        plan = self.m.plan('restore', backup_id=backup['id'], restore_path=str(self.roots / 'restored'))
        with patch.object(self.m, 'scan', side_effect=AssertionError('full scan')):
            result = snapshots.apply_restore(self.m, [plan['id']], True)
        self.assertTrue(result['results'][0]['inventory_ids'])

    def test_invalid_backup_scope_rejected(self):
        with self.assertRaises(ValueError): self.m.backups(verify=False, ids='all')
        self.assertEqual(self.m.backups(verify=False, ids=[])['backups'], [])
