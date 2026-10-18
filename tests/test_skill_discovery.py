import base64
import hashlib
import json
import os
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from unittest.mock import patch

import test_maintenance as fixtures
import maintenance
import skill_discovery as d
import task_scopes


def claw(owner='example', slug='demo'):
    return {'ownerHandle': owner, 'slug': slug, 'displayName': slug,
            'summary': 'Demo description', 'stats': {'stars': 3, 'downloads': 25},
            'latestVersion': {'version': '1.0'}}


def skillsmp(name='demo', github=None):
    return {'id': 'example-' + name, 'name': name, 'author': 'example',
            'description': 'Demo description', 'stars': 5,
            'skillUrl': 'https://skillsmp.com/creators/example/' + name,
            'githubUrl': github or 'https://github.com/example/skills/tree/main/skills/' + name}


def file_response(text):
    content = text.encode()
    return {'type': 'file', 'encoding': 'base64', 'size': len(content),
            'sha': hashlib.sha1(b'blob ' + str(len(content)).encode() + b'\0' + content).hexdigest(),
            'content': base64.b64encode(content).decode()}


class DiscoveryTests(unittest.TestCase):
    setUp = fixtures.MaintenanceTests.setUp
    tearDown = fixtures.MaintenanceTests.tearDown
    skill = fixtures.MaintenanceTests.skill
    row = fixtures.MaintenanceTests.row

    def test_skillhub_category_paging_cache_and_identity(self):
        rows = [{'slug': 'demo', 'name': 'Demo', 'homepage': 'https://api.skillhub.cn/'+owner+'/demo',
                 'category': 'office-efficiency', 'downloads': 30, 'description_zh': '中文说明'} for owner in ('a', 'b')]
        def remote(url, provider):
            self.assertEqual(provider, 'skillhub')
            if '/categories' in url:
                return {'items': [{'key': 'office-efficiency', 'name': '办公效率'}]}
            self.assertIn('category=office-efficiency', url)
            return {'code': 0, 'data': {'total': 80, 'skills': rows}}
        payload = {'provider': 'skillhub', 'view': 'trending', 'category': 'office-efficiency', 'page': 2}
        with patch.object(d, 'remote_json', side_effect=remote) as get:
            result = d.refresh(self.m, payload)
            self.assertEqual(len(result['items']), 2)
            self.assertNotEqual(result['items'][0]['id'], result['items'][1]['id'])
            self.assertEqual(result['total'], 80)
            self.assertEqual(result['categories'][0]['name'], '办公效率')
            self.assertIn('page=2', get.call_args_list[0].args[0])
            d.refresh(self.m, payload)
            self.assertEqual(get.call_count, 2)
            self.assertEqual(d.cached(self.m, dict(payload, page=1))['items'], [])

    def test_all_catalog_pages_and_clawhub_cursor(self):
        calls = []
        def remote(url, provider):
            calls.append(url)
            if provider == 'skillhub':
                if '/categories' in url:
                    return {'items': [{'key': 'office-efficiency', 'name': '办公效率'}]}
                page = 2 if 'page=2&' in url else 1
                return {'code': 0, 'data': {'total': 61, 'skills': [
                    {'slug': str(page), 'name': 'Page ' + str(page), 'homepage': 'https://api.skillhub.cn/example/' + str(page)}]}}
            return {'items': [claw(slug='page-2' if 'cursor=next-page' in url else 'page-1')],
                    'nextCursor': None if 'cursor=next-page' in url else 'next-page'}
        with patch.object(d, 'remote_json', side_effect=remote):
            first = d.refresh(self.m, {'provider': 'skillhub', 'view': 'all', 'page': 1})
            second = d.refresh(self.m, {'provider': 'skillhub', 'view': 'all', 'page': 2})
            claw_first = d.refresh(self.m, {'provider': 'clawhub', 'view': 'all', 'page': 1})
            claw_second = d.refresh(self.m, {'provider': 'clawhub', 'view': 'all', 'page': 2})
        self.assertEqual(first['total'], 61)
        self.assertEqual([i['name'] for i in second['items']], ['Page 2'])
        self.assertEqual(first['categories'][0]['name'], '办公效率')
        self.assertTrue(claw_first['has_more'])
        self.assertFalse(claw_second['has_more'])
        self.assertEqual([i['slug'] for i in claw_second['items']], ['page-2'])
        self.assertTrue(any('cursor=next-page' in url for url in calls))
        self.assertEqual(d.cached(self.m, {'provider': 'clawhub', 'view': 'all', 'page': 1})['items'][0]['slug'], 'page-1')

    def test_catalog_supports_pages_beyond_first_thousand(self):
        self.assertEqual(d.selection({'provider':'skillhub','view':'all','page':1001}),('skillhub','',1001))

    def test_skillhub_installed_status_uses_exact_slug_not_name(self):
        row=self.row('a')
        self.m.db.execute('insert or replace into maintenance_items(id,source) values (?,?)',
                          (row['id'],json.dumps({'provider':'skillhub','slug':'exact-skill'})))
        self.m.db.execute('insert into discovery_cache(cache_key,provider,fetched_at,attempted_at,payload) values (?,?,?,?,?)',
                          (d.request_key('skillhub','all','',{'provider':'skillhub','view':'all','page':1}),
                           'skillhub',time.time(),time.time(),json.dumps({'items':[
                               {'id':'same','provider':'skillhub','slug':'exact-skill','name':'different name','github_url':'','url':'https://api.skillhub.cn/a/exact-skill'},
                               {'id':'other','provider':'skillhub','slug':'a','name':'a','github_url':'','url':'https://api.skillhub.cn/b/a'}],
                               'total':2,'categories':[]})))
        items=d.cached(self.m,{'provider':'skillhub','view':'all','page':1})['items']
        self.assertEqual(items[0]['installed'],'source_match')
        self.assertEqual(items[1]['installed'],'name_only')

    def test_clawhub_download_uses_bounded_zip_installer(self):
        with patch.object(self.a,'download_zip_archive',return_value=self.roots) as download:
            root,source=self.a.install_source_from_url('https://clawhub.ai/api/v1/download?slug=demo',self.base)
        self.assertEqual(root,self.roots)
        self.assertEqual(source['source_type'],'clawhub')
        self.assertEqual(source['slug'],'demo')
        download.assert_called_once_with('https://clawhub.ai/api/v1/download?slug=demo',self.base)
        with self.assertRaises(ValueError):
            self.a.install_source_from_url('https://clawhub.ai/api/v1/download?slug=demo&other=1',self.base)
        with patch.object(self.a,'download_zip_archive',side_effect=zipfile.BadZipFile):
            with self.assertRaisesRegex(ValueError,'未提供可安装'):
                self.a.install_source_from_url('https://clawhub.ai/api/v1/download?slug=demo',self.base)

    def test_search_filters_description_matches_and_sorts_titles_across_source_pages(self):
        names = ['guide ' + str(i) for i in range(30)] + ['my-pdf-tool', 'pdf', 'tool-pdf', 'pdf-reader']
        def remote(url, provider):
            if '/categories' in url:
                return {'items': []}
            page = 1 if 'page=1&' in url else 2
            batch = names[:30] if page == 1 else names[30:]
            return {'code': 0, 'data': {'total': len(names), 'skills': [
                {'slug': str(index), 'name': name, 'homepage': 'https://api.skillhub.cn/example/' + str(index),
                 'description_zh': 'PDF document tool'}
                for index, name in enumerate(batch, 0 if page == 1 else 30)]}}
        payload = {'provider': 'skillhub', 'view': 'search', 'query': 'PDF'}
        with patch.object(d, 'remote_json', side_effect=remote) as get:
            result = d.refresh(self.m, payload)
        self.assertEqual([item['name'] for item in result['items']], ['pdf', 'pdf-reader', 'my-pdf-tool', 'tool-pdf'])
        self.assertEqual(result['total'], 4)
        self.assertFalse(result['search_limited'])
        self.assertEqual(get.call_count, 3)
        self.assertEqual(d.cached(self.m, dict(payload, page=2))['items'], [])

    def test_search_reports_when_candidate_limit_may_hide_titles(self):
        def remote(url, provider):
            if '/categories' in url:
                return {'items': []}
            return {'code': 0, 'data': {'total': 500, 'skills': [
                {'slug': 'candidate-' + str(i), 'name': 'other-' + str(i),
                 'homepage': 'https://api.skillhub.cn/example/' + str(i)} for i in range(30)]}}
        with patch.object(d, 'remote_json', side_effect=remote) as get:
            result = d.refresh(self.m, {'provider': 'skillhub', 'view': 'search', 'query': 'pdf'})
        self.assertEqual(result['items'], [])
        self.assertTrue(result['search_limited'])
        self.assertEqual(get.call_count, d.SEARCH_CANDIDATE_PAGES + 1)

    def test_search_paginates_after_ranking_and_filters_other_providers(self):
        names = ['pdf-' + str(i) for i in range(31)] + ['other-pdf']
        def remote(url, provider):
            if '/categories' in url:
                return {'items': []}
            page = 1 if 'page=1&' in url else 2
            batch = names[:30] if page == 1 else names[30:]
            return {'code': 0, 'data': {'total': len(names), 'skills': [
                {'slug': name, 'name': name, 'homepage': 'https://api.skillhub.cn/example/' + name}
                for name in batch]}}
        payload = {'provider': 'skillhub', 'view': 'search', 'query': 'pdf'}
        with patch.object(d, 'remote_json', side_effect=remote):
            first = d.refresh(self.m, payload)
        second = d.cached(self.m, dict(payload, page=2))
        self.assertEqual(first['total'], 32)
        self.assertEqual(len(first['items']), 30)
        self.assertEqual([item['name'] for item in second['items']], ['pdf-9', 'other-pdf'])

        rows = [dict(claw(slug='unrelated'), summary='pdf utility'), claw(slug='pdf-reader'), claw(slug='helper-pdf')]
        with patch.object(d, 'remote_json', return_value={'items': rows}):
            other = d.refresh(self.m, {'provider': 'clawhub', 'view': 'search', 'query': 'pdf'})
        self.assertEqual([item['name'] for item in other['items']], ['pdf-reader', 'helper-pdf'])
        self.assertEqual(other['total'], 2)

    def test_discovery_rejects_unsupported_combinations(self):
        for payload in ({'provider': 'skillhub', 'view': 'new'}, {'provider': 'skillsmp', 'view': 'trending'},
                        {'provider': 'clawhub', 'category': 'office'}, {'provider': 'skillhub', 'page': -1}):
            with self.assertRaises(ValueError):
                d.cached(self.m, payload)

    def populate(self, view='search', query='demo'):
        def remote(url, provider):
            return {'items': [claw()]} if provider == 'clawhub' else {'success': True, 'data': {'skills': [skillsmp()]}}
        with patch.object(d, 'remote_json', side_effect=remote):
            return d.refresh(self.m, {'view': view, 'query': query})

    def candidate(self):
        return next(i for i in self.populate()['items'] if i['provider'] == 'skillsmp')

    def verify(self, **changes):
        item = self.candidate()
        response = file_response('---\nname: demo\ndescription: Demo\n---\n# Demo\n')
        response.update(changes)
        with patch.object(d, 'remote_json', side_effect=[{'sha': 'a' * 40}, response]) as get:
            return item, d.verify(self.m, {'id': item['id']}), get

    def test_trends_and_new_use_official_claw_sorts_only(self):
        with patch.object(d, 'remote_json', return_value={'items': [claw()]}) as get:
            d.refresh(self.m, {'view': 'trending'})
            d.refresh(self.m, {'view': 'new'})
        urls = [c.args[0] for c in get.call_args_list]
        self.assertIn('sort=trending', urls[0]); self.assertIn('sort=createdAt', urls[1])
        self.assertTrue(all('nonSuspiciousOnly=true' in u for u in urls))
        self.assertTrue(all(c.args[1] == 'clawhub' for c in get.call_args_list))

    def test_search_joins_providers_but_not_same_named_identities(self):
        result = self.populate()
        self.assertEqual(len(result['items']), 2)
        self.assertEqual(len({i['id'] for i in result['items']}), 2)

    def test_only_verified_same_git_location_is_grouped(self):
        url = 'https://github.com/example/skills/tree/' + 'a' * 40 + '/skills/demo'
        items = [
            {'id': 'one', 'provider': 'clawhub', 'name': 'demo', 'url': 'https://clawhub.ai/a/demo',
             'verification_fresh': True, 'verification': {'install_url': url}},
            {'id': 'two', 'provider': 'skillsmp', 'name': 'demo', 'url': 'https://skillsmp.com/a/demo',
             'verification_fresh': True, 'verification': {'install_url': url}},
            {'id': 'three', 'provider': 'skillsmp', 'name': 'demo', 'url': 'https://skillsmp.com/b/demo',
             'verification_fresh': False, 'verification': {'install_url': url}},
        ]
        grouped = d.group_verified_copies(items)
        self.assertEqual([item['id'] for item in grouped], ['one', 'three'])
        self.assertEqual(grouped[0]['alternatives'][0]['id'], 'two')

    def test_claw_owner_is_part_of_identity(self):
        items = d.normalize('clawhub', {'items': [claw('a'), claw('b'), claw('a')]})
        self.assertEqual(len(items), 2); self.assertNotEqual(items[0]['id'], items[1]['id'])

    def test_success_cached_across_managers_and_manual_refresh(self):
        self.populate()
        with patch.object(d, 'fetch', side_effect=AssertionError('Repeated request')):
            other = maintenance.Manager(self.a)
            try:
                self.assertEqual(len(d.refresh(other, {'view': 'search', 'query': 'demo'})['items']), 2)
            finally:
                other.db.close()

    def test_search_ttl_is_separate_from_catalog_ttl(self):
        self.populate(); self.populate('trending')
        with patch.object(d.time, 'time', return_value=time.time() + 7 * 3600):
            self.assertFalse(d.cached(self.m, {'view': 'search', 'query': 'demo'})['providers'][0]['needs_refresh'])
            self.assertTrue(d.cached(self.m, {'view': 'trending'})['providers'][0]['needs_refresh'])

    def test_failure_preserves_stale_result_and_other_provider(self):
        first = self.populate()
        self.m.db.execute('update discovery_cache set fetched_at=?', (time.time() - 2 * 86400,)); self.m.db.commit()
        def fetch(provider, *args):
            if provider == 'clawhub': raise TimeoutError()
            return d.normalize('skillsmp', {'success': True, 'data': {'skills': [skillsmp('other')]}})
        with patch.object(d, 'fetch', side_effect=fetch):
            after = d.refresh(self.m, {'view': 'search', 'query': 'demo'})
        self.assertIn(first['items'][0]['id'], {i['id'] for i in after['items']})
        self.assertTrue(after['providers'][0]['error']); self.assertFalse(after['providers'][1]['error'])

    def test_rate_limit_is_provider_wide_and_cannot_be_bypassed_by_new_query(self):
        error = urllib.error.HTTPError('https://clawhub.ai', 429, 'limited', {'Retry-After': '300'}, None)
        with patch.object(d, 'fetch', side_effect=error) as get:
            d.refresh(self.m, {'view': 'trending'})
            d.refresh(self.m, {'view': 'new'})
        self.assertEqual(get.call_count, 1)
        self.assertFalse(d.cached(self.m, {'view': 'new'})['providers'][0]['needs_refresh'])

    def test_failed_query_retries_after_cooldown(self):
        with patch.object(d, 'fetch', side_effect=TimeoutError()) as get:
            d.refresh(self.m, {'view': 'trending'}); d.refresh(self.m, {'view': 'trending'})
        self.assertEqual(get.call_count, 1)
        with patch.object(d.time, 'time', return_value=time.time() + 61), patch.object(d, 'fetch', return_value=[]):
            self.assertFalse(d.refresh(self.m, {'view': 'trending'})['providers'][0]['error'])

    def test_failed_http200_envelope_is_not_empty_success(self):
        with self.assertRaises(ValueError):d.normalize('skillsmp', {'success': False, 'error': 'quota'})
        with self.assertRaises(ValueError):d.normalize('clawhub', {'error': 'bad'})

    def test_no_local_binding_or_file_changes_during_collection(self):
        files = {str(p): p.read_bytes() for p in self.roots.rglob('*') if p.is_file()}
        before = [tuple(r) for r in self.m.db.execute('select id,source,last_check from maintenance_items')]
        self.populate()
        self.assertEqual(before, [tuple(r) for r in self.m.db.execute('select id,source,last_check from maintenance_items')])
        self.assertEqual(files, {str(p): p.read_bytes() for p in self.roots.rglob('*') if p.is_file()})
        self.assertFalse(list((self.home/'plans').iterdir()))

    def test_same_name_is_not_installed_identity(self):
        self.skill('demo', 'local'); self.m.scan()
        result = self.populate()
        self.assertTrue(all(i['installed'] == 'name_only' for i in result['items']))

    def test_only_exact_registered_source_path_matches_installation(self):
        self.m.bind_source(self.row('a'), 'https://github.com/example/skills', 'main', 'skills/demo')
        result = self.populate()
        gh = next(i for i in result['items'] if i['provider'] == 'skillsmp')
        self.assertEqual(gh['installed'], 'source_match')
        self.assertEqual(next(i for i in result['items'] if i['provider'] == 'clawhub')['installed'], 'unknown')

    def test_browse_uses_read_only_connection_even_before_new_schema_exists(self):
        self.m.db.executescript('drop table discovery_cache;drop table discovery_candidates;drop table discovery_cooldowns;')
        with patch.object(maintenance.Manager, '__init__', side_effect=AssertionError('Writer initialized')):
            result = maintenance.api_dispatch(self.a, {'action': 'discovery-cached'})
        self.assertEqual(result['items'], []); self.assertTrue(result['providers'][0]['needs_refresh'])

    def test_verify_pins_commit_hashes_blob_and_only_reads_known_path(self):
        item, result, get = self.verify()
        self.assertIn('verification', result)
        self.assertEqual(get.call_count, 2)
        self.assertIn('/contents/skills/demo/SKILL.md?ref=' + 'a'*40, get.call_args_list[-1].args[0])
        self.assertEqual(result['verification']['install_url'], 'https://github.com/example/skills/tree/' + 'a'*40 + '/skills/demo')
        with patch.object(d, 'remote_json', side_effect=AssertionError('Repeated verification')):
            self.assertTrue(d.verify(self.m, {'id': item['id']})['from_cache'])

    def test_wrong_blob_symlink_and_oversized_file_cannot_be_installed(self):
        for changes in ({'sha': 'b'*40}, {'type': 'symlink'}, {'size': 128001}):
            with self.subTest(changes=changes):
                _, result, _ = self.verify(**changes)
                self.assertIn('error', result); self.assertNotIn('verification', result)

    def test_metadata_identity_mismatch_is_not_verified(self):
        item = self.candidate()
        response = file_response('---\nname: another\ndescription: Demo\n---\n')
        with patch.object(d, 'remote_json', side_effect=[{'sha': 'a'*40}, response]):
            self.assertIn('error', d.verify(self.m, {'id': item['id']}))

    def test_invalid_candidate_id_or_client_supplied_url_cannot_fetch(self):
        with patch.object(d, 'remote_json', side_effect=AssertionError('Network used')):
            with self.assertRaises(ValueError):d.verify(self.m, {'id': 'not-saved', 'url': 'http://localhost'})
        for url in ('http://github.com/a/b/tree/main/x', 'https://github.com.evil/a/b/tree/main/x',
                    'https://github.com/a/b/tree/main/../private', 'https://github.com/a/b/tree/main/%2e%2e/x',
                    'https://github.com/a/b/tree/main/x?token=secret', 'https://user:secret@github.com/a/b/tree/main/x'):
            with self.subTest(url=url), self.assertRaises(ValueError):d.github_location(url)

    def test_source_change_invalidates_verification(self):
        item, _, _ = self.verify()
        self.m.db.execute('update discovery_cache set fetched_at=1'); self.m.db.commit()
        def get(url, provider):
            return {'items': [claw()]} if provider=='clawhub' else {'success': True, 'data': {'skills': [skillsmp(github='https://github.com/other/repo/tree/main/demo')]}}
        with patch.object(d, 'remote_json', side_effect=get):
            result = d.refresh(self.m, {'view': 'search', 'query': 'demo'})
        self.assertIsNone(next(i for i in result['items'] if i['id']==item['id'])['verification'])

    def test_redirect_cannot_forward_token_to_other_host(self):
        req = urllib.request.Request('https://skillsmp.com/api/v1/skills/search', headers={'Authorization': 'Bearer secret'})
        with self.assertRaises(ValueError):d.SameHostRedirect().redirect_request(req, None, 302, '', {}, 'https://evil.example/path')

    def test_optional_api_key_not_returned_or_stored(self):
        with patch.dict(os.environ, {'SKILLSMP_API_KEY': 'private-value'}), patch.object(d, 'read', return_value=b'{"success":true}') as get:
            d.remote_json('https://skillsmp.com/api/v1/skills/search?q=pdf', 'skillsmp')
        self.assertEqual(get.call_args.args[0].get_header('Authorization'), 'Bearer private-value')
        self.assertNotIn('private-value', json.dumps(self.populate()))

    def test_discovery_jobs_do_not_lock_skill_writes(self):
        scope = task_scopes.describe(self.a, {'action': 'discovery-refresh', 'view': 'trending'})
        self.assertEqual(scope['lane'], 'network'); self.assertFalse(scope['write'])
        self.assertFalse(scope['global']); self.assertEqual(scope['resources'], [])

    def test_invalid_or_empty_queries_fail_before_network(self):
        for payload in ({'view': 'search'}, {'view': 'arbitrary'}, {'view': 'search', 'query': 'a'*161}, {'view': 'search', 'query': 'a\nb'}):
            with self.subTest(payload=payload), patch.object(d, 'fetch', side_effect=AssertionError('Network used')):
                with self.assertRaises(ValueError):d.refresh(self.m, payload)

    def test_result_count_is_bounded(self):
        self.assertEqual(len(d.normalize('clawhub', {'items': [claw(slug=str(i)) for i in range(100)]})), 50)

    def test_list_does_not_repeat_full_file_previews(self):
        item, result, _ = self.verify()
        self.assertTrue(result['verification']['preview'])
        listed = next(i for i in d.cached(self.m, {'view': 'search', 'query': 'demo'})['items'] if i['id']==item['id'])
        self.assertNotIn('preview', listed['verification'])
        self.assertIn('file_url', listed['verification'])

    def test_verification_rejects_changed_source_before_network(self):
        item = self.candidate()
        with patch.object(d, 'remote_json', side_effect=AssertionError('Network used')):
            result = d.verify(self.m, {'id': item['id'], 'github_url': 'https://github.com/old/repo/tree/main/demo'})
        self.assertIn('来源信息已变化', result['error'])

    def test_rate_limit_uses_longest_finite_server_deadline(self):
        error = urllib.error.HTTPError('https://clawhub.ai', 429, '', {'Retry-After': '30', 'X-RateLimit-Reset': str(time.time()+300)}, None)
        self.assertGreater(d.failure(error)[1], time.time()+290)
        error.headers = {'Retry-After': 'inf', 'RateLimit-Reset': '120'}
        self.assertLessEqual(d.failure(error)[1], time.time()+121)
        self.assertGreater(d.failure(error)[1], time.time()+110)


if __name__ == '__main__':
    unittest.main()
