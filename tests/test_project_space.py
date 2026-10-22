import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import test_maintenance as fixtures
import project_space as projects
import maintenance as core
import space_cleanup as space
import project_cleanup as cleanup


class ProjectSpaceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.MaintenanceTests()
        self.fixture.setUp()
        self.m = self.fixture.m
        self.root = self.fixture.base / 'projects'
        self.root.mkdir()
        self.first = self.root / 'first'
        self.first.mkdir()
        self.child = self.first / 'child'
        self.child.mkdir()
        self.other = self.root / 'other'
        self.other.mkdir()
        self.records = [('codex', str(self.first)), ('codex', str(self.child)), ('claude_code', str(self.other))]
        self.discover = patch.object(projects, 'discover', side_effect=lambda home: (self.records, []))
        self.discover.start()

    def tearDown(self):
        self.discover.stop()
        self.fixture.tearDown()

    def catalog(self):
        return projects.catalog(self.m, self.fixture.base)

    def scan(self, paths, agents=('codex',)):
        catalog = self.catalog()
        return projects.scan(self.m, {'catalog_id': catalog['id'], 'agents': list(agents),
                              'ids': [p['id'] for p in catalog['projects'] if p['path'] in map(str, paths)]})

    def test_scoped_metadata_counts_models_dependencies_hidden_files_without_content_reads(self):
        (self.first / 'weights.gguf').write_bytes(b'x' * 10000)
        (self.first / '.private-file').write_bytes(b'private')
        dependency = self.first / 'node_modules'
        dependency.mkdir()
        (dependency / 'dependency.js').write_bytes(b'code')
        (self.child / 'keep.png').write_bytes(b'image')
        with patch.object(space, 'read_file', side_effect=AssertionError('No content read during space statistics')):
            report = self.scan([self.first, self.child])
        self.assertEqual(report['state'], 'completed')
        self.assertEqual(report['summary']['files'], 4)
        self.assertEqual(sum(row['space']['bytes'] for row in report['projects']), 10016)
        self.assertEqual(next(p for p in report['projects'] if p['path'] == str(self.first))['space']['files'], 3)
        self.assertFalse(self.m.db.execute('select 1 from maintenance_space_scans').fetchone())

    def test_removed_child_is_not_scanned_through_parent_and_rescan_keeps_scope(self):
        (self.first / 'own.txt').write_text('own')
        (self.child / 'excluded.txt').write_text('excluded')
        report = self.scan([self.first])
        self.assertEqual(report['summary']['files'], 1)
        fresh = self.catalog()
        self.assertNotIn(projects.project_id(self.child), fresh['selection']['ids'])
        self.records.append(('codex', str(self.other)))
        report = projects.scan(self.m, {'rescan': True})
        self.assertEqual([p['path'] for p in report['projects']], [str(self.first)])

    def test_rejects_forged_paths_other_agent_projects_and_missing_catalog(self):
        catalog = self.catalog()
        for payload in ({'catalog_id': 'missing', 'agents': ['codex'], 'ids': [projects.project_id(self.first)]},
                        {'catalog_id': catalog['id'], 'agents': ['codex'], 'ids': [projects.project_id(self.other)]},
                        {'catalog_id': catalog['id'], 'agents': ['codex'], 'ids': ['/']},
                        {'catalog_id': catalog['id'], 'agents': ['unsupported'], 'ids': []}):
            with self.assertRaises(ValueError):
                projects.scan(self.m, payload)
        self.assertIsNone(projects.latest(self.m)['report'])

    def test_aliases_and_hardlinks_do_not_double_count(self):
        alias = self.root / 'alias'
        alias.symlink_to(self.first, target_is_directory=True)
        self.records.append(('claude_code', str(alias)))
        (self.first / 'one.txt').write_text('same')
        os.link(self.first / 'one.txt', self.first / 'two.txt')
        catalog = self.catalog()
        row = next(p for p in catalog['projects'] if p['path'] == str(self.first))
        self.assertEqual(row['agents'], ['codex', 'claude_code'])
        report = self.scan([self.first], ('codex', 'claude_code'))
        self.assertEqual(report['summary']['files'], 1)

    def test_changed_directory_identity_is_rejected_without_reading_replacement(self):
        catalog = self.catalog()
        self.first.rename(self.root / 'before')
        self.first.mkdir()
        (self.first / 'different.txt').write_text('replacement')
        report = projects.scan(self.m, {'catalog_id': catalog['id'], 'agents': ['codex'], 'ids': [projects.project_id(self.first)]})
        self.assertEqual(report['state'], 'partial')
        self.assertEqual(report['projects'][0]['space']['state'], 'failed')
        self.assertEqual(report['summary']['files'], 0)

    def test_stop_and_deadline_keep_explicit_partial_statistics(self):
        (self.first / 'file.txt').write_text('file')
        with patch.object(projects, 'SCAN_SECONDS', 0):
            report = self.scan([self.first])
        self.assertEqual(report['state'], 'partial')
        self.assertEqual(report['projects'][0]['space']['state'], 'pending')
        self.assertIsNone(report['projects'][0]['space'].get('allocated_bytes'))

    def test_system_packages_and_registered_skills_cannot_be_added_for_cleanup(self):
        for path in ['/Applications', '/System', '/usr', str(self.fixture.roots), str(self.root / 'Editor.app')]:
            with self.assertRaises((ValueError, OSError)):
                projects.add_project(self.m, {'agent': 'codex', 'path': path})
        for path in [Path('/Applications/Editor.app/output'), self.root / 'Editor.app' / 'output']:
            self.assertTrue(space.blocked(path, set()))

    def test_manual_project_registration_does_not_modify_files(self):
        note = self.other / 'original.txt'
        note.write_text('keep')
        result = projects.add_project(self.m, {'agent': 'codex', 'path': str(self.other)})
        self.assertIn(projects.project_id(self.other), result['selection']['ids'])
        self.assertEqual(note.read_text(), 'keep')
        self.assertIsNone(projects.latest(self.m)['report'])

    def test_stop_requested_before_worker_starts_is_not_lost(self):
        self.m.cancel_requested = lambda: True
        (self.first / 'keep.txt').write_text('keep')
        report = self.scan([self.first])
        self.assertEqual(report['state'], 'partial')
        self.assertEqual(report['stop_reason'], '扫描已停止')
        self.assertEqual(report['summary']['files'], 0)
        self.assertEqual((self.first / 'keep.txt').read_text(), 'keep')

    def prepare_cleanup(self):
        (self.first / 'a.png').write_bytes(b'image')
        (self.first / 'b.png').write_bytes(b'image')
        (self.first / 'report.html').write_text('<img src="a.png"><img src="b.png">')
        (self.first / 'model.gguf').write_bytes(b'model')
        (self.first / 'original.pdf').write_bytes(b'document')
        disk = self.scan([self.first])
        result = space.scan(self.m, str(self.first), exclude_roots=[str(self.child)])
        self.m.db.execute('insert or replace into maintenance_project_analyses values(?,?)',
                          (str(self.first), json.dumps({'state': 'completed', 'scan_id': result['id']})))
        self.m.db.commit()
        return {'report_id': disk['id'], 'paths': [str(self.first)]}, result

    def test_referenced_duplicates_offer_truthful_impact_and_preserve_a_keeper(self):
        scope, result = self.prepare_cleanup()
        options = cleanup.advice(self.m, scope)
        self.assertEqual(len(options['groups']), 1)
        group = options['groups'][0]
        self.assertEqual(group['files'], 1)
        self.assertIn('可能无法显示', group['impact'])
        self.assertFalse(next(r for r in result['items'] if r['name'] == 'model.gguf')['selectable'])
        self.assertFalse(next(r for r in result['items'] if r['name'] == 'original.pdf')['selectable'])
        self.assertEqual(len(list(self.first.glob('*.png'))), 2)

    def test_confirmed_move_restore_and_permanent_purge_only_touch_fixture_trash(self):
        scope, _ = self.prepare_cleanup()
        group = cleanup.advice(self.m, scope)['groups'][0]
        with self.assertRaises(ValueError):
            cleanup.move(self.m, {**scope, 'groups': [group['id']]})
        result = cleanup.move(self.m, {**scope, 'groups': [group['id']], 'confirmed': True})
        self.assertEqual(result['completed'], 1)
        self.assertEqual(len(list(self.first.glob('*.png'))), 1)
        tid = next(r['id'] for r in space.trash_list(self.m)['items'] if r['state'] == 'available')
        with self.assertRaises(ValueError):
            space.purge(self.m, {'ids': [tid]})
        self.assertEqual(cleanup.restore_many(self.m, {'ids': [tid], 'confirmed': True})['completed'], 1)
        self.assertEqual(len(list(self.first.glob('*.png'))), 2)
        # Reanalysis yields a fresh offer; old group IDs cannot authorize new files.
        old = group['id']
        scope, _ = self.prepare_cleanup()
        with self.assertRaises(ValueError):
            cleanup.move(self.m, {**scope, 'groups': [old], 'confirmed': True})
        group = cleanup.advice(self.m, scope)['groups'][0]
        cleanup.move(self.m, {**scope, 'groups': [group['id']], 'confirmed': True})
        ids = [r['id'] for r in space.trash_list(self.m)['items'] if r['state'] == 'available']
        self.assertEqual(space.purge(self.m, {'ids': ids, 'confirmed': True})['completed'], 1)
        self.assertEqual(len(list(self.first.glob('*.png'))), 1)
        self.assertTrue((self.first / 'model.gguf').exists())
        self.assertTrue((self.first / 'original.pdf').exists())

    def test_tampered_or_symlinked_trash_content_is_retained(self):
        scope, _ = self.prepare_cleanup()
        group = cleanup.advice(self.m, scope)['groups'][0]
        cleanup.move(self.m, {**scope, 'groups': [group['id']], 'confirmed': True})
        item = next(r for r in space.trash_list(self.m)['items'] if r['state'] == 'available')
        stored = Path(item['stored'])
        stored.unlink()
        stored.symlink_to(self.first / 'model.gguf')
        result = space.purge(self.m, {'ids': [item['id']], 'confirmed': True})
        self.assertEqual(result['completed'], 0)
        self.assertTrue(stored.is_symlink())
        self.assertEqual((self.first / 'model.gguf').read_bytes(), b'model')


if __name__ == '__main__':
    unittest.main()
