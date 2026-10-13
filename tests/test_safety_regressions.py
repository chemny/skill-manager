"""Safety regressions use only isolated ASM_HOME and simulated downloads."""
import json
import errno
import os
from pathlib import Path
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

import test_maintenance as fixtures
import maintenance as core
import recovery
import update_providers as providers
from component_ownership import Ownership


class SafetyRegressionTests(unittest.TestCase):
    setUp = fixtures.MaintenanceTests.setUp
    tearDown = fixtures.MaintenanceTests.tearDown
    skill = fixtures.MaintenanceTests.skill
    row = fixtures.MaintenanceTests.row

    def test_partial_backup_copy_is_not_reported_available(self):
        row=self.row('a');before=core.manifest(Path(row['path']))
        def full_disk(source,destination):
            destination.mkdir()
            (destination/'SKILL.md').write_text('partial')
            raise OSError(errno.ENOSPC,'injected disk full')
        with patch.object(core,'copy_path',full_disk):
            with self.assertRaises(OSError):self.m.backup(row)
        self.assertEqual(core.manifest(Path(row['path'])),before)
        self.assertEqual(self.m.history()['operations'][0]['state'],'failed')
        saved=self.m.db.execute('select state from maintenance_backups').fetchall()
        self.assertEqual([r['state'] for r in saved],['invalid'])

    def test_stage_disk_full_keeps_original_and_verified_backup(self):
        row=self.row('b');before=core.manifest(Path(row['path']))
        plan=self.m.plan('sync',row,self.row('a'),allow_changes=True)
        copy=core.copy_path
        def full_disk(source,destination):
            if destination.name.startswith('.asm-stage-'):
                raise OSError(errno.ENOSPC,'injected disk full')
            return copy(source,destination)
        with patch.object(core,'copy_path',full_disk):
            with self.assertRaises(ValueError):self.m.apply(plan['id'])
        self.assertEqual(core.manifest(Path(row['path'])),before)
        self.assertEqual(self.m.history()['operations'][0]['state'],'failed')
        for saved in self.m.db.execute('select id from maintenance_backups'):
            self.assertTrue(self.m.verify_backup(saved['id'])['valid'])

    def historical(self):
        root = self.base / 'historical'
        root.mkdir()
        item = root / 'saved'
        item.mkdir()
        (item / 'SKILL.md').write_text('---\nname: saved\nversion: 1.0\n---\nold')
        value = recovery.discover(self.m, str(root))
        return item, next(c for c in value['candidates'] if c['path'] == str(item))

    def remote_plan(self):
        row = self.row('a')
        self.m.bind_source(row, 'https://github.com/example/original', 'main', 'a')
        remote = self.base / 'remote'
        remote.mkdir()
        (remote / 'SKILL.md').write_text('---\nname: a\nversion: 2.0\n---\nremote')
        with patch.object(self.m, 'remote', return_value=(remote, self.m.source(row))):
            return self.m.plan('update', row, allow_changes=True)

    def deny_success(self):
        self.m.db.execute("""create trigger deny_success before update of state
            on maintenance_operations when new.state='succeeded'
            begin select raise(abort,'simulated bookkeeping failure'); end""")
        self.m.db.commit()

    def test_remote_version_survives_symlinked_os_temp_root(self):
        real = self.base / 'temp'
        real.mkdir()
        alias = self.base / 'temp-alias'
        alias.symlink_to(real, target_is_directory=True)
        row = self.row('a')
        self.m.bind_source(row, 'https://github.com/example/repo', path='a')
        def download(_row, folder):
            self.assertFalse(any(p.is_symlink() for p in folder.parents))
            skill = folder / 'a'
            skill.mkdir()
            (skill / 'SKILL.md').write_text('---\nname: a\nversion: 2.0\n---\nnew')
            return skill, self.m.source(row)
        with patch.object(tempfile, 'tempdir', str(alias)), patch.object(self.m, 'remote', side_effect=download):
            checked = self.m.check(row)
        self.assertEqual(checked['remote_version'], '2.0')
        self.assertEqual(checked['status'], 'update_available')
        self.assertEqual(recovery.skill_info(self.m, alias), {})

    def test_live_skills_are_not_discovered_as_historical_backups(self):
        self.m.protect(self.row('a'), True)
        value = recovery.discover(self.m, str(self.roots))
        self.assertFalse([c for c in value['candidates'] if str(self.roots) in c['path']])
        self.assertTrue(value['errors'])
        self.assertTrue((self.roots / 'a' / 'SKILL.md').is_file())

    def test_remote_version_survives_shared_batch_temp_root(self):
        real = self.base / 'archives'
        real.mkdir()
        alias = self.base / 'archives-alias'
        alias.symlink_to(real, target_is_directory=True)
        self.m._repository_archive_home = alias
        row = self.row('a')
        self.m.bind_source(row, 'https://github.com/example/repo', 'main', 'a')
        def archive(_info, _commit, folder):
            skill = folder / 'repo' / 'a'
            skill.mkdir(parents=True)
            (skill / 'SKILL.md').write_text('---\nname: a\nversion: 2.0\n---\nnew')
            return skill.parent
        with patch.object(self.a, 'github_json', return_value={'sha':'abc'}), patch.object(self.a, 'download_github_archive', side_effect=archive):
            checked = self.m.check(row)
        self.assertEqual(checked['remote_version'], '2.0')
        self.assertEqual(checked['status'], 'update_available')

    def test_cached_unknown_version_from_old_reader_is_rechecked(self):
        import version_policy
        row = self.row('a')
        self.m.bind_source(row, 'https://github.com/example/repo', 'main', 'a')
        self.m._use_check_cache = True
        remote = self.base / 'remote'
        remote.mkdir()
        (remote / 'SKILL.md').write_text('---\nname: a\nversion: 2.0\n---\nnew')
        with patch.object(self.m, 'remote', return_value=(remote, self.m.source(row))) as downloaded:
            with patch.object(version_policy, 'POLICY_VERSION', 3), patch.object(recovery, 'skill_info', return_value={}):
                first = self.m.check(row)
            second = self.m.check(row)
        self.assertEqual(first['remote_version'], 'unknown')
        self.assertEqual(second['status'], 'update_available')
        self.assertFalse(second['cache_hit'])
        self.assertEqual(downloaded.call_count, 2)

    def test_component_cannot_enter_historical_catalog(self):
        home = self.base / 'codex'
        system = home / 'skills' / '.system'
        item = system / 'skill-creator'
        item.mkdir(parents=True)
        (item / 'SKILL.md').write_text('---\nname: skill-creator\n---\nsystem')
        with patch.dict(os.environ, {'CODEX_HOME':str(home)}):
            value = recovery.discover(self.m, str(system))
        self.assertFalse([c for c in value['candidates'] if c['path'] == str(item)])
        self.assertTrue(item.exists())

    def test_historical_plan_rechecks_live_installation_at_apply(self):
        item, candidate = self.historical()
        plan = self.m.plan('purge', backup_id=candidate['id'])
        config = self.a.load_config()
        config['platforms']['shared']['roots'].append(str(item.parent))
        self.a.save_config(config)
        self.m.scan()
        with self.assertRaisesRegex(ValueError, 'installed'):
            self.m.apply(plan['id'])
        self.assertTrue(item.exists())

    def test_historical_recycle_rechecks_live_installation(self):
        item, candidate = self.historical()
        plan = self.m.plan('trash-backup', backup_id=candidate['id'])
        self.m.r.upsert_capability(self.a.capability_from_skill_md('shared', item, item / 'SKILL.md'))
        self.m.db.commit()
        with self.assertRaisesRegex(ValueError, 'installed'):
            self.m.apply(plan['id'])
        self.assertIsNone(self.m.db.execute('select * from maintenance_backup_trash where id=?', (candidate['id'],)).fetchone())

    def test_root_ancestor_alias_cannot_uninstall_shared_content(self):
        alias = self.base / 'profile-alias'
        alias.symlink_to(self.base, target_is_directory=True)
        config = self.a.load_config()
        config['platforms']['claude_code'] = {'enabled':True, 'roots':[str(alias / 'skills')]}
        self.a.save_config(config)
        self.m.scan()
        with self.assertRaisesRegex(ValueError, 'shared symlink'):
            self.m.plan('delete', self.row(str(alias / 'skills' / 'a')))
        self.assertTrue((self.roots / 'a').exists())

    def test_destination_root_revalidated_before_move(self):
        plan = self.m.plan('deactivate', self.row('a'))
        destination = self.a.disabled_root_for(self.roots)
        outside = self.base / 'outside'
        outside.mkdir()
        destination.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'shared symlink'):
            self.m.apply(plan['id'])
        self.assertTrue((self.roots / 'a').exists())
        self.assertFalse((outside / 'a').exists())

    def test_destination_root_revalidated_after_backup(self):
        plan = self.m.plan('deactivate', self.row('a'))
        destination = self.a.disabled_root_for(self.roots)
        outside = self.base / 'outside'
        outside.mkdir()
        original = self.m.make_backup
        def backup(*args, **kwargs):
            result = original(*args, **kwargs)
            destination.symlink_to(outside, target_is_directory=True)
            return result
        with patch.object(self.m, 'make_backup', side_effect=backup):
            with self.assertRaisesRegex(ValueError, 'shared symlink'):
                self.m.apply(plan['id'])
        self.assertTrue((self.roots / 'a').exists())
        self.assertFalse((outside / 'a').exists())

    def test_system_owned_link_is_not_a_user_uninstall_target(self):
        home = self.base / 'codex'
        system = home / 'skills' / '.system'
        system.mkdir(parents=True)
        link = system / 'skill-creator'
        link.symlink_to(self.roots / 'a', target_is_directory=True)
        user_link = self.roots / 'system-reference'
        user_link.symlink_to(link, target_is_directory=True)
        with patch.dict(os.environ, {'CODEX_HOME':str(home)}):
            self.assertTrue(Ownership().classify(link)['read_only'])
            self.assertFalse(Ownership().classify(user_link)['read_only'])
            with self.assertRaises(ValueError):
                self.m.validate_target(link)
            self.m.scan()
            self.m.apply(self.m.plan('delete', self.row(str(user_link)))['id'])
        self.assertTrue(link.is_symlink())
        self.assertTrue((self.roots / 'a').exists())

    def test_source_rebind_invalidates_prepared_update(self):
        plan = self.remote_plan()
        before = core.manifest(self.roots / 'a')
        self.m.bind_source(self.row('a'), 'https://github.com/example/replacement', 'main', 'a')
        with self.assertRaisesRegex(ValueError, 'Bound source changed'):
            self.m.apply(plan['id'])
        self.assertEqual(core.manifest(self.roots / 'a'), before)
        self.assertFalse(self.m.backups()['backups'])

    def test_source_change_during_download_rejects_staging(self):
        row = self.row('a')
        self.m.bind_source(row, 'https://github.com/example/original', path='a')
        def download(_row, folder):
            old = self.m.source(row)
            self.m.bind_source(row, 'https://github.com/example/replacement', path='a')
            return self.roots / 'b', old
        with patch.object(self.m, 'remote', side_effect=download):
            with self.assertRaisesRegex(ValueError, 'Bound source changed'):
                self.m.plan('update', row, allow_changes=True)

    def test_source_change_during_backup_cannot_install_old_payload(self):
        plan = self.remote_plan()
        before = core.manifest(self.roots / 'a')
        backup = self.m.make_backup
        def changed(*args, **kwargs):
            result = backup(*args, **kwargs)
            self.m.bind_source(self.row('a'), 'https://github.com/example/replacement', path='a')
            return result
        with patch.object(self.m, 'make_backup', side_effect=changed):
            with self.assertRaisesRegex(ValueError, 'Bound source changed'):
                self.m.apply(plan['id'])
        self.assertEqual(core.manifest(self.roots / 'a'), before)

    def test_recreated_channel_cannot_substitute_a_repository(self):
        self.m.r.update_channel('add', {'name':'team', 'kind':'git', 'base_url':'https://old.example/repo'})
        row = self.row('a')
        self.m.bind_source(row, 'https://old.example/repo', path='a')
        original = self.m.source(row)
        self.m.r.update_channel('delete', {'name':'team'})
        self.m.r.update_channel('add', {'name':'team', 'kind':'git', 'base_url':'https://new.example/repo'})
        self.assertEqual(self.m.source(row), original)
        with patch.object(self.a, 'clone_git_repository') as clone:
            with self.assertRaisesRegex(ValueError, '重新绑定'):
                providers.download(self.a, original, self.base)
        clone.assert_not_called()

    def test_recreated_channel_cannot_substitute_a_skillhub_service(self):
        self.m.r.update_channel('add', {'name':'team', 'kind':'skillhub', 'base_url':'https://old.example'})
        src = providers.source(self.a, 'https://old.example/api/v1/skills/a')
        self.m.r.update_channel('delete', {'name':'team'})
        self.m.r.update_channel('add', {'name':'team', 'kind':'skillhub', 'base_url':'https://new.example'})
        with patch.object(providers, 'api_json') as request:
            with self.assertRaisesRegex(ValueError, '重新绑定'):
                providers.download(self.a, src, self.base)
        request.assert_not_called()

    def test_old_binding_without_channel_recovers_only_its_exact_endpoint(self):
        self.m.r.update_channel('add', {'name':'team', 'kind':'git', 'base_url':'https://old.example/repo'})
        row = self.row('a')
        original = {'provider':'git','url':'https://old.example/repo','ref':'main','path':'a'}
        self.m.state(row)
        self.m.db.execute('update maintenance_items set source=? where id=?', (json.dumps(original), row['id']))
        self.m.db.commit()
        self.assertEqual(self.m.source(row)['channel'], 'team')
        self.m.r.update_channel('delete', {'name':'team'})
        self.m.r.update_channel('add', {'name':'team', 'kind':'git', 'base_url':'https://new.example/repo'})
        self.assertEqual(self.m.source(row), original)
        with self.assertRaisesRegex(ValueError, '重新绑定'):
            providers.validate_source(self.a, self.m.source(row))

    def test_legacy_binding_update_uses_existing_database_connection(self):
        self.m.r.update_channel('add', {'name':'team', 'kind':'git', 'base_url':'https://old.example/repo'})
        row = self.row('a')
        self.m.state(row)
        original = {'provider':'git','url':'https://old.example/repo','ref':'main','path':'a'}
        self.m.db.execute('update maintenance_items set source=? where id=?', (json.dumps(original), row['id']))
        self.m.db.commit()
        remote = self.base / 'remote'
        remote.mkdir()
        (remote / 'SKILL.md').write_text('---\nname: a\nversion: 2.0\n---\nnew')
        with patch.object(self.m, 'remote', return_value=(remote, self.m.source(row))):
            plan = self.m.plan('update', row, allow_changes=True)
        with patch.object(self.a, 'Registry', side_effect=AssertionError('nested connection during replacement')):
            result = self.m.apply(plan['id'])
        self.assertEqual(result['state'], 'succeeded')
        self.assertIn('version: 2.0', (self.roots / 'a' / 'SKILL.md').read_text())

    def test_registered_install_keeps_channel_binding_for_later_updates(self):
        self.m.r.update_channel('add', {'name':'team', 'kind':'git', 'base_url':'https://old.example/repo'})
        def download(_url, folder):
            skill = folder / 'incoming'
            skill.mkdir()
            (skill / 'SKILL.md').write_text('---\nname: incoming\nversion: 1.0\n---\ncontent')
            return skill, {'source_type':'git','source_url':'https://old.example/repo','source_ref':'main','source_path':'','channel':'team','slug':''}
        with patch.object(self.a, 'install_source_from_url', side_effect=download):
            selection = self.m.identify_install('https://old.example/repo', str(self.roots))
        plan = self.m.plan_install(selection['id'], selection['candidates'][0]['remote_path'])
        self.assertEqual(plan['source']['channel'], 'team')
        self.m.apply(plan['id'])
        installed = self.m.source(self.row(str(self.roots / 'incoming')))
        self.assertEqual(installed['channel'], 'team')
        self.assertEqual(providers.validate_source(self.a, installed)['base_url'], 'https://old.example/repo')

    def test_install_refuses_changed_channel_before_writing(self):
        self.m.r.update_channel('add', {'name':'team', 'kind':'git', 'base_url':'https://old.example/repo'})
        source = self.base / 'incoming'
        source.mkdir()
        (source / 'SKILL.md').write_text('---\nname: incoming\n---\ncontent')
        selection = core.dispatch(self.m, {'action':'install-local-identify','path':str(source),'target_root':str(self.roots)})
        plan = self.m.plan_install(selection['id'], selection['candidates'][0]['remote_path'])
        plan['source'] = {'provider':'git','channel':'team','url':'https://old.example/repo','ref':'main','path':''}
        self.m.db.execute('update maintenance_plans set payload=? where id=?', (json.dumps(plan), plan['id']))
        self.m.db.commit()
        self.m.r.update_channel('delete', {'name':'team'})
        self.m.r.update_channel('add', {'name':'team', 'kind':'git', 'base_url':'https://new.example/repo'})
        with self.assertRaisesRegex(ValueError, '重新绑定'):
            self.m.apply(plan['id'])
        self.assertFalse((self.roots / 'incoming').exists())

    def test_explicit_install_root_named_backups_is_still_protected(self):
        root = self.base / '.backups'
        item = root / 'live'
        item.mkdir(parents=True)
        (item / 'SKILL.md').write_text('---\nname: live\n---\ninstalled')
        config = self.a.load_config()
        config['platforms']['shared']['roots'].append(str(root))
        self.a.save_config(config)
        self.m.scan()
        with self.assertRaisesRegex(ValueError, 'installed'):
            recovery.ensure_storage_safe(self.m, item)

    def test_late_database_failure_restores_files_and_database(self):
        before = core.manifest(self.roots / 'b')
        row = self.row('b')
        state = self.m.state(row)
        plan = self.m.plan('sync', row, self.row('a'), allow_changes=True)
        self.m.db.execute("""create trigger deny_health before update of health on capabilities
            begin select raise(abort,'simulated health failure'); end""")
        self.m.db.commit()
        with self.assertRaisesRegex(ValueError, 'simulated health failure'):
            self.m.apply(plan['id'])
        self.assertEqual(core.manifest(self.roots / 'b'), before)
        self.assertEqual(self.m.state(row)['baseline'], state['baseline'])
        self.assertEqual(self.m.get_plan(plan['id'])['state'], 'failed')
        self.assertTrue(self.m.backups()['backups'])

    def test_success_record_failure_rolls_back_replacement(self):
        before = core.manifest(self.roots / 'b')
        plan = self.m.plan('sync', self.row('b'), self.row('a'), allow_changes=True)
        self.deny_success()
        with self.assertRaisesRegex(ValueError, 'bookkeeping failure'):
            self.m.apply(plan['id'])
        self.assertEqual(core.manifest(self.roots / 'b'), before)
        self.assertEqual(self.m.history()['operations'][0]['state'], 'failed')

    def test_success_record_failure_rolls_back_new_install(self):
        source = self.base / 'incoming'
        source.mkdir()
        (source / 'SKILL.md').write_text('---\nname: incoming\ndescription: fixture\n---\ncontent')
        selection = core.dispatch(self.m, {'action':'install-local-identify','path':str(source),'target_root':str(self.roots)})
        plan = self.m.plan_install(selection['id'], selection['candidates'][0]['remote_path'])
        self.deny_success()
        with self.assertRaisesRegex(ValueError, 'bookkeeping failure'):
            self.m.apply(plan['id'])
        self.assertFalse((self.roots / 'incoming').exists())
        self.assertFalse(self.m.db.execute("select id from capabilities where name='incoming'").fetchone())

    def test_success_record_failure_rolls_back_deactivation(self):
        row = self.row('a')
        plan = self.m.plan('deactivate', row)
        self.deny_success()
        with self.assertRaisesRegex(ValueError, 'bookkeeping failure'):
            self.m.apply(plan['id'])
        self.assertTrue((self.roots / 'a').exists())
        self.assertFalse(Path(plan['destination']).exists())
        self.assertEqual(self.m.select(row['id'])['path'], str(self.roots / 'a'))

    def test_success_record_failure_rolls_back_uninstall(self):
        before = core.manifest(self.roots / 'a')
        plan = self.m.plan('delete', self.row('a'))
        self.deny_success()
        with self.assertRaisesRegex(ValueError, 'bookkeeping failure'):
            self.m.apply(plan['id'])
        self.assertEqual(core.manifest(self.roots / 'a'), before)
        self.assertEqual(self.row('a')['health'], 'ok')

    def test_certificate_failures_never_disable_verification(self):
        calls = [lambda:self.a.gitlab_json('https://gitlab.example/api'),
                 lambda:self.a.download_zip_archive('https://example.com/skill.zip', self.base),
                 lambda:self.a.read_http_text('https://example.com/skills')]
        for call in calls:
            with self.subTest(call=call):
                error = urllib.error.URLError('CERTIFICATE_VERIFY_FAILED')
                with patch.object(self.a.urllib.request, 'urlopen', side_effect=error) as request:
                    with self.assertRaises(urllib.error.URLError):
                        call()
                self.assertEqual(request.call_count, 1)
                self.assertNotIn('context', request.call_args.kwargs)

    def test_scan_full_reconciliation_includes_new_skills_and_cleans_cache(self):
        self.m.inventory()
        self.skill('newly-installed', 'new')
        self.m.scan()
        self.assertTrue(self.m.cached_inventory()['cache']['dirty'])
        value = self.m.inventory()
        self.assertIn('newly-installed', [i['name'] for i in value['items']])
        cached = self.m.cached_inventory()
        self.assertFalse(cached['cache']['dirty'])
        self.assertEqual(cached['cache']['generation'], value['cache']['generation'])

    def test_scan_buttons_request_full_reconciliation_not_old_ids(self):
        html = (fixtures.SCRIPTS.parent / 'assets' / 'maintenance.html').read_text()
        handlers = html.split("el('scan').onclick=", 1)[1].split("el('checkVisible').onclick=", 1)[0]
        self.assertEqual(handlers.count('await refresh(viewScope(),true)'), 2)
        self.assertNotIn('await refresh();', handlers)


if __name__ == '__main__':
    unittest.main()
