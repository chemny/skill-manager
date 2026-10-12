import shutil
import unittest
from unittest.mock import patch
import test_maintenance as fixtures
import maintenance
import snapshots


class SimplifiedWorkflowTests(unittest.TestCase):
    setUp = fixtures.MaintenanceTests.setUp
    tearDown = fixtures.MaintenanceTests.tearDown
    skill = fixtures.MaintenanceTests.skill
    row = fixtures.MaintenanceTests.row

    def test_restore_identical_has_no_write(self):
        backup=self.m.backup(self.row('a'))
        before=self.m.db.total_changes
        status=snapshots.restore_targets(self.m,{'entries':[{'id':backup['id']}]})
        self.assertTrue(status['items'][0]['identical'])
        self.assertTrue(status['items'][0]['exists'])
        self.assertEqual(self.m.db.total_changes,before)

    def test_restore_missing_target(self):
        backup=self.m.backup(self.row('a'))
        shutil.rmtree(self.roots/'a')
        status=snapshots.restore_targets(self.m,{'entries':[{'id':backup['id']}]})
        self.assertFalse(status['items'][0]['exists'])
        self.assertFalse(status['items'][0]['identical'])

    def test_restore_conflict_then_backup_before_replacement(self):
        backup=self.m.backup(self.row('a'))
        self.skill('a','changed')
        status=snapshots.restore_targets(self.m,{'entries':[{'id':backup['id']}]})
        self.assertTrue(status['items'][0]['exists'])
        self.assertFalse(status['items'][0]['identical'])
        result=maintenance.dispatch(self.m,{'action':'execute-restore-many','entries':[{'id':backup['id']}],'allow_overwrite':True,'confirmed':True})
        self.assertEqual(result['state'],'succeeded')
        self.assertTrue(result['results'][0]['backups'])

    def test_duplicate_destinations_rejected(self):
        backup=self.m.backup(self.row('a'))
        with self.assertRaisesRegex(ValueError,'同一位置'):
            snapshots.restore_targets(self.m,{'entries':[{'id':backup['id']}]*2})

    def test_bundle_target_and_entry_validation(self):
        package=snapshots.dispatch(self.m,{'action':'create-snapshot','scope':'all'})
        detail=snapshots.read_package(snapshots.package_path(self.m,package['id']))
        item=detail['items'][0]
        status=snapshots.restore_targets(self.m,{'entries':[{'id':package['id'],'key':item['key']}],'target_root':str(self.roots)})
        self.assertTrue(status['items'][0]['identical'])
        with self.assertRaises(ValueError):
            snapshots.restore_targets(self.m,{'entries':[{'id':package['id'],'key':'unknown'}],'target_root':str(self.roots)})

    def test_open_only_registered_directory(self):
        with patch('maintenance.subprocess.run') as opened:
            result=maintenance.dispatch(self.m,{'action':'open-directory','path':str(self.roots)})
            self.assertTrue(result['opened'])
            self.assertEqual(opened.call_count,1)
            with self.assertRaises(ValueError):
                maintenance.dispatch(self.m,{'action':'open-directory','path':str(self.base)})
