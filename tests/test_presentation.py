"""Presentation and diagnostics tests; all writes use isolated temporary state."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import diagnostics
from skill_presentation import concise
import test_maintenance as fixtures
import maintenance


class PresentationTests(unittest.TestCase):
    def test_unrecognized_description_is_not_fabricated(self):
        value = concise('Measures lunar dust density. Use when studying dust.', 'video-thing')
        self.assertEqual(value['text'], 'Measures lunar dust density.')
        self.assertEqual(value['basis'], 'original-excerpt')

    def test_trigger_examples_do_not_assign_capabilities(self):
        self.assertEqual(concise('Use when security concerns arise.', 'unknown')['basis'], 'original-excerpt')
        self.assertEqual(concise('Not for video generation.', 'unknown')['basis'], 'original-excerpt')

    def test_override_and_chinese_purpose(self):
        self.assertEqual(concise('Writes reports.', override='整理报告')['basis'], 'user')
        self.assertEqual(concise('制作课程，整理讲稿。触发场景：很多说明')['text'], '制作课程，整理讲稿')

    def test_secrets_redacted_in_persisted_diagnostics(self):
        secrets = ['privatepassword', 'querysecret', 'bearerprivate', 'apikeyprivate', 'ghp_abcdefghijklmnop']
        error = ValueError('https://user:privatepassword@example.com/repo?token=querysecret Authorization: Bearer bearerprivate api_key=apikeyprivate ghp_abcdefghijklmnop')
        with tempfile.TemporaryDirectory() as home:
            event = diagnostics.record(home, error, 'preview', 'job-123')
            raw = (Path(home) / 'logs' / 'errors.jsonl').read_text()
            for secret in secrets:
                self.assertNotIn(secret, raw)
                self.assertNotIn(secret, event['error'])
            self.assertEqual(diagnostics.recent(home)[0]['error_id'], event['error_id'])
            self.assertEqual((Path(home) / 'logs' / 'errors.jsonl').stat().st_mode & 0o777, 0o600)

    def test_error_log_failure_preserves_original_failure(self):
        with tempfile.TemporaryDirectory() as home:
            (Path(home) / 'logs').write_text('occupied')
            value = diagnostics.record(home, ValueError('Restore conflict: target exists'), 'preview')
            self.assertTrue(value['log_unavailable'])
            self.assertEqual(value['code'], 'restore_conflict')


# Reuse the fixture setup, but do not inherit the entire baseline suite.
class CorePresentationTests(unittest.TestCase):
    setUp = fixtures.MaintenanceTests.setUp
    tearDown = fixtures.MaintenanceTests.tearDown
    skill = fixtures.MaintenanceTests.skill
    row = fixtures.MaintenanceTests.row

    def test_yaml_folded_indicator_and_saved_brief_preserve_source(self):
        p = self.roots / 'a' / 'SKILL.md'
        p.write_text('---\nname: a\ndescription: >-\n  Create browser automation\n  for web tasks.\n---\nbody')
        self.m.scan()
        before = p.read_bytes()
        row = self.row('a')
        result = maintenance.dispatch(self.m, {'action':'set-description', 'target':row['id'], 'description':'操作网页表单'})
        self.assertTrue(result['saved'])
        inventory = self.m.inventory()
        item = next(i for i in inventory['items'] if i['id'] == row['id'])
        self.assertEqual(item['description'], 'Create browser automation for web tasks.')
        self.assertNotEqual(item['display_description'], '操作网页表单')
        self.assertNotEqual(item['description_basis'], 'user')
        self.assertEqual(json.loads((self.home/'descriptions.json').read_text())[str(p.parent.resolve())], '操作网页表单')
        self.assertEqual(p.read_bytes(), before)
        maintenance.dispatch(self.m, {'action':'set-description', 'target':row['id'], 'description':''})
        self.assertNotIn(str(p.parent.resolve()), json.loads((self.home/'descriptions.json').read_text()))

    def test_skill_without_frontmatter_uses_intro_without_rewriting(self):
        p = self.roots / 'a' / 'SKILL.md'
        p.write_text('# a\n\n将文章排版成公众号 HTML。\n\n## Instructions\nDo something')
        before = p.read_bytes()
        self.m.scan()
        row = self.row(str(self.roots/'a'))
        item = next(i for i in self.m.inventory()['items'] if i['id'] == row['id'])
        self.assertEqual(item['display_description'], '将文章排版成公众号 HTML')
        self.assertEqual(item['description_origin'], 'skill-intro')
        self.assertEqual(p.read_bytes(), before)

    def test_backup_target_conflict_is_checked_without_inventory(self):
        row = self.row('a')
        b = self.m.backup(row)
        listing = self.m.backups(False)['backups'][0]
        self.assertEqual(listing['name'], 'a')
        self.assertEqual(listing['reason'], 'manual')
        self.assertTrue(listing['target_exists'])
        with self.assertRaisesRegex(ValueError, 'Restore conflict'):
            self.m.plan('restore', backup_id=b['id'])
        plan = self.m.plan('restore', backup_id=b['id'], allow_changes=True)
        self.assertEqual(plan['action'], 'restore')
        self.assertTrue((self.roots/'a').exists())
        self.m.discard_plan(plan['id'])

    def test_failed_jobs_are_traceable_after_queue_restart(self):
        import time
        from web_jobs import Jobs
        jobs = Jobs(self.a)
        with patch.object(maintenance, 'dispatch', side_effect=PermissionError('denied')):
            job = jobs.start({'action':'preview'})
            for _ in range(100):
                latest = jobs.snapshot(job['id'])
                if latest['state'] != 'running':
                    break
                time.sleep(.02)
        self.assertEqual(latest['state'], 'failed')
        self.assertEqual(latest['code'], 'permission_denied')
        self.assertEqual(diagnostics.recent(self.home)[0]['error_id'], latest['error_id'])
        self.assertEqual(maintenance.dispatch(self.m, {'action':'history'})['errors'][0]['job_id'], job['id'])
        with self.assertRaises(ValueError):
            Jobs(self.a).snapshot(job['id'])

class FlatWorkflowTests(unittest.TestCase):
    setUp = fixtures.MaintenanceTests.setUp
    tearDown = fixtures.MaintenanceTests.tearDown
    skill = fixtures.MaintenanceTests.skill
    row = fixtures.MaintenanceTests.row

    def test_scan_imports_usage_without_requiring_a_second_action(self):
        with patch.object(self.m, 'import_usage', return_value={'imported_at':'now'}) as read:
            report = self.m.scan(import_logs=True)
        read.assert_called_once_with(days=90)
        self.assertEqual(report['usage_import']['imported_at'], 'now')

    def test_failed_logs_do_not_lose_successful_scan(self):
        with patch.object(self.m, 'import_usage', side_effect=OSError('unreadable log')):
            report = self.m.scan(import_logs=True)
        self.assertGreater(report['count'], 0)
        self.assertEqual(report['automation_errors'][0]['action'], 'import-usage')
        self.assertTrue(self.m.inventory()['items'])

    def test_open_folder_uses_registered_skill_without_shell(self):
        self.m.scan(import_logs=False)
        row = self.row('a')
        with patch('maintenance.sys.platform', 'darwin'), patch('maintenance.subprocess.run') as run:
            result = maintenance.dispatch(self.m, {'action':'open-folder','target':row['id']})
        self.assertTrue(result['opened'])
        self.assertEqual(run.call_args.args[0], ['open',str((self.roots/'a').resolve())])
        self.assertNotIn('shell', run.call_args.kwargs)
