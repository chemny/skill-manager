import json
import threading
import unittest
from unittest.mock import patch
import test_maintenance as fixtures
import maintenance as core
import web_jobs

class UpdatePageTests(unittest.TestCase):
    setUp=fixtures.MaintenanceTests.setUp
    tearDown=fixtures.MaintenanceTests.tearDown
    skill=fixtures.MaintenanceTests.skill
    row=fixtures.MaintenanceTests.row

    def test_successful_check_invalidates_after_local_change(self):
        row=self.row('a')
        self.m.bind_source(row,'https://github.com/example/skills','main','')
        remote=self.base/'remote';remote.mkdir()
        import shutil
        shutil.copyfile(self.roots/'a'/'SKILL.md',remote/'SKILL.md')
        with patch.object(core.Manager,'remote',return_value=(remote,{'provider':'github','url':'https://github.com/example/skills','ref':'main','path':''})):
            result=self.m.check(row)
        self.assertEqual(result['status'],'latest')
        (self.roots/'a'/'SKILL.md').write_text('changed locally')
        item=next(i for i in self.m.inventory()['items'] if i['id']==row['id'])
        self.assertEqual(item['update_check']['status'],'stale')

    def test_check_run_persists_and_reports_partial_progress(self):
        rows=[self.row('a'),self.row('b')]
        for row in rows:
            self.m.bind_source(row,'https://github.com/example/skills','main',row['name'])
        gate=threading.Event();release=threading.Event()
        def check(manager,row):
            if row['name']=='b':gate.set();release.wait(4)
            return {'id':row['id'],'status':'latest','checked_at':core.stamp()}
        jobs=web_jobs.Jobs(self.a)
        with patch.object(core.Manager,'check',check):
            job=jobs.start({'action':'check-many','ids':[r['id'] for r in rows],'scope':'all'})
            try:
                self.assertTrue(gate.wait(2));snapshot=jobs.snapshot(job['id'])
                self.assertEqual(snapshot['completed'],1)
                self.assertEqual(len(snapshot['partial_checks']),1)
                saved=json.loads((self.home/'update-run.json').read_text())
                self.assertEqual(saved['completed'],1)
            finally:release.set()
            for _ in range(100):
                if not jobs.busy():break
                threading.Event().wait(.02)
        saved=json.loads((self.home/'update-run.json').read_text())
        self.assertEqual(saved['state'],'succeeded');self.assertEqual(saved['total'],2)
        self.assertEqual(core.api_dispatch(self.a,{'action':'update-summary'})['run']['scope'],'all')

    def test_local_modifications_are_not_evidence_of_remote_updates(self):
        import shutil
        row=self.row('a');self.m.bind_source(row,'https://github.com/example/skills','main','a')
        remote=self.base/'remote';shutil.copytree(self.roots/'a',remote)
        baseline=json.dumps(core.manifest(self.roots/'a'))
        self.m.db.execute('update maintenance_items set baseline=? where id=?',(baseline,row['id']));self.m.db.commit()
        with (self.roots/'a'/'SKILL.md').open('a') as f:f.write('\nlocal edits')
        source=self.m.source(row)
        with patch.object(self.m,'remote',return_value=(remote,source)):
            checked=self.m.check(row)
            self.assertEqual(checked['status'],'local_modified')
            self.assertFalse(checked['remote_update_available'])
            with (remote/'SKILL.md').open('a') as f:f.write('\nremote edits')
            self.assertTrue(self.m.check(row)['remote_update_available'])

    def test_verified_update_sets_latest_only_on_matching_sources(self):
        alias=self.roots/'alias';alias.symlink_to(self.roots/'a');self.m.scan()
        row=self.m.select(str(self.roots/'a'));ref=self.m.select(str(alias))
        self.m.bind_source(row,'https://github.com/example/skills','main','a')
        self.m.bind_source(ref,'https://github.com/example/other','main','a')
        remote=self.base/'remote';remote.mkdir()
        (remote/'SKILL.md').write_text('---\nname: a\nversion: 2.0\n---\nupdated')
        with patch.object(self.m,'remote',return_value=(remote,self.m.source(row))):
            self.m.check(row)
            plan=self.m.plan('update',row,allow_changes=True)
        result=self.m.apply(plan['id'])
        self.assertEqual(result['state'],'succeeded')
        checked=json.loads(self.m.state(row)['last_check'])
        self.assertEqual(checked['status'],'latest')
        self.assertEqual(checked['basis'],'verified-update')
        self.assertEqual(checked['local_fingerprint'],core.fingerprint(core.manifest(self.roots/'a')))
        self.assertIsNone(self.m.state(ref)['last_check'])
        self.assertTrue(self.m.verify_backup(result['backups'][0]['id'])['valid'])
