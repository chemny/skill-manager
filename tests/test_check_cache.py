import concurrent.futures
import datetime as dt
import json
import time
import unittest
from unittest.mock import patch
import test_maintenance as fixtures
import maintenance


class CheckCacheTests(unittest.TestCase):
    setUp = fixtures.MaintenanceTests.setUp
    tearDown = fixtures.MaintenanceTests.tearDown
    skill = fixtures.MaintenanceTests.skill
    row = fixtures.MaintenanceTests.row

    def setup_source(self):
        row = self.row('a')
        self.m.bind_source(row,'https://github.com/example/skills','main','a')
        self.m._use_check_cache = True
        return row

    def fetch(self, manager, row, folder):
        return self.roots/'a', manager.source(row)

    def test_cache_survives_manager_restart_without_network(self):
        row = self.setup_source()
        with patch.object(maintenance.Manager,'remote',autospec=True,side_effect=self.fetch):
            first = self.m.check(row)
        self.assertFalse(first['cache_hit'])
        self.assertEqual(first['status'],'latest')
        other = maintenance.Manager(self.a);other._use_check_cache=True
        try:
            with patch.object(maintenance.Manager,'remote',side_effect=AssertionError('unexpected network')):
                second = other.check(row)
            self.assertTrue(second['cache_hit'])
            self.assertEqual(first['checked_at'],second['checked_at'])
        finally: other.db.close()

    def test_expiry_and_force_fetch_again(self):
        row = self.setup_source()
        with patch.object(maintenance.Manager,'remote',autospec=True,side_effect=self.fetch):
            self.m.check(row)
            cached = self.m.db.execute('select * from maintenance_check_cache').fetchone()
            value=json.loads(cached['payload']);value['checked_at']=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(minutes=16)).isoformat()
            self.m.db.execute('update maintenance_check_cache set payload=?',(json.dumps(value),));self.m.db.commit()
            self.assertFalse(self.m.check(row)['cache_hit'])
            self.assertTrue(self.m.check(row)['cache_hit'])
            self.m._force_check=True
            self.assertFalse(self.m.check(row)['cache_hit'])

    def test_file_source_and_baseline_changes_invalidate(self):
        row = self.setup_source()
        with patch.object(maintenance.Manager,'remote',autospec=True,side_effect=self.fetch):
            self.m.check(row)
            (self.roots/'a/notes.txt').write_text('local change')
            self.assertFalse(self.m.check(row)['cache_hit'])
            self.assertTrue(self.m.check(row)['cache_hit'])
            self.m.bind_source(row,'https://github.com/example/other','dev','a')
            self.assertFalse(self.m.check(row)['cache_hit'])
            self.m.db.execute('update maintenance_items set baseline=? where id=?',(json.dumps(maintenance.manifest(self.roots/'a')),row['id']));self.m.db.commit()
            self.assertFalse(self.m.check(row)['cache_hit'])

    def test_failure_retries_on_next_request(self):
        row=self.setup_source()
        with patch.object(maintenance.Manager,'remote',side_effect=OSError('offline')) as remote:
            self.assertEqual(self.m.check(row)['status'],'check_failed')
            self.assertFalse(self.m.check(row)['cache_hit'])
            self.assertEqual(remote.call_count,2)
        with patch.object(maintenance.Manager,'remote',autospec=True,side_effect=self.fetch):
            self.assertEqual(self.m.check(row)['status'],'latest')

    def test_failed_retry_preserves_confirmed_update_and_retries_again(self):
        row=self.setup_source()
        with patch.object(maintenance.Manager,'remote',autospec=True,side_effect=self.fetch):
            first=self.m.check(row)
        value=json.loads(self.m.db.execute('select payload from maintenance_check_cache').fetchone()[0])
        value.update(status='update_available',local_version='1.0',remote_version='2.0',checked_at=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(minutes=16)).isoformat())
        self.m.db.execute('update maintenance_check_cache set payload=?',(json.dumps(value),));self.m.db.commit()
        with patch.object(maintenance.Manager,'remote',side_effect=OSError('offline')) as remote:
            for _ in range(2):
                result=self.m.check(row)
                self.assertEqual(result['status'],'update_available')
                self.assertTrue(result['attempt_failed'])
                self.assertEqual(result['checked_at'],value['checked_at'])
            self.assertEqual(remote.call_count,2)
        with patch.object(maintenance.Manager,'remote',autospec=True,side_effect=self.fetch):
            result=self.m.check(row)
        self.assertEqual(result['status'],'latest')
        self.assertNotIn('attempt_failed',result)

    def test_server_rate_limit_cooldown_is_respected(self):
        from urllib.error import HTTPError
        row=self.setup_source()
        error=HTTPError('https://example.com',429,'rate limit',{'Retry-After':'120'},None)
        with patch.object(maintenance.Manager,'remote',side_effect=error) as remote:
            self.assertEqual(self.m.check(row)['code'],'remote_rate_limit')
            result=self.m.check(row)
            self.assertTrue(result['cache_hit'])
            self.assertGreater(result['retry_after_seconds'],0)
            self.assertEqual(remote.call_count,1)

    def test_local_change_does_not_reuse_old_update_evidence(self):
        row=self.setup_source()
        with patch.object(maintenance.Manager,'remote',autospec=True,side_effect=self.fetch):
            self.m.check(row)
        value=json.loads(self.m.db.execute('select payload from maintenance_check_cache').fetchone()[0]);value['status']='update_available'
        self.m.db.execute('update maintenance_check_cache set payload=?',(json.dumps(value),));self.m.db.commit()
        (self.roots/'a/notes.txt').write_text('changed')
        with patch.object(maintenance.Manager,'remote',side_effect=OSError('offline')):
            self.assertEqual(self.m.check(row)['status'],'check_failed')

    def test_policy_upgrade_invalidates_old_decisions(self):
        import version_policy
        row=self.setup_source()
        with patch.object(maintenance.Manager,'remote',autospec=True,side_effect=self.fetch):
            with patch.object(version_policy,'POLICY_VERSION',1):self.m.check(row)
            self.assertFalse(self.m.check(row)['cache_hit'])
            self.assertTrue(self.m.check(row)['cache_hit'])

    def test_previous_policy_cache_reused_without_cloud_request(self):
        import version_policy
        row=self.setup_source()
        with patch.object(maintenance.Manager,'remote',autospec=True,side_effect=self.fetch):
            with patch.object(version_policy,'POLICY_VERSION',2):first=self.m.check(row)
        with patch.object(maintenance.Manager,'remote',side_effect=AssertionError('unexpected cloud query')):
            result=self.m.check(row)
        self.assertTrue(result['cache_hit']);self.assertEqual(result['checked_at'],first['checked_at'])

    def test_concurrent_requests_share_comparison(self):
        row=self.setup_source();calls=[]
        def fetch(manager,row,folder):
            calls.append(row['id']);time.sleep(.1)
            return self.fetch(manager,row,folder)
        def check():
            manager=maintenance.Manager(self.a);manager._use_check_cache=True
            try:return manager.check(row)
            finally:manager.db.close()
        with patch.object(maintenance.Manager,'remote',fetch),concurrent.futures.ThreadPoolExecutor(2) as pool:
            results=list(pool.map(lambda _:check(),range(2)))
        self.assertEqual(len(calls),1)
        self.assertEqual(sum(r['cache_hit'] for r in results),1)

    def test_all_scope_ignores_client_subset_and_checks_readonly_plugin(self):
        import web_jobs
        self.setup_source()
        self.m.bind_source(self.row('b'),'https://github.com/example/skills','main','b')
        self.m.db.execute("update capabilities set source_type='builtin' where id=?",(self.row('a')['id'],));self.m.db.commit()
        def remote(manager,row,folder):
            return self.roots/row['name'],manager.source(row)
        jobs=web_jobs.Jobs(self.a)
        with patch.object(maintenance.Manager,'remote',remote):
            job=jobs.start({'action':'check-many','scope':'all','ids':[self.row('b')['id']]})
            for _ in range(200):
                value=jobs.snapshot(job['id'])
                if value['state'] in ('succeeded','failed'):break
                time.sleep(.01)
        self.assertEqual(value['state'],'succeeded')
        self.assertEqual(len(value['result']['checks']),2)
        self.assertEqual(value['result']['run']['remote'],2)
        self.assertTrue(self.m.db.execute('select payload from maintenance_check_runs where id=?',(job['id'],)).fetchone())

    def test_identical_running_jobs_reused(self):
        import threading,web_jobs
        self.setup_source();gate=threading.Event();release=threading.Event()
        def remote(manager,row,folder):
            gate.set();release.wait(3)
            return self.fetch(manager,row,folder)
        jobs=web_jobs.Jobs(self.a);request={'action':'check-many','ids':[self.row('a')['id']]}
        with patch.object(maintenance.Manager,'remote',remote):
            first=jobs.start(request)
            try:
                self.assertTrue(gate.wait(2))
                self.assertEqual(jobs.start(request)['id'],first['id'])
            finally:release.set()
            for _ in range(200):
                if not jobs.busy():break
                time.sleep(.01)

    def test_rate_limit_does_not_block_other_providers(self):
        import web_jobs
        def source(manager,row,state=None):
            return {'provider':'github' if row['name']=='a' else 'skillhub','url':'https://example.com/'+row['name'],'ref':'main','path':''}
        def check(manager,row):
            return {'id':row['id'],'status':'check_failed' if row['name']=='a' else 'latest',
                    'code':'remote_rate_limit' if row['name']=='a' else '', 'remote_requested':True}
        jobs=web_jobs.Jobs(self.a)
        with patch.object(maintenance.Manager,'source',source),patch.object(maintenance.Manager,'check',check):
            job=jobs.start({'action':'check-many','ids':[self.row('a')['id'],self.row('b')['id']]})
            for _ in range(200):
                value=jobs.snapshot(job['id'])
                if value['state'] in ('succeeded','failed'):break
                time.sleep(.01)
        self.assertEqual(len(value['result']['checks']),2)
        self.assertEqual(value['result']['checks'][1]['status'],'latest')
        self.assertEqual(value['completed'],value['total'])
