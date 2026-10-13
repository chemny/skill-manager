import unittest
from unittest.mock import patch
import test_maintenance as fixtures
import maintenance as core
import recovery


class OperationProgressTests(unittest.TestCase):
    setUp = fixtures.MaintenanceTests.setUp
    tearDown = fixtures.MaintenanceTests.tearDown
    skill = fixtures.MaintenanceTests.skill
    row = fixtures.MaintenanceTests.row

    def record(self):
        events = []
        self.m.batch_progress = lambda completed,total,message: events.append((completed,total,message))
        return events

    def test_delete_counts_only_successful_items(self):
        events = self.record()
        ids = [self.m.backup(self.row('a'))['id'] for _ in range(3)]
        apply = self.m.apply
        count = [0]
        def fail_second(pid):
            count[0] += 1
            if count[0] == 2: raise OSError('fixture failure')
            return apply(pid)
        with patch.object(self.m, 'apply', fail_second):
            result = core.dispatch(self.m, {'action':'execute-backups','operation':'trash-backup','ids':ids,'confirmed':True})
        self.assertEqual(result['state'], 'partial')
        self.assertEqual(result['completed'], 1)
        self.assertEqual([(a,b) for a,b,_ in events if b is not None], [(0,3),(1,3)])
        self.assertTrue((self.roots/'a/SKILL.md').exists())

    def test_empty_trash_skips_retained_and_reports_progress(self):
        ids = [self.m.backup(self.row('a'))['id'] for _ in range(3)]
        core.dispatch(self.m, {'action':'execute-backups','operation':'trash-backup','ids':ids,'confirmed':True})
        recovery.retain(self.m, ids[-1], True)
        events = self.record()
        result = core.dispatch(self.m, {'action':'empty-trash','confirmed':True})
        self.assertEqual(result['skipped_count'], 1)
        self.assertEqual(result['completed'], 2)
        self.assertEqual([(a,b) for a,b,_ in events if b is not None], [(0,2),(1,2),(2,2)])
        self.assertTrue((self.roots/'a/SKILL.md').exists())

    def test_snapshot_reports_each_written_skill(self):
        events = self.record()
        result = core.dispatch(self.m, {'action':'create-snapshot','scope':'all'})
        self.assertEqual(result['count'], 2)
        self.assertEqual([(a,b) for a,b,_ in events][:3], [(0,2),(1,2),(2,2)])
        self.assertIn('校验', events[-1][2])

    def test_restore_reports_final_count(self):
        ids = [self.m.backup(self.row(name))['id'] for name in ('a','b')]
        for name in ('a','b'):(self.roots/name/'SKILL.md').write_text('changed')
        events = self.record()
        result = core.dispatch(self.m, {'action':'execute-restore-many','entries':[{'id':i} for i in ids],'allow_overwrite':True,'confirmed':True})
        self.assertEqual(result['completed'], 2)
        self.assertEqual([(a,b) for a,b,_ in events if b is not None], [(0,2),(1,2),(2,2)])

    def test_no_confirmation_still_rejected(self):
        events = self.record()
        with self.assertRaises(ValueError):core.dispatch(self.m, {'action':'empty-trash'})
        self.assertEqual(events, [])

    def test_scan_progress_without_total_or_file_changes(self):
        events = self.record()
        root = self.roots.parent/'historical-backups'
        root.mkdir()
        skill = root/'sample'
        skill.mkdir()
        content = '---\nname: sample\ndescription: backup fixture\n---\nSample'
        (skill/'SKILL.md').write_text(content)
        with patch.object(recovery, 'locations', return_value=[root]):
            result = recovery.discover(self.m)
        self.assertTrue(result['complete'])
        self.assertEqual(result['summary']['found'], 1)
        self.assertEqual(events[0][0], 0)
        self.assertEqual(events[-1][0], 2)
        self.assertTrue(all(total is None for _,total,_ in events))
        self.assertIn('发现 1 项', events[-1][2])
        self.assertEqual((skill/'SKILL.md').read_text(), content)

    def test_scan_partial_still_reports_uncovered_locations(self):
        events = self.record()
        def roots(m, extra, errors):
            errors.append({'path':'fixture','message':'unreadable'})
            return []
        with patch.object(recovery, 'locations', side_effect=roots):
            result = recovery.discover(self.m)
        self.assertFalse(result['complete'])
        self.assertEqual(len(result['errors']), 1)
        self.assertEqual(events[-1][0], 0)
