import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import space_cleanup as cleanup
import test_maintenance as fixtures


class SpaceCleanupTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.MaintenanceTests()
        self.fixture.setUp()
        self.m = self.fixture.m
        self.project = self.fixture.base / 'project'
        self.project.mkdir()

    def tearDown(self):
        self.fixture.tearDown()

    def file(self, name, data=b'fixture'):
        path = self.project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def move(self, report, ids, confirmed=True):
        return cleanup.move_selected(self.m, {'scan_id': report['id'], 'ids': ids, 'confirmed': confirmed})

    def test_read_only_scan_and_duplicate_keeper(self):
        a = self.file('render-1.png')
        b = self.file('render-2.png')
        source = self.file('main.py', b'print(1)')
        before = {p: p.read_bytes() for p in (a, b, source)}
        report = cleanup.scan(self.m, str(self.project))
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        images = [r for r in report['items'] if r['name'].endswith('.png')]
        self.assertEqual(sum(r['advice'] == 'clean' for r in images), 1)
        self.assertEqual(sum(r['selectable'] for r in images), 1)
        self.assertFalse(next(r for r in report['items'] if r['name'] == 'main.py')['selectable'])

    def test_reference_protection_and_sensitive_exclusions(self):
        media = self.file('final.png')
        self.file('README.md', b'![final](final.png)')
        self.file('.env', b'SECRET=fixture')
        self.file('.codex/private.png')
        self.file('agent/SKILL.md')
        self.file('agent/asset.png')
        self.file('model.gguf')
        outside = self.fixture.base / 'outside.png'
        outside.write_bytes(b'outside')
        (self.project / 'link.png').symlink_to(outside)
        os.link(media, self.project / 'hard.png')
        report = cleanup.scan(self.m, str(self.project))
        names = {r['relative']: r for r in report['items']}
        self.assertNotIn('.env', names)
        self.assertNotIn('link.png', names)
        self.assertNotIn('.codex/private.png', names)
        self.assertNotIn('agent/asset.png', names)
        for name in ('final.png', 'hard.png', 'model.gguf'):
            self.assertFalse(names[name]['selectable'])
        self.assertTrue(outside.exists())

    def test_offline_files_count_without_content_reads_or_cleanup(self):
        cloud = self.file('cloud.png', b'image')
        self.file('local.png', b'image')
        inode = cloud.stat().st_ino
        original = cleanup.read_file
        reads = []
        def checked_read(path, *args, **kwargs):
            self.assertNotEqual(path, cloud, 'Offline file must not trigger hydration')
            reads.append(path)
            return original(path, *args, **kwargs)
        with patch.object(cleanup, 'is_offline', side_effect=lambda s: s.st_ino == inode), patch.object(cleanup, 'read_file', side_effect=checked_read):
            report = cleanup.scan(self.m, str(self.project))
        self.assertEqual(report['summary']['bytes'], 10)
        self.assertEqual(report['summary']['offline_files'], 1)
        row = next(r for r in report['items'] if r['path'] == str(cloud))
        self.assertFalse(row['selectable'])
        self.assertEqual(reads, [])

    def test_cloud_references_prevent_automatic_duplicate_cleanup(self):
        doc = self.file('README.md', b'cloud references')
        self.file('a.png', b'image')
        self.file('b.png', b'image')
        inode = doc.stat().st_ino
        with patch.object(cleanup, 'is_offline', side_effect=lambda s: s.st_ino == inode):
            report = cleanup.scan(self.m, str(self.project))
        self.assertTrue(report['references_incomplete'])
        self.assertEqual(report['summary']['suggested_bytes'], 0)
        self.assertTrue(any(r.get('duplicate_keeper') for r in report['items']))

    def test_content_deadline_keeps_inventory_and_safe_suggestions(self):
        self.file('a.png', b'image')
        self.file('b.png', b'image')
        original = cleanup.read_file
        def slow_hash(path, *args, **kwargs):
            if kwargs.get('digest'):
                raise TimeoutError('fixture deadline')
            return original(path, *args, **kwargs)
        with patch.object(cleanup, 'read_file', side_effect=slow_hash):
            report = cleanup.scan(self.m, str(self.project), seconds=10)
        self.assertEqual(report['summary']['files'], 2)
        self.assertEqual(report['summary']['bytes'], 10)
        self.assertTrue(report['analysis_limited'])
        self.assertEqual(report['summary']['suggested_bytes'], 0)

    def test_confirmation_move_restore_and_no_overwrite(self):
        path = self.file('old.mp4', b'movie')
        report = cleanup.scan(self.m, str(self.project))
        with self.assertRaises(ValueError):
            self.move(report, [report['items'][0]['id']], False)
        self.assertTrue(path.exists())
        result = self.move(report, [report['items'][0]['id']])
        self.assertEqual(result['completed'], 1)
        self.assertFalse(path.exists())
        item = cleanup.trash_list(self.m)['items'][0]
        self.assertEqual(Path(item['stored']).read_bytes(), b'movie')
        self.assertTrue(cleanup.results(self.m, report['id'])['items'][0]['recycled'])
        path.write_bytes(b'new')
        with self.assertRaises(FileExistsError):
            cleanup.restore(self.m, {'id': item['id'], 'confirmed': True})
        self.assertEqual(path.read_bytes(), b'new')
        path.unlink()
        cleanup.restore(self.m, {'id': item['id'], 'confirmed': True})
        self.assertEqual(path.read_bytes(), b'movie')
        with self.assertRaises(ValueError):
            cleanup.restore(self.m, {'id': item['id'], 'confirmed': True})

    def test_changed_file_and_new_reference_are_skipped(self):
        path = self.file('old.png')
        report = cleanup.scan(self.m, str(self.project))
        path.write_bytes(b'changed')
        self.assertEqual(self.move(report, [report['items'][0]['id']])['completed'], 0)
        report = cleanup.scan(self.m, str(self.project))
        self.file('index.html', b'<img src="old.png">')
        self.assertEqual(self.move(report, [report['items'][0]['id']])['completed'], 0)
        self.assertTrue(path.exists())

    def test_parent_symlink_replacement_and_protected_roots(self):
        path = self.file('output/old.png')
        report = cleanup.scan(self.m, str(self.project))
        path.parent.rename(self.project / 'original')
        path.parent.symlink_to(self.project / 'original', target_is_directory=True)
        self.assertEqual(self.move(report, [report['items'][0]['id']])['completed'], 0)
        self.assertTrue((self.project / 'original/old.png').exists())
        for root in (self.fixture.home, self.fixture.roots, Path.home(), Path('/')):
            with self.assertRaises(ValueError):
                cleanup.scan(self.m, str(root))

    def test_cache_age_and_bounded_scan(self):
        path = self.file('__pycache__/old.pyc')
        self.file('old.py', b'print(1)')
        old = time.time() - 8 * 86400
        os.utime(path, (old, old))
        self.file('unused.png', b'unique')
        report = cleanup.scan(self.m, str(self.project))
        self.assertEqual(next(r for r in report['items'] if r['name'] == 'old.pyc')['advice'], 'clean')
        with patch.object(cleanup, 'MAX_FILES', 1):
            self.assertTrue(cleanup.scan(self.m, str(self.project))['truncated'])

    def test_tampered_recycle_file_is_not_restored(self):
        self.file('old.png', b'image')
        report = cleanup.scan(self.m, str(self.project))
        self.move(report, [report['items'][0]['id']])
        item = cleanup.trash_list(self.m)['items'][0]
        Path(item['stored']).write_bytes(b'other')
        with self.assertRaises(ValueError):
            cleanup.restore(self.m, {'id': item['id'], 'confirmed': True})
        self.assertFalse((self.project / 'old.png').exists())

    def test_cached_model_weights_are_kept(self):
        path = self.file('__pycache__/weights.gguf', b'model')
        old = time.time() - 9 * 86400
        os.utime(path, (old, old))
        report = cleanup.scan(self.m, str(self.project))
        self.assertFalse(report['items'][0]['selectable'])
        self.assertEqual(report['items'][0]['advice'], 'keep')
        self.assertEqual(report['summary']['suggested_bytes'], 0)

    def test_originals_models_and_orphaned_cache_are_retained(self):
        self.file('reference/input.png', b'image')
        self.file('render.png', b'image')
        self.file('a.glb', b'model')
        self.file('b.glb', b'model')
        orphan = self.file('__pycache__/missing.pyc')
        old = time.time() - 9 * 86400
        os.utime(orphan, (old, old))
        report = cleanup.scan(self.m, str(self.project))
        by_name = {r['relative']: r for r in report['items']}
        for name in ('reference/input.png', 'a.glb', 'b.glb', '__pycache__/missing.pyc'):
            self.assertFalse(by_name[name]['selectable'], name)
        self.assertEqual(by_name['render.png']['duplicate_keeper'], str(self.project/'reference/input.png'))

    def test_dependency_cache_in_custom_runtime_is_not_a_cleanup_candidate(self):
        self.file('runtime/lib/python3/site-packages/pkg/module.py', b'print(1)')
        cache = self.file('runtime/lib/python3/site-packages/pkg/__pycache__/module.pyc', b'cache')
        old = time.time()-9*86400
        os.utime(cache,(old,old))
        report = cleanup.scan(self.m,str(self.project))
        self.assertEqual(report['items'],[])
        self.assertEqual(cache.read_bytes(),b'cache')

    def test_hash_budget_skip_is_reported_as_incomplete(self):
        self.file('a.png', b'image')
        self.file('b.png', b'image')
        with patch.object(cleanup, 'HASH_BUDGET', 1):
            report = cleanup.scan(self.m, str(self.project))
        self.assertTrue(report['analysis_limited'])
        self.assertEqual(report['coverage']['hash_skipped'], 2)
        self.assertEqual(report['summary']['suggested_bytes'], 0)

    def test_model_textures_are_retained_with_the_model_even_when_duplicated(self):
        self.file('textures/material.png', b'texture')
        self.file('copy.png', b'texture')
        self.file('asset.gltf', b'{"images":[{"uri":"textures/material.png"}]}')
        report = cleanup.scan(self.m,str(self.project))
        texture=next(r for r in report['items'] if r['relative']=='textures/material.png')
        self.assertEqual(texture['protected_role'],'model_dependency')
        self.assertFalse(texture['selectable'])
        copy=next(r for r in report['items'] if r['relative']=='copy.png')
        self.assertTrue(copy['selectable'])
        self.assertEqual(copy['duplicate_keeper'],texture['path'])


if __name__ == '__main__':
    unittest.main()
