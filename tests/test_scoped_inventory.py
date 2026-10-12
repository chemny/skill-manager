import unittest
from unittest.mock import patch
import test_maintenance as fixtures
import maintenance


class ScopedInventoryTests(unittest.TestCase):
    setUp = fixtures.MaintenanceTests.setUp
    tearDown = fixtures.MaintenanceTests.tearDown
    skill = fixtures.MaintenanceTests.skill
    row = fixtures.MaintenanceTests.row

    def test_scoped_refresh_reads_only_requested_files(self):
        full = self.m.inventory()
        original = maintenance.size
        paths = []
        def size(path, *args, **kwargs):
            paths.append(str(path))
            return original(path, *args, **kwargs)
        with patch.object(maintenance, 'size', size):
            scoped = self.m.inventory([self.row('a')['id']])
        self.assertEqual([i['name'] for i in scoped['items']], ['a'])
        self.assertNotIn(str(self.roots/'b'), paths)
        self.assertEqual(len(self.m.cached_inventory()['items']), len(full['items']))

    def test_empty_scope_does_not_expand_to_all(self):
        self.assertEqual(self.m.inventory([])['items'], [])

    def test_scope_includes_only_related_aliases(self):
        (self.roots/'alias').symlink_to(self.roots/'a', target_is_directory=True)
        self.m.scan()
        result = self.m.inventory([self.row(str(self.roots/'a'))['id']])
        self.assertEqual({i['path'] for i in result['items']},
                         {str(self.roots/'a'), str(self.roots/'alias')})

    def test_invalid_scope_rejected(self):
        for ids in ('a', [None], [3]):
            with self.assertRaises(ValueError):
                self.m.inventory(ids)
