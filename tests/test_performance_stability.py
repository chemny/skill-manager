import http.client
import io
import json
import os
from pathlib import Path
import sqlite3
import ssl
import threading
import unittest
import urllib.error
import zipfile
from unittest.mock import patch

import test_maintenance as fixtures
import check_batches
import diagnostics
import maintenance as core
import network_io


class Response(io.BytesIO):
    def __init__(self, body, headers=None):
        super().__init__(body)
        self.headers = headers or {}


class TransportTests(unittest.TestCase):
    def test_incomplete_response_retries_whole_body(self):
        with patch('network_io.urllib.request.urlopen', side_effect=[
                http.client.IncompleteRead(b'partial', 10), Response(b'complete')]) as opener, \
                patch('network_io.time.sleep'):
            self.assertEqual(network_io.read('https://fixture'), b'complete')
            self.assertEqual(opener.call_count, 2)

    def test_content_length_mismatch_retries(self):
        with patch('network_io.urllib.request.urlopen', side_effect=[
                Response(b'bad', {'Content-Length':'8'}), Response(b'complete', {'Content-Length':'8'})]), \
                patch('network_io.time.sleep'):
            self.assertEqual(network_io.read('https://fixture'), b'complete')

    def test_timeout_reset_and_server_failure_retry(self):
        for error in (TimeoutError('timeout'), ConnectionResetError('reset'),
                      urllib.error.HTTPError('https://fixture',503,'unavailable',{},None)):
            with self.subTest(error=type(error).__name__), patch('network_io.urllib.request.urlopen',
                    side_effect=[error,Response(b'ok')]) as opener, patch('network_io.time.sleep'):
                self.assertEqual(network_io.read('https://fixture'),b'ok')
                self.assertEqual(opener.call_count,2)

    def test_permanent_errors_and_rate_limits_are_not_retried(self):
        for code in (401,403,404,429):
            error=urllib.error.HTTPError('https://fixture',code,'failure',{},None)
            with self.subTest(code=code),patch('network_io.urllib.request.urlopen',side_effect=error) as opener:
                with self.assertRaises(urllib.error.HTTPError):network_io.read('https://fixture')
                self.assertEqual(opener.call_count,1)

    def test_certificate_failure_never_bypasses_verification(self):
        error=urllib.error.URLError(ssl.SSLCertVerificationError('CERTIFICATE_VERIFY_FAILED'))
        with patch('network_io.urllib.request.urlopen',side_effect=error) as opener:
            with self.assertRaises(urllib.error.URLError):network_io.read('https://fixture')
            self.assertEqual(opener.call_count,1)
            self.assertNotIn('context',opener.call_args.kwargs)

    def test_retries_and_deadline_are_bounded(self):
        with patch('network_io.urllib.request.urlopen',side_effect=TimeoutError('timeout')) as opener, \
                patch('network_io.time.sleep'):
            with self.assertRaises(TimeoutError):network_io.read('https://fixture')
            self.assertEqual(opener.call_count,3)
        with patch('network_io.urllib.request.urlopen') as opener, \
                patch('network_io.time.monotonic',side_effect=[0,2,2]):
            with self.assertRaises(TimeoutError):network_io.read('https://fixture',timeout=1)
            opener.assert_not_called()

    def test_size_limits_reject_headers_and_streams(self):
        for response in (Response(b'large',{'Content-Length':'100'}),Response(b'large')):
            with patch('network_io.urllib.request.urlopen',return_value=response) as opener:
                with self.assertRaisesRegex(ValueError,'size limit'):network_io.read('https://fixture',limit=4)
                self.assertEqual(opener.call_count,1)

    def test_failed_download_removes_partial_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/'archive.zip'
            with patch('network_io.urllib.request.urlopen',return_value=Response(b'bad',{'Content-Length':'8'})):
                with self.assertRaises(http.client.IncompleteRead):
                    network_io.read('https://fixture',destination=target,attempts=1)
            self.assertFalse(target.exists())

    def test_transport_and_limit_errors_have_actionable_codes(self):
        self.assertEqual(diagnostics.classify(http.client.IncompleteRead(b'',8))[0],'remote_unavailable')
        error=urllib.error.HTTPError('https://fixture',403,'API rate limit exceeded',{},None)
        self.assertEqual(diagnostics.classify(error)[0],'remote_rate_limit')


