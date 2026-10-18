import json
from pathlib import Path
import unittest
import urllib.error
from unittest.mock import patch

import test_maintenance as fixtures
import classification
import source_search


class ClassificationSearchTests(unittest.TestCase):
    setUp=fixtures.MaintenanceTests.setUp
    tearDown=fixtures.MaintenanceTests.tearDown
    skill=fixtures.MaintenanceTests.skill
    row=fixtures.MaintenanceTests.row

    def test_classification_uses_purpose_and_preserves_files(self):
        folder=self.skill('video-editor','Video editing using FFmpeg.');self.m.scan()
        before=(folder/'SKILL.md').read_bytes()
        result=classification.classify(self.m,self.row('video-editor'),True)
        self.assertEqual(result['after']['category'],'设计与多媒体')
        self.assertIn('音视频',result['after']['tags'])
        self.assertEqual((folder/'SKILL.md').read_bytes(),before)

    def test_manual_fields_survive_reclassification(self):
        classification.classify(self.m,self.row('a'),True)
        self.m.db.commit()
        self.m.set_labels([self.row('a')['id']],category='内容创作',tags=['写作'])
        self.skill('a','Code programming testing automation workflow')
        out=classification.classify(self.m,self.row('a'),True)
        self.assertEqual(out['after'],{'category':'内容创作','tags':['写作']})

    def test_old_labels_are_user_owned(self):
        self.m.db.execute('insert into maintenance_labels values (?,?,?)',(str(self.roots/'a'),'内容创作','["写作"]'))
        out=classification.classify(self.m,self.row('a'),True)
        self.assertTrue(out['manual_preserved'])

    def test_auto_category_fills_legacy_empty_category_without_changing_tags(self):
        folder=self.skill('video-editor','Video editing using FFmpeg.');self.m.scan()
        self.m.db.execute('insert into maintenance_labels values (?,?,?)',(str(folder),'','["写作"]'))
        before=(folder/'SKILL.md').read_bytes()
        result=classification.batch(self.m,True,field='category')
        labels,_=classification.existing(self.m,str(folder))
        self.assertEqual(labels,{'category':'设计与多媒体','tags':['写作']})
        self.assertEqual((folder/'SKILL.md').read_bytes(),before)
        self.assertGreaterEqual(result['changed'],1)

    def test_auto_tags_fill_only_missing_tags_and_keep_category(self):
        folder=self.skill('video-editor','Video editing using FFmpeg.');self.m.scan()
        self.m.db.execute('insert into maintenance_labels values (?,?,?)',(str(folder),'办公效率','[]'))
        result=classification.batch(self.m,True,field='tags')
        labels,_=classification.existing(self.m,str(folder))
        self.assertEqual(labels['category'],'办公效率')
        self.assertIn('音视频',labels['tags'])
        self.assertEqual(result['changed'],1)
        again=classification.batch(self.m,True,field='tags')
        self.assertEqual(again['changed'],0)

    def test_auto_fields_reject_unknown_scope(self):
        with self.assertRaises(ValueError): classification.batch(self.m,True,field='unknown')

    def test_low_confidence_category_is_applied_and_manual_edit_is_preserved(self):
        folder=self.skill('营销设计','Unspecified helper.');self.m.scan()
        row=self.row('营销设计')
        proposed=classification.infer(self.m,row)
        self.assertEqual(proposed['confidence'],'low')
        self.assertTrue(proposed['category'])
        self.m.db.execute('insert into maintenance_labels values (?,?,?)',(str(folder),'','["数据"]'))
        result=classification.batch(self.m,True,field='category')
        self.assertGreaterEqual(result['changed'],1)
        labels,state=classification.existing(self.m,str(folder))
        self.assertEqual(labels,{'category':proposed['category'],'tags':['数据']})
        self.assertFalse(state['manual_category'])
        self.m.set_labels([row['id']],category='办公效率')
        classification.batch(self.m,True,field='category')
        labels,state=classification.existing(self.m,str(folder))
        self.assertEqual(labels,{'category':'办公效率','tags':['数据']})
        self.assertTrue(state['manual_category'])

    def test_unmatched_skill_goes_to_other(self):
        inferred=classification.infer(self.m,self.row('a'))
        self.assertEqual(inferred['confidence'],'unmatched')
        self.assertEqual(inferred['category'],'其他')
        result=classification.batch(self.m,True,field='category')
        self.assertGreaterEqual(result['changed'],1)
        self.assertEqual(classification.existing(self.m,str(self.roots/'a'))[0]['category'],'其他')

    def test_scoped_inventory_republishes_labels_after_classification(self):
        self.m.inventory()
        row=self.row('a')
        self.m.set_labels([row['id']],category='内容创作')
        self.assertTrue(self.m.cached_inventory()['cache']['dirty'])
        self.m.inventory(ids=[row['id']],lightweight=True)
        fresh=self.m.cached_inventory()
        self.assertFalse(fresh['cache']['dirty'])
        self.assertEqual(next(i for i in fresh['items'] if i['id']==row['id'])['classification']['category'],'内容创作')

    def test_batch_dedup_and_undo_skips_later_manual_changes(self):
        (self.roots/'alias').symlink_to(self.roots/'a',target_is_directory=True);self.m.scan()
        out=classification.batch(self.m,True)
        self.assertEqual(out['total'],2)
        self.m.set_labels([self.m.select(str(self.roots/'a'))['id']],category='内容创作')
        undo=classification.undo(self.m,out['id'])
        self.assertEqual(undo,{'restored':1,'skipped':1})
        with self.assertRaises(ValueError): classification.undo(self.m,out['id'])

    def test_default_order_and_cache(self):
        seen=[]
        with patch.object(source_search,'query_channel',side_effect=lambda m,c,q:seen.append(c['name']) or []):
            first=source_search.search(self.m,'test')
            second=source_search.search(self.m,'test')
        self.assertEqual(seen,['github','skills-sh','skillhub'])
        self.assertTrue(any(t['status']=='cached' for t in second['trace']))
        self.assertFalse(first['confirmed'])

    def test_network_failure_promotes_domestic_then_continues(self):
        seen=[]
        def query(m,c,q):
            seen.append(c['name'])
            if c['name']=='github': raise urllib.error.URLError('offline')
            return []
        with patch.object(source_search,'query_channel',side_effect=query):
            result=source_search.search(self.m,'test')
        self.assertEqual(seen,['github','skillhub','skills-sh'])
        self.assertEqual(result['trace'][0]['status'],'failed')
        self.assertEqual(self.m.db.execute('select count(*) from source_channel_health').fetchone()[0],1)

    def test_rate_limit_is_cooled_and_does_not_abort(self):
        seen=[]
        def query(m,c,q):
            seen.append(c['name'])
            if c['name']=='github': raise urllib.error.HTTPError('https://api.github.com',429,'limit',{'Retry-After':'3600'},None)
            return []
        with patch.object(source_search,'query_channel',side_effect=query):
            source_search.search(self.m,'one');result=source_search.search(self.m,'two')
        self.assertEqual(seen.count('github'),1)
        self.assertTrue(any(t['status']=='cooldown' for t in result['trace']))

    def test_domestic_preference_is_persisted(self):
        source_search.set_policy(self.m,'domestic-first')
        seen=[]
        with patch.object(source_search,'query_channel',side_effect=lambda m,c,q:seen.append(c['name']) or []):
            source_search.search(self.m,'test')
        self.assertEqual(seen[0],'skillhub')

    def test_all_services_fail_still_returns_trace(self):
        with patch.object(source_search,'query_channel',side_effect=ValueError('bad response')):
            result=source_search.search(self.m,'test')
        self.assertEqual(result['candidates'],[])
        self.assertEqual(sum(t['status']=='failed' for t in result['trace']),3)

    def test_vercel_candidates_do_not_guess_subdirectory(self):
        import update_providers
        with patch.object(update_providers,'api_json',return_value={'skills':[{'source':'vercel-labs/skills','skillId':'find-skills','name':'find-skills'}]}):
            out=source_search.query_channel(self.m,{'kind':'skills-sh','name':'skills-sh','base_url':'https://skills.sh'},'find')
        self.assertEqual(out[0]['source']['path'],'')
        self.assertIsNone(self.m.source(self.row('a')))

    def test_unavailable_channel_does_not_report_empty_search(self):
        with patch.object(source_search,'query_channel') as query:
            result=source_search.search(self.m,'ffmpeg','gitcode')
        query.assert_not_called()
        self.assertEqual(result['trace'][0]['status'],'unsupported')
        self.assertIn('授权令牌',result['trace'][0]['reason'])

    def test_gitlab_explicit_search_returns_unbound_repository_candidate(self):
        import update_providers
        payload=[{'name':'Video Skill','web_url':'https://gitlab.com/example/video-skill'},
                 {'name':'Invalid','web_url':'https://invalid.example/video-skill'}]
        with patch.object(update_providers,'api_json',return_value=payload) as api:
            result=source_search.search(self.m,'video','gitlab')
        self.assertIn('/api/v4/projects?',api.call_args.args[1])
        self.assertEqual(result['trace'][0]['status'],'success')
        self.assertEqual(len(result['candidates']),1)
        self.assertEqual(result['candidates'][0]['source']['provider'],'gitlab')
        self.assertIn('待核对',result['candidates'][0]['from_path'])
        self.assertIsNone(self.m.source(self.row('a')))

    def test_skillsmp_explicit_search_returns_unbound_candidate(self):
        import update_providers
        payload={'data':{'skills':[{'name':'Video Skill','githubUrl':'https://github.com/example/video-skill'}]}}
        with patch.object(update_providers,'api_json',return_value=payload):
            result=source_search.search(self.m,'video','skillsmp')
        self.assertEqual(result['trace'][0]['status'],'success')
        self.assertEqual(result['candidates'][0]['name'],'Video Skill')
        self.assertIsNone(self.m.source(self.row('a')))

    def test_batch_checkpoint_survives_next_call(self):
        with patch.object(source_search,'query_channel',return_value=[]):
            first=source_search.batch(self.m,1);second=source_search.batch(self.m,1);third=source_search.batch(self.m,1)
        self.assertEqual(first['processed'],1)
        self.assertEqual(first['remaining'],1)
        self.assertEqual(second['processed'],1)
        self.assertEqual(third['processed'],0)

    def test_update_automatically_classifies_and_classification_failure_is_nonfatal(self):
        row=self.row('a');self.m.bind_source(row,'https://github.com/example/repo',path='a')
        remote=self.base/'remote';remote.mkdir()
        (remote/'SKILL.md').write_text('---\nname: video-editor\nversion: 2\n---\nVideo editing using FFmpeg.')
        with patch.object(self.m,'remote',return_value=(remote,{'commit':'abc','url':'https://github.com/example/repo','path':'a','ref':'main'})):
            plan=self.m.plan('update',row,allow_changes=True)
        result=self.m.apply(plan['id'])
        self.assertEqual(result['classification']['category'],'设计与多媒体')
        (remote/'SKILL.md').write_text('---\nname: video-editor\nversion: 3\n---\nVideo editing using FFmpeg.')
        with patch.object(self.m,'remote',return_value=(remote,{'commit':'def','url':'https://github.com/example/repo','path':'a','ref':'main'})):
            plan=self.m.plan('update',self.row('video-editor'),allow_changes=True)
        with patch.object(classification,'classify',side_effect=ValueError('unavailable')):
            result=self.m.apply(plan['id'])
        self.assertEqual(result['state'],'succeeded')
        self.assertTrue(result['classification_pending'])

    def test_network_cooldown_keeps_domestic_first_on_next_query(self):
        seen=[]
        def query(m,c,q):
            seen.append(c['name'])
            if c['name']=='github':raise urllib.error.URLError('offline')
            return []
        with patch.object(source_search,'query_channel',side_effect=query):
            source_search.search(self.m,'one');seen.clear();source_search.search(self.m,'two')
        self.assertEqual(seen,['skillhub','skills-sh'])

    def test_install_automatically_creates_classification(self):
        downloaded=self.base/'downloaded';downloaded.mkdir()
        (downloaded/'SKILL.md').write_text('---\nname: video-editor\nversion: 1\ndescription: Video editing with FFmpeg\n---\nVideo editing using FFmpeg.')
        from maintenance import dispatch
        result=dispatch(self.m,{'action':'install-local-identify','path':str(downloaded),'target_root':str(self.roots)})
        plan=self.m.plan_install(result['id'],result['candidates'][0]['remote_path'])
        applied=self.m.apply(plan['id'])
        self.assertEqual(applied['classification']['category'],'设计与多媒体')

    def test_batch_continues_after_one_local_failure(self):
        original=self.m.discover_sources
        def discover(selector,remote=False):
            if selector==self.row('a')['id']:raise ValueError('corrupt input')
            return original(selector,remote)
        with patch.object(self.m,'discover_sources',side_effect=discover),patch.object(source_search,'query_channel',return_value=[]):
            batch=source_search.batch(self.m,10)
        self.assertEqual(batch['processed'],2)
        self.assertEqual(batch['items'][0]['trace'][0]['status'],'failed')

    def test_readded_channel_does_not_reuse_old_host_cache(self):
        self.m.r.update_channel('add',{'name':'team','kind':'git','base_url':'https://example.com/old/repo'})
        with patch.object(source_search,'query_channel',return_value=[]) as query:
            source_search.search(self.m,'test','team')
            self.m.r.update_channel('delete',{'name':'team'})
            self.m.r.update_channel('add',{'name':'team','kind':'git','base_url':'https://example.com/new/repo'})
            source_search.search(self.m,'test','team')
        self.assertEqual(query.call_count,2)

    def test_concurrent_manual_change_is_not_overwritten_or_undone(self):
        original=classification.classify
        def classify(m,row,apply=False):
            result=original(m,row,apply)
            if row['name']=='b':m.set_labels([self.row('a')['id']],category='内容创作',tags=['写作'])
            return result
        with patch.object(classification,'classify',side_effect=classify):out=classification.batch(self.m,True)
        classification.undo(self.m,out['id'])
        labels,_=classification.existing(self.m,str(self.roots/'a'))
        self.assertEqual(labels,{'category':'内容创作','tags':['写作']})

    def test_batch_run_skip_survives_expired_cooldowns(self):
        with patch.object(source_search,'query_channel',return_value=[]):
            first=source_search.batch(self.m,1)
            self.m.db.execute('update source_candidates set searched_at=0,payload=?',(json.dumps({'candidates':[],'trace':[],'next_attempt':0}),))
            self.m.db.commit()
            second=source_search.batch(self.m,1,skip_paths=[first['items'][0]['real_path']])
        self.assertNotEqual(first['items'][0]['real_path'],second['items'][0]['real_path'])
        self.assertEqual(second['remaining'],0)

    def test_tags_do_not_leak_from_unrelated_examples(self):
        self.skill('10x-chat','Chat with web AI agents using browser automation.\n'+'Example image video audio design '*100)
        self.m.scan()
        self.m.db.execute('update capabilities set description=? where name=?',('Chat with web AI agents using browser automation.','10x-chat'))
        row=self.row('10x-chat')
        result=classification.infer(self.m,row)
        self.assertEqual(result['category'],'工具与自动化')
        self.assertNotIn('音视频',result['tags'])


if __name__=='__main__':unittest.main()
