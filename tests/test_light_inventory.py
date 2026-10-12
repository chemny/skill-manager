import json
from pathlib import Path
import unittest
from unittest.mock import patch
import test_maintenance as fixtures
import maintenance


class LightInventoryTests(unittest.TestCase):
    setUp = fixtures.MaintenanceTests.setUp
    tearDown = fixtures.MaintenanceTests.tearDown
    skill = fixtures.MaintenanceTests.skill
    row = fixtures.MaintenanceTests.row

    def test_light_refresh_never_walks_hashes_discovers_sources_or_updates_usage(self):
        before = self.m.inventory()
        with patch('maintenance.size', side_effect=AssertionError('tree walk')), \
                patch('maintenance.manifest', side_effect=AssertionError('hash tree')), \
                patch('library_metadata.file_candidates', side_effect=AssertionError('source discovery')), \
                patch.object(self.m, 'source', side_effect=AssertionError('source subprocess')), \
                patch.object(self.m.r, 'all_capabilities', side_effect=AssertionError('usage writes')):
            refreshed = self.m.inventory(lightweight=True)
        self.assertTrue(refreshed['cache']['lightweight'])
        self.assertEqual(refreshed['space'], before['space'])
        self.assertEqual(len(refreshed['items']), len(before['items']))

    def test_light_refresh_updates_metadata_and_preserves_newer_remote_notice(self):
        aid = self.row('a')['id']
        self.m.inventory()
        check = {'status':'update_available', 'local_version':'1.0', 'remote_version':'1.3', 'local_fingerprint':'old', 'baseline_known':False}
        self.m.db.execute('update maintenance_items set last_check=?,protected=1 where id=?', (json.dumps(check), aid))
        self.m.db.commit()
        (self.roots/'a/SKILL.md').write_text('---\nname: a\nversion: 1.1\ndescription: new description\n---\n')
        value = self.m.inventory([aid], lightweight=True)['items'][0]
        self.assertEqual(value['version'], '1.1')
        self.assertEqual(value['description'], 'new description')
        self.assertEqual(value['update_check']['status'], 'update_available')
        self.assertEqual(value['update_check']['local_version'], '1.1')
        self.assertTrue(value['protected'])

    def test_light_refresh_retains_checks_when_only_nested_contents_change(self):
        aid = self.row('a')['id']
        self.m.inventory()
        checked = {'status':'latest', 'local_fingerprint':'last-verified', 'checked_at':maintenance.stamp()}
        self.m.db.execute('update maintenance_items set last_check=? where id=?', (json.dumps(checked), aid))
        self.m.db.commit()
        (self.roots/'a/notes.txt').write_text('new local content')
        value = self.m.inventory([aid], lightweight=True)['items'][0]
        self.assertEqual(value['update_check'], checked)
        # A lightweight display is not evidence that content was fully verified.
        self.assertTrue(self.m.inventory([aid], lightweight=True)['cache']['lightweight'])

    def test_scoped_refresh_does_not_renew_other_items_timestamp(self):
        with patch('maintenance.stamp', return_value='2026-10-09T00:00:00+00:00'):
            self.m.inventory()
        with patch('maintenance.stamp', return_value='2026-10-09T00:16:00+00:00'):
            self.m.inventory([self.row('a')['id']], lightweight=True)
        items = {i['name']:i for i in self.m.cached_inventory()['items']}
        self.assertEqual(items['a']['synced_at'], '2026-10-09T00:16:00+00:00')
        self.assertEqual(items['b']['synced_at'], '2026-10-09T00:00:00+00:00')

    def test_aliases_share_one_metadata_read(self):
        (self.roots/'alias').symlink_to(self.roots/'a', target_is_directory=True)
        self.m.scan(); self.m.inventory()
        original, reads = Path.read_text, []
        def read(path, *args, **kwargs):
            if path.name == 'SKILL.md':
                reads.append(str(path.resolve()))
            return original(path, *args, **kwargs)
        with patch.object(Path, 'read_text', read):
            self.m.inventory(lightweight=True)
        self.assertEqual(reads.count(str(self.roots/'a/SKILL.md')), 1)

    def test_missing_cache_gets_initial_full_snapshot_and_empty_scope_stays_empty(self):
        self.assertFalse(self.m.inventory(lightweight=True)['cache']['lightweight'])
        self.assertEqual(self.m.inventory([], lightweight=True)['items'], [])

    def test_disappeared_file_is_reported_without_sizing(self):
        self.m.inventory()
        aid = self.row('a')['id']
        maintenance.remove_path(self.roots/'a')
        with patch('maintenance.size', side_effect=AssertionError('tree walk')):
            value = self.m.inventory([aid], lightweight=True)['items'][0]
        self.assertFalse(value['present'])
        self.assertEqual(value['target_space']['allocated_bytes'], 0)
