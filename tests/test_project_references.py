import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import project_references as refs
import space_cleanup as space
import test_space_cleanup as fixtures


class ReferenceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SpaceCleanupTests()
        self.fixture.setUp()
        self.m = self.fixture.m
        self.root = self.fixture.project

    def tearDown(self):
        self.fixture.tearDown()

    def scan(self):
        return space.scan(self.m, str(self.root))

    def test_exact_paths_not_same_name_and_encoded_json(self):
        a = self.fixture.file('a/图片 one.png')
        b = self.fixture.file('b/图片 one.png')
        self.fixture.file('a/report.json', json.dumps({'image': './%E5%9B%BE%E7%89%87%20one.png'}).encode())
        report = self.scan()
        rows = {r['path']: r for r in report['items']}
        self.assertTrue(rows[str(a)]['references'])
        self.assertFalse(rows[str(b)]['references'])
        self.assertFalse(refs.paths_in('old-one.png', self.root/'README.md', self.root, {str(self.root/'one.png')}))

    def test_large_stream_boundary_and_cache_invalidation(self):
        a = self.fixture.file('a.png')
        self.fixture.file('b.png')
        doc = self.fixture.file('report.md', b' ' * (refs.CHUNK - 3) + b'![x](./a.png)' + b' ' * 400000)
        report = self.scan()
        self.assertFalse(report['references_incomplete'])
        self.assertTrue(next(r for r in report['items'] if r['path'] == str(a))['references'])
        cached = self.scan()
        self.assertEqual(cached['coverage']['reference_bytes'], 0)
        doc.write_text('![x](b.png)')
        changed = self.scan()
        self.assertGreater(changed['coverage']['reference_bytes'], 0)
        self.assertFalse(next(r for r in changed['items'] if r['path'] == str(a))['references'])

    def test_budget_continuation_preserves_hits(self):
        a = self.fixture.file('a.png')
        self.fixture.file('b.png')
        self.fixture.file('report.md', b'![x](a.png) ' + b' ' * 400000 + b'![x](b.png)')
        with patch.object(refs, 'READ_BUDGET', 180000):
            first = self.scan()
            second = self.scan()
            third = self.scan()
        self.assertTrue(first['references_incomplete'])
        self.assertEqual(first['coverage']['reference_pending'][0]['reason'], 'read_budget')
        self.assertGreater(second['coverage']['reference_pending'][0]['offset'], first['coverage']['reference_pending'][0]['offset'])
        self.assertFalse(third['references_incomplete'])
        self.assertTrue(next(r for r in third['items'] if r['path'] == str(a))['references'])

    def test_file_uri_and_material_dependency(self):
        a = self.fixture.file('texture.png')
        self.fixture.file('copy.png')
        self.fixture.file('model.mtl', ('map_Kd '+a.as_uri()).encode())
        row = next(r for r in self.scan()['items'] if r['path'] == str(a))
        self.assertEqual(row['protected_role'], 'model_dependency')
        self.assertFalse(row['selectable'])

    def test_candidate_change_invalidates_cache(self):
        self.fixture.file('report.md', b'![x](new.png)')
        self.scan()
        a = self.fixture.file('new.png')
        report = self.scan()
        self.assertGreater(report['coverage']['reference_bytes'], 0)
        self.assertTrue(next(r for r in report['items'] if r['path'] == str(a))['references'])

    def test_markdown_inside_json_string(self):
        a = self.fixture.file('image.png')
        self.fixture.file('report.json', json.dumps({'description': 'Preview: ![image](./image.png) here'}).encode())
        self.assertTrue(next(r for r in self.scan()['items'] if r['path'] == str(a))['references'])

    def test_invalid_text_and_read_failure_remain_incomplete(self):
        self.fixture.file('bad.md', b'\xffbad encoding')
        report = self.scan()
        self.assertTrue(report['references_incomplete'])
        self.assertEqual(report['coverage']['reference_pending'][0]['reason'], 'decode_error')
        with patch.object(refs.os, 'open', side_effect=PermissionError('fixture')):
            rows = [{'path': str(self.root/'bad.md'), 'size': 13, 'offline': False,
                     'fingerprint': [], 'duplicate_eligible': False}]
            _, cov = refs.check(self.m, self.root, rows, None, space)
        self.assertEqual(cov['reference_pending'][0]['reason'], 'read_error')

    def test_large_batch_does_not_evict_continuation(self):
        self.fixture.file('a.png')
        self.fixture.file('report.md', b' ' * 600000)
        with patch.object(refs, 'READ_BUDGET', 180000):
            first = self.scan()
            # Other projects in the same batch can produce many cache entries.
            self.m.db.executemany('insert into maintenance_reference_cache values(?,?,?)',
                                 [(str(i), time.time()+1, '{}') for i in range(10010)])
            self.m.db.commit()
            second = self.scan()
            third = self.scan()
        offsets = [r['coverage']['reference_pending'][0]['offset'] for r in (first, second, third)]
        self.assertTrue(offsets[0] < offsets[1] < offsets[2])


if __name__ == '__main__':
    unittest.main()
