import base64
import hashlib
import json
import os
import time
import unittest
from unittest.mock import patch

import test_maintenance as fixtures
import daily_recommendations as d
import skill_discovery

TEXT = '---\nname: pdf-helper\ndescription: Extract tables from PDF files\n---\n' + ('Read the supplied PDF and extract tables into a spreadsheet. Confirm the destination first. ' * 3)
CONTENT = TEXT.encode()
SHA = hashlib.sha1(b'blob '+str(len(CONTENT)).encode()+b'\0'+CONTENT).hexdigest()


def remote(self, url, html=False):
    self.calls += 1
    if html:
        return '<h2><a href="/example/skills">Skills</a></h2>'
    if '/search/' in url:
        return {'items': [{'full_name': 'example/skills'}]}
    if '/git/trees/' in url:
        return {'tree': [{'path': 'skills/pdf/SKILL.md', 'type': 'blob', 'mode': '100644', 'sha': SHA, 'size': len(CONTENT)}]}
    if '/git/blobs/' in url:
        return {'encoding': 'base64', 'content': base64.b64encode(CONTENT).decode()}
    if '/releases/latest' in url:
        return {'tag_name': 'v1.0.0', 'body': 'First release'}
    return {'id': 123, 'full_name': 'example/skills', 'default_branch': 'main', 'stargazers_count': 50}


