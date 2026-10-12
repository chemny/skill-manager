"""Duplicate review and reversible sharing, isolated from real Skills."""
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
import test_maintenance as fixture
import maintenance as core
from duplicates import review

class DuplicateTests(unittest.TestCase):
    setUp=fixture.MaintenanceTests.setUp
    tearDown=fixture.MaintenanceTests.tearDown
    skill=fixture.MaintenanceTests.skill
    row=fixture.MaintenanceTests.row

    def copies(self):
        shutil.copy2(self.roots/'a'/'SKILL.md',self.roots/'b'/'SKILL.md')
        self.m.scan();self.m.inventory()
        return self.m.select(str(self.roots/'a')),self.m.select(str(self.roots/'b'))

    def test_review_counts_independent_paths_not_shared_references(self):
        a,b=self.copies()
        (self.roots/'alias').symlink_to(self.roots/'a',target_is_directory=True)
        self.m.scan();self.m.inventory()
        v=review(self.m)
        self.assertEqual(len(v['groups']),1)
        self.assertEqual(len(v['groups'][0]['targets']),2)
        self.assertEqual(v['groups'][0]['status'],'identical')
        self.assertEqual(v['groups'][0]['targets'][0]['retainers'],[b['id']])

    def test_different_contents_do_not_offer_share(self):
        a,b=self.copies();(self.roots/'b'/'notes').write_text('local')
        v=review(self.m)
        self.assertEqual(v['groups'][0]['status'],'different')
        self.assertFalse(any(t['retainers'] for t in v['groups'][0]['targets']))
        with self.assertRaisesRegex(ValueError,'exactly equal'):
            self.m.plan('share',b,a)

    def test_share_backup_restore_and_no_duplicate_afterwards(self):
        a,b=self.copies();plan=self.m.plan('share',b,a)
        self.assertEqual(plan['estimated_net_freed_bytes'],0)
        applied=self.m.apply(plan['id'])
        self.assertTrue((self.roots/'b').is_symlink())
        self.assertEqual((self.roots/'b').resolve(),self.roots/'a')
        backup=applied['backups'][0]['id']
        self.assertTrue(self.m.verify_backup(backup)['valid'])
        self.m.inventory();self.assertEqual(review(self.m)['groups'],[])
        self.m.apply(self.m.plan('restore',backup_id=backup,allow_changes=True)['id'])
        self.assertFalse((self.roots/'b').is_symlink())
        self.assertEqual(core.manifest(self.roots/'b'),core.manifest(self.roots/'a'))

    def test_changed_source_blocks_apply_preserving_original(self):
        a,b=self.copies();plan=self.m.plan('share',b,a)
        (self.roots/'a'/'SKILL.md').write_text('changed')
        with self.assertRaisesRegex(ValueError,'Source changed'):self.m.apply(plan['id'])
        self.assertFalse((self.roots/'b').is_symlink())

    def test_protected_source_cannot_be_retained(self):
        a,b=self.copies();self.m.protect(a,True)
        v=review(self.m)
        self.assertFalse(any(t['retainers'] for t in v['groups'][0]['targets']))
        with self.assertRaisesRegex(ValueError,'unprotected'):self.m.plan('share',b,a)

    def test_failed_placement_rolls_back_and_preserves_backup(self):
        a,b=self.copies();plan=self.m.plan('share',b,a);replace=os.replace
        def fail(src,dst):
            if Path(src).name.startswith('.asm-stage-') and Path(dst)==self.roots/'b':raise OSError('fixture failure')
            return replace(src,dst)
        with patch.object(core.os,'replace',side_effect=fail):
            with self.assertRaisesRegex(ValueError,'fixture failure'):self.m.apply(plan['id'])
        self.assertFalse((self.roots/'b').is_symlink())
        self.assertEqual(core.manifest(self.roots/'b'),core.manifest(self.roots/'a'))
        self.assertTrue(self.m.backups()['backups'])

    def test_changed_references_block_apply(self):
        a,b=self.copies();plan=self.m.plan('share',b,a)
        (self.roots/'alias').symlink_to(self.roots/'a',target_is_directory=True);self.m.scan()
        with self.assertRaisesRegex(ValueError,'references'):self.m.apply(plan['id'])
        self.assertFalse((self.roots/'b').is_symlink())

    def test_permissions_and_nested_links_require_review(self):
        a,b=self.copies();os.chmod(self.roots/'b'/'SKILL.md',0o600)
        self.assertEqual(review(self.m)['groups'][0]['status'],'content_equal')
        with self.assertRaisesRegex(ValueError,'exactly equal'):self.m.plan('share',b,a)
        os.chmod(self.roots/'b'/'SKILL.md',(self.roots/'a'/'SKILL.md').stat().st_mode&0o777)
        for p in ('a','b'):(self.roots/p/'nested').symlink_to(self.roots/'a'/'SKILL.md')
        self.assertFalse(any(t['retainers'] for t in review(self.m)['groups'][0]['targets']))
        with self.assertRaisesRegex(ValueError,'Nested symlinks'):self.m.plan('share',b,a)

    def test_duplicate_snapshot_invalidated_by_registry_change(self):
        a,b=self.copies();review(self.m)
        self.assertTrue(core.dispatch(self.m,{'action':'duplicate-cached'})['groups'])
        self.m.protect(a,True);self.m.inventory()
        self.assertFalse(core.dispatch(self.m,{'action':'duplicate-cached'})['groups'])

    def test_no_large_content_staged_for_share_preview(self):
        a,b=self.copies();plan=self.m.plan('share',b,a)
        staged=self.home/'plans'/plan['id']
        self.assertTrue((staged/'content').is_symlink())
        self.assertFalse((staged/'incoming').exists())
