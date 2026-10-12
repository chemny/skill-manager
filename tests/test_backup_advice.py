"""Evidence-based advice for historical backups, with no network or deletion."""
import unittest
from pathlib import Path
from unittest.mock import patch
import test_maintenance as fixtures
import maintenance as core
import recovery


class HistoricalAdviceTests(unittest.TestCase):
    setUp=fixtures.MaintenanceTests.setUp
    tearDown=fixtures.MaintenanceTests.tearDown
    skill=fixtures.MaintenanceTests.skill
    row=fixtures.MaintenanceTests.row

    def historical(self, count=1, name='a-1.0.0'):
        root=self.base/'historical';root.mkdir(exist_ok=True)
        for n in range(count):
            parent=root/('batch-'+str(n));parent.mkdir()
            core.copy_path(self.roots/'a',parent/name)
        with patch.object(recovery,'locations',return_value=[root]):recovery.discover(self.m)
        return [b for b in self.m.backups(False)['backups'] if b.get('external')]

    def assess(self, rows):
        before={b['path']:core.manifest(core.Path(b['path'])) for b in rows}
        self.m.db.execute('pragma query_only=on')
        try:
            with patch.object(self.m,'remote',side_effect=AssertionError('No cloud requests')):
                result=recovery.backup_advice(self.m,[b['id'] for b in rows])
        finally:self.m.db.execute('pragma query_only=off')
        for path,value in before.items():self.assertEqual(core.manifest(core.Path(path)),value)
        return {b['id']:b['cleanup_advice'] for b in result['items']}

    def test_single_historical_copy_is_compared_and_kept(self):
        rows=self.historical();advice=self.assess(rows)[rows[0]['id']]
        self.assertEqual(advice['code'],'keep')
        self.assertIn('当前内容',advice['summary'])

    def test_exact_duplicates_keep_one_deterministic_copy(self):
        rows=self.historical(3);result=self.assess(rows)
        kept=[bid for bid,a in result.items() if a['code']=='keep']
        self.assertEqual(len(kept),1)
        for a in result.values():
            if a['code']=='cleanable':self.assertEqual(a['keeper_id'],kept[0])

    def test_retained_duplicate_is_always_the_keeper(self):
        rows=self.historical(2);recovery.retain(self.m,rows[0]['id'],True)
        result=self.assess(rows)
        self.assertEqual(result[rows[0]['id']]['code'],'keep')
        self.assertEqual(result[rows[1]['id']]['keeper_id'],rows[0]['id'])

    def test_modified_backup_does_not_become_disposable(self):
        rows=self.historical(2)
        (core.Path(rows[0]['path'])/'notes').write_text('unique work')
        result=self.assess(rows)
        self.assertTrue(all(a['code']!='cleanable' for a in result.values()))

    def test_different_permissions_are_not_complete_duplicates(self):
        rows=self.historical(2)
        (Path(rows[0]['path'])/'SKILL.md').chmod(0o600)
        with patch.object(recovery,'locations',return_value=[self.base/'historical']):recovery.discover(self.m)
        rows=[b for b in self.m.backups(False)['backups'] if b.get('external')]
        self.assertTrue(all(a['code']!='cleanable' for a in self.assess(rows).values()))

    def test_missing_installation_has_specific_reason(self):
        rows=self.historical(name='orphan');(core.Path(rows[0]['path'])/'SKILL.md').write_text('old content without metadata')
        with patch.object(recovery,'locations',return_value=[self.base/'historical']):recovery.discover(self.m)
        rows=[b for b in self.m.backups(False)['backups'] if b.get('external')]
        advice=self.assess(rows)[rows[0]['id']]
        self.assertEqual(advice['label'],'自行判断');self.assertEqual(advice['summary'],'未找到对应安装项')

    def test_different_contents_are_not_deleted_based_on_name_or_old_version(self):
        rows=self.historical();self.skill('a','new content')
        advice=self.assess(rows)[rows[0]['id']]
        self.assertEqual(advice['code'],'review');self.assertEqual(advice['summary'],'与安装项内容不同')

    def test_nested_backup_reference_is_preserved(self):
        rows=self.historical();(self.roots/'reference').symlink_to(core.Path(rows[0]['path']),target_is_directory=True)
        self.m.scan();advice=self.assess(rows)[rows[0]['id']]
        self.assertEqual(advice['code'],'keep');self.assertIn('引用',advice['summary'])


if __name__=='__main__':unittest.main()