class DailyTests(unittest.TestCase):
    setUp = fixtures.MaintenanceTests.setUp
    tearDown = fixtures.MaintenanceTests.tearDown
    skill = fixtures.MaintenanceTests.skill

    def test_three_sources_verified_skill_and_same_day_cache(self):
        with patch.object(d.AnonymousGitHub, 'get', remote):
            result = d.refresh(self.m)
        self.assertEqual(len(result['items']), 1)
        self.assertEqual(result['items'][0]['signals'], ['new', 'topic', 'trending'])
        self.assertIn('/skills/pdf/SKILL.md', result['items'][0]['url'])
        self.assertEqual(result['request_count'], 11)
        with patch.object(d.AnonymousGitHub, 'get', side_effect=AssertionError('Must not fetch')):
            self.assertEqual(d.refresh(self.m)['items'], result['items'])
            self.assertEqual(skill_discovery.dispatch(self.m, {'action': 'discovery-cached', 'daily': True})['items'], result['items'])

    def test_next_day_deduplicates(self):
        with patch.object(d.AnonymousGitHub, 'get', remote), patch.object(d, 'today', return_value='2026-10-13'):
            d.refresh(self.m)
        with patch.object(d.AnonymousGitHub, 'get', remote), patch.object(d, 'today', return_value='2026-10-14'):
            result = d.refresh(self.m)
        self.assertEqual(result['items'], [])
        self.assertTrue(result['complete'])
        self.assertFalse(result['stale'])

    def test_outage_preserves_previous_and_backs_off(self):
        with patch.object(d.AnonymousGitHub, 'get', remote), patch.object(d, 'today', return_value='2026-10-13'):
            original = d.refresh(self.m)
        with patch.object(d, 'today', return_value='2026-10-14'), patch.object(d.AnonymousGitHub, 'get', side_effect=TimeoutError('offline')) as get:
            result = d.refresh(self.m)
            count = get.call_count
            d.refresh(self.m)
            self.assertEqual(get.call_count, count)
        self.assertEqual(result['items'], [])
        self.assertTrue(result['stale'])
        self.assertEqual(len(result['errors']), 7)

    def test_partial_failure_is_visible(self):
        def partial(client, url, html=False):
            if '/topics/' in url:
                raise TimeoutError('offline')
            return remote(client, url, html)
        with patch.object(d.AnonymousGitHub, 'get', partial):
            result = d.refresh(self.m)
        self.assertEqual(len(result['items']), 1)
        self.assertEqual(len(result['errors']), 3)
        self.assertFalse(result['complete'])

    def test_no_skill_file_not_recommended(self):
        def no_skill(client, url, html=False):
            return {'tree': []} if '/git/trees/' in url else remote(client, url, html)
        with patch.object(d.AnonymousGitHub, 'get', no_skill):
            self.assertTrue(all(i.get('selection') for i in d.refresh(self.m)['items']))

    def test_project_internal_skills_not_recommended(self):
        def internal(client, url, html=False):
            result = remote(client, url, html)
            if isinstance(result, dict) and result.get('id') == 123:
                result.update(full_name='example/skills', description='Agent skills')
            if '/git/trees/' in url:
                result['tree'][0]['path'] = '.agents/skills/changelog/SKILL.md'
            return result
        with patch.object(d.AnonymousGitHub, 'get', internal):
            self.assertTrue(all(i.get('selection') for i in d.refresh(self.m)['items']))

    def test_general_ai_project_not_a_skill_catalog(self):
        def general(client, url, html=False):
            result = remote(client, url, html)
            if isinstance(result, dict) and result.get('id') == 123:
                result.update(full_name='example/ai-editor', description='An AI editor')
            return result
        with patch.object(d.AnonymousGitHub, 'get', general):
            self.assertTrue(all(i.get('selection') for i in d.refresh(self.m)['items']))

    def test_corrupt_skill_not_recommended(self):
        def corrupt(client, url, html=False):
            return {'encoding': 'base64', 'content': 'YQ=='} if '/git/blobs/' in url else remote(client, url, html)
        with patch.object(d.AnonymousGitHub, 'get', corrupt):
            result = d.refresh(self.m)
        self.assertEqual(result['items'], [])
        self.assertTrue(result['errors'])

    def test_repeat_thresholds(self):
        old = {'stars': 2, 'major': 1, 'recommended_at': time.time()}
        self.assertIsNone(d.repeat_reason(old, 4, 1, ['trending'], time.time()))
        self.assertIn('关注度', d.repeat_reason(old, 102, 1, [], time.time()))
        self.assertIn('主版本', d.repeat_reason(old, 2, 2, [], time.time()))
        self.assertIn('再次', d.repeat_reason(old, 2, 1, ['trending'], time.time()+15*86400))
        self.assertEqual(d.major_version({'tag_name': 'v2.0.0', 'body': ''}), 0)

    def test_offline_first_use_does_not_invent_recommendations(self):
        with patch.object(d.AnonymousGitHub, 'get', side_effect=TimeoutError('offline')):
            result = d.refresh(self.m)
        self.assertEqual(result['items'], [])
        self.assertTrue(result['errors'])

    def test_old_policy_cache_requires_refresh(self):
        self.m.db.execute('insert into daily_recommendation_state values(1,?,?,0)',
                          (d.today(), json.dumps({'items':[], 'errors':[], 'complete':True})))
        self.m.db.commit()
        self.assertTrue(d.cached(self.m)['providers'][0]['needs_refresh'])

    def test_pool_does_not_supply_old_fillers(self):
        with patch.object(d.AnonymousGitHub, 'get', remote):
            original = d.refresh(self.m)
        self.assertEqual(self.m.db.execute('select count(*) from daily_recommendation_pool').fetchone()[0],1)
        filled = d.fill_recommendations(self.m, [original['items'][0]])
        self.assertEqual(len(filled),1)
        self.assertEqual(len({i['identity'] for i in filled}),1)
        self.assertFalse(filled[0].get('selection',False))

    def test_topic_and_readme_changes_are_not_recent_evidence(self):
        client = unittest.mock.Mock()
        client.get.return_value = []
        self.assertIsNone(d.timely_evidence(client,'api',{'created_at':'2020-01-01T00:00:00Z'}, {'path':'SKILL.md'}, {'topic'}))
        now=d.dt.datetime.now(d.dt.timezone.utc).isoformat()
        client.get.side_effect=[[{'sha':'a'*40,'commit':{'committer':{'date':now}}}],
                                {'files':[{'filename':'README.md','status':'modified','patch':'+'+'a'*80}]}]
        self.assertIsNone(d.timely_evidence(client,'api',{}, {'path':'SKILL.md'}, {'topic'}))

    def test_recent_instruction_update_has_evidence(self):
        now=d.dt.datetime.now(d.dt.timezone.utc).isoformat()
        client=unittest.mock.Mock()
        client.get.side_effect=[[{'sha':'a'*40,'commit':{'committer':{'date':now}}}],
                                {'files':[{'filename':'SKILL.md','status':'modified','patch':'+Validate every input before transforming the document.'}]}]
        evidence=d.timely_evidence(client,'api',{}, {'path':'SKILL.md'}, {'topic'})
        self.assertEqual(evidence['kind'],'updated')
        self.assertEqual(evidence['date'],now)

    def test_agent_variants_with_same_name_are_not_two_recommendations(self):
        def variants(client, url, html=False):
            result=remote(client,url,html)
            if '/git/trees/' in url:
                entry=result['tree'][0]
                result['tree']=[dict(entry,path='configs/'+agent+'/pdf/SKILL.md') for agent in ('a','b')]
            return result
        with patch.object(d.AnonymousGitHub,'get',variants):
            result=d.refresh(self.m)
        self.assertEqual(len(result['items']),1)

    def test_whitespace_change_is_not_recent_content_update(self):
        now=d.dt.datetime.now(d.dt.timezone.utc).isoformat()
        line='Validate every input before transforming the document.'
        client=unittest.mock.Mock()
        client.get.side_effect=[[{'sha':'a'*40,'commit':{'committer':{'date':now}}}],
                                {'files':[{'filename':'SKILL.md','status':'modified','patch':'-'+line+'\n+  '+line}]}]
        self.assertIsNone(d.timely_evidence(client,'api',{}, {'path':'SKILL.md'}, {'topic'}))

    def test_anonymous_request_even_with_environment_token(self):
        def fake_read(request, **kwargs):
            self.assertNotIn('authorization', {k.lower(): v for k,v in request.header_items()})
            self.assertEqual(kwargs['attempts'], 1)
            return b'{}'
        with patch.dict(os.environ, {'GITHUB_TOKEN': 'must-not-send'}), patch.object(d, 'read', fake_read):
            d.AnonymousGitHub(self.m).get('https://api.github.com/repos/example/skills')

    def test_budget_and_cooldown_no_request(self):
        client = d.AnonymousGitHub(self.m)
        client.calls = 40
        with patch.object(d, 'read') as get, self.assertRaises(RuntimeError):
            client.get('https://api.github.com/repos/example/skills')
        get.assert_not_called()
        client.calls = 0
        client.cooldowns['core'] = time.time()+3600
        with self.assertRaises(RuntimeError):
            client.get('https://api.github.com/repos/example/skills')

    def test_parser_only_heading_repositories(self):
        parser = d.RepositoryLinks()
        parser.feed('<a href="/ignore/me">ignore</a><h3><a href="/topics/ai">tag</a><a href="/example/skills">skill</a></h3>')
        self.assertEqual(parser.repos, ['example/skills'])


if __name__ == '__main__':
    unittest.main()
