import unittest
import zipfile
from unittest.mock import patch
import test_maintenance as fixtures
import recovery
import maintenance as core


class DiscoveryTests(unittest.TestCase):
    setUp = fixtures.MaintenanceTests.setUp
    tearDown = fixtures.MaintenanceTests.tearDown
    skill = fixtures.MaintenanceTests.skill

    def test_adjacent_names_and_content_validation(self):
        root=self.base/'skills-backup-20261013';root.mkdir()
        (root/'SKILL.md.bak').write_text('partial')
        for name,member in [('skill.zip','nested/SKILL.md'),('unrelated.zip','notes.txt'),('unsafe.zip','../SKILL.md')]:
            with zipfile.ZipFile(root/name,'w') as z:z.writestr(member,'test')
        (root/'broken.zip').write_bytes(b'broken')
        self.assertIn(root,recovery.locations(self.m))
        before=core.manifest(root)
        with patch.object(recovery,'locations',return_value=[root]):result=recovery.discover(self.m)
        self.assertEqual({c['format'] for c in result['candidates']},{'partial','archive'})
        self.assertEqual(len(result['errors']),2)
        self.assertEqual(len(result['rejected']),1)
        self.assertEqual(before,core.manifest(root))
        partial=next(c for c in result['candidates'] if c['format']=='partial')
        self.assertEqual(recovery.backup_detail(self.m,partial['id'])['comparison_state'],'unsupported')
        with self.assertRaises(ValueError):recovery.import_candidate(self.m,partial['id'],str(self.roots/'restore'),'backup',True)

    def test_depth_limit_is_explicit(self):
        root=self.base/'backups';p=root
        for _ in range(7):p=p/'nested'
        p.mkdir(parents=True);(p/'SKILL.md').write_text('deep')
        with patch.object(recovery,'locations',return_value=[root]):result=recovery.discover(self.m)
        self.assertFalse(result['complete'])
        self.assertTrue(result['unscanned'])
        self.assertTrue((p/'SKILL.md').exists())

    def test_live_skill_and_symlinks_not_collected(self):
        link=self.base/'backup-link';link.symlink_to(self.roots,target_is_directory=True)
        with patch.object(recovery,'locations',return_value=[self.roots,link]):result=recovery.discover(self.m)
        self.assertFalse(result['candidates'])
        self.assertTrue(result['errors'])

    def test_archive_budget_is_reported(self):
        root=self.base/'backup';root.mkdir()
        with zipfile.ZipFile(root/'skill.zip','w') as z:z.writestr('SKILL.md','test')
        with patch.object(recovery,'locations',return_value=[root]),patch('backup_discovery.MAX_ARCHIVE_BYTES',1):
            result=recovery.discover(self.m)
        self.assertFalse(result['complete'])
        self.assertFalse(result['candidates'])

    def test_tar_inspection_never_extracts_and_ignored_stays_ignored(self):
        import io
        import tarfile
        root=self.base/'backups';root.mkdir()
        with tarfile.open(root/'skill.tgz','w:gz') as archive:
            info=tarfile.TarInfo('nested/SKILL.md');info.size=4
            archive.addfile(info,io.BytesIO(b'test'))
        with patch.object(recovery,'locations',return_value=[root]):
            result=recovery.discover(self.m)
            bid=result['candidates'][0]['id']
            recovery.ignore(self.m,bid)
            again=recovery.discover(self.m)
        self.assertEqual(again['summary']['ignored'],1)
        self.assertFalse((root/'nested').exists())
        self.assertEqual(again['candidates'][0]['state'],'ignored')
