import json
import socket
import unittest
import urllib.error
from unittest.mock import patch

import test_maintenance as fixtures
import github_subset
import network_io
import update_checks
import maintenance as core


class UpdateEvidenceTests(unittest.TestCase):
    setUp = fixtures.MaintenanceTests.setUp
    tearDown = fixtures.MaintenanceTests.tearDown
    skill = fixtures.MaintenanceTests.skill
    row = fixtures.MaintenanceTests.row

    def prepared(self):
        row = self.row('a')
        self.m.bind_source(row, 'https://github.com/example/skills', 'main', 'a')
        self.m._use_check_cache = True
        with patch.object(self.m, 'remote', return_value=(self.roots/'a', self.m.source(row))):
            first = self.m.check(row)
        return row, first

    def test_failed_attempt_never_temporarily_overwrites_saved_success(self):
        row, first = self.prepared()
        original = core.manifest
        observed = []
        def manifest(*args, **kwargs):
            observed.append(json.loads(self.m.state(row)['last_check'])['status'])
            return original(*args, **kwargs)
        self.m._force_check = True
        with patch.object(self.m, 'remote', side_effect=TimeoutError('timeout')), \
                patch('maintenance.manifest', side_effect=manifest):
            checked = self.m.check(row)
        self.assertEqual(observed, ['latest', 'latest'])
        self.assertEqual(checked['checked_at'], first['checked_at'])
        self.assertTrue(checked['attempt_failed'])

    def test_retained_success_keeps_structured_source_failure(self):
        row, _ = self.prepared()
        self.m._force_check = True
        error = github_subset.SourcePathError('a', ['skills/a'])
        with patch.object(self.m, 'remote', side_effect=error):
            checked = self.m.check(row)
        self.assertTrue(checked['attempt_failed'])
        self.assertEqual(checked['source_issue']['candidate_paths'], ['skills/a'])
        self.assertFalse(checked['source_issue']['candidates_verified'])

    def test_saved_success_survives_cache_eviction_but_not_identity_change(self):
        row, first = self.prepared()
        self.m.db.execute('delete from maintenance_check_cache')
        self.m.db.commit()
        self.m._force_check = True
        with patch.object(self.m, 'remote', side_effect=TimeoutError('timeout')):
            checked = self.m.check(row)
        self.assertTrue(checked.get('attempt_failed'))
        self.assertEqual(checked['checked_at'], first['checked_at'])
        self.m.db.execute('delete from maintenance_check_cache')
        self.m.db.execute('update maintenance_items set baseline=? where id=?', ('{}', row['id']))
        self.m.db.commit()
        with patch.object(self.m, 'remote', side_effect=TimeoutError('timeout')):
            checked = self.m.check(row)
        self.assertEqual(checked['status'], 'check_failed')
        self.assertNotIn('attempt_failed', checked)

    def test_failed_attempt_does_not_discard_existing_available_update(self):
        row, _ = self.prepared()
        remote = self.skill('new', 'newer')
        (remote/'SKILL.md').write_text('---\nname: a\nversion: 2.0\n---\nnew')
        self.m._force_check = True
        with patch.object(self.m, 'remote', return_value=(remote, self.m.source(row))):
            first = self.m.check(row)
        with patch.object(self.m, 'remote', side_effect=TimeoutError('timeout')):
            checked = self.m.check(row)
        self.assertEqual(checked['status'], 'update_available')
        self.assertTrue(checked['attempt_failed'])
        self.assertEqual(checked['remote_version'], first['remote_version'])

    def test_headerless_rate_limit_has_bounded_cooldown(self):
        row, _ = self.prepared()
        self.m._force_check = True
        error = urllib.error.HTTPError('https://fixture', 429, 'limited', {}, None)
        with patch.object(self.m, 'remote', side_effect=error) as remote:
            self.m.check(row)
            checked = self.m.check(row)
        self.assertEqual(remote.call_count, 1)
        self.assertGreater(checked['retry_after_seconds'], 0)
        self.assertLessEqual(checked['retry_after_seconds'], 60)

    def test_targeted_check_cannot_bypass_git_transport_cooldown(self):
        row, _ = self.prepared()
        self.m._force_check = self.m._targeted_checks = self.m._github_git_fallback = True
        error = urllib.error.HTTPError('https://github.com', 429, 'limited', {}, None)
        error.rate_limit_scope = 'github-git'
        with patch.object(self.m, 'remote', side_effect=error) as remote:
            self.m.check(row)
            checked = self.m.check(row)
        self.assertEqual(remote.call_count, 1)
        self.assertEqual(checked['rate_limit_scope'], 'github-git')
        self.assertGreater(checked['retry_after_seconds'], 0)

    def test_failed_batch_respects_saved_git_transport_cooldown(self):
        import check_batches
        row, _ = self.prepared()
        self.m._force_check = True
        error = urllib.error.HTTPError('https://github.com', 429, 'limited', {}, None)
        error.rate_limit_scope = 'github-git'
        with patch.object(self.m, 'remote', side_effect=error):
            self.m.check(row)
        with patch.object(core.Manager, 'remote') as remote:
            result = check_batches.run(self.m, [row['id']], scope='failed')
        remote.assert_not_called()
        self.assertEqual(result['run']['cached'], 1)
        self.assertTrue(result['checks'][0]['attempt_failed'])

    def test_cache_eviction_cannot_reuse_evidence_after_file_or_source_change(self):
        row, _ = self.prepared()
        self.m.db.execute('delete from maintenance_check_cache'); self.m.db.commit()
        (self.roots/'a/notes.txt').write_text('changed locally')
        with patch.object(self.m, 'remote', side_effect=TimeoutError('timeout')):
            self.assertEqual(self.m.check(row)['status'], 'check_failed')
        self.m.bind_source(row, 'https://github.com/example/other', 'main', 'a')
        with patch.object(self.m, 'remote', side_effect=TimeoutError('timeout')):
            checked = self.m.check(row)
        self.assertEqual(checked['status'], 'check_failed')
        self.assertNotIn('attempt_failed', checked)

    def test_failed_scope_rechecks_failure_but_not_success(self):
        import check_batches
        first, _ = self.prepared()
        second = self.row('b')
        self.m.bind_source(second, 'https://github.com/example/skills', 'main', 'b')
        with patch.object(self.m, 'remote', side_effect=TimeoutError('timeout')):
            self.m.check(second)
        def remote(manager, row, folder):
            return self.roots/row['name'], manager.source(row)
        with patch.object(core.Manager, 'remote', autospec=True, side_effect=remote) as fetch:
            result = check_batches.run(self.m, [first['id'], second['id']], scope='failed')
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual([c['id'] for c in result['checks']], [second['id']])
        self.assertEqual(result['failed'], 0)

    def test_permanent_dns_error_does_not_trigger_batch_tail_retry(self):
        import check_batches
        row, _ = self.prepared()
        error = urllib.error.URLError(socket.gaierror(socket.EAI_NONAME, 'no host'))
        with patch.object(core.Manager, 'remote', side_effect=error) as fetch:
            result = check_batches.run(self.m, [row['id']], force=True)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(result['failed'], 1)
        self.assertNotIn('retried', result['run'])

    def test_successful_recovery_removes_old_failure_details(self):
        row, _ = self.prepared()
        self.m._force_check = True
        with patch.object(self.m, 'remote', side_effect=github_subset.SourcePathError('a', ['skills/a'])):
            self.m.check(row)
        with patch.object(self.m, 'remote', return_value=(self.roots/'a', self.m.source(row))):
            checked = self.m.check(row)
        self.assertEqual(checked['status'], 'latest')
        for key in ('attempt_failed', 'source_issue', 'retry_at', 'attempted_at', 'error', 'code'):
            self.assertNotIn(key, checked)

    def test_cache_generation_changes_when_single_item_finishes(self):
        row, _ = self.prepared()
        self.m.inventory()
        generation = self.m.db.execute('select generation from maintenance_inventory_cache where id=1').fetchone()[0]
        self.m._force_check = True
        with patch.object(self.m, 'remote', return_value=(self.roots/'a', self.m.source(row))):
            self.m.check(row)
        self.assertGreater(self.m.db.execute('select generation from maintenance_inventory_cache where id=1').fetchone()[0], generation)
        self.assertTrue(self.m.cached_inventory()['cache']['dirty'])


