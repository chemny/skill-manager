"""Configuration-only cache updates must not scan or weaken write protection."""
import json
import unittest
from unittest.mock import patch

import test_maintenance as fixtures
import maintenance as core
import web_execution


class AgentCacheRemovalTests(unittest.TestCase):
    setUp = fixtures.MaintenanceTests.setUp
    tearDown = fixtures.MaintenanceTests.tearDown
    skill = fixtures.MaintenanceTests.skill
    row = fixtures.MaintenanceTests.row

    def prepare(self):
        self.a.update_source('add', 'codex', str(self.roots))
        self.a.update_source('add', 'claude_code', str(self.roots))
        self.m.scan()
        return self.m.inventory()

    def test_removal_patches_snapshot_without_scanning_or_hashing(self):
        before = self.prepare()
        registered = {r['id'] for r in self.m.r.all_capabilities()}
        with patch.object(core, 'manifest', side_effect=AssertionError('No file hashing')), \
             patch.object(core.Manager, 'inventory', side_effect=AssertionError('No rebuild')):
            self.a.update_source('remove-agent', 'codex', '')
        after = self.m.cached_inventory()
        self.assertTrue(after['cache']['available'])
        self.assertFalse(after['cache']['dirty'])
        self.assertEqual(after['cache']['generated_at'], before['cache']['generated_at'])
        self.assertEqual(after['cache']['generation'], before['cache']['generation'] + 1)
        self.assertEqual({r['id'] for r in self.m.r.all_capabilities()}, registered)
        ids = {i['id'] for i in after['items']}
        self.assertEqual(len(ids), 4)
        self.assertFalse(any(i['platform'] == 'codex' for i in after['items']))
        self.assertTrue(all(i['known_agents'] == ['claude_code'] for i in after['items']))
        self.assertTrue(all(not i['shared'] and set(i['references']) <= ids for i in after['items']))
        self.assertFalse(after['shared_targets'])
        self.assertFalse(any(l['platform'] == 'codex' for l in after['locations']))
        self.assertEqual(after['space']['installed_unique'], before['space']['installed_unique'])
        self.assertEqual(set(after['cache']['recheck_ids']), ids)
        self.assertTrue(all(i['synced_at'] == next(b['synced_at'] for b in before['items'] if b['id'] == i['id']) for i in after['items']))
        refreshed = self.m.inventory(after['cache']['recheck_ids'], lightweight=True)
        self.assertTrue(refreshed['cache']['lightweight'])
        self.assertFalse(self.m.cached_inventory()['cache'].get('recheck_ids'))

    def test_dirty_snapshot_stays_dirty_and_keeps_pending_check_updates(self):
        self.prepare()
        row = next(i for i in self.m.cached_inventory()['items'] if i['platform'] == 'claude_code')
        check = {'status':'check_failed', 'checked_at':'2099-01-01T00:00:00Z', 'message':'offline'}
        self.m.db.execute('update maintenance_items set last_check=? where id=?', (json.dumps(check), row['id']))
        self.m.invalidate_cache()
        self.a.update_source('remove-agent', 'codex', '')
        after = self.m.cached_inventory()
        self.assertTrue(after['cache']['available'])
        self.assertTrue(after['cache']['dirty'])
        self.assertEqual(next(i for i in after['items'] if i['id'] == row['id'])['update_check']['status'], 'check_failed')

    def test_only_removed_agent_items_leave_space_totals(self):
        self.a.update_source('add', 'codex', str(self.roots))
        config = self.a.load_config()
        config['platforms']['shared']['enabled'] = False
        self.a.save_config(config)
        self.m.scan(); self.m.inventory()
        self.a.update_source('remove-agent', 'codex', '')
        after = self.m.cached_inventory()
        self.assertTrue(after['cache']['available'])
        self.assertEqual(after['items'], [])
        self.assertEqual(after['space']['installed_unique'], {'bytes':0, 'allocated_bytes':0})
        self.assertEqual(after['insights']['source_coverage']['total_targets'], 0)
        self.assertTrue((self.roots/'a/SKILL.md').is_file())

    def test_removed_agent_protection_still_blocks_shared_directory_uninstall(self):
        self.prepare()
        removed = next(dict(r) for r in self.m.r.all_capabilities() if r['platform'] == 'codex' and r['name'] == 'a')
        self.m.protect(removed, True)
        self.a.update_source('remove-agent', 'codex', '')
        remaining = next(i for i in self.m.cached_inventory()['items'] if i['name'] == 'a')
        refs = web_execution.uninstall_references(self.m, [remaining['id']])['references'][remaining['id']]
        self.assertTrue(any(r['platform'] == 'codex' and r['protected'] for r in refs))
        with self.assertRaisesRegex(ValueError, 'protected'):
            self.m.plan('delete', self.m.select(remaining['id']))
        self.assertTrue((self.roots/'a/SKILL.md').is_file())

    def test_missing_or_invalid_snapshot_is_not_fabricated(self):
        for payload in (None, '{bad', '{}'):
            with self.subTest(payload=payload):
                self.m.db.execute('update maintenance_inventory_cache set payload=? where id=1', (payload,))
                self.m.db.commit()
                self.a.update_source('remove-agent', 'codex', '')
                self.assertFalse(self.m.cached_inventory()['cache']['available'])

    def test_unrelated_installations_keep_freshness_and_do_not_need_recheck(self):
        root=self.base/'codex-only';root.mkdir()
        skill=root/'c';skill.mkdir();(skill/'SKILL.md').write_text('---\nname: c\n---\nCodex only')
        self.a.update_source('add','codex',str(root));self.m.scan()
        before=self.m.inventory()
        remaining={i['id']:i['synced_at'] for i in before['items'] if i['platform']!='codex'}
        self.a.update_source('remove-agent','codex','')
        after=self.m.cached_inventory()
        self.assertTrue(after['cache']['available'])
        self.assertEqual(after['cache']['recheck_ids'],[])
        self.assertEqual({i['id']:i['synced_at'] for i in after['items']},remaining)
        expected={key:sum(i['target_space'][key] for i in after['items']) for key in ('bytes','allocated_bytes')}
        self.assertEqual(after['space']['installed_unique'],expected)
        self.assertTrue((skill/'SKILL.md').is_file())
