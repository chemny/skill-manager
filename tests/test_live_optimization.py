"""Summary transport and source preparation must preserve comparison and write safety."""
import datetime as dt
import gzip
import http.client
import hashlib
import io
import json
from pathlib import Path
import ssl
import subprocess
import threading
from types import SimpleNamespace
import unittest
import urllib.error
import zipfile
from unittest.mock import patch

import test_maintenance as fixtures
import check_batches
import diagnostics
import github_transport
import maintenance as core
import update_checks


class OptimizationTests(unittest.TestCase):
    setUp = fixtures.MaintenanceTests.setUp
    tearDown = fixtures.MaintenanceTests.tearDown
    skill = fixtures.MaintenanceTests.skill
    row = fixtures.MaintenanceTests.row

    def source(self):
        row = self.row('a')
        self.m.bind_source(row, 'https://github.com/example/skills', 'main', '')
        return row, self.m.source(row)

    def test_batch_source_is_per_id_and_revalidated_next_batch(self):
        row, source = self.source()
        with patch.object(self.m, '_source', wraps=self.m._source) as discover:
            with core.source_batch(self.m):
                self.assertEqual(self.m.source(row), source)
                self.assertEqual(self.m.source(row), source)
            self.assertEqual(discover.call_count, 1)
            self.m.bind_source(row, 'https://github.com/example/other', 'dev', '')
            with core.source_batch(self.m):
                self.assertIn('/other', self.m.source(row)['url'])
            self.assertEqual(discover.call_count, 2)
        self.assertFalse(hasattr(self.m.r, '_batch_update_channels'))

    def test_old_hash_collision_cannot_serialize_unrelated_repositories(self):
        first = self.row('a')
        bucket = int(hashlib.sha256(str(Path(first['path']).resolve()).encode()).hexdigest(),16)%64
        name = next('collision-'+str(n) for n in range(10000)
                    if int(hashlib.sha256(str(self.roots/('collision-'+str(n))).encode()).hexdigest(),16)%64 == bucket)
        self.skill(name,'other');self.m.scan()
        second = self.row(name)
        for row in (first,second):
            self.m.bind_source(row,'https://github.com/example/'+row['name'],'main','')
        gate = threading.Barrier(2)
        def remote(manager,row,folder):
            gate.wait(timeout=3)
            return Path(row['path']),manager.source(row)
        with patch.object(core.Manager,'remote',remote):
            result = check_batches.run(self.m,[first['id'],second['id']])
        self.assertEqual(result['failed'],0)
        self.assertEqual(result['run']['remote'],2)

    def test_same_path_lock_is_shared_only_while_in_use(self):
        lock = update_checks.path_lock('fixture-path')
        self.assertIs(lock,update_checks.path_lock('fixture-path'))
        self.assertIsNot(lock,update_checks.path_lock('different-path'))
        del lock
        self.assertNotIn('fixture-path',update_checks.LOCKS)

    def test_git_discovery_shared_but_subdirectories_stay_distinct(self):
        (self.roots/'.git').mkdir()
        def git(args, **kwargs):
            value = ('https://github.com/example/skills' if 'get-url' in args else
                     str(self.roots) if '--show-toplevel' in args else 'main')
            return SimpleNamespace(stdout=value)
        with patch('maintenance.subprocess.run', side_effect=git) as run:
            with core.source_batch(self.m):
                first, second = [self.m.source(self.row(name)) for name in ('a','b')]
            self.assertEqual(run.call_count, 3)
        self.assertEqual(first['path'], 'a')
        self.assertEqual(second['path'], 'b')

    def test_explicit_bindings_of_shared_files_are_not_merged(self):
        alias = self.roots/'alias'; alias.symlink_to(self.roots/'a')
        self.m.scan()
        first, second = self.m.select(str(self.roots/'a')), self.m.select(str(alias))
        self.m.bind_source(first, 'https://github.com/example/first', 'main', '')
        self.m.bind_source(second, 'https://github.com/example/second', 'main', '')
        with core.source_batch(self.m):
            self.assertNotEqual(self.m.source(first)['url'], self.m.source(second)['url'])
        with patch.object(core.Manager, 'remote', side_effect=AssertionError('Conflicting sources fetched')):
            result = check_batches.run(self.m, [first['id'], second['id']])
        self.assertEqual(result['run']['remote'], 0)
        self.assertIn('不一致', result['skipped'][0]['reason'])

    def test_preparation_publishes_progress_before_discovery(self):
        events = []
        def discover(row):
            self.assertEqual(events[0]['completed'], 0)
            self.assertIsNone(events[0]['total'])
            return None
        with patch.object(self.m, 'source', side_effect=discover):
            check_batches.run(self.m, [self.row('a')['id']], progress=lambda run, rows:events.append(run))

    def test_summary_keeps_decisions_and_fetches_full_detail_readonly(self):
        value = self.m.inventory()
        original = value['items'][0]
        original['description'] = 'long description '*500
        original['local_source_candidates'] = [{'evidence':'x'*10000}]
        original['update_check'] = {'status':'update_available','remote_version':'2.0',
                                    'differences':{'changed':['huge'*10000]}}
        original['protected'] = True
        self.m.db.execute('update maintenance_inventory_cache set payload=?', (json.dumps(value),))
        self.m.db.commit()
        compact = core.inventory_summary(value)
        item = compact['items'][0]
        self.assertLess(len(json.dumps(compact)), len(json.dumps(value))*.3)
        self.assertEqual(item['description'], original['description'])
        self.assertTrue(item['protected'])
        self.assertEqual(item['update_check']['remote_version'], '2.0')
        self.assertEqual(item['real_path'], original['real_path'])
        self.assertTrue(item['_summary'])
        self.assertNotIn('differences', item['update_check'])
        self.assertIn('differences', original['update_check'])
        with patch.object(self.a.Registry, '__init__', side_effect=AssertionError('Schema write')):
            detail = core.api_dispatch(self.a, {'action':'inventory-detail','id':item['id']})
        self.assertEqual(detail['item']['description'], original['description'])
        self.assertEqual(detail['item']['local_source_candidates'], original['local_source_candidates'])

    def test_job_projection_never_strips_write_recovery_evidence(self):
        result = {'differences':{'changed':['required']},'references':['alias']}
        write = {'action':'execute','result':result}
        self.assertEqual(core.job_summary(write), write)
        check = core.job_summary({'action':'check-many','result':{'checks':[result]}})
        self.assertNotIn('differences', check['result']['checks'][0])

    def test_fallback_reads_same_repository_exact_commit_without_rest(self):
        row, source = self.source()
        sha = 'a'*40
        remote = self.base/'remote'; remote.mkdir()
        (remote/'SKILL.md').write_text('---\nname: a\nversion: 2.0\n---\nnew')
        limited = urllib.error.HTTPError('https://api.github.com/repos/example/skills', 403,
                                        'API rate limit exceeded', {'Retry-After':'1800'}, None)
        with patch.object(self.a, 'github_json', side_effect=limited) as api, \
                patch('github_transport.lookup', return_value=sha+'\trefs/heads/main\n') as git, \
                patch.object(self.a, 'download_github_archive', return_value=remote) as download:
            self.m._use_check_cache = True
            self.m._github_api_state = {}
            checked = self.m.check(row)
            self.assertEqual(checked['remote_version'], '2.0')
            self.assertEqual(checked['source']['commit'], sha)
            self.assertIn('api_retry_at', checked)
            self.assertEqual(download.call_args.args[1], sha)
            self.assertEqual(git.call_args.args[0],'https://github.com/example/skills.git')
            self.m._force_check = True
            self.m.check(row)
            self.assertEqual(api.call_count, 1)

    def test_failed_fallback_retains_rest_cooldown_across_batches(self):
        row, source = self.source()
        self.m._use_check_cache = True
        deadline = (dt.datetime.now(dt.timezone.utc)+dt.timedelta(minutes=30)).isoformat()
        self.m._github_api_state = {'limited':True, 'retry_at':deadline}
        with patch('github_transport.resolve', side_effect=ConnectionError('offline')):
            first = self.m.check(row)
        self.assertEqual(first['api_retry_at'], deadline)
        with patch.object(self.a, 'github_json', side_effect=AssertionError('REST cooldown bypass')), \
                patch('github_transport.resolve', side_effect=ConnectionError('offline')) as git:
            result = check_batches.run(self.m, [row['id']])
        self.assertTrue(git.called)
        self.assertEqual(result['checks'][0]['api_retry_at'], deadline)

    def test_compression_roundtrip_and_opt_out(self):
        for encoding, compressed in [('gzip',True),('gzip;q=0',False),('br',False),('',False),('gzip;q=invalid',False)]:
            headers = {}
            handler = SimpleNamespace(headers={'Accept-Encoding':encoding}, wfile=io.BytesIO(),
                                      send_response=lambda status:None, send_header=lambda k,v:headers.update({k:v}), end_headers=lambda:None)
            value = {'items':['x'*100000]}
            self.a.json_response(handler, value)
            data = handler.wfile.getvalue()
            self.assertEqual(headers.get('Content-Encoding') == 'gzip', compressed)
            self.assertEqual(json.loads(gzip.decompress(data) if compressed else data), value)
            self.assertEqual(int(headers['Content-Length']), len(data))
            self.assertEqual(headers['Cache-Control'], 'no-store')

    def test_archive_connection_fallback_is_same_ref_and_revalidated(self):
        sha = 'a'*40
        calls = []
        def read(request, **kwargs):
            calls.append((request,kwargs))
            if len(calls) == 1: raise http.client.IncompleteRead(b'',8)
            with zipfile.ZipFile(kwargs['destination'],'w') as archive:
                archive.writestr('example-'+sha+'/SKILL.md', '---\nname: a\n---\ncontent')
        destination = self.base/'download'; destination.mkdir()
        with patch('network_io.read', side_effect=read), patch.dict('os.environ', {'GITHUB_TOKEN':'fixture-secret'}):
            root = self.a.download_github_archive({'owner':'example','repo':'skills'},sha,destination)
        self.assertTrue((root/'SKILL.md').is_file())
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][0].full_url, 'https://codeload.github.com/example/skills/zip/'+sha)
        self.assertFalse(calls[1][0].has_header('Authorization'))
        self.assertEqual(calls[1][1]['limit'], 64000000)

    def test_direct_archive_bypasses_rest_without_changing_commit(self):
        sha = 'a'*40
        destination = self.base/'download'; destination.mkdir()
        def read(request, **kwargs):
            self.assertEqual(request.full_url, 'https://codeload.github.com/example/skills/zip/'+sha)
            with zipfile.ZipFile(kwargs['destination'],'w') as archive:
                archive.writestr('example-'+sha+'/SKILL.md', 'content')
        with patch('network_io.read', side_effect=read) as download:
            self.a.download_github_archive({'owner':'example','repo':'skills','_direct_archive':True},sha,destination)
        self.assertEqual(download.call_count, 1)

    def test_archive_limits_are_reported_without_unsafe_fallback(self):
        destination = self.base/'download'; destination.mkdir()
        for error in (ValueError('Remote response exceeds size limit'), urllib.error.URLError(ssl.SSLCertVerificationError('CERTIFICATE_VERIFY_FAILED'))):
            with patch('network_io.read', side_effect=error) as download:
                with self.assertRaises(type(error)):
                    self.a.download_github_archive({'owner':'example','repo':'skills'},'a'*40,destination)
                self.assertEqual(download.call_count, 1)
        self.assertEqual(diagnostics.classify(ValueError('Remote response exceeds size limit'))[0], 'remote_archive_limit')

    def test_archive_quota_does_not_block_selected_directory_checks(self):
        row, source = self.source()
        self.m._use_check_cache = True
        quota = urllib.error.HTTPError('https://codeload.github.com/example/skills/zip/abc',429,'rate limit',{'Retry-After':'1800'},None)
        with patch.object(core.Manager,'remote',side_effect=quota):
            checked = self.m.check(row)
        self.assertEqual(checked['rate_limit_scope'], 'github-archive')
        def selected(manager,row,folder):
            self.assertTrue(manager._targeted_checks)
            return Path(row['path']),manager.source(row)
        with patch.object(core.Manager,'remote',selected):
            result = check_batches.run(self.m,[row['id']],force=True)
        self.assertEqual(result['run']['remote'], 1)
        self.assertEqual(result['failed'],0)