class ConnectionBudgetTests(unittest.TestCase):
    def test_real_timeout_leaves_budget_for_recovery(self):
        import io
        clock = [0.0]
        calls = []
        def opener(request, timeout):
            calls.append(timeout)
            if len(calls) == 1:
                clock[0] += timeout
                raise TimeoutError('first attempt consumed its timeout')
            return io.BytesIO(b'recovered')
        with patch('network_io.time.monotonic', side_effect=lambda: clock[0]), \
                patch('network_io.time.sleep', side_effect=lambda delay: clock.__setitem__(0, clock[0]+delay)):
            value = network_io.read('https://fixture', timeout=20, opener=opener)
        self.assertEqual(value, b'recovered')
        self.assertEqual(len(calls), 2)
        self.assertLess(clock[0], 20)

    def test_permanent_dns_error_is_not_transient(self):
        self.assertFalse(network_io.transient(urllib.error.URLError(socket.gaierror(socket.EAI_NONAME, 'no host'))))
        self.assertTrue(network_io.transient(urllib.error.URLError(socket.gaierror(socket.EAI_AGAIN, 'temporary'))))

    def test_git_rate_limit_is_not_immediately_retried(self):
        error = urllib.error.HTTPError('https://fixture', 429, 'limited', {}, None)
        with patch.object(github_subset, 'git_run', side_effect=error) as run, \
                patch('github_subset.time.sleep'):
            with self.assertRaises(urllib.error.HTTPError):
                github_subset.git_request('/tmp', ['ls-remote', 'https://fixture'])
        self.assertEqual(run.call_count, 1)

    def test_git_process_http_429_is_classified_as_rate_limit(self):
        from types import SimpleNamespace
        def process(*args, **kwargs):
            kwargs['stderr'].write(b'fatal: unable to access repository: The requested URL returned error: 429')
            return SimpleNamespace(poll=lambda:128, returncode=128, wait=lambda:128)
        with patch('github_subset.subprocess.Popen', side_effect=process), \
                patch.object(github_subset, 'repository_size', return_value=0):
            with self.assertRaises(urllib.error.HTTPError) as raised:
                github_subset.git_run('/tmp', ['ls-remote', 'https://github.com/example/skills.git'])
        self.assertEqual(raised.exception.code, 429)
        self.assertEqual(raised.exception.rate_limit_scope, 'github-git')


