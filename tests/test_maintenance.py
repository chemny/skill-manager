import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import maintenance as core

class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.home = self.base / 'state'
        self.roots = self.base / 'skills'; self.roots.mkdir()
        os.environ['ASM_HOME'] = str(self.home)
        spec = importlib.util.spec_from_file_location('asm_test', SCRIPTS / 'agent_skill_manager.py')
        self.a = importlib.util.module_from_spec(spec); sys.modules['asm_test'] = self.a
        spec.loader.exec_module(self.a)
        self.a.save_config({'scan_schema':2, 'platforms': {p: {'enabled': p=='shared', 'roots': [str(self.roots)] if p=='shared' else []} for p in self.a.DEFAULT_CONFIG['platforms']}})
        self.m = core.Manager(self.a)
        self.skill('a','one'); self.skill('b','two')
        self.m.scan()

    def tearDown(self):
        self.m.db.close(); self.tmp.cleanup()
        os.environ.pop('ASM_HOME',None)

    def skill(self, name, text, display=None):
        p = self.roots / name; p.mkdir(exist_ok=True)
        (p/'SKILL.md').write_text(f'---\nname: {display or name}\nversion: 1.0\n---\n{text}')
        return p

    def row(self, name): return self.m.select(name)

    def test_identical_copies_have_content_identity_but_changed_assets_do_not(self):
        copy = self.skill('copy', 'one', 'a')
        self.m.scan()
        items = [i for i in self.m.inventory()['items'] if i['name'] == 'a']
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]['content_fingerprint'], items[1]['content_fingerprint'])
        (copy/'asset.txt').write_text('different')
        items = [i for i in self.m.inventory()['items'] if i['name'] == 'a']
        self.assertNotEqual(items[0].get('content_fingerprint'), items[1].get('content_fingerprint'))

    def test_hidden_backup_skills_are_not_active_installations(self):
        backup = self.roots/'.cmm-skill-backups'/'old'/'a'
        backup.mkdir(parents=True)
        (backup/'SKILL.md').write_text('---\nname: a\n---\nbackup')
        self.m.scan()
        self.assertNotIn(str(backup), [i['path'] for i in self.m.inventory()['items']])

    def test_ambiguous_name_and_shared_link(self):
        self.skill('other','other','a'); self.m.scan()
        with self.assertRaisesRegex(ValueError,'Ambiguous'): self.row('a')
        p=self.roots/'ref'; p.symlink_to(self.roots/'b',target_is_directory=True)
        broken=self.roots/'broken'; broken.symlink_to(self.roots/'absent')
        self.m.scan()
        inv=self.m.inventory()
        self.assertFalse(inv['shared_targets'])
        self.assertTrue(next(i for i in inv['items'] if i['path']==str(p))['multiple_references'])
        ref=self.m.select(str(p))
        result=self.m.apply(self.m.plan('delete',ref)['id'])
        self.assertFalse(core.exists(p)); self.assertTrue((self.roots/'b'/'SKILL.md').exists())
        self.assertEqual(result['net_freed_bytes_estimate'],0)
        bid=result['backups'][0]['id']
        self.assertTrue(self.m.verify_backup(bid)['valid'])
        self.m.apply(self.m.plan('restore',backup_id=bid)['id'])
        self.assertTrue(p.is_symlink())
        self.assertEqual(os.readlink(p),str(self.roots/'b'))

    def test_sync_conflict_extras_stale_plan(self):
        (self.roots/'b'/'my-notes.txt').write_text('preserve')
        with self.assertRaisesRegex(ValueError,'conflicts'): self.m.plan('sync',self.row('b'),self.row('a'))
        plan=self.m.plan('sync',self.row('b'),self.row('a'),allow_changes=True)
        (self.roots/'b'/'my-notes.txt').write_text('changed')
        with self.assertRaisesRegex(ValueError,'Target changed'): self.m.apply(plan['id'])
        plan=self.m.plan('sync',self.row('b'),self.row('a'),allow_changes=True)
        self.m.apply(plan['id'])
        self.assertEqual((self.roots/'b'/'my-notes.txt').read_text(),'changed')
        self.assertEqual((self.roots/'b'/'SKILL.md').read_text(),(self.roots/'a'/'SKILL.md').read_text())
        with self.assertRaisesRegex(ValueError,'already applied'): self.m.apply(plan['id'])

    def test_backup_restore_tamper_and_purge(self):
        row=self.row('a'); bid=self.m.backup(row)['id']
        self.assertTrue(self.m.verify_backup(bid)['valid'])
        with self.assertRaisesRegex(ValueError,'conflict'): self.m.plan('restore',backup_id=bid)
        old=(self.roots/'a'/'SKILL.md').read_text()
        (self.roots/'a'/'SKILL.md').write_text('edit')
        plan=self.m.plan('restore',backup_id=bid,allow_changes=True)
        self.m.apply(plan['id'])
        self.assertEqual((self.roots/'a'/'SKILL.md').read_text(),old)
        container,_=self.m.backup_record(bid)
        (container/'content'/'SKILL.md').write_text('tampered')
        self.assertFalse(self.m.verify_backup(bid)['valid'])
        with self.assertRaisesRegex(ValueError,'damaged'): self.m.plan('restore',backup_id=bid)
        p=self.m.plan('purge',backup_id=bid)
        self.m.apply(p['id']); self.assertFalse(container.exists())

    def test_protection_and_identity_after_move(self):
        row=self.row('a'); rid=row['id']
        self.m.protect(row,True)
        with self.assertRaisesRegex(ValueError,'protected'): self.m.plan('delete',row)
        self.m.protect(row,False)
        self.m.r.record_usage_event(rid,'shared',core.stamp(),'manual','high','test','read')
        self.m.db.commit()
        p=self.m.plan('deactivate',row); self.m.apply(p['id']); self.m.scan(); self.a.refresh_registry(self.m.r)
        after=self.m.select(rid)
        self.assertEqual(after['id'],rid); self.assertEqual(after['status'],'inactive')
        self.assertEqual(self.m.db.execute('select count(*) from usage_events where capability_id=?',(rid,)).fetchone()[0],1)
        self.m.apply(self.m.plan('activate',after)['id']); self.m.scan()
        self.assertEqual(self.row('a')['id'],rid)

    def test_hidden_backups_are_not_installed_skills(self):
        hidden=self.roots/'.old-backups'/'a'; hidden.mkdir(parents=True)
        (hidden/'SKILL.md').write_text('---\nname: a\n---\nbackup')
        self.m.scan()
        self.assertEqual(self.row('a')['path'],str(self.roots/'a'))
        self.assertNotIn(str(hidden),[r['path'] for r in self.m.inventory()['items']])

    def test_partial_scan_preserves_registry(self):
        original=Path.iterdir
        def fail(p):
            if p==self.roots: raise PermissionError('no access')
            return original(p)
        with patch.object(Path,'iterdir',fail):
            out=self.m.scan()
        self.assertTrue(any(r['result']=='incomplete' for r in out['roots']))
        self.assertEqual(self.row('a')['health'],'ok')
        self.assertEqual(self.m.inventory()['items'][0]['usage_status'],'unknown')

    def test_update_pins_remote_and_preserves_local(self):
        row=self.row('a'); self.m.bind_source(row,'https://github.com/example/repo',path='skills/a')
        remote=self.base/'remote'; remote.mkdir()
        (remote/'SKILL.md').write_text('---\nname: a\nversion: 2.0\n---\nnew')
        (remote/'new.txt').write_text('new')
        (self.roots/'a'/'extra.txt').write_text('local extra')
        def fetch(row,folder): return remote, {'commit':'abc','url':'https://github.com/example/repo','path':'skills/a','ref':'main'}
        with patch.object(self.m,'remote',fetch):
            check=self.m.check(row)
            self.assertEqual(check['status'],'update_available')
            self.assertEqual(check['update_decision'],'review')
            self.assertFalse(check['baseline_known'])
            with self.assertRaisesRegex(ValueError,'conflicts'): self.m.plan('update',row)
            plan=self.m.plan('update',row,allow_changes=True)
        (remote/'SKILL.md').write_text('remote changed after preview')
        self.m.apply(plan['id'])
        self.assertIn('version: 2.0',(self.roots/'a'/'SKILL.md').read_text())
        self.assertEqual((self.roots/'a'/'extra.txt').read_text(),'local extra')
        with patch.object(self.m,'remote',side_effect=OSError('offline')):
            self.assertEqual(self.m.check(row)['status'],'check_failed')

    def test_source_change_and_stage_tamper(self):
        p=self.m.plan('sync',self.row('b'),self.row('a'),allow_changes=True)
        (self.roots/'a'/'SKILL.md').write_text('changed source')
        with self.assertRaisesRegex(ValueError,'Source changed'): self.m.apply(p['id'])
        p=self.m.plan('sync',self.row('b'),self.row('a'),allow_changes=True)
        (self.home/'plans'/p['id']/'content'/'SKILL.md').write_text('stage tamper')
        with self.assertRaisesRegex(ValueError,'Staged content changed'): self.m.apply(p['id'])

    def test_overlay_does_not_follow_nested_symlink(self):
        outside=self.base/'outside'; outside.mkdir(); (outside/'keep').write_text('untouched')
        (self.roots/'b'/'nested').symlink_to(outside,target_is_directory=True)
        (self.roots/'a'/'nested').mkdir(); (self.roots/'a'/'nested'/'keep').write_text('incoming')
        with self.assertRaisesRegex(ValueError,'symlink'):
            self.m.plan('sync',self.row('b'),self.row('a'),allow_changes=True)
        self.assertEqual((outside/'keep').read_text(),'untouched')

    def test_journal_recovery(self):
        target=self.roots/'a'; before=core.manifest(target)
        oid='op-interrupted'; hold=target.with_name('.asm-hold-'+oid)
        pid='plan-interrupted'
        os.replace(target,hold)
        self.m.operation(oid,'update','running',{'target':str(target),'hold':str(hold),'plan_id':pid,'expected_after':'different','originally_present':True})
        self.m.db.execute('insert into maintenance_plans values (?,?,?,?)',(pid,core.stamp(),'running','{}')); self.m.db.commit()
        result=self.m.recover(oid)
        self.assertEqual(result['state'],'rolled_back')
        self.assertEqual(core.manifest(target),before)

    def test_failed_swap_rolls_back(self):
        row=self.row('b'); before=core.manifest(Path(row['path']))
        plan=self.m.plan('sync',row,self.row('a'),allow_changes=True)
        original=os.replace
        def fail(src,dst):
            if Path(src).name.startswith('.asm-stage-'): raise OSError('simulated install failure')
            return original(src,dst)
        with patch.object(os,'replace',fail):
            with self.assertRaisesRegex(ValueError,'failed'): self.m.apply(plan['id'])
        self.assertEqual(core.manifest(Path(row['path'])),before)
        self.assertEqual(self.m.history()['operations'][0]['state'],'failed')

    def test_external_move_keeps_identity(self):
        row=self.row('a'); original=self.roots/'a'; destination=self.roots/'renamed'
        os.replace(original,destination); self.m.scan()
        self.assertEqual(self.m.select(str(destination))['id'],row['id'])

    def test_shared_root_cannot_be_managed_as_one_agent(self):
        alias=self.base/'claude-skills'; alias.symlink_to(self.roots,target_is_directory=True)
        config=self.a.load_config(); config['platforms']['claude_code']={'enabled':True,'roots':[str(alias)]}; self.a.save_config(config)
        self.m.scan(); row=self.m.select(str(alias/'a'))
        with self.assertRaisesRegex(ValueError,'shared symlink'): self.m.plan('deactivate',row)
        with self.assertRaisesRegex(ValueError,'shared symlink'): self.m.plan('delete',row)
        actual=self.m.select(str(self.roots/'a'))
        self.assertEqual(len(self.m.plan('delete',actual)['affected_references']),2)

    def test_incremental_logs_keep_old_events_and_skip_undated(self):
        row=self.row('a'); log=self.base/'session.jsonl'
        event={'timestamp':core.stamp(),'type':'tool_call','name':'read_file','path':str(self.roots/'a'/'SKILL.md')}
        undated={'type':'tool_call','name':'read_file','path':str(self.roots/'a'/'SKILL.md')}
        log.write_text(json.dumps(event)+'\n'+json.dumps(undated)+'\n')
        with patch.object(self.a,'usage_log_files',return_value=[log]):
            first=self.a.import_usage_from_logs(self.m.r)
            second=self.a.import_usage_from_logs(self.m.r)
        self.assertEqual(first['inserted'],1); self.assertEqual(first['undated_skipped'],1)
        self.assertEqual(second['lines'],0)
        self.assertEqual(self.m.db.execute('select count(*) from usage_events').fetchone()[0],1)
        # A new import window doesn't delete old evidence.
        with patch.object(self.a,'usage_log_files',return_value=[]):
            self.a.import_usage_from_logs(self.m.r,days=1)
        self.assertEqual(self.m.db.execute('select count(*) from usage_events').fetchone()[0],1)

    def test_http_access_and_common_core(self):
        import urllib.request, urllib.error, subprocess, time
        env={**os.environ,'ASM_HOME':str(self.home)}
        child=subprocess.Popen([sys.executable,str(SCRIPTS/'agent_skill_manager.py'),'web','--port','0'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        try:
            record=None
            for _ in range(50):
                try:
                    record=json.loads((self.home/'service.json').read_text()); core.service_request(record); break
                except (OSError,ValueError): time.sleep(.05)
            self.assertIsNotNone(record)
            base=record['url']
            page=urllib.request.urlopen(base+'manage').read().decode()
            self.assertNotIn('__ASM_TOKEN__',page)
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(urllib.request.Request(base+'api/maintenance',data=b'{"action":"inventory"}',headers={'Content-Type':'application/json'}))
            self.assertEqual(error.exception.code,403)
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(urllib.request.Request(base+'api/state',headers={'Host':'evil.example'}))
            self.assertEqual(error.exception.code,403)
            req=urllib.request.Request(base+'api/maintenance',data=json.dumps({'action':'preview','operation':'delete','target':self.row('a')['id']}).encode(),headers={'X-ASM-Token':record['token'],'Content-Type':'application/json'})
            plan=json.load(urllib.request.urlopen(req))
            self.assertEqual(plan['action'],'delete'); self.assertTrue((self.roots/'a').exists())
            def api(payload):
                req=urllib.request.Request(base+'api/maintenance',data=json.dumps(payload).encode(),headers={'X-ASM-Token':record['token'],'Content-Type':'application/json'})
                return json.load(urllib.request.urlopen(req))
            # Stale jobs and malformed payloads return structured diagnostics,
            # rather than dropping the HTTP connection or leaking raw errors.
            with self.assertRaises(urllib.error.HTTPError) as stale:
                api({'action':'job-status','id':'missing-job'})
            expired = json.load(stale.exception)
            self.assertEqual(expired['code'], 'task_expired')
            self.assertTrue(expired['error_id'].startswith('err-'))
            bad = urllib.request.Request(base+'api/maintenance', data=b'[]', headers={'X-ASM-Token':record['token'],'Content-Type':'application/json'})
            with self.assertRaises(urllib.error.HTTPError) as malformed:
                urllib.request.urlopen(bad)
            self.assertEqual(json.load(malformed.exception)['code'], 'invalid_request')
            def task(payload):
                job=api({'action':'job-start','request':payload})
                for _ in range(100):
                    job=api({'action':'job-status','id':job['id']})
                    if job['state']!='running': return job
                    time.sleep(.02)
                self.fail('HTTP task did not finish')
            with self.assertRaises(urllib.error.HTTPError):
                api({'action':'job-start','request':{'action':'apply','id':plan['id']}})
            self.assertTrue((self.roots/'a').exists())
            # The browser submits a reviewed plan, not a fresh unchecked write.
            job=task({'action':'apply','id':plan['id'],'confirmed':True})
            self.assertEqual(job['state'],'succeeded')
            self.assertFalse((self.roots/'a').exists())
            bid=job['result']['backups'][0]['id']
            self.assertTrue(api({'action':'verify-backup','id':bid})['valid'])
            previews=task({'action':'preview','operation':'restore','id':bid})
            self.assertEqual(previews['state'],'succeeded')
            restored=task({'action':'apply','id':previews['result']['id'],'confirmed':True})
            self.assertEqual(restored['result']['state'],'succeeded')
            self.assertTrue((self.roots/'a'/'SKILL.md').exists())
            self.assertEqual(api({'action':'get-plan','id':plan['id']})['state'],'applied')
            history=task({'action':'history'})['result']['operations']
            self.assertEqual({o['action'] for o in history},{'delete','restore'})
            core.service_request(record,True); child.wait(timeout=5)
            self.assertEqual(child.returncode,0)
        finally:
            if child.poll() is None: child.terminate(); child.wait(timeout=5)
            child.stderr.close()

    def test_web_inventory_shared_size_and_backup_summary(self):
        alias=self.base/'claude'; alias.symlink_to(self.roots,target_is_directory=True)
        config=self.a.load_config();config['platforms']['claude_code']={'enabled':True,'roots':[str(alias)]};self.a.save_config(config)
        self.m.scan();row=self.row(str(self.roots/'a'))
        backup=self.m.backup(row)
        inv=self.m.inventory();actual=next(i for i in inv['items'] if i['id']==row['id'])
        reference=next(i for i in inv['items'] if i['path']==str(alias/'a'))
        self.assertFalse(actual['shared']);self.assertEqual(actual['known_agents'],['claude_code'])
        self.assertEqual(actual['target_space'],reference['target_space'])
        self.assertEqual(actual['backup_summary']['latest']['id'],backup['id'])
        self.assertEqual(inv['last_scan']['count'],4)
        # A fast listing must not falsely claim current verification after tampering.
        container,_=self.m.backup_record(backup['id'])
        (container/'content'/'SKILL.md').write_text('damaged')
        fast=core.api_dispatch(self.a,{'action':'backups'})['backups'][0]
        self.assertIsNone(fast['verified'])
        self.assertFalse(core.api_dispatch(self.a,{'action':'verify-backup','id':backup['id']})['valid'])

    def test_web_jobs_busy_reload_and_confirmation(self):
        import threading, time
        import web_jobs
        jobs=web_jobs.Jobs(self.a);entered=threading.Event();release=threading.Event()
        def delayed(m,payload):
            entered.set(); release.wait(timeout=3);return {'count':2}
        with patch.object(core,'dispatch',delayed):
            job=jobs.start({'action':'scan'})
            self.assertTrue(entered.wait(timeout=2))
            self.assertEqual(jobs.snapshot()['active']['id'],job['id'])
            with self.assertRaises(web_jobs.BusyError): jobs.start({'action':'scan'})
            with self.assertRaisesRegex(ValueError,'Confirmation'): jobs.start({'action':'apply','id':'plan','confirmed':'false'})
            release.set()
            for _ in range(100):
                if jobs.snapshot(job['id'])['state']!='running': break
                time.sleep(.01)
        self.assertEqual(jobs.snapshot(job['id'])['result']['count'],2)
        self.assertIsNone(jobs.snapshot()['active'])
        # Completed jobs remain accessible to a reloaded page.
        self.assertEqual(jobs.snapshot(job['id'])['state'],'succeeded')
        with self.assertRaisesRegex(ValueError,'service may have restarted'):jobs.snapshot('missing')

    def test_web_update_checks_deduplicate_shared_targets(self):
        import time, web_jobs
        alias=self.base/'claude';alias.symlink_to(self.roots,target_is_directory=True)
        config=self.a.load_config();config['platforms']['claude_code']={'enabled':True,'roots':[str(alias)]};self.a.save_config(config)
        self.m.scan();jobs=web_jobs.Jobs(self.a)
        ids=[i['id'] for i in self.m.inventory()['items']]
        job=jobs.start({'action':'check-many','ids':ids})
        for _ in range(200):
            current=jobs.snapshot(job['id'])
            if current['state']!='running':break
            time.sleep(.01)
        self.assertEqual(current['state'],'succeeded')
        self.assertEqual(len(current['result']['checks']),0)
        self.assertEqual(len(current['result']['skipped']),2)
        self.assertTrue(all(c['reason']=='缺少可靠的更新来源' for c in current['result']['skipped']))
        self.assertEqual(current['completed'],current['total'])

    def test_successful_mutation_invalidates_cached_update(self):
        row=self.row('b');self.m.state(row)
        self.m.db.execute('update maintenance_items set last_check=? where id=?',(json.dumps({'status':'latest'}),row['id']));self.m.db.commit()
        self.m.apply(self.m.plan('sync',row,self.row('a'),allow_changes=True)['id'])
        self.assertIsNone(next(i for i in self.m.inventory()['items'] if i['id']==row['id'])['update_check'])

    def test_backup_dependencies_block_purge(self):
        bid=self.m.backup(self.row('a'))['id']
        restore=self.m.plan('restore',backup_id=bid,allow_changes=True)
        with self.assertRaisesRegex(ValueError,'pending plan'): self.m.plan('purge',backup_id=bid)
        self.m.discard_plan(restore['id'])
        purge=self.m.plan('purge',backup_id=bid)
        other=self.m.plan('restore',backup_id=bid,allow_changes=True)
        with self.assertRaisesRegex(ValueError,'pending plan'): self.m.apply(purge['id'])
        self.m.discard_plan(other['id'])
        self.m.apply(purge['id'])

    def test_archive_permissions_and_traversal(self):
        import zipfile
        archive=self.base/'skill.zip'
        with zipfile.ZipFile(archive,'w') as z:
            file=zipfile.ZipInfo('bin/run'); file.create_system=3; file.external_attr=0o100755<<16; z.writestr(file,'#!/bin/sh\n')
        folder=self.base/'extract';folder.mkdir()
        with zipfile.ZipFile(archive) as z:self.a.safe_extract_zip(z,folder)
        self.assertEqual((folder/'bin'/'run').stat().st_mode & 0o777,0o755)
        with zipfile.ZipFile(archive,'w') as z:z.writestr('../escape','no')
        with zipfile.ZipFile(archive) as z:
            with self.assertRaisesRegex(ValueError,'Unsafe'):self.a.safe_extract_zip(z,folder)
        self.assertFalse((self.base/'escape').exists())

    def test_restore_when_configured_root_is_missing(self):
        import shutil
        row=self.row('a'); original=(self.roots/'a'/'SKILL.md').read_bytes()
        backup=self.m.backup(row)['id'];shutil.rmtree(self.roots)
        result=self.m.apply(self.m.plan('restore',backup_id=backup)['id'])
        self.assertEqual(result['state'],'succeeded')
        self.assertEqual((self.roots/'a'/'SKILL.md').read_bytes(),original)

    def test_inventory_cache_persists_and_never_traverses(self):
        self.assertFalse(self.m.cached_inventory()['cache']['available'])
        fresh=self.m.inventory()
        with patch.object(core, 'size', side_effect=AssertionError('cache traversed filesystem')), patch.object(self.m, 'source', side_effect=AssertionError('cache discovered sources')):
            cached=self.m.cached_inventory()
        self.assertTrue(cached['cache']['from_cache'])
        self.assertFalse(cached['cache']['dirty'])
        self.assertEqual(cached['items'], fresh['items'])
        self.m.db.close(); self.m=core.Manager(self.a)
        self.assertTrue(self.m.cached_inventory()['cache']['available'])
        self.m.protect(self.row('a'),True)
        self.assertTrue(self.m.cached_inventory()['cache']['dirty'])
        self.assertFalse(self.m.inventory()['cache']['dirty'])

    def test_corrupt_cache_and_concurrent_invalidation(self):
        for payload in ('bad-json', '[]', '{"cache":null}', '{"cache":{"schema":1}}'):
            self.m.db.execute('update maintenance_inventory_cache set payload=? where id=1',(payload,));self.m.db.commit()
            self.assertFalse(self.m.cached_inventory()['cache']['available'])
        import insights
        original=insights.summarize
        def changed(items):
            self.m.invalidate_cache()
            return original(items)
        with patch.object(insights, 'summarize', changed):
            self.assertTrue(self.m.inventory()['cache']['dirty'])
        self.assertTrue(self.m.cached_inventory()['cache']['dirty'])

    def test_source_coverage_distinct_targets_and_candidates(self):
        alias=self.roots/'alias';alias.symlink_to(self.roots/'a',target_is_directory=True)
        self.skill('copy','different content','a');self.m.scan()
        src=self.m.bind_source(self.row(str(alias)),'https://github.com/example/a',ref='v1',path='skills/a')
        inv=self.m.inventory();info=inv['insights'];items={i['path']:i for i in inv['items']}
        self.assertEqual(info['source_coverage']['total_targets'],3)
        self.assertEqual(info['source_coverage']['check_supported'],1)
        self.assertEqual(info['source_coverage']['source_unknown'],2)
        a=items[str(self.roots/'a')];copy=items[str(self.roots/'copy')]
        self.assertEqual(a['source_candidates'][0]['evidence'],'same-target')
        self.assertEqual(copy['source_candidates'][0]['evidence'],'name-only')
        self.assertIsNone(a['source']);self.assertIsNone(copy['source'])
        self.assertEqual(src['evidence'],'user-confirmed')
        self.assertFalse(info['independent_copies'])

    def test_independent_copies_exclude_aliases_and_conflicts(self):
        self.skill('b','different local version','a')
        alias=self.roots/'alias';alias.symlink_to(self.roots/'a',target_is_directory=True)
        self.m.scan()
        self.m.bind_source(self.row(str(self.roots/'a')),'https://github.com/example/a',ref='v1')
        self.m.bind_source(self.row(str(self.roots/'b')),'https://github.com/example/a',ref='v2')
        inv=self.m.inventory();groups=inv['insights']['independent_copies']
        self.assertEqual(len(groups),1);self.assertEqual(len(groups[0]['targets']),2)
        self.assertEqual(len(inv['same_source_copies'][0]),2)
        self.m.bind_source(self.row(str(alias)),'https://github.com/example/other')
        info=self.m.inventory()['insights']
        self.assertEqual(info['source_coverage']['source_conflict'],1)
        self.assertFalse(info['independent_copies'])

    def test_collection_repo_does_not_group_different_skills(self):
        self.m.bind_source(self.row('a'),'https://github.com/example/collection')
        self.m.bind_source(self.row('b'),'https://github.com/example/collection')
        self.assertFalse(self.m.inventory()['insights']['independent_copies'])

    def test_stale_builtin_flag_does_not_protect_user_skill(self):
        (self.roots/'public-alias').symlink_to(self.roots/'a',target_is_directory=True)
        self.m.scan()
        self.m.state(self.row('b'))
        self.m.db.execute('update maintenance_items set source=? where id=?',(json.dumps({'provider':'gitlab','url':'https://gitlab.com/a/b'}),self.row('b')['id']))
        self.m.db.execute("update capabilities set source_type='builtin' where id=?",(self.row(str(self.roots/'a'))['id'],));self.m.db.commit()
        info=self.m.inventory()['insights']
        self.assertEqual(info['source_coverage']['observe_only'],0)
        self.assertEqual(info['source_coverage']['provider_unsupported'],1)
        self.m.protect(self.row('b'),True)
        self.assertEqual([i['name'] for i in self.m.inventory()['insights']['space_review']], ['a'])

    def test_space_breakdown_does_not_follow_links_or_remove_content(self):
        p=self.roots/'a'
        for name in ('.venv','outputs','__pycache__','.git'):
            (p/name).mkdir();(p/name/'file').write_bytes(b'x'*8192)
        external=self.base/'outside';external.mkdir();(external/'secret').write_bytes(b'x'*131072)
        (p/'external').symlink_to(external,target_is_directory=True)
        before=core.manifest(p)
        result=core.dispatch(self.m,{'action':'inspect-space','target':self.row('a')['id']})
        entries={i['name']:i for i in result['entries']}
        self.assertEqual(entries['.venv']['category'],'dependencies')
        self.assertEqual(entries['outputs']['category'],'artifacts')
        self.assertEqual(entries['__pycache__']['category'],'cache')
        self.assertEqual(entries['.git']['category'],'version_records')
        self.assertEqual(entries['external']['category'],'reference')
        self.assertLess(entries['external']['space']['bytes'],1024)
        self.assertEqual(before,core.manifest(p))

    def test_cached_inventory_available_during_background_job(self):
        import threading,time,web_jobs
        self.m.inventory();jobs=web_jobs.Jobs(self.a)
        entered=threading.Event();release=threading.Event()
        def delayed(manager, *args):
            entered.set();release.wait(timeout=3);return {'count':0}
        with patch.object(core.Manager,'scan',delayed):
            job=jobs.start({'action':'scan'})
            self.assertTrue(entered.wait(timeout=2))
            try:
                self.assertTrue(jobs.busy())
                self.assertTrue(core.api_dispatch(self.a,{'action':'inventory-cached'})['cache']['available'])
            finally:release.set()
            for _ in range(100):
                if jobs.snapshot(job['id'])['state']!='running':break
                time.sleep(.01)
        self.assertEqual(jobs.snapshot(job['id'])['state'],'succeeded')

    def test_cache_does_not_authorize_stale_writes(self):
        self.m.inventory();row=self.row('a')
        plan=self.m.plan('delete',row)
        (self.roots/'a'/'SKILL.md').write_text('external change')
        self.assertTrue(self.m.cached_inventory()['cache']['available'])
        with self.assertRaisesRegex(ValueError,'Target changed'):self.m.apply(plan['id'])
        self.assertTrue((self.roots/'a'/'SKILL.md').exists())

    def fake_install_download(self, url, staging):
        repo=staging/'repository';(repo/'skills'/'new').mkdir(parents=True)
        (repo/'skills'/'new'/'SKILL.md').write_text('---\nname: newly-installed\nversion: 2.0\n---\nfixed content')
        return repo,{'source_type':'github','source_url':'https://github.com/example/repo','source_ref':'main','source_path':''}

    def identify(self):
        with patch.object(self.a,'install_source_from_url',self.fake_install_download):
            return self.m.identify_install('https://github.com/example/repo',str(self.roots))

    def test_install_staged_confirmation_history_and_source(self):
        selection=self.identify();c=selection['candidates'][0]
        plan=self.m.plan_install(selection['id'],c['remote_path'])
        target=Path(plan['target']);self.assertFalse(target.exists())
        self.assertEqual(self.m.get_plan(selection['id'])['state'],'discarded')
        with self.assertRaisesRegex(ValueError,'Confirmation'):core.dispatch(self.m,{'action':'apply','id':plan['id']})
        with patch.object(self.a,'install_source_from_url',side_effect=AssertionError('apply redownloaded')):
            result=core.dispatch(self.m,{'action':'apply','id':plan['id'],'confirmed':True})
        self.assertEqual(result['state'],'succeeded')
        self.assertIn('fixed content',(target/'SKILL.md').read_text())
        self.assertEqual(self.m.source(self.m.select(str(target)))['path'],'skills/new')
        installed=next(i for i in self.m.inventory()['items'] if i['real_path']==str(target))
        self.assertEqual(installed['installation']['basis'],'installed')
        self.assertTrue(installed['installation']['at'])
        self.assertEqual(self.m.history()['operations'][0]['action'],'install')
        with self.assertRaisesRegex(ValueError,'already applied'):self.m.apply(plan['id'])

    def test_install_existing_or_late_target_is_never_overwritten(self):
        selection=self.identify();c=selection['candidates'][0]
        (self.roots/c['target_name']).symlink_to(self.base/'absent')
        with self.assertRaisesRegex(ValueError,'already exists'):self.m.plan_install(selection['id'],c['remote_path'])
        (self.roots/c['target_name']).unlink()
        plan=self.m.plan_install(selection['id'],c['remote_path'])
        target=Path(plan['target']);target.mkdir();(target/'notes').write_text('keep')
        with self.assertRaisesRegex(ValueError,'Target changed'):self.m.apply(plan['id'])
        self.assertEqual((target/'notes').read_text(),'keep')
        (target/'notes').unlink()
        with self.assertRaisesRegex(ValueError,'Target changed'):self.m.apply(plan['id'])
        self.assertTrue(target.is_dir())

    def test_install_target_appearing_during_copy_is_preserved(self):
        selection=self.identify();plan=self.m.plan_install(selection['id'],selection['candidates'][0]['remote_path'])
        target=Path(plan['target']);original=core.copy_path
        def race(src,dst):
            original(src,dst)
            if Path(dst).name.startswith('.asm-stage-'):
                target.mkdir();(target/'notes').write_text('external content')
        with patch.object(core,'copy_path',side_effect=race):
            with self.assertRaisesRegex(ValueError,'Target changed'):self.m.apply(plan['id'])
        self.assertEqual((target/'notes').read_text(),'external content')

    def test_install_rejects_changed_staging_aliases_and_credentials(self):
        selection=self.identify();plan=self.m.plan_install(selection['id'],selection['candidates'][0]['remote_path'])
        (self.home/'plans'/plan['id']/'content'/'SKILL.md').write_text('tampered')
        with self.assertRaisesRegex(ValueError,'Staged content changed'):self.m.apply(plan['id'])
        alias=self.base/'alias';alias.symlink_to(self.roots,target_is_directory=True)
        config=self.a.load_config();config['platforms']['claude_code']={'enabled':True,'roots':[str(alias)]};self.a.save_config(config)
        self.assertNotIn(str(alias),[r['path'] for r in self.m.install_roots()['roots']])
        with self.assertRaisesRegex(ValueError,'editable'):self.m.identify_install('https://github.com/example/repo',str(alias))
        with self.assertRaisesRegex(ValueError,'credentials'):self.m.identify_install('https://secret@github.com/example/repo',str(self.roots))

    def test_install_changed_discovery_is_rejected_and_discardable(self):
        selection=self.identify()
        (Path(selection['archive'])/'skills'/'new'/'SKILL.md').write_text('changed after recognition')
        with self.assertRaisesRegex(ValueError,'Downloaded content changed'):self.m.plan_install(selection['id'],'skills/new')
        self.assertIn(selection['id'],[p['id'] for p in self.m.plans()['plans']])
        self.m.discard_plan(selection['id'])
        self.assertFalse((self.home/'plans'/selection['id']).exists())

    def test_install_recovery_registers_completed_content(self):
        selection=self.identify();plan=self.m.plan_install(selection['id'],selection['candidates'][0]['remote_path'])
        target=Path(plan['target']);core.copy_path(self.home/'plans'/plan['id']/'content',target)
        self.m.operation('interrupted-install','install','running',{'target':str(target),'plan_id':plan['id'],'expected_after':plan['after'],'originally_present':False})
        result=self.m.recover('interrupted-install')
        self.assertEqual(result['state'],'succeeded')
        self.assertEqual(self.m.source(self.m.select(str(target)))['path'],'skills/new')
        installed=next(i for i in self.m.inventory()['items'] if i['real_path']==str(target))
        self.assertEqual(installed['installation']['basis'],'installed')
        self.assertTrue(installed['installation']['at'])

    def test_install_failure_removes_only_new_target(self):
        selection=self.identify();plan=self.m.plan_install(selection['id'],selection['candidates'][0]['remote_path'])
        with patch.object(self.m,'register_changed_path',side_effect=OSError('simulated registration failure')):
            with self.assertRaisesRegex(ValueError,'failed'):self.m.apply(plan['id'])
        self.assertFalse(Path(plan['target']).exists())
        self.assertTrue((self.roots/'a'/'SKILL.md').exists())
        self.assertEqual(self.m.history()['operations'][0]['state'],'failed')

if __name__=='__main__': unittest.main()
