import os
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

import test_maintenance as fixtures
import disk_discovery as disk


class DiskDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.MaintenanceTests()
        self.fixture.setUp()
        self.m = self.fixture.m
        self.root = self.fixture.base / 'disk'
        self.root.mkdir()
        self.home = self.root / 'user'
        self.home.mkdir()

    def tearDown(self):
        self.fixture.tearDown()

    def file(self, name, data=b'fixture'):
        p = self.home / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def scan(self, **kwargs):
        return disk.scan(self.m, [self.root], self.home, **kwargs)

    def test_discover_without_reading_files_or_entering_project(self):
        marker = self.file('project/package.json', b'PRIVATE')
        self.file('project/deep/another/AGENTS.md')
        self.file('project/large.mp4')
        with patch.object(disk.space, 'read_file', side_effect=AssertionError('must not read content')):
            r = self.scan()
        self.assertEqual([p['path'] for p in r['projects']], [str(marker.parent)])
        self.assertEqual(r['summary']['files_analyzed'], 0)
        self.assertEqual(r['summary']['content_bytes_read'], 0)
        self.assertNotIn('PRIVATE', str(r))
        self.assertTrue(r['read_only'])
        self.assertEqual(disk.latest(self.m)['report']['mode'], 'directory_discovery')

    def test_codex_only_reads_working_directory_metadata(self):
        project = self.file('project/readme.txt').parent
        database = self.home / '.codex/state_5.sqlite'
        database.parent.mkdir(parents=True)
        with sqlite3.connect(database) as db:
            db.execute('create table threads(cwd text, first_user_message text)')
            db.executemany('insert into threads values(?,?)', [(str(project), 'PRIVATE conversation')] * 2)
        self.file('.codex/sessions/secret/AGENTS.md')
        r = self.scan()
        self.assertEqual(r['summary']['known_projects'], 1)
        self.assertNotIn('PRIVATE conversation', str(r))
        self.assertEqual(len(r['projects']), 1)

    def test_cache_dependencies_and_symlinks_are_not_traversed(self):
        self.file('node_modules/vendor/package.json')
        self.file('.cache/package.json')
        self.file('Library/private/package.json')
        actual = self.file('work/pyproject.toml').parent
        (self.home / 'alias').symlink_to(actual, target_is_directory=True)
        r = self.scan()
        self.assertEqual([p['path'] for p in r['projects']], [str(actual)])
        self.assertGreater(r['issue_counts']['内部数据、缓存或依赖目录'], 0)

    def test_stop_and_bounds_preserve_partial_report(self):
        for i in range(5):
            self.file(f'p{i}/data/output.png')
        r = self.scan(limits={'directories': 2})
        self.assertEqual(r['state'], 'partial')
        self.assertTrue(r['pending_locations'])
        original = disk.excluded
        def request_stop(*args):
            disk.stop()
            return original(*args)
        with patch.object(disk, 'excluded', side_effect=request_stop):
            r = self.scan()
        self.assertEqual(r['state'], 'partial')
        self.assertEqual(r['stop_reason'], '用户停止')


if __name__ == '__main__':
    unittest.main()