class PinnedSourcePathTests(unittest.TestCase):
    def setUp(self):
        github_subset._cache.clear()

    def test_missing_path_reuses_only_same_repository_commit_and_directory(self):
        from types import SimpleNamespace
        from pathlib import Path
        manager = SimpleNamespace(a=SimpleNamespace(parse_github_url=lambda url:{'owner':'example','repo':'skills'}))
        source = {'url':'https://github.com/example/skills', 'path':'missing'}
        with patch.object(github_subset, 'api_entries', side_effect=github_subset.SourcePathError('missing', ['skills/missing'])) as entries:
            for _ in range(2):
                with self.assertRaises(github_subset.SourcePathError) as error:
                    github_subset.snapshot(manager, source, 'a'*40, Path('/tmp'), Path('/tmp'))
                self.assertEqual(error.exception.source_issue['candidate_paths'], ['skills/missing'])
            self.assertEqual(entries.call_count, 1)
            for changed, commit in ((source, 'b'*40), ({**source,'path':'other'}, 'a'*40),
                                    ({**source,'url':'https://github.com/another/skills'}, 'a'*40)):
                with self.assertRaises(github_subset.SourcePathError):
                    github_subset.snapshot(manager, changed, commit, Path('/tmp'), Path('/tmp'))
            self.assertEqual(entries.call_count, 4)
            with self.assertRaises(github_subset.SourcePathError):
                github_subset.snapshot(manager, source, 'a'*40, Path('/tmp'), Path('/tmp/another-name'))
            self.assertEqual(entries.call_count, 5)

    def test_transport_failure_is_not_pinned_as_missing_directory(self):
        from types import SimpleNamespace
        from pathlib import Path
        manager = SimpleNamespace(a=SimpleNamespace(parse_github_url=lambda url:{'owner':'example','repo':'skills'}))
        source = {'url':'https://github.com/example/skills', 'path':'missing'}
        with patch.object(github_subset, 'git_entries', side_effect=TimeoutError('temporary')) as entries:
            for _ in range(2):
                with self.assertRaises(TimeoutError):
                    github_subset.snapshot(manager, source, 'a'*40, Path('/tmp'), Path('/tmp'), use_git=True)
            self.assertEqual(entries.call_count, 2)


if __name__ == '__main__':
    unittest.main()