class GitResolutionTests(unittest.TestCase):
    def test_frontend_failed_attempt_is_visible_without_losing_confirmed_update(self):
        text = (Path(__file__).resolve().parents[1]/'assets/maintenance.html').read_text()
        function = 'function listUpdateState'+text.split('function listUpdateState',1)[1].split('const updateLabels',1)[0]
        script = 'const updateStatus=i=>i.update_check.status;'+function+'''
const assert=require('node:assert/strict');
assert.equal(listUpdateState({update_check:{status:'latest',attempt_failed:true}}),'check_failed');
assert.equal(listUpdateState({update_check:{status:'baseline_unknown',attempt_failed:true}}),'check_failed');
assert.equal(listUpdateState({update_check:{status:'update_available',attempt_failed:true}}),'update_available');
assert.equal(listUpdateState({update_check:{status:'latest'}}),'latest');
'''
        subprocess.run(['node','-e',script],check=True,capture_output=True,text=True)

    def manager(self):
        return SimpleNamespace(a=SimpleNamespace(parse_github_url=lambda url:{'owner':'example','repo':'skills'}))

    def test_fallback_only_for_transient_errors_and_real_quota(self):
        for code in (401,404,403):
            self.assertFalse(github_transport.can_fallback(urllib.error.HTTPError('https://fixture', code, 'forbidden', {}, None)))
        self.assertFalse(github_transport.can_fallback(urllib.error.URLError(ssl.SSLCertVerificationError('CERTIFICATE_VERIFY_FAILED'))))
        self.assertTrue(github_transport.can_fallback(TimeoutError()))
        self.assertTrue(github_transport.can_fallback(urllib.error.HTTPError('https://fixture',429,'rate limit',{},None)))

    def test_tag_is_peeled_and_cached_and_tls_is_required(self):
        manager = self.manager()
        source = {'url':'https://github.com/example/skills', 'ref':'refs/tags/v1'}
        sha = 'b'*40
        with patch('github_transport.lookup', return_value='a'*40+'\trefs/tags/v1\n'+sha+'\trefs/tags/v1^{}\n') as git:
            self.assertEqual(github_transport.resolve(manager, source)[0], sha)
            self.assertEqual(github_transport.resolve(manager, source)[0], sha)
            self.assertEqual(git.call_count, 1)
            self.assertEqual(git.call_args.args[0],'https://github.com/example/skills.git')

    def test_ambiguous_branch_tag_and_missing_ref_do_not_guess(self):
        sha='a'*40
        for output in (sha+'\trefs/heads/main\n'+sha+'\trefs/tags/main\n', sha+'\trefs/heads/unrelated\n'):
            with patch('github_transport.lookup', return_value=output):
                with self.assertRaises(ValueError):
                    github_transport.resolve(self.manager(), {'url':'https://github.com/example/skills','ref':'main'})

    def test_unsafe_ref_is_rejected_before_git(self):
        with patch('github_transport.lookup') as git:
            for ref in ('--upload-pack=other','*','refs/heads/a\nb','with space','with\tspace','null\x00byte'):
                with self.subTest(ref=ref), self.assertRaises(ValueError):
                    github_transport.resolve(self.manager(), {'url':'https://github.com/example/skills','ref':ref})
            git.assert_not_called()

    def test_failed_repository_lookup_is_not_repeated_for_every_skill(self):
        manager = self.manager()
        source = {'url':'https://github.com/example/skills', 'ref':'main'}
        with patch('github_transport.lookup', side_effect=ConnectionError('timed out')) as git:
            for _ in range(10):
                with self.assertRaises(ConnectionError): github_transport.resolve(manager, source)
            self.assertEqual(git.call_count, 1)
        with patch('github_transport.lookup', return_value='a'*40+'\trefs/heads/main\n'):
            self.assertEqual(github_transport.resolve(self.manager(), source)[0], 'a'*40)

    def test_integrity_failure_is_not_a_transport_retry(self):
        error = ValueError('安装包文件与更新源校验清单不一致; changed SKILL.md')
        self.assertFalse(github_transport.can_fallback(error))
        self.assertEqual(diagnostics.classify(error)[0], 'remote_integrity')
