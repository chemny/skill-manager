import json
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import test_maintenance as fixtures
import maintenance as core
import project_analysis as analysis
import space_cleanup as space
import disk_discovery as discovery


def slow_worker(*args):
    time.sleep(10)


class ProjectAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.MaintenanceTests()
        self.fixture.setUp()
        self.m = self.fixture.m
        self.root = self.fixture.base / 'project'
        self.root.mkdir()
        self.child = self.root / 'child'
        self.child.mkdir()
        self.report = {'id': 'test-project-list', 'projects': [
            {'name': p.name, 'path': str(p), 'source': 'codex'} for p in (self.root, self.child)]}
        self.m.db.execute('insert into maintenance_disk_scans values(?,?,?)',
                          (self.report['id'], core.stamp(), json.dumps(self.report)))
        self.m.db.commit()

    def tearDown(self):
        self.fixture.tearDown()

    def test_single_and_batch_keep_sources_and_do_not_double_count(self):
        (self.root / 'draft.png').write_bytes(b'image')
        (self.root / 'draft-copy.png').write_bytes(b'image')
        (self.child / 'video.mp4').write_bytes(b'movie')
        result = analysis.analyze(self.m, {'report_id': self.report['id'], 'scope': 'known'})
        self.assertEqual(result['state'], 'complete', result)
        self.assertEqual(result['completed'], 2)
        self.assertEqual(sum(r['summary']['bytes'] for r in result['analyses']), 15)
        parent = result['analyses'][0]
        self.assertEqual(parent['summary']['suggested_bytes'], 0)
        self.assertEqual(parent['excluded_projects'], [str(self.child)])
        self.assertEqual((self.child / 'video.mp4').read_bytes(), b'movie')
        self.assertEqual((self.root / 'draft.png').read_bytes(), b'image')
        self.assertEqual(discovery.latest(self.m)['report']['projects'][0]['analysis']['scan_id'], parent['scan_id'])
        for _ in range(12):
            space.scan(self.m, str(self.child))
        self.assertIsNotNone(space.results(self.m, parent['scan_id']))

    def test_rejects_unregistered_roots_and_stale_report(self):
        for payload in ({'report_id': 'missing', 'scope': 'known'},
                        {'report_id': self.report['id'], 'paths': ['/']},
                        {'report_id': self.report['id'], 'paths': []}):
            with self.assertRaises(ValueError):
                analysis.analyze(self.m, payload)

    def test_timeout_terminates_worker_and_preserves_file(self):
        path = self.root / 'file.png'
        path.write_bytes(b'original')
        with patch.object(analysis, 'worker', slow_worker), patch.object(analysis, 'PROJECT_SECONDS', .2):
            started = time.monotonic()
            result = analysis.analyze(self.m, {'report_id': self.report['id'], 'paths': [str(self.root)]})
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(result['analyses'][0]['state'], 'timeout')
        self.assertEqual(path.read_bytes(), b'original')

    def test_stop_keeps_completed_project_results(self):
        def finish_first(m, root, exclusions, deadline, notify):
            analysis.stop()
            return {'state': 'stopped', 'reason': '已停止'}
        with patch.object(analysis, 'run_worker', side_effect=finish_first):
            result = analysis.analyze(self.m, {'report_id': self.report['id'], 'scope': 'known'})
        self.assertEqual(result['state'], 'partial')
        self.assertEqual(result['completed'], 1)
        self.assertEqual(result['remaining'], [str(self.child)])


if __name__ == '__main__':
    unittest.main()
