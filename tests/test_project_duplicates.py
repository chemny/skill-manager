import json
import os
from pathlib import Path
import time
import unittest
from unittest.mock import patch

import test_maintenance as fixtures
import project_analysis
import project_cleanup
import project_duplicates as duplicates
import project_space
import space_cleanup as space


class ProjectDuplicatesTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.MaintenanceTests()
        self.fixture.setUp()
        self.m = self.fixture.m
        self.first = self.fixture.base / 'workspace' / 'projects' / 'first'
        self.second = self.fixture.base / 'workspace' / 'projects' / 'second'
        self.generated = self.fixture.base / '.codex' / 'generated_images'
        for path in (self.first, self.second, self.generated):
            path.mkdir(parents=True)
        self.env = patch.dict(os.environ, {'CODEX_HOME':str(self.generated.parent)})
        self.env.start()
        self.paths = [str(p) for p in (self.first, self.second, self.generated)]
        self.report = {'id':'comparison-fixture','scope_paths':self.paths,
            'projects':[{'path':str(p),'name':p.name,'source':'codex'} for p in (self.first,self.second,self.generated)]}
        self.m.db.execute('insert into maintenance_disk_scans values(?,?,?)',
                          (self.report['id'], 'fixture', json.dumps(self.report)))
        self.m.db.commit()

    def tearDown(self):
        self.env.stop()
        self.fixture.tearDown()

    def analyze(self, root):
        scan = space.scan(self.m, str(root))
        value = {'path':str(root),'scan_id':scan['id'],'policy_version':space.POLICY_VERSION,
                 'state':'completed','excluded_projects':[]}
        self.m.db.execute('insert or replace into maintenance_project_analyses values(?,?)', (str(root),json.dumps(value)))
        self.m.db.commit()
        return scan

    def build(self):
        return duplicates.build(self.m, self.report, self.paths, time.monotonic()+10)

    def payload(self):
        return {'report_id':self.report['id'],'paths':self.paths}

    def prepare(self):
        (self.first/'reference').mkdir()
        (self.first/'reference/master.png').write_bytes(b'original image')
        (self.second/'own.png').write_bytes(b'original image')
        for name in ('one.png','two.png'):
            (self.generated/name).write_bytes(b'original image')
        for root in (self.first,self.second,self.generated):
            self.analyze(root)
        return self.build()

    def test_cross_project_images_keep_each_project_copy_and_recheck_before_move(self):
        value = self.prepare()
        self.assertEqual(value['cross_groups'], 1)
        self.assertEqual(len(value['candidates']), 2)
        self.assertTrue(all(r['root']==str(self.generated) for r in value['candidates']))
        self.assertTrue(all(r['keeper']==str(self.first/'reference/master.png') for r in value['candidates']))
        options = project_cleanup.advice(self.m,self.payload())
        self.assertEqual(sum(g['files'] for g in options['groups']),2)
        payload = {**self.payload(),'groups':[g['id'] for g in options['groups']],'confirmed':True}
        result = project_cleanup.move(self.m,payload)
        self.assertEqual(result['completed'],2,result)
        self.assertTrue((self.first/'reference/master.png').is_file())
        self.assertTrue((self.second/'own.png').is_file())
        self.assertEqual(len(space.trash_list(self.m)['items']),2)

    def test_changed_external_keeper_blocks_the_move(self):
        self.prepare()
        options = project_cleanup.advice(self.m,self.payload())
        (self.first/'reference/master.png').write_bytes(b'changed image!')
        result = project_cleanup.move(self.m,{**self.payload(),'groups':[g['id'] for g in options['groups']],'confirmed':True})
        self.assertEqual(result['completed'],0)
        self.assertTrue((self.generated/'one.png').is_file())
        self.assertEqual(space.trash_list(self.m)['items'],[])

    def test_stale_comparison_and_old_policy_cannot_authorize_moves(self):
        self.prepare()
        groups = project_cleanup.advice(self.m,self.payload())['groups']
        self.analyze(self.first)
        self.assertIsNone(duplicates.latest(self.m,self.report,self.paths))
        with self.assertRaises(ValueError):
            project_cleanup.move(self.m,{**self.payload(),'groups':[g['id'] for g in groups],'confirmed':True})
        row=self.m.db.execute('select payload from maintenance_project_analyses where path=?',(str(self.generated),)).fetchone()
        scan=json.loads(row[0]);self.m.db.execute('update maintenance_space_scans set payload=json_set(payload,\'$.policy_version\',1) where id=?',(scan['scan_id'],));self.m.db.commit()
        self.assertIn(str(self.generated),project_cleanup.advice(self.m,self.payload())['missing'])

    def test_hash_and_reference_limits_produce_partial_status(self):
        (self.first/'a.png').write_bytes(b'image')
        (self.second/'b.png').write_bytes(b'image')
        for root in (self.first,self.second,self.generated):self.analyze(root)
        with patch.object(duplicates,'HASH_BUDGET',1):
            value=self.build()
        self.assertEqual(value['state'],'partial')
        self.assertEqual(sum(c['hash_skipped'] for c in value['coverage'].values()),2)
        self.assertEqual(value['candidates'],[])
        (self.first/'large.txt').write_bytes(b'x'*300000)
        result=project_analysis.analyze(self.m,{'report_id':self.report['id'],'paths':[str(self.first)]})
        self.assertEqual(result['analyses'][0]['state'],'completed')
        self.assertFalse(result['analyses'][0]['references_incomplete'])

    def test_current_protection_rules_also_filter_cached_older_scan_items(self):
        runtime=self.first/'runtime/lib/python3/site-packages/pkg'
        (runtime/'__pycache__').mkdir(parents=True)
        (runtime/'module.py').write_text('print(1)')
        path=runtime/'__pycache__/module.pyc'
        path.write_bytes(b'cache')
        old=time.time()-9*86400
        os.utime(path,(old,old))
        with patch.object(space,'SKIP_DIRS',space.SKIP_DIRS-{'site-packages'}):self.analyze(self.first)
        result=project_cleanup.advice(self.m,{'report_id':self.report['id'],'paths':[str(self.first)]})
        self.assertEqual(result['groups'],[])
        self.assertEqual(path.read_bytes(),b'cache')

    def test_partial_batch_can_compare_previous_projects_on_continuation(self):
        (self.first/'a.png').write_bytes(b'image')
        (self.generated/'generated.png').write_bytes(b'image')
        first=project_analysis.analyze(self.m,{'report_id':self.report['id'],'paths':[str(self.first)],'context_paths':self.paths})
        self.assertEqual(first['state'],'partial')
        project_analysis.analyze(self.m,{'report_id':self.report['id'],'paths':[str(self.second),str(self.generated)],'context_paths':self.paths})
        value=duplicates.latest(self.m,self.report,self.paths)
        self.assertEqual(value['state'],'completed')
        self.assertEqual(len(value['candidates']),1)
        self.assertEqual(value['candidates'][0]['root'],str(self.generated))

    def test_layout_splits_projects_and_keeps_excluded_descendants_out(self):
        workspace=self.first.parents[1]
        dated=workspace/'2026-10-22'/'design-project'
        dated.mkdir(parents=True)
        (dated/'AGENTS.md').write_text('project')
        (dated/'assets').mkdir()
        (dated/'outputs'/'draft-run').mkdir(parents=True)
        standalone=workspace/'2026-10-22'/'no-markers-project'
        (standalone/'outputs'/'draft-run').mkdir(parents=True)
        for root in (self.first,self.second):
            (root/'package.json').write_text('{}')
            (root/'keep.txt').write_text('keep')
        with patch.object(project_space,'discover',return_value=([('codex',str(workspace))],[])):
            catalog=project_space.catalog(self.m,self.fixture.base)
            roots={p['path']:p for p in catalog['projects']}
            for path in (self.first,self.second,dated):self.assertIn(str(path),roots)
            self.assertNotIn(str(dated/'assets'),roots)
            self.assertNotIn(str(dated/'outputs'),roots)
            self.assertNotIn(str(standalone/'outputs'),roots)
            chosen=[p['id'] for p in catalog['projects'] if p['path']!=str(self.second)]
            report=project_space.scan(self.m,{'catalog_id':catalog['id'],'agents':['codex'],'ids':chosen})
            self.assertEqual(report['summary']['files'],3)
            fresh=project_space.catalog(self.m,self.fixture.base)
            self.assertNotIn(project_space.project_id(self.second),fresh['selection']['ids'])


if __name__=='__main__':
    unittest.main()
