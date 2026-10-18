import io
import hashlib
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
import zipfile

import test_maintenance as fixtures
import update_providers as providers
from skill_locations import locations


class UpdateProviderTests(unittest.TestCase):
    setUp = fixtures.MaintenanceTests.setUp
    tearDown = fixtures.MaintenanceTests.tearDown
    skill = fixtures.MaintenanceTests.skill
    row = fixtures.MaintenanceTests.row

    def archive(self, text='remote', version='2.0'):
        body = io.BytesIO()
        with zipfile.ZipFile(body, 'w') as zf:
            zf.writestr('SKILL.md', f'---\nname: a\nversion: {version}\n---\n{text}')
        return body.getvalue()

    def test_custom_channels_validation_and_builtin_protection(self):
        for url in ('http://example.com', 'https://secret@example.com', 'https://example.com?token=secret'):
            with self.assertRaises(ValueError):
                self.m.r.update_channel('add', {'name':'bad', 'kind':'skillhub', 'base_url':url})
        registry = self.m.r
        registry.update_channel('add', {'name':'team', 'label':'Team', 'kind':'skillhub', 'base_url':'https://example.com'})
        source = providers.source(self.a, 'https://example.com/api/v1/skills/a')
        self.assertEqual(source['channel'], 'team')
        with self.assertRaises(ValueError):
            registry.update_channel('add', {'name':'skillhub','kind':'skillhub','base_url':'https://example.com'})
        with self.assertRaises(ValueError): registry.update_channel('delete', {'name':'skillhub'})
        registry.update_channel('toggle', {'name':'team', 'enabled':False})
        with self.assertRaises(ValueError): providers.service(self.a, 'team')

    def test_skillhub_install_check_update_creates_backup(self):
        url = 'https://api.skillhub.cn/api/v1/skills/a'
        def reply(channel, suffix, limit=0):
            if suffix.startswith('/api/v1/download'): return self.archive()
            if '/files?' in suffix:
                with zipfile.ZipFile(io.BytesIO(self.archive())) as zf:
                    return json.dumps({'files':[{'path':n, 'sha256':hashlib.sha256(zf.read(n)).hexdigest()} for n in zf.namelist()]}).encode()
            return json.dumps({'latestVersion':{'version':'2.0'}}).encode()
        self.m.bind_source(self.row('a'), url)
        with patch.object(providers, 'request', side_effect=reply):
            checked = self.m.check(self.row('a'))
            self.assertEqual(checked['status'], 'update_available')
            self.assertEqual(checked['remote_version'], '2.0')
            self.assertEqual(checked['update_decision'], 'review')
            plan = self.m.plan('update', self.row('a'), allow_changes=True)
            self.m.apply(plan['id'])
            self.assertIn('remote', (self.roots/'a/SKILL.md').read_text())
            self.assertTrue(self.m.backups()['backups'])
            self.assertEqual(self.m.check(self.row('a'))['status'], 'latest')
            self.assertEqual(self.m.source(self.row('a'))['ref'], '')
            install = self.m.identify_install(url, str(self.roots))
            self.assertEqual(install['source']['source_type'], 'skillhub')
            self.assertEqual(install['source']['source_ref'], '')

    def test_search_does_not_bind_same_named_source(self):
        def reply(channel, suffix):
            return {'data':{'skills':[{'slug':'a', 'name':'a', 'version':'2.0'}]}}
        with patch.object(providers, 'api_json', side_effect=reply):
            found = providers.search(self.a, 'a', 'skillhub')
        self.assertEqual(len(found['candidates']), 1)
        self.assertIsNone(self.m.source(self.row('a')))

    def test_channel_capabilities_and_local_connection(self):
        channels={c['name']:c for c in self.m.r.update_channels()}
        self.assertTrue(channels['skillhub']['search_supported'])
        self.assertTrue(channels['skillsmp']['search_supported'])
        self.assertTrue(channels['gitlab']['search_supported'])
        self.assertTrue(channels['gitcode']['test_supported'])
        self.assertFalse(channels['gitcode']['search_supported'])
        self.assertNotIn('find-skills', channels)
        self.assertFalse(self.m.r.channel_enabled('find-skills'))
        self.assertFalse(channels['local']['search_supported'])
        self.assertIn('无需网络',providers.test_connection(self.a,'local')['message'])
        self.assertEqual(next(c for c in self.m.r.update_channels() if c['name']=='local')['last_test_outcome'],'success')

    def test_gitlab_connection_checks_search_endpoint(self):
        with patch.object(providers, 'api_json', return_value=[{'name':'agent-skills'}]) as api:
            result = providers.test_connection(self.a, 'gitlab')
        self.assertIn('搜索可用', result['message'])
        self.assertIn('/api/v4/projects?', api.call_args.args[1])

    def test_repository_connection_failure_is_recorded(self):
        with patch.object(providers.urllib.request,'urlopen',side_effect=TimeoutError('offline')):
            with self.assertRaises(TimeoutError): providers.test_connection(self.a,'gitcode')
        channel=next(c for c in self.m.r.update_channels() if c['name']=='gitcode')
        self.assertEqual(channel['last_test_outcome'],'failure')
        self.assertFalse(channel['search_supported'])

    def test_traversal_and_symlinks_rejected(self):
        src = providers.source(self.a, 'https://api.skillhub.cn/api/v1/skills/a')
        for filename in ('../outside', '/outside'):
            body = io.BytesIO()
            with zipfile.ZipFile(body,'w') as zf: zf.writestr(filename, 'bad')
            with patch.object(providers, 'api_json', return_value={'latestVersion':{'version':'1.0'}}), patch.object(providers, 'request', return_value=body.getvalue()):
                folder=self.base/('download-'+str(len(filename)));folder.mkdir()
                with self.assertRaises(ValueError): providers.download(self.a, src, folder)

    def test_custom_agent_empty_root_and_shared_directory(self):
        empty=self.base/'empty';empty.mkdir()
        self.a.update_source('add', 'my-agent', str(empty), label='My Agent')
        self.a.update_source('add', 'my-agent', str(self.roots), label='My Agent')
        from skill_locations import agent_catalog
        added=next(a for a in agent_catalog(self.a.load_config()) if a['id']=='my-agent')
        self.assertTrue(added['added'])
        self.assertEqual(added['label'], 'My Agent')
        entries,_=locations(self.a.load_config())
        self.assertEqual(next(e for e in entries if e['platform']=='my-agent')['agent'], 'my-agent')
        self.m.scan()
        item=next(i for i in self.m.inventory()['items'] if i['path']==str(self.roots/'a'))
        self.assertEqual(item['known_agents'], ['my-agent'])

    def test_skillhub_rejects_hash_mismatch_and_wrong_metadata(self):
        src = providers.source(self.a, 'https://api.skillhub.cn/api/v1/skills/a')
        for mode in ('hash', 'metadata'):
            body = io.BytesIO()
            text = '---\nname: a\n---\nremote'
            with zipfile.ZipFile(body, 'w') as zf:
                zf.writestr('SKILL.md', text)
                if mode == 'metadata':
                    zf.writestr('_meta.json', json.dumps({'slug':'other', 'version':'1.0'}))
            def reply(channel, suffix):
                if '/files?' in suffix:
                    return {'files':[{'path':'SKILL.md','sha256':'wrong' if mode=='hash' else hashlib.sha256(text.encode()).hexdigest()}]}
                return {'latestVersion':{'version':'1.0'}}
            folder = self.base / mode
            folder.mkdir()
            with patch.object(providers, 'api_json', side_effect=reply), patch.object(providers, 'request', return_value=body.getvalue()):
                with self.assertRaises(ValueError): providers.download(self.a, src, folder)

    def test_redirect_drops_credentials_and_rejects_downgrade(self):
        import urllib.request
        req = urllib.request.Request('https://api.example.com/download', headers={'X-API-Key':'secret', 'Authorization':'Bearer secret'})
        handler = providers.PublicRedirect()
        redirected = handler.redirect_request(req, None, 302, 'Found', {}, 'https://cdn.example.com/file')
        self.assertIsNone(redirected.get_header('X-api-key'))
        self.assertIsNone(redirected.get_header('Authorization'))
        with self.assertRaises(ValueError):
            handler.redirect_request(req, None, 302, 'Found', {}, 'http://cdn.example.com/file')


if __name__ == '__main__': unittest.main()
