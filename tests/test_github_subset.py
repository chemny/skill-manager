"""Selected-directory comparisons keep pinning, complete manifests and write safety."""
import base64
import concurrent.futures
import datetime as dt
import json
from pathlib import Path
import ssl
import time
import unittest
import urllib.error
from unittest.mock import patch
from types import SimpleNamespace

import test_maintenance as fixtures
import check_batches
import diagnostics
import github_subset as subset
import github_transport
import maintenance as core


class SubsetTests(unittest.TestCase):
    setUp = fixtures.MaintenanceTests.setUp
    tearDown = fixtures.MaintenanceTests.tearDown
    skill = fixtures.MaintenanceTests.skill
    row = fixtures.MaintenanceTests.row

    def fixture(self, files=None):
        subset._cache.clear()
        self.row_a=self.row('a'); self.local=Path(self.row_a['path'])
        self.m.bind_source(self.row_a,'https://github.com/example/skills','main','a')
        self.source=self.m.source(self.row_a)
        self.commit='a'*40; self.root='b'*40; self.tree='c'*40
        self.files=files or {'SKILL.md':(self.local/'SKILL.md').read_bytes(), 'new.txt':b'new'}
        self.entries=[{'path':name,'sha':subset.blob_hash(body),'type':'blob','mode':'100644','size':len(body)} for name,body in self.files.items()]
        self.calls=[]
        def remote(url):
            self.calls.append(url)
            if '/commits/main' in url: return {'sha':self.commit}
            if '/git/commits/' in url: return {'tree':{'sha':self.root}}
            if url.endswith('/git/trees/'+self.root+'?recursive=1'):
                return {'tree':[{'path':'a','sha':self.tree,'type':'tree'},
                                *[{**e,'path':'a/'+e['path']} for e in self.entries],
                                {'path':'unrelated','sha':'d'*40,'type':'tree'}]}
            if url.endswith('/git/trees/'+self.root):
                return {'tree':[{'path':'a','sha':self.tree,'type':'tree'}, {'path':'unrelated','sha':'d'*40,'type':'tree'}]}
            if url.endswith('/git/trees/'+self.tree): return {'tree':self.entries}
            if '/git/blobs/' in url:
                body=next(body for body in self.files.values() if subset.blob_hash(body)==url.rsplit('/',1)[-1])
                return {'encoding':'base64','content':base64.b64encode(body).decode()}
            raise AssertionError('Unexpected request: '+url)
        self.remote=remote
        return self.row_a

    def snapshot(self, **kwargs):
        folder=self.base/('snapshot-'+str(time.time_ns())); folder.mkdir()
        return subset.snapshot(self.m,self.source,self.commit,folder,self.local,**kwargs)

    def test_only_changed_files_download_and_unrelated_tree_is_not_read(self):
        self.fixture()
        with patch.object(self.m,'remote_json',side_effect=self.remote):
            target,stats=self.snapshot()
        self.assertEqual(stats['reused_local_files'],1)
        self.assertEqual(stats['selected_file_bytes'],3)
        self.assertEqual((target/'new.txt').read_bytes(),b'new')
        self.assertEqual(sum('/git/blobs/' in url for url in self.calls),1)
        self.assertFalse(any(url.endswith('d'*40) for url in self.calls))
        self.assertFalse((self.local/'new.txt').exists())

    def test_cached_blobs_are_shared_but_each_directory_gets_complete_snapshot(self):
        self.fixture()
        with patch.object(self.m,'remote_json',side_effect=self.remote):
            first,_=self.snapshot(); second,_=self.snapshot()
        self.assertEqual((first/'new.txt').read_bytes(),(second/'new.txt').read_bytes())
        self.assertEqual(sum('/git/blobs/' in url for url in self.calls),1)

    def test_legacy_and_selected_check_have_identical_decisions_and_differences(self):
        row=self.fixture({'SKILL.md':b'---\nname: a\nversion: 2.0\n---\nremote', 'new.txt':b'new'})
        (self.local/'local.txt').write_text('local-only')
        remote=self.base/'legacy';(remote/'a').mkdir(parents=True)
        for name,body in self.files.items(): (remote/'a'/name).write_bytes(body)
        original={p.name:p.read_bytes() for p in self.local.iterdir()}
        with patch.object(self.m,'remote_json',side_effect=self.remote), patch.object(self.a,'download_github_archive',return_value=remote) as archive:
            before=self.m.check(row)
            self.m._targeted_checks=True
            after=self.m.check(row)
        self.assertEqual(archive.call_count,1)
        for key in ('status','differences','local_version','remote_version','local_fingerprint'):
            self.assertEqual(before[key],after[key],key)
        self.assertEqual(after['status'],'update_available')
        self.assertEqual(original,{p.name:p.read_bytes() for p in self.local.iterdir()})

    def test_unrelated_archive_size_failure_no_longer_blocks_check(self):
        row=self.fixture()
        with patch.object(self.m,'remote_json',side_effect=self.remote), patch.object(self.a,'download_github_archive',side_effect=ValueError('Remote response exceeds size limit')) as archive:
            before=self.m.check(row);self.m._targeted_checks=True;after=self.m.check(row)
        self.assertEqual(before['code'],'remote_archive_limit')
        self.assertNotEqual(after['status'],'check_failed')
        self.assertEqual(archive.call_count,1)

    def test_actual_update_download_does_not_use_check_only_snapshot(self):
        row=self.fixture(); self.m._targeted_checks=True
        remote=self.base/'legacy';(remote/'a').mkdir(parents=True)
        (remote/'a'/'SKILL.md').write_text('remote')
        with patch.object(self.m,'remote_json',side_effect=self.remote), patch.object(self.a,'download_github_archive',return_value=remote) as archive, patch.object(subset,'snapshot',side_effect=AssertionError('Write path used read-only comparison')):
            self.m.remote(row,self.base)
        self.assertEqual(archive.call_count,1)

    def test_wrong_path_does_not_search_another_skill(self):
        self.fixture();self.source['path']='missing'
        with patch.object(self.m,'remote_json',side_effect=self.remote),patch.object(subset,'git_entries') as git:
            with self.assertRaisesRegex(ValueError,'Confirmed source path'):self.snapshot()
        git.assert_not_called()

    def test_hash_mismatch_is_never_cached_or_used(self):
        self.fixture()
        def corrupt(url):
            if '/git/blobs/' in url:return {'encoding':'base64','content':base64.b64encode(b'corrupt').decode()}
            return self.remote(url)
        with patch.object(self.m,'remote_json',side_effect=corrupt) as request:
            for _ in range(2):
                with self.assertRaisesRegex(ValueError,'integrity mismatch'):self.snapshot()
            self.assertEqual(sum('/git/blobs/' in c.args[0] for c in request.call_args_list),2)

    def test_symlink_or_submodule_is_rejected(self):
        for mode,kind in [('120000','blob'),('160000','commit')]:
            self.fixture();self.entries[1].update(mode=mode,type=kind)
            with patch.object(self.m,'remote_json',side_effect=self.remote):
                with self.assertRaisesRegex(ValueError,'symlinks or submodules'):self.snapshot()

    def test_cross_platform_unsafe_paths_are_rejected(self):
        for path in ('../escape','/absolute','C:/escape','file:stream','folder\\escape','NUL','AUX.txt','trailing.'):
            with self.subTest(path=path),self.assertRaises(ValueError):subset.relative(path)
        self.assertEqual(subset.relative('scripts/safe.py'),'scripts/safe.py')

    def test_case_colliding_files_are_rejected_for_windows_compatibility(self):
        self.fixture({'SKILL.md':b'a','skill.md':b'b'})
        with patch.object(self.m,'remote_json',side_effect=self.remote):
            with self.assertRaisesRegex(ValueError,'duplicate'):self.snapshot()

    def test_truncated_tree_is_not_treated_as_complete_comparison(self):
        self.fixture()
        def truncated(url):return {**self.remote(url),'truncated':True} if '/git/trees/' in url else self.remote(url)
        with patch.object(self.m,'remote_json',side_effect=truncated):
            with self.assertRaisesRegex(ValueError,'truncated'):self.snapshot()

    def test_truncated_repository_index_falls_back_to_complete_selected_tree(self):
        self.fixture()
        def truncated(url):
            value=self.remote(url)
            return {**value,'truncated':True} if '?recursive=1' in url else value
        with patch.object(self.m,'remote_json',side_effect=truncated):
            target,stats=self.snapshot()
        self.assertEqual(stats['file_count'],2)
        self.assertEqual((target/'new.txt').read_bytes(),b'new')

    def test_wrong_root_is_rejected_before_walking_unrelated_subdirectories(self):
        self.fixture();self.source['path']=''
        def truncated(url):
            value=self.remote(url)
            return {**value,'truncated':True} if '?recursive=1' in url else value
        with patch.object(self.m,'remote_json',side_effect=truncated):
            with self.assertRaisesRegex(ValueError,'Confirmed source path'):self.snapshot()
        self.assertFalse(any(url.endswith(self.tree) for url in self.calls))

    def test_permanent_errors_and_certificate_failures_do_not_fallback(self):
        for error in (urllib.error.HTTPError('https://api.github.com',404,'missing',{},None),urllib.error.URLError(ssl.SSLCertVerificationError('CERTIFICATE_VERIFY_FAILED'))):
            self.fixture()
            with patch.object(self.m,'remote_json',side_effect=error),patch.object(subset,'git_entries') as git:
                with self.assertRaises(type(error)):self.snapshot()
                git.assert_not_called()

    def test_transient_tree_error_uses_git_same_pinned_commit(self):
        self.fixture();self.m._github_api_state={}
        error=urllib.error.HTTPError('https://api.github.com',429,'rate limit',{'Retry-After':'900'},None)
        with patch.object(self.m,'remote_json',side_effect=error),patch.object(subset,'git_entries',return_value=self.entries) as tree,patch.object(subset,'git_blobs',return_value={subset.blob_hash(b'new'):b'new'}):
            _,stats=self.snapshot()
        self.assertEqual(tree.call_args.args[2],self.commit)
        self.assertEqual(stats['comparison_transport'],'git-selected')
        self.assertTrue(self.m._github_api_state['limited'])

    def test_archive_cooldown_is_honored_by_writes_but_not_unrelated_check_transport(self):
        row=self.fixture(); self.m._use_check_cache=True
        error=urllib.error.HTTPError('https://codeload.github.com/example/skills/zip/abc',429,'rate limit',{'Retry-After':'1800'},None)
        with patch.object(self.m,'remote',side_effect=error):self.m.check(row)
        with patch.object(self.m,'remote',side_effect=AssertionError('Archive retried')) as download:
            cached=self.m.check(row)
        self.assertTrue(cached['cache_hit']);download.assert_not_called()
        with patch.object(self.a,'github_json',side_effect=self.remote),patch.object(self.a,'download_github_archive',side_effect=AssertionError('Archive retried')):
            result=check_batches.run(self.m,[row['id']])
        self.assertEqual(result['run']['remote'],1)
        self.assertEqual(result['failed'],0)

    def test_git_commit_mismatch_is_rejected(self):
        self.fixture()
        def git(repo,args,**kwargs):return ('e'*40).encode() if args[0]=='rev-parse' else b''
        with patch.object(subset,'git_run',side_effect=git) as run:
            with self.assertRaisesRegex(ValueError,'Pinned Git commit'):subset.git_repository(self.m,self.source,self.commit,self.base)
        fetch=next(c.args[1] for c in run.call_args_list if c.args[1][0]=='fetch')
        self.assertEqual(fetch[-1],self.commit)
        self.assertIn('--filter=blob:none',fetch)

    def test_failed_initialization_retry_gets_a_clean_private_repository(self):
        self.fixture();folders=[]
        def git(repo,args,**kw):
            if args[0]=='init':folders.append(repo)
            if args[0]=='fetch' and len(folders)==1:raise TimeoutError('network timed out')
            return self.commit.encode() if args[0]=='rev-parse' else b''
        with patch.object(subset,'git_run',side_effect=git),patch.object(subset.time,'sleep'):
            with self.assertRaises(TimeoutError):subset.git_repository(self.m,self.source,self.commit,self.base)
            self.m._selected_git_repos.clear()
            repository=subset.git_repository(self.m,self.source,self.commit,self.base)
        self.assertEqual(len(folders),2);self.assertNotEqual(*folders)
        self.assertEqual(repository,folders[1])

    def test_concurrent_immutable_metadata_has_single_loader(self):
        subset._cache.clear();calls=[]
        def load():calls.append(1);time.sleep(.02);return {'valid':True}
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda _:subset.shared('fixture-key',load),range(4)))
        self.assertEqual(len(calls),1);self.assertTrue(all(r['valid'] for r in results))

    def test_source_path_and_integrity_have_actionable_error_codes(self):
        self.assertEqual(diagnostics.classify(ValueError('Confirmed source path does not contain SKILL.md'))[0],'remote_source_path')
        self.assertEqual(diagnostics.classify(ValueError('Remote Git blob integrity mismatch'))[0],'remote_integrity')

    def test_git_pack_rename_during_size_monitoring_is_not_a_failed_check(self):
        transient=SimpleNamespace(path='/fixture/pack/tmp_idx',is_symlink=lambda:False,is_dir=lambda **kw:False,
                                  stat=lambda **kw:(_ for _ in ()).throw(FileNotFoundError('Git renamed this file')))
        valid=SimpleNamespace(path='/fixture/pack/data',is_symlink=lambda:False,is_dir=lambda **kw:False,
                              stat=lambda **kw:SimpleNamespace(st_size=42))
        with patch.object(subset.os,'scandir') as scan:
            scan.return_value.__enter__.return_value=iter([transient,valid])
            self.assertEqual(subset.repository_size(self.base),42)

    def test_git_size_monitoring_does_not_hide_permission_errors(self):
        with patch.object(subset.os,'scandir',side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError):subset.repository_size(self.base)

    def test_git_timeout_terminates_its_process_group(self):
        if subset.os.name!='posix':self.skipTest('POSIX process group')
        process=SimpleNamespace(pid=12345,poll=lambda:None,wait=lambda:None,kill=lambda:None)
        with patch.object(subset.subprocess,'Popen',return_value=process),patch.object(subset.os,'killpg') as kill,patch.object(subset.time,'monotonic',side_effect=[0,40]):
            with self.assertRaises(TimeoutError):subset.git_run(self.base,['fetch'],timeout=35)
        kill.assert_called_once_with(12345,subset.signal.SIGKILL)

    def test_git_wrong_root_only_reads_metadata_and_does_not_fetch_blobs(self):
        self.fixture()
        with patch.object(subset,'git_repository',return_value=self.base),patch.object(subset,'git_run',return_value=b'') as git:
            with self.assertRaisesRegex(ValueError,'Confirmed source path'):
                subset.git_entries(self.m,self.source,self.commit,self.base)
        self.assertEqual(git.call_count,1)
        self.assertEqual(git.call_args.args[1][0],'ls-tree')

    def test_git_index_is_loaded_once_for_two_skills_in_the_same_commit(self):
        self.fixture()
        body=b'100644 blob '+self.entries[0]['sha'].encode()+b'\ta/SKILL.md\0'+b'100644 blob '+self.entries[0]['sha'].encode()+b'\tb/SKILL.md\0'
        with patch.object(subset,'git_repository',return_value=self.base),patch.object(subset,'git_run',return_value=body) as git:
            first=subset.git_entries(self.m,self.source,self.commit,self.base)
            second=subset.git_entries(self.m,{**self.source,'path':'b'},self.commit,self.base)
        self.assertEqual(git.call_count,1)
        self.assertEqual(first,second)

    def test_missing_path_retains_failure_with_unverified_candidates(self):
        self.fixture();self.source.update(path='old/a')
        with patch.object(self.m,'remote_json',side_effect=self.remote):
            with self.assertRaises(subset.SourcePathError) as caught:self.snapshot()
        issue=caught.exception.source_issue
        self.assertEqual(issue['declared_path'],'old/a')
        self.assertEqual(issue['candidate_paths'],['a'])
        self.assertFalse(issue['candidates_verified'])
        self.assertEqual(diagnostics.classify(caught.exception)[0],'remote_source_path')
        self.assertEqual(self.m.source(self.row_a)['path'],'a')

    def test_missing_source_diagnostics_persist_without_rebinding_or_success(self):
        row=self.fixture();self.m.bind_source(row,self.source['url'],'main','old/a')
        self.m._use_check_cache=True;self.m._targeted_checks=True
        with patch.object(self.m,'remote_json',side_effect=self.remote):checked=self.m.check(row)
        self.assertEqual(checked['status'],'check_failed')
        self.assertEqual(checked['code'],'remote_source_path')
        self.assertEqual(checked['source_issue']['candidate_paths'],['a'])
        self.assertEqual(self.m.source(row)['path'],'old/a')
        self.assertEqual(json.loads(self.m.state(row)['last_check'])['source_issue'],checked['source_issue'])

    def test_verified_unique_relocation_updates_binding_after_comparison(self):
        row=self.fixture()
        old={**self.source,'path':'old/a','evidence':'verified-package'}
        self.m.db.execute('update maintenance_items set source=? where id=?',(json.dumps(old),row['id']))
        self.m.db.commit()
        self.m._use_check_cache=True;self.m._targeted_checks=True
        with patch.object(self.m,'remote_json',side_effect=self.remote):
            checked=self.m.check(row)
            cached=self.m.check(row)
        self.assertNotEqual(checked['status'],'check_failed')
        self.assertEqual(checked['source_correction'],'a')
        self.assertEqual(self.m.source(row)['path'],'a')
        self.assertTrue(cached['cache_hit'])

    def test_verified_relocation_rejects_mismatched_skill_name(self):
        row=self.fixture({'SKILL.md':b'---\nname: other\n---\nwrong skill'})
        old={**self.source,'path':'old/a','evidence':'verified-package'}
        self.m.db.execute('update maintenance_items set source=? where id=?',(json.dumps(old),row['id']))
        self.m.db.commit()
        self.m._use_check_cache=True;self.m._targeted_checks=True
        with patch.object(self.m,'remote_json',side_effect=self.remote):checked=self.m.check(row)
        self.assertEqual(checked['code'],'remote_source_path')
        self.assertEqual(self.m.source(row)['path'],'old/a')

    def test_identical_installed_skill_resolves_repository_root(self):
        row=self.fixture()
        old={**self.source,'path':'','evidence':'installed-metadata'}
        self.m.db.execute('update maintenance_items set source=? where id=?',(json.dumps(old),row['id']))
        self.m.db.commit()
        self.m._use_check_cache=True;self.m._targeted_checks=True
        with patch.object(self.m,'remote_json',side_effect=self.remote):checked=self.m.check(row)
        self.assertNotEqual(checked['status'],'check_failed')
        self.assertEqual(self.m.source(row)['path'],'a')

    def test_changed_installed_skill_does_not_auto_bind_repository_root(self):
        row=self.fixture({'SKILL.md':b'---\nname: a\n---\nchanged remotely'})
        old={**self.source,'path':'','evidence':'installed-metadata'}
        self.m.db.execute('update maintenance_items set source=? where id=?',(json.dumps(old),row['id']))
        self.m.db.commit()
        self.m._targeted_checks=True;self.m._use_check_cache=True
        with patch.object(self.m,'remote_json',side_effect=self.remote):checked=self.m.check(row)
        self.assertEqual(checked['code'],'remote_source_path')
        self.assertEqual(self.m.source(row)['path'],'')

    def test_batch_fetch_uses_two_commands_for_multiple_verified_blobs(self):
        self.fixture()
        ids=[e['sha'] for e in self.entries]
        framed=b''.join(i.encode()+b' blob '+str(len(body)).encode()+b'\n'+body+b'\n' for i,body in zip(ids,self.files.values()))
        with patch.object(subset,'git_repository',return_value=self.base),patch.object(subset,'git_run',return_value=framed) as git:
            actual=subset.git_blobs(self.m,self.source,self.commit,self.base,ids)
        self.assertEqual(actual,dict(zip(ids,self.files.values())))
        self.assertEqual(git.call_count,2)
        self.assertIn('--stdin',git.call_args_list[0].args[1])
        self.assertEqual(git.call_args_list[0].kwargs['input_data'],('\n'.join(ids)+'\n').encode())

    def test_batch_framing_truncation_extra_data_and_wrong_hash_are_rejected(self):
        self.fixture();i=self.entries[1]['sha']
        valid=i.encode()+b' blob 3\nnew\n'
        for data in (valid[:-1],valid+b'extra',valid.replace(b'new',b'bad'),valid.replace(b'blob',b'tree'),b'bad missing\n'):
            with self.subTest(data=data),patch.object(subset,'git_repository',return_value=self.base),patch.object(subset,'git_run',return_value=data):
                with self.assertRaisesRegex(ValueError,'integrity'):subset.git_blobs(self.m,self.source,self.commit,self.base,[i])

    def test_git_transient_retry_has_one_shared_deadline(self):
        with patch.object(subset,'git_run',side_effect=[ConnectionResetError('reset'),b'valid']) as git,patch.object(subset.time,'sleep'):
            self.assertEqual(subset.git_request(self.base,['fetch','origin','a'*40]),b'valid')
        self.assertEqual(git.call_count,2)
        self.assertEqual(git.call_args_list[0].kwargs['timeout'],12)
        self.assertLessEqual(git.call_args_list[1].kwargs['timeout'],35)

    def test_git_retry_never_bypasses_permanent_or_integrity_failure(self):
        for error in (ValueError('Remote Git blob integrity mismatch'),urllib.error.HTTPError('https://github.com',404,'missing',{},None),urllib.error.URLError(ssl.SSLCertVerificationError('CERTIFICATE_VERIFY_FAILED'))):
            with self.subTest(error=error),patch.object(subset,'git_run',side_effect=error) as git:
                with self.assertRaises(type(error)):subset.git_request(self.base,['fetch','origin','a'*40])
                self.assertEqual(git.call_count,1)

    def test_large_metadata_index_is_shared_under_aggregate_memory_limit(self):
        subset._cache.clear();calls=[]
        def load():calls.append(1);return {'paths':'x'*1_200_000}
        subset.shared('large-metadata',load);subset.shared('large-metadata',load)
        self.assertEqual(len(calls),1)
        for n in range(20):subset.shared('bounded-'+str(n),load)
        self.assertLessEqual(sum(v[2] for v in subset._cache.values()),16_000_000)

    def test_ref_lookup_retries_bounded_reads_with_tls_and_no_permanent_error_retry(self):
        import github_transport
        with patch.object(subset,'git_run',side_effect=[ConnectionResetError('reset'),b'a'*40+b'\tHEAD\n']) as git,patch.object(subset.time,'sleep'):
            self.assertTrue(github_transport.lookup('https://github.com/example/skills.git',['HEAD']))
        self.assertEqual(git.call_count,2)
        self.assertLessEqual(git.call_args_list[0].kwargs['timeout'],15)
        self.assertLessEqual(git.call_args_list[1].kwargs['timeout'],30)

    def test_git_command_preserves_certificate_and_protocol_protection(self):
        process=SimpleNamespace(returncode=0,pid=123,poll=lambda:0,wait=lambda:None)
        with patch.object(subset.subprocess,'Popen',return_value=process) as run:
            subset.git_run(self.base,['ls-remote','https://github.com/example/skills.git','HEAD'])
        command=run.call_args.args[0]
        self.assertIn('http.sslVerify=true',command)
        self.assertIn('protocol.file.allow=never',command)
        self.assertNotIn('GIT_SSL_NO_VERIFY',run.call_args.kwargs['env'])

    def test_git_missing_repository_is_not_a_generic_network_failure(self):
        def launch(command,**kwargs):
            kwargs['stderr'].write(b'remote: Repository not found.\nfatal: repository unavailable\n')
            return SimpleNamespace(returncode=128,pid=123,poll=lambda:128,wait=lambda:None)
        with patch.object(subset.subprocess,'Popen',side_effect=launch):
            with self.assertRaises(urllib.error.HTTPError) as caught:subset.git_run(self.base,['ls-remote','https://github.com/example/skills.git','HEAD'])
        self.assertEqual(caught.exception.code,404)
        self.assertEqual(diagnostics.classify(caught.exception)[0],'remote_not_found')

    def test_tls_disconnect_is_retryable_not_misclassified_as_certificate_failure(self):
        def launch(command,**kwargs):
            kwargs['stderr'].write(b'OpenSSL SSL_read: Connection reset by peer, errno 54\n')
            return SimpleNamespace(returncode=128,pid=123,poll=lambda:128,wait=lambda:None)
        with patch.object(subset.subprocess,'Popen',side_effect=launch):
            with self.assertRaises(ConnectionError) as caught:subset.git_run(self.base,['fetch','origin','a'*40])
        self.assertTrue(github_transport.can_fallback(caught.exception))
        self.assertEqual(diagnostics.classify(caught.exception)[0],'remote_unavailable')

    def test_certificate_verification_errors_remain_nonretryable_with_original_reason(self):
        for message in ('SSL certificate problem: unable to get local issuer certificate',
                        'server certificate verification failed', 'schannel: SEC_E_UNTRUSTED_ROOT'):
            def launch(command,**kwargs):
                kwargs['stderr'].write(message.encode())
                return SimpleNamespace(returncode=128,pid=123,poll=lambda:128,wait=lambda:None)
            with self.subTest(message=message),patch.object(subset.subprocess,'Popen',side_effect=launch):
                with self.assertRaises(urllib.error.URLError) as caught:subset.git_run(self.base,['fetch','origin','a'*40])
            self.assertFalse(github_transport.can_fallback(caught.exception))
            self.assertIn(message,str(caught.exception))
            self.assertEqual(diagnostics.classify(caught.exception)[0],'remote_certificate')

    def test_single_item_check_preserves_saved_rest_cooldown(self):
        row=self.fixture();deadline=(dt.datetime.now(dt.timezone.utc)+dt.timedelta(minutes=30)).isoformat()
        saved={'id':row['id'],'status':'check_failed','checked_at':core.stamp(),'code':'remote_unavailable','api_retry_at':deadline}
        self.m.db.execute('update maintenance_items set last_check=? where id=?',(json.dumps(saved),row['id']))
        self.m.db.commit();self.m._use_check_cache=True;self.m._targeted_checks=True
        def stage(manager,source,commit,folder,local,**kw):
            self.assertTrue(kw['use_git']);return self.local,{}
        with patch.object(self.m,'remote_json',side_effect=AssertionError('REST cooldown bypass')),patch('github_transport.resolve',return_value=(self.commit,'main')),patch.object(subset,'snapshot',side_effect=stage):
            checked=self.m.check(row)
        self.assertNotEqual(checked['status'],'check_failed')
        self.assertEqual(checked['api_retry_at'],deadline)

    def test_local_change_during_comparison_cannot_leave_success_in_saved_result(self):
        row=self.fixture();self.m._use_check_cache=True
        def remote(row):
            (self.local/'SKILL.md').write_text('externally changed during I/O')
            return {'id':row['id'],'status':'latest','checked_at':core.stamp()}
        with patch.object(self.m,'_check_remote',side_effect=remote):checked=self.m.check(row)
        self.assertEqual(checked['status'],'stale')
        saved=json.loads(self.m.state(row)['last_check'])
        self.assertEqual(saved['status'],'stale')
        self.assertEqual(self.m.db.execute('select count(*) from maintenance_check_cache').fetchone()[0],0)

    def test_chunk_prefetch_batches_only_missing_verified_selected_objects(self):
        row=self.fixture();self.m._github_api_state={'limited':True}
        ids=[self.entries[1]['sha']]
        with patch.object(github_transport,'resolve',return_value=(self.commit,'main')), \
                patch.object(subset,'git_entries',return_value=self.entries), \
                patch.object(subset,'git_run',return_value=b''), \
                patch.object(subset,'git_blobs',return_value={ids[0]:b'new'}) as blobs:
            stats=subset.prefetch(self.m,[row,row])
        self.assertEqual(stats['bundles'],1)
        self.assertEqual(stats['objects'],1)
        self.assertEqual(blobs.call_args.args[-1],ids)
        self.assertEqual(self.m._prefetched_git_blobs[(self.source['url'],self.commit)],{ids[0]:b'new'})
        self.assertFalse(blobs.call_args.args[0]._selected_git_repos[(self.source['url'],self.commit)].exists())

    def test_prefetched_snapshot_still_revalidates_hash_and_pinned_commit(self):
        self.fixture();i=self.entries[1]['sha']
        self.m._prefetched_git_blobs={(self.source['url'],self.commit):{i:b'new'}}
        with patch.object(subset,'git_entries',return_value=self.entries),patch.object(subset,'git_blobs',return_value={}) as blobs:
            target,_=self.snapshot(use_git=True)
        self.assertEqual((target/'new.txt').read_bytes(),b'new')
        self.assertEqual(blobs.call_args.args[-1],[])
        subset._cache.clear()
        self.m._prefetched_git_blobs[(self.source['url'],self.commit)][i]=b'bad'
        with patch.object(subset,'git_entries',return_value=self.entries),patch.object(subset,'git_blobs',return_value={}):
            with self.assertRaisesRegex(ValueError,'integrity'):self.snapshot(use_git=True)
        self.m._prefetched_git_blobs={(self.source['url'],'f'*40):{i:b'new'}}
        with patch.object(subset,'git_entries',return_value=self.entries),patch.object(subset,'git_blobs',return_value={i:b'new'}) as blobs:
            self.snapshot(use_git=True)
        self.assertEqual(blobs.call_args.args[-1],[i])

    def test_prefetch_failure_is_isolated_and_ordinary_checks_still_report_failure(self):
        row=self.fixture();self.m._github_api_state={'limited':True}
        self.m._use_check_cache=True;self.m._targeted_checks=True
        with patch.object(github_transport,'resolve',return_value=(self.commit,'main')), \
                patch.object(subset,'git_entries',return_value=self.entries), \
                patch.object(subset,'git_run',return_value=b''), \
                patch.object(subset,'git_blobs',side_effect=ConnectionResetError('reset')):
            stats=subset.prefetch(self.m,[row,row])
            checked=self.m.check(row)
        self.assertEqual(stats['fallbacks'],1)
        self.assertEqual(self.m._prefetched_git_blobs,{})
        self.assertEqual(checked['status'],'check_failed')
        self.assertEqual(checked['code'],'remote_unavailable')

    def test_prefetch_invalid_manifest_never_downloads_or_changes_binding(self):
        row=self.fixture();self.m._github_api_state={'limited':True}
        for mode in ('120000','160000'):
            self.entries[1]['mode']=mode
            with patch.object(github_transport,'resolve',return_value=(self.commit,'main')), \
                    patch.object(subset,'git_entries',return_value=self.entries), \
                    patch.object(subset,'git_blobs') as blobs:
                stats=subset.prefetch(self.m,[row,row])
            blobs.assert_not_called();self.assertEqual(stats['bundles'],0)
        self.assertEqual(self.m.source(row),self.source)

    def test_prefetch_aggregate_limit_falls_back_without_installation_writes(self):
        row=self.fixture();self.m._github_api_state={'limited':True}
        with patch.object(github_transport,'resolve',return_value=(self.commit,'main')), \
                patch.object(subset,'git_entries',return_value=self.entries), \
                patch.object(subset,'git_run',return_value=b''), \
                patch.object(subset,'git_blobs',return_value={'x':b'x'*16_000_001}):
            stats=subset.prefetch(self.m,[row,row])
        self.assertEqual(stats['fallbacks'],1);self.assertEqual(self.m._prefetched_git_blobs,{})
        self.assertFalse((self.local/'new.txt').exists())

    def test_failed_prefetch_does_not_poison_normal_repository_or_success(self):
        row=self.fixture();self.m._github_api_state={'limited':True}
        self.m._selected_git_repos={};self.m._use_check_cache=True;self.m._targeted_checks=True
        i=self.entries[1]['sha']
        with patch.object(github_transport,'resolve',return_value=(self.commit,'main')), \
                patch.object(subset,'git_entries',return_value=self.entries), \
                patch.object(subset,'git_run',return_value=b''), \
                patch.object(subset,'git_blobs',side_effect=[ValueError('Remote response exceeds size limit'),{i:b'new'}]):
            stats=subset.prefetch(self.m,[row,row])
            checked=self.m.check(row)
        self.assertEqual(stats['fallbacks'],1)
        self.assertNotEqual(checked['status'],'check_failed')
        self.assertEqual(self.m._selected_git_repos,{})
        self.assertFalse((self.local/'new.txt').exists())

    def test_prefetch_does_not_bypass_available_rest_transport(self):
        row=self.fixture();self.m._github_api_state={'limited':False}
        with patch.object(github_transport,'resolve') as resolve,patch.object(subset,'git_entries') as entries:
            stats=subset.prefetch(self.m,[row,row])
        resolve.assert_not_called();entries.assert_not_called()
        self.assertEqual(stats['bundles'],0)

    def test_git_final_size_limit_remains_enforced_for_fast_commands(self):
        process=SimpleNamespace(returncode=0,pid=123,poll=lambda:0,wait=lambda:None)
        with patch.object(subset.subprocess,'Popen',return_value=process), \
                patch.object(subset,'repository_size',return_value=subset.MAX_TRANSFER+1):
            with self.assertRaisesRegex(ValueError,'size limit'):subset.git_run(self.base,['fetch'])

    def test_two_skills_share_download_but_keep_independent_differences_and_versions(self):
        row=self.fixture();other=self.row('b')
        self.m.bind_source(other,self.source['url'],'main','b')
        (self.local/'personal.txt').write_text('local-only change')
        files={name:f'---\nname: {name}\nversion: 2.0\n---\nremote'.encode() for name in ('a','b')}
        entries={name:[{'path':'SKILL.md','sha':subset.blob_hash(body),'mode':'100644','type':'blob'}] for name,body in files.items()}
        bodies={subset.blob_hash(body):body for body in files.values()}
        self.m._github_api_state={'limited':True};self.m._use_check_cache=True;self.m._targeted_checks=True
        with patch.object(github_transport,'resolve',return_value=(self.commit,'main')), \
                patch.object(subset,'git_entries',side_effect=lambda m,s,*a:entries[s['path']]), \
                patch.object(subset,'git_run',return_value=b''), \
                patch.object(subset,'git_blobs',side_effect=lambda m,s,c,f,ids:{i:bodies[i] for i in ids}) as blobs:
            stats=subset.prefetch(self.m,[row,other])
            first=self.m.check(row);second=self.m.check(other)
        self.assertEqual(stats['bundles'],1)
        self.assertEqual(len(blobs.call_args_list[0].args[-1]),2)
        self.assertTrue(all(not call.args[-1] for call in blobs.call_args_list[1:]))
        self.assertEqual(first['status'],'update_available');self.assertEqual(second['status'],'update_available')
        self.assertIn('personal.txt',first['differences']['removed'])
        self.assertNotIn('personal.txt',second['differences']['removed'])
        self.assertEqual((self.local/'personal.txt').read_text(),'local-only change')
