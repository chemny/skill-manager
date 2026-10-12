import hashlib
import io
import json
import stat
import unittest
import zipfile
import test_maintenance as fixtures
import source_verify as verify


class SourceVerifyTests(unittest.TestCase):
    setUp = fixtures.MaintenanceTests.setUp
    tearDown = fixtures.MaintenanceTests.tearDown
    row = fixtures.MaintenanceTests.row
    skill = fixtures.MaintenanceTests.skill

    def test_same_document_different_companion_is_not_exact(self):
        local = {'SKILL.md':'a','scripts/run.py':'b'}
        self.assertFalse(verify.compare(local,{'SKILL.md':'a','scripts/run.py':'c'})['exact'])
        self.assertFalse(verify.compare(local,{'SKILL.md':'a'})['exact'])
        self.assertFalse(verify.compare(local,{'SKILL.md':'a','scripts/run.py':'b','other':'c'})['exact'])
        self.assertTrue(verify.compare(local,local)['exact'])
        self.assertFalse(verify.compare({}, {})['exact'])

    def test_bounded_archive_paths_and_package_hashes(self):
        def archive(names):
            buffer=io.BytesIO()
            with zipfile.ZipFile(buffer,'w') as z:
                for name,value in names: z.writestr(name,value)
            buffer.seek(0)
            return zipfile.ZipFile(buffer)
        with archive([('repo/skills/a/SKILL.md',b'a'),('repo/skills/a/ref.md',b'b')]) as z:
            packages=verify.archive_packages(z)
        self.assertEqual(packages[0]['path'],'skills/a')
        self.assertEqual(packages[0]['files']['ref.md'],hashlib.sha256(b'b').hexdigest())
        with archive([('repo/../SKILL.md',b'a')]) as z:
            with self.assertRaises(ValueError): verify.archive_packages(z)

    def test_bind_rechecks_bytes_and_leaves_skill_untouched(self):
        row=self.row('a'); file=self.roots/'a'/'SKILL.md'; before=file.read_bytes()
        files=verify.package_hashes(self.roots/'a')
        src={'url':'https://github.com/owner/repo','ref':'main','path':'skills/a'}
        changed={**files,'other':'bad'}
        with self.assertRaises(ValueError): verify.bind_verified(self.m,row,src,changed,{})
        self.assertIsNone(self.m.source(row))
        result=verify.bind_verified(self.m,row,src,files,{'method':'full-package-sha256'})
        self.assertEqual(result['source']['evidence'],'verified-package')
        self.assertEqual(file.read_bytes(),before)
        with self.assertRaises(ValueError): verify.bind_verified(self.m,row,src,files,{})

    def test_all_aliases_share_source(self):
        alias=self.roots/'alias';alias.symlink_to(self.roots/'a');self.m.scan()
        row=self.m.select(str(self.roots/'a'))
        result=verify.bind_verified(self.m,row,{'url':'https://github.com/owner/repo'},verify.package_hashes(row['path']),{})
        self.assertGreaterEqual(result['references'],2)
        self.assertEqual(self.m.source(row),self.m.source(self.m.select(str(alias))))

    def test_package_link_is_rejected(self):
        (self.roots/'a'/'link').symlink_to(self.roots/'b'/'SKILL.md')
        with self.assertRaises(ValueError): verify.package_hashes(self.roots/'a')

    def test_only_valid_managed_receipt_is_excluded(self):
        root=self.roots/'a'; files=verify.package_hashes(root)
        receipt=root/'.paseo-managed-files.json'
        receipt.write_text(json.dumps({'version':1,'files':files}))
        self.assertEqual(verify.package_hashes(root),files)
        receipt.write_text(json.dumps({'version':1,'files':{'SKILL.md':'wrong'}}))
        self.assertIn(receipt.name,verify.package_hashes(root))
