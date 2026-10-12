"""Recovery provenance, bounded taxonomy, evidence import and remote request reuse."""
import datetime as dt
import hashlib
import json
import unittest
from pathlib import Path
from unittest.mock import patch
import urllib.error
import test_maintenance as fixtures
import library_metadata as metadata
import maintenance
import diagnostics

class LibraryMetadataTests(unittest.TestCase):
    setUp = fixtures.MaintenanceTests.setUp
    tearDown = fixtures.MaintenanceTests.tearDown
    skill = fixtures.MaintenanceTests.skill
    row = fixtures.MaintenanceTests.row

    def lock(self):
        (self.base/'.skill-lock.json').write_text(json.dumps({'skills':{'a':{'sourceUrl':'https://github.com/owner/collection.git','skillPath':'skills/a/SKILL.md','installedAt':'2026-01-01T01:00:00Z'}}}))

    def test_lock_is_scoped_to_exact_install_directory_and_date(self):
        self.lock()
        a=self.row('a'); source=self.m.source(a)
        self.assertEqual(source['path'],'skills/a')
        self.assertEqual(source['evidence'],'installer-lock')
        dates=metadata.installation(self.roots/'a')
        self.assertEqual(dates['basis'],'installer-record')
        elsewhere=self.base/'other'/'skills'/'a';elsewhere.mkdir(parents=True)
        self.assertIsNone(metadata.installer_record(elsewhere))
        self.assertIsNone(self.m.source(self.row('b')))

    def test_real_directory_date_not_link_creation_or_mtime(self):
        link=self.roots/'alias';link.symlink_to(self.roots/'a')
        self.assertEqual(metadata.directory_date(link),metadata.directory_date(self.roots/'a'))
        with patch('library_metadata.directory_date',return_value=None):
            self.assertEqual(metadata.installation(link,first_seen='2026-02-01')['basis'],'first-recorded')
        self.assertEqual(metadata.installation(link,'2026-02-01')['basis'],'installed')

    def test_file_repo_link_is_only_candidate_not_bound_source(self):
        (self.roots/'a'/'README.md').write_text('Examples: https://github.com/example/other/tree/main/skills/demo')
        item=next(i for i in self.m.inventory()['items'] if i['id']==self.row('a')['id'])
        self.assertIsNone(item['source'])
        self.assertEqual(item['source_candidates'][0]['source']['path'],'skills/demo')
        self.assertIsNone(metadata.repository(self.a,'https://github.com.evil.example/a/b'))
        self.assertIsNone(metadata.repository(self.a,'https://user:secret@github.com/a/b'))
        self.assertEqual(metadata.repository(self.a,'git@gitlab.com:group/repo.git')['provider'],'gitlab')

    def test_labels_shared_aliases_independent_copies_and_no_skill_edits(self):
        before=(self.roots/'a'/'SKILL.md').read_bytes()
        link=self.roots/'alias';link.symlink_to(self.roots/'a');self.m.scan()
        a=self.m.select(str(self.roots/'a')); alias=self.m.select(str(link));b=self.row('b')
        result=self.m.set_labels([a['id'],alias['id']],'内容创作',['写作'])
        self.assertEqual(result['saved'],1)
        items=self.m.inventory()['items']
        self.assertTrue(all(i['classification']=={'category':'内容创作','tags':['写作']} for i in items if i['real_path']==str(self.roots/'a')))
        self.assertEqual(next(i['classification'] for i in items if i['id']==b['id']),{'category':'','tags':[]})
        self.assertEqual((self.roots/'a'/'SKILL.md').read_bytes(),before)

    def test_batch_tag_limit_is_atomic_and_remove_is_reversible(self):
        a=self.row('a');b=self.row('b')
        self.m.set_labels([a['id']],tags=['写作','图片','文档'])
        with self.assertRaisesRegex(ValueError,'three'):
            self.m.set_labels([b['id'],a['id']],tags=['自动化'],mode='add')
        self.assertIsNone(self.m.db.execute('select * from maintenance_labels where real_path=?',(str(self.roots/'b'),)).fetchone())
        self.m.set_labels([a['id']],tags=['图片'],mode='remove')
        labels=json.loads(self.m.db.execute('select tags from maintenance_labels').fetchone()[0])
        self.assertEqual(labels,['写作','文档'])
        with self.assertRaises(ValueError): self.m.set_labels([a['id']],category='随意分类')

    def test_replay_backfills_skipped_events_without_duplicates(self):
        logs=self.base/'logs';logs.mkdir()
        now=dt.datetime.now(dt.timezone.utc)
        def event(age):
            return {'timestamp':(now-dt.timedelta(days=age)).isoformat(),'type':'tool_use','name':'Read','input':{'file_path':str(self.roots/'a'/'SKILL.md')}}
        file=logs/'session.jsonl';file.write_text('\n'.join(json.dumps(event(age)) for age in (1,60))+'\n')
        roots=[{'agent':'codex','path':str(logs)}]
        with patch.object(self.a,'usage_log_roots',return_value=[('codex',logs)]):
            progress=[]
            first=self.m.import_usage(30,roots=roots,progress=lambda *values:progress.append(values))
            second=self.m.import_usage(90,True,roots)
            third=self.m.import_usage(90,True,roots)
        self.assertEqual([first['inserted'],second['inserted'],third['inserted']],[1,1,0])
        usage=self.m.usage(90,self.row('a')['id'])['items'][0]
        self.assertEqual(usage['recorded_reads'],2)
        self.assertEqual(len(usage['daily']),2)
        self.assertEqual(first['agents']['codex']['files'],1)
        self.assertEqual(progress[0][:2],(0,1))
        self.assertEqual(progress[-1][:2],(1,1))
        self.assertNotIn(str(logs),str(progress))

    def test_named_skill_tool_is_evidence_but_ordinary_mention_is_not(self):
        self.m.db.execute("update capabilities set platform='claude_code' where id=?",(self.row('a')['id'],));self.m.db.commit()
        mapping=self.a.capability_skill_path_map(self.m.r)
        event={'type':'tool_use','name':'Skill','input':{'skill':'a'}}
        self.assertEqual(self.a.explicit_skill_events(event,mapping,'claude_code')[0][1],'invocation')
        self.assertFalse(self.a.explicit_skill_events({'type':'message','text':'use a skill'},mapping,'claude_code'))
        self.assertFalse(self.a.explicit_skill_events(event,mapping,'codex'))

    def test_relative_read_context_survives_incremental_import(self):
        logs=self.base/'logs';logs.mkdir();f=logs/'one.jsonl'
        now=dt.datetime.now(dt.timezone.utc).isoformat()
        f.write_text(json.dumps({'type':'session_meta','cwd':str(self.roots)})+'\n')
        with patch.object(self.a,'usage_log_roots',return_value=[('codex',logs)]):
            self.m.import_usage(90)
            with f.open('a') as h: h.write(json.dumps({'timestamp':now,'type':'tool_use','name':'Read','input':{'file_path':'a/SKILL.md'}})+'\n')
            result=self.m.import_usage(90)
        self.assertEqual(result['inserted'],1)

    def test_probe_finds_exact_blob_with_collection_subdirectory(self):
        body=(self.roots/'a'/'SKILL.md').read_bytes()
        blob=hashlib.sha1(b'blob '+str(len(body)).encode()+b'\0'+body).hexdigest()
        with patch.object(self.a,'github_json',side_effect=[{'default_branch':'main'},{'sha':'commit'},{'tree':[{'type':'blob','path':'nested/different/SKILL.md','sha':blob}]}]) as api:
            result=self.m.probe_source(self.row('a')['id'],'https://github.com/o/r')
            self.assertEqual(result['candidates'][0]['source']['path'],'nested/different')
            self.assertEqual(result['candidates'][0]['evidence'],'content-match')
            self.m.remote_json('https://api.github.com/repos/o/r')
            self.assertEqual(api.call_count,3)

    def test_remote_search_does_not_bind_and_network_errors_are_distinct(self):
        with patch.object(self.a,'github_json',return_value={'items':[{'html_url':'https://github.com/o/r'}]}):
            result=self.m.discover_sources(self.row('a')['id'],True)
        self.assertEqual(result['items'][0]['candidates'][0]['evidence'],'remote-search')
        self.assertIsNone(self.m.source(self.row('a')))
        error=urllib.error.HTTPError('https://api.github.com/x',403,'limit',{'X-RateLimit-Remaining':'0'},None)
        self.assertEqual(diagnostics.classify(error)[0],'remote_rate_limit')
        self.assertEqual(diagnostics.classify(urllib.error.HTTPError('https://api.github.com/x',404,'missing',{},None))[0],'remote_not_found')

    def test_batch_rate_limit_stops_remaining_requests_preserves_unchecked(self):
        import time
        from web_jobs import Jobs
        jobs=Jobs(self.a)
        ids=[self.row('a')['id'],self.row('b')['id']]
        for name in ('a','b'):
            self.m.bind_source(self.row(name),'https://github.com/example/skills','main',name)
        with patch.object(maintenance.Manager,'check',return_value={'id':ids[0],'status':'check_failed','code':'remote_rate_limit'}) as check:
            job=jobs.start({'action':'check-many','ids':ids})
            for _ in range(100):
                value=jobs.snapshot(job['id'])
                if value['state']!='running': break
                time.sleep(.02)
        self.assertEqual(check.call_count,1)
        self.assertEqual(value['result']['run']['state'],'partial')
        self.assertEqual(value['result']['run']['completed'],2)
        self.assertEqual(len(value['result']['skipped']),1)
        self.assertIsNone(self.m.state(self.row('b'))['last_check'])
