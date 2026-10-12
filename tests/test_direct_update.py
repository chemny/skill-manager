"""One-click updates only operate on isolated fixture directories."""
import unittest
from unittest.mock import patch
import test_maintenance as fixtures
import maintenance as core
import task_scopes
import web_jobs


class DirectUpdateTests(unittest.TestCase):
    setUp = fixtures.MaintenanceTests.setUp
    tearDown = fixtures.MaintenanceTests.tearDown
    skill = fixtures.MaintenanceTests.skill
    row = fixtures.MaintenanceTests.row

    def prepare(self):
        row = self.row('a')
        self.m.bind_source(row, 'https://github.com/example/skills', 'main', 'a')
        self.remote = self.base / 'remote'
        self.remote.mkdir()
        (self.remote / 'SKILL.md').write_text('---\nname: a\nversion: 2.0\n---\nnew release')
        return row

    def fetch(self):
        return patch.object(self.m, 'remote', return_value=(self.remote, self.m.source(self.row('a'))))

    def test_direct_update_backs_up_local_edits_and_preserves_extras(self):
        row = self.prepare()
        (self.roots / 'a' / 'notes').write_text('my notes')
        old = (self.roots / 'a' / 'SKILL.md').read_text()
        with self.fetch():
            self.assertEqual(self.m.check(row)['update_decision'], 'review')
            result = core.dispatch(self.m, {'action':'update-now', 'target':row['id'], 'confirmed':True})
        self.assertIn('version: 2.0', (self.roots / 'a' / 'SKILL.md').read_text())
        self.assertEqual((self.roots / 'a' / 'notes').read_text(), 'my notes')
        bid = result['backups'][0]['id']
        self.assertTrue(self.m.verify_backup(bid)['valid'])
        container, _ = self.m.backup_record(bid)
        self.assertEqual((container / 'content' / 'SKILL.md').read_text(), old)
        self.assertEqual(self.m.inventory()['items'][0]['update_check']['status'], 'latest')

    def test_confirmation_and_valid_check_required(self):
        row = self.prepare()
        for confirmed in (False, True):
            with self.fetch(), patch.object(self.m, 'apply', side_effect=AssertionError('must not apply')):
                with self.assertRaises(ValueError):
                    core.dispatch(self.m, {'action':'update-now', 'target':row['id'], 'confirmed':confirmed})

    def test_local_edits_after_check_are_not_overwritten(self):
        row = self.prepare()
        with self.fetch():
            self.m.check(row)
            (self.roots / 'a' / 'notes').write_text('new work')
            with self.assertRaisesRegex(ValueError, '重新检查'):
                self.m.update_now(row)
        self.assertIn('version: 1.0', (self.roots / 'a' / 'SKILL.md').read_text())

    def test_downloaded_version_must_still_be_newer(self):
        row = self.prepare()
        with self.fetch():
            self.m.check(row)
            for version in ('1.0', '0.9', 'unknown'):
                (self.remote / 'SKILL.md').write_text('---\nname: a\nversion: '+version+'\n---\nchanged')
                with self.assertRaisesRegex(ValueError, '重新检查'):
                    self.m.update_now(row)
        self.assertEqual(self.m.history()['operations'], [])
        self.assertEqual(self.m.backups()['backups'], [])
        self.assertFalse(any(p['state']=='preview' for p in self.m.plans()['plans']))

    def test_protection_and_backup_failure_do_not_overwrite(self):
        row = self.prepare()
        with self.fetch():
            self.m.check(row)
            self.m.protect(row, True)
            with self.assertRaisesRegex(ValueError, 'protected'): self.m.update_now(row)
            self.m.protect(row, False)
            with patch.object(self.m, 'make_backup', side_effect=ValueError('backup failed')):
                with self.assertRaisesRegex(ValueError, 'backup failed'): self.m.update_now(row)
        self.assertIn('version: 1.0', (self.roots / 'a' / 'SKILL.md').read_text())

    def test_job_is_target_scoped_write(self):
        row = self.prepare()
        scope = task_scopes.describe(self.a, {'action':'update-now','target':row['id']})
        self.assertTrue(scope['write'])
        self.assertFalse(scope['global'])
        self.assertEqual(scope['operation'], 'update')
        self.assertIn(str(self.roots / 'a'), scope['resources'])
        self.assertIn('update-now', task_scopes.COMMITS)
        self.assertIn('update-now', web_jobs.Jobs.ACTIONS)