class PerformanceStabilityTests(unittest.TestCase):
    setUp=fixtures.MaintenanceTests.setUp
    tearDown=fixtures.MaintenanceTests.tearDown
    skill=fixtures.MaintenanceTests.skill
    row=fixtures.MaintenanceTests.row

    def test_scoped_inventory_reads_cache_once(self):
        self.m.inventory()
        with patch.object(self.m,'cached_inventory',wraps=self.m.cached_inventory) as cached:
            self.m.inventory([self.row('a')['id']],lightweight=True)
        self.assertEqual(cached.call_count,1)

    def test_filesystem_walk_allows_independent_database_writer(self):
        self.m.inventory()
        original=core.size
        visited=[]
        def measure(path,*args,**kwargs):
            self.assertFalse(self.m.db.in_transaction)
            other=sqlite3.connect(self.a.DB_PATH,timeout=.05)
            try:
                with other:
                    other.execute('update maintenance_items set protected=protected')
            finally:other.close()
            visited.append(path)
            return original(path,*args,**kwargs)
        with patch('maintenance.size',measure):self.m.inventory([self.row('a')['id']])
        self.assertTrue(visited)
        self.assertFalse(self.m.db.in_transaction)

    def test_empty_uncached_scope_closes_transaction(self):
        self.m.inventory([])
        self.assertFalse(self.m.db.in_transaction)

    def test_space_traversal_once_preserves_hardlink_and_alias_totals(self):
        (self.roots/'alias').symlink_to(self.roots/'a')
        os.link(self.roots/'a/SKILL.md',self.roots/'b/shared.txt')
        self.m.scan()
        seen=set();expected={'bytes':0,'allocated_bytes':0}
        for path in (self.roots/'a',self.roots/'b',self.roots/'alias'):
            occupied=core.size(path,seen)
            for key in expected:expected[key]+=occupied[key]
        with patch('maintenance.size',wraps=core.size) as size:
            result=self.m.inventory()
        self.assertEqual(result['space']['installed_unique'],expected)
        self.assertEqual(sum(Path(c.args[0]).resolve()==self.roots/'a' and not Path(c.args[0]).is_symlink()
                             for c in size.call_args_list),1)

    def test_scoped_patch_returns_complete_source_coverage(self):
        self.m.inventory()
        result=self.m.inventory([self.row('a')['id']],lightweight=True)
        self.assertEqual(result['insights']['source_coverage']['total_targets'],2)

    def test_git_bookkeeping_does_not_invalidate_remote_comparison(self):
        row=self.row('a');folder=self.roots/'a'
        (folder/'.git').mkdir();(folder/'.git/FETCH_HEAD').write_text('first')
        self.m.bind_source(row,'https://github.com/example/skills','main','a')
        self.m._use_check_cache=True
        with patch.object(self.m,'remote',return_value=(folder,self.m.source(row))) as remote:
            self.m.check(row)
            (folder/'.git/FETCH_HEAD').write_text('second')
            self.assertTrue(self.m.check(row)['cache_hit'])
            self.assertEqual(remote.call_count,1)
        self.assertEqual(self.m.inventory()['items'][0]['update_check']['status'],'latest')

    def test_update_with_git_bookkeeping_keeps_safety_and_backup(self):
        row=self.row('a');folder=self.roots/'a'
        (folder/'.git').mkdir();(folder/'.git/FETCH_HEAD').write_text('first')
        self.m.bind_source(row,'https://github.com/example/skills','main','a')
        remote=self.base/'remote';remote.mkdir()
        (remote/'SKILL.md').write_text('---\nname: a\nversion: 2.0\n---\nremote')
        with patch.object(self.m,'remote',return_value=(remote,self.m.source(row))):
            self.m.check(row)
            result=self.m.update_now(row)
        self.assertEqual(result['state'],'succeeded')
        self.assertTrue(self.m.verify_backup(result['backups'][0]['id'])['valid'])
        checked=json.loads(self.m.state(row)['last_check'])
        self.assertEqual(checked['local_fingerprint'],core.comparison_fingerprint(core.manifest(folder)))

    def test_installer_exact_subdirectory_beats_generic_repository_metadata(self):
        row=self.row('a')
        self.m.db.execute('update capabilities set github_url=?,github_path=? where id=?',
                          ('https://github.com/example/wrong','',row['id']));self.m.db.commit()
        (self.base/'.skill-lock.json').write_text(json.dumps({'skills':{'a':{
            'sourceUrl':'https://github.com/example/right','skillPath':'skills/a'}}}))
        source=self.m.source(self.row('a'))
        self.assertEqual(source['url'],'https://github.com/example/right')
        self.assertEqual(source['path'],'skills/a')

    def test_installer_lock_is_read_once_per_inventory_not_cached_between_operations(self):
        lock=self.base/'.skill-lock.json'
        def save(path):lock.write_text(json.dumps({'skills':{'a':{
            'sourceUrl':'https://github.com/example/skills','skillPath':path,
            'installedAt':'2026-01-01T00:00:00Z'}}}))
        save('skills/a');original=Path.read_text;reads=[]
        def read(path,*args,**kwargs):
            if path==lock:reads.append(path)
            return original(path,*args,**kwargs)
        with patch.object(Path,'read_text',read):
            first=self.m.inventory();self.assertEqual(len(reads),1)
            save('new/a');second=self.m.inventory();self.assertEqual(len(reads),2)
        self.assertEqual(next(i for i in first['items'] if i['name']=='a')['source']['path'],'skills/a')
        self.assertEqual(next(i for i in second['items'] if i['name']=='a')['source']['path'],'new/a')

    def test_distinct_repositories_check_concurrently_without_shared_sqlite_connections(self):
        for row in (self.row('a'),self.row('b')):
            self.m.bind_source(row,'https://github.com/example/'+row['name'],'main','')
        barrier=threading.Barrier(2)
        connections=[]
        def remote(manager,row,folder):
            connections.append(manager.db)
            barrier.wait(timeout=3)
            return Path(row['path']),manager.source(row)
        with patch.object(core.Manager,'remote',remote):
            result=check_batches.run(self.m,[self.row('a')['id'],self.row('b')['id']])
        self.assertEqual(result['failed'],0)
        self.assertEqual(len({id(c) for c in connections}),2)

    def test_rate_limit_isolated_by_channel_not_provider(self):
        def source(manager,row,state=None):
            return {'provider':'skillhub','channel':row['name'],'url':'https://'+row['name']+'.example/api/v1/skills/x'}
        def check(manager,row):
            return {'id':row['id'],'status':'check_failed' if row['name']=='a' else 'latest',
                    'code':'remote_rate_limit' if row['name']=='a' else ''}
        with patch.object(core.Manager,'source',source),patch.object(core.Manager,'check',check):
            result=check_batches.run(self.m,[self.row('a')['id'],self.row('b')['id']])
        self.assertEqual(len(result['checks']),2)
        self.assertEqual(result['checks'][1]['status'],'latest')

    def test_archive_cache_lifetime_ends_with_update_batch(self):
        with check_batches.repository_archives(self.m):
            path=self.m._repository_archive_home
            self.assertTrue(path.exists())
            self.m._archive_cache['x']='value'
        self.assertFalse(path.exists())
        self.assertFalse(hasattr(self.m,'_archive_cache'))

    def test_cache_hit_never_dirties_unchanged_inventory(self):
        row=self.row('a');self.m.bind_source(row,'https://github.com/example/skills','main','a')
        self.m._use_check_cache=True
        with patch.object(self.m,'remote',return_value=(Path(row['path']),self.m.source(row))):self.m.check(row)
        self.m.inventory()
        generation=self.m.cached_inventory()['cache']['generation']
        self.assertTrue(self.m.check(row)['cache_hit'])
        cached=self.m.cached_inventory()
        self.assertFalse(cached['cache']['dirty'])
        self.assertEqual(cached['cache']['generation'],generation)

    def test_cli_checks_use_shared_batch_engine_and_close_connection(self):
        arguments=self.a.build_parser().parse_args(['check-updates','a'])
        with patch('check_batches.run',return_value={'checks':[]}) as batch,patch('maintenance.emit'):
            arguments.func(arguments)
        manager,ids=batch.call_args.args
        self.assertEqual(ids,[self.row('a')['id']])
        with self.assertRaises(sqlite3.ProgrammingError):manager.db.execute('select 1')

    def test_batch_repository_concurrency_is_at_most_three(self):
        self.skill('c','c');self.skill('d','d');self.m.scan()
        rows=[self.row(name) for name in ('a','b','c','d')]
        for row in rows:self.m.bind_source(row,'https://github.com/example/'+row['name'],'main','')
        mutex,barrier=threading.Lock(),threading.Barrier(3)
        counts={'running':0,'maximum':0,'started':0}
        def check(manager,row):
            with mutex:
                counts['running']+=1;counts['started']+=1;ordinal=counts['started']
                counts['maximum']=max(counts['maximum'],counts['running'])
            if ordinal<=3:barrier.wait(timeout=3)
            with mutex:counts['running']-=1
            return {'id':row['id'],'status':'latest'}
        with patch.object(core.Manager,'check',check):result=check_batches.run(self.m,[r['id'] for r in rows])
        self.assertEqual(len(result['checks']),4)
        self.assertEqual(counts['maximum'],3)

    def test_large_single_repository_uses_three_workers_without_sharing_connections(self):
        for n in range(15):self.skill('extra-'+str(n),'extra')
        self.m.scan()
        rows=[dict(r) for r in self.m.db.execute("select * from capabilities where kind='skill'")]
        for row in rows:self.m.bind_source(row,'https://github.com/example/monorepo','main',row['name'])
        gate=threading.Barrier(3);mutex=threading.Lock();connections=[]
        def check(manager,row):
            with mutex:
                ordinal=len(connections);connections.append(manager.db)
            if ordinal<3:gate.wait(timeout=3)
            return {'id':row['id'],'status':'latest'}
        with patch.object(core.Manager,'check',check):result=check_batches.run(self.m,[r['id'] for r in rows],force=True)
        self.assertEqual(len(result['checks']),len(rows))
        self.assertEqual(len({id(c) for c in connections}),3)
        self.assertEqual(result['failed'],0)

    def test_prepared_sources_shared_but_network_cache_is_batch_local(self):
        for row in (self.row('a'),self.row('b')):
            self.m.bind_source(row,'https://github.com/example/'+row['name'],'main','')
        contexts=[]
        def check(manager,row):
            contexts.append(manager._remote_json_cache)
            return {'id':row['id'],'status':'latest'}
        with patch.object(core.Manager,'check',check):
            check_batches.run(self.m,[self.row('a')['id'],self.row('b')['id']],force=True)
            self.assertIs(contexts[0],contexts[1]);old=contexts[0];contexts.clear()
            check_batches.run(self.m,[self.row('a')['id'],self.row('b')['id']],force=True)
            self.assertIsNot(old,contexts[0])

    def test_transient_batch_failure_retries_once_and_clears_only_failed_metadata(self):
        row=self.row('a');self.m.bind_source(row,'https://github.com/example/skills','main','a')
        calls=[]
        def check(manager,row):
            calls.append(1)
            if len(calls)==1:
                manager._github_ref_errors[('https://github.com/example/skills','main')]=TimeoutError('temporary')
                manager._selected_git_repos[('https://github.com/example/skills','a'*40)]=ConnectionResetError('reset')
                manager._selected_git_repos[('https://github.com/example/skills','b'*40)]=self.base
                return {'id':row['id'],'status':'check_failed','code':'remote_unavailable'}
            self.assertNotIn(('https://github.com/example/skills','main'),manager._github_ref_errors)
            self.assertNotIn(('https://github.com/example/skills','a'*40),manager._selected_git_repos)
            self.assertEqual(manager._selected_git_repos[('https://github.com/example/skills','b'*40)],self.base)
            return {'id':row['id'],'status':'latest'}
        with patch.object(core.Manager,'check',check):result=check_batches.run(self.m,[row['id']],force=True)
        self.assertEqual(len(calls),2);self.assertEqual(len(result['checks']),1)
        self.assertEqual(result['run']['first_pass_failed'],1)
        self.assertEqual(result['run']['retried'],1);self.assertEqual(result['run']['retry_succeeded'],1)
        self.assertEqual(result['failed'],0)

    def test_persistent_transport_failure_is_not_hidden_or_retried_forever(self):
        row=self.row('a');self.m.bind_source(row,'https://github.com/example/skills','main','a')
        with patch.object(core.Manager,'check',return_value={'id':row['id'],'status':'check_failed','code':'remote_unavailable'}) as check:
            result=check_batches.run(self.m,[row['id']],force=True)
        self.assertEqual(check.call_count,2);self.assertEqual(result['failed'],1)
        self.assertEqual(result['run']['state'],'partial');self.assertEqual(result['run']['retry_succeeded'],0)

    def test_batch_retry_never_retries_permanent_security_or_source_failures(self):
        row=self.row('a');self.m.bind_source(row,'https://github.com/example/skills','main','a')
        for code in ('remote_source_path','remote_certificate','remote_permission','remote_not_found',
                     'remote_unsafe_entries','remote_integrity','remote_archive_limit','remote_rate_limit'):
            with self.subTest(code=code),patch.object(core.Manager,'check',return_value={'id':row['id'],'status':'check_failed','code':code}) as check:
                result=check_batches.run(self.m,[row['id']],force=True)
            self.assertEqual(check.call_count,1);self.assertEqual(result['failed'],1)

    def test_batch_retry_can_reveal_source_failure_but_cannot_claim_success(self):
        row=self.row('a');self.m.bind_source(row,'https://github.com/example/skills','main','a')
        with patch.object(core.Manager,'check',side_effect=[{'id':row['id'],'status':'check_failed','code':'remote_unavailable'},
                {'id':row['id'],'status':'check_failed','code':'remote_source_path'}]):
            result=check_batches.run(self.m,[row['id']],force=True)
        self.assertEqual(result['failed'],1);self.assertEqual(result['checks'][0]['code'],'remote_source_path')
        self.assertEqual(result['run']['retry_resolved_transport'],1)
        self.assertEqual(result['run']['retry_succeeded'],0)

    def test_batch_retry_honors_new_channel_cooldown_without_counting_a_row_twice(self):
        rows=[self.row('a'),self.row('b')]
        def source(manager,row,state=None):
            return {'provider':'skillhub','channel':'same','url':'https://same.example/api/v1/skills/x'}
        def check(manager,row):
            return {'id':row['id'],'status':'check_failed','code':'remote_unavailable' if row['name']=='a' else 'remote_rate_limit'}
        with patch.object(core.Manager,'source',source),patch.object(core.Manager,'check',autospec=True,side_effect=check) as check_call:
            result=check_batches.run(self.m,[r['id'] for r in rows],force=True)
        self.assertEqual(check_call.call_count,2)
        self.assertEqual(result['run']['completed'],result['run']['total'])
        self.assertEqual(result['failed'],2)
        self.assertNotIn('retried',result['run'])

    def archive(self):
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w') as archive:
            archive.writestr('repo/a/SKILL.md','---\nname: a\nversion: 2.0\n---\nremote')
            link=zipfile.ZipInfo('repo/unrelated-link');link.external_attr=0o120777<<16
            archive.writestr(link,'/outside')
        return stream.getvalue()

    def test_unrelated_repository_links_do_not_break_skill_check(self):
        row=self.row('a');self.m.bind_source(row,'https://github.com/example/skills','main','a')
        with patch.object(self.a,'github_json',return_value={'sha':'a'*40}), \
                patch('network_io.urllib.request.urlopen',return_value=Response(self.archive())):
            result=self.m.check(row)
        self.assertEqual(result['status'],'update_available')

    def test_selected_skill_links_still_rejected(self):
        row=self.row('a');self.m.bind_source(row,'https://github.com/example/skills','main','')
        with patch.object(self.a,'github_json',return_value={'sha':'a'*40}), \
                patch('network_io.urllib.request.urlopen',return_value=Response(self.archive())):
            result=self.m.check(row)
        self.assertEqual(result['status'],'check_failed')
        self.assertIn('symlink',result['error'])

    def test_generic_installer_does_not_silently_drop_repository_links(self):
        destination=self.base/'download';destination.mkdir()
        with patch('network_io.urllib.request.urlopen',return_value=Response(self.archive())):
            with self.assertRaisesRegex(ValueError,'symlink'):
                self.a.download_github_archive({'owner':'example','repo':'skills'},'main',destination)

    def test_zip_limits_and_traversal_are_validated_before_extraction(self):
        for name in ('../escape','repo/../../escape','..\\escape'):
            stream=io.BytesIO()
            with zipfile.ZipFile(stream,'w') as archive:archive.writestr(name,'bad')
            with zipfile.ZipFile(stream) as archive:
                with self.assertRaisesRegex(ValueError,'Unsafe'):self.a.safe_extract_zip(archive,self.base/'extract')
        with patch.object(zipfile.ZipFile,'infolist',return_value=[zipfile.ZipInfo('x')]*10001):
            with zipfile.ZipFile(io.BytesIO(self.archive())) as archive:
                with self.assertRaisesRegex(ValueError,'limit'):self.a.safe_extract_zip(archive,self.base/'extract')
