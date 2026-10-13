"""Evidence-based advice for historical backups, with no network or deletion."""
import unittest
from pathlib import Path
from unittest.mock import patch
import test_maintenance as fixtures
import maintenance as core
import recovery


class HistoricalAdviceTests(unittest.TestCase):
    setUp=fixtures.MaintenanceTests.setUp
    tearDown=fixtures.MaintenanceTests.tearDown
    skill=fixtures.MaintenanceTests.skill
    row=fixtures.MaintenanceTests.row

    def historical(self, count=1, name='a-1.0.0'):
        root=self.base/'historical';root.mkdir(exist_ok=True)
        for n in range(count):
            parent=root/('batch-'+str(n));parent.mkdir()
            core.copy_path(self.roots/'a',parent/name)
        with patch.object(recovery,'locations',return_value=[root]):recovery.discover(self.m)
        return [b for b in self.m.backups(False)['backups'] if b.get('external')]

    def assess(self, rows):
        before={b['path']:core.manifest(core.Path(b['path'])) for b in rows}
        self.m.db.execute('pragma query_only=on')
        try:
            with patch.object(self.m,'remote',side_effect=AssertionError('No cloud requests')):
                result=recovery.backup_advice(self.m,[b['id'] for b in rows])
        finally:self.m.db.execute('pragma query_only=off')
        for path,value in before.items():self.assertEqual(core.manifest(core.Path(path)),value)
        return {b['id']:b['cleanup_advice'] for b in result['items']}

    def test_single_historical_copy_is_compared_and_kept(self):
        rows=self.historical();advice=self.assess(rows)[rows[0]['id']]
        self.assertEqual(advice['code'],'keep')
        self.assertIn('当前内容',advice['summary'])

    def test_advice_cache_skips_comparison_and_invalidates_only_related_items(self):
        bid=self.m.backup(self.row('a'))['id']
        first=recovery.backup_advice(self.m,[bid])
        self.assertEqual(first['cache']['computed'],1)
        with patch.object(recovery,'backup_detail',side_effect=AssertionError('cache must skip comparison')):
            second=recovery.backup_advice(self.m,[bid])
            self.assertEqual(second['cache']['hits'],1)
            self.m.backup(self.row('b'))
            self.assertEqual(recovery.backup_advice(self.m,[bid])['cache']['hits'],1)
        (self.roots/'a'/'nested').mkdir()
        (self.roots/'a'/'nested'/'edit.txt').write_text('changed')
        self.assertEqual(recovery.backup_advice(self.m,[bid])['cache']['computed'],1)

    def test_advice_cache_expires_and_persists_across_connections(self):
        from backup_advice_cache import Cache
        bid=self.m.backup(self.row('a'))['id']
        recovery.backup_advice(self.m,[bid])
        with patch.object(recovery,'backup_detail',side_effect=AssertionError('cache must persist')):
            self.assertEqual(recovery.backup_advice(self.m,[bid])['cache']['hits'],1)
        cache=Cache(self.m)
        cache.db.execute('update advice set saved=saved-3601');cache.db.commit();cache.close()
        self.assertEqual(recovery.backup_advice(self.m,[bid])['cache']['computed'],1)

    def test_retention_change_invalidates_cached_advice(self):
        bid=self.m.backup(self.row('a'))['id']
        recovery.backup_advice(self.m,[bid])
        recovery.retain(self.m,bid,True)
        result=recovery.backup_advice(self.m,[bid])
        self.assertEqual(result['cache']['computed'],1)
        self.assertEqual(result['items'][0]['cleanup_advice']['reason'],'已标记防止误删')

    def test_cache_permission_failure_does_not_block_analysis(self):
        import backup_advice_cache
        bid=self.m.backup(self.row('a'))['id']
        with patch.object(backup_advice_cache.os,'chmod',side_effect=PermissionError('cache permissions')):
            result=recovery.backup_advice(self.m,[bid])
        self.assertEqual(result['items'][0]['cleanup_advice']['code'],'keep')

    def test_unexpected_analysis_failure_closes_cache_and_context(self):
        from backup_advice_cache import Cache
        bid=self.m.backup(self.row('a'))['id']
        cache=Cache(self.m)
        try:
            with patch('backup_advice_cache.Cache',return_value=cache), patch.object(recovery,'backup_detail',side_effect=RuntimeError('injected failure')):
                with self.assertRaises(RuntimeError):recovery.backup_advice(self.m,[bid])
            self.assertIsNone(cache.db)
            self.assertFalse(hasattr(self.m,'_advice_rows'))
        finally:cache.close()

    def test_concurrent_requests_share_one_comparison(self):
        from concurrent.futures import ThreadPoolExecutor
        bid=self.m.backup(self.row('a'))['id']
        with patch.object(recovery,'backup_detail',wraps=recovery.backup_detail) as detail:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results=list(pool.map(lambda _:core.api_dispatch(self.a,{'action':'backup-advice','ids':[bid]}),range(2)))
        self.assertEqual(detail.call_count,1)
        self.assertEqual(sum(r['cache']['hits'] for r in results),1)

    def test_transient_analysis_error_is_not_a_completed_review_and_can_recover(self):
        bid=self.m.backup(self.row('a'))['id']
        with patch.object(recovery,'backup_detail',side_effect=OSError('temporary read failure')):
            failed=recovery.backup_advice(self.m,[bid])['items'][0]
        self.assertIsNone(failed['cleanup_advice'])
        self.assertIn('temporary read failure',failed['error'])
        recovered=recovery.backup_advice(self.m,[bid])['items'][0]
        self.assertEqual(recovered['cleanup_advice']['code'],'keep')
        self.assertNotIn('error',recovered)

    def test_one_failed_analysis_does_not_discard_other_results(self):
        first=self.m.backup(self.row('a'))['id'];second=self.m.backup(self.row('b'))['id']
        original=recovery.backup_detail
        def analyze(m,bid,row):
            if bid==first:raise PermissionError('cannot read this backup')
            return original(m,bid,row)
        with patch.object(recovery,'backup_detail',side_effect=analyze):
            items={i['id']:i for i in recovery.backup_advice(self.m,[first,second])['items']}
        self.assertIsNone(items[first]['cleanup_advice'])
        self.assertEqual(items[second]['cleanup_advice']['code'],'keep')
        self.assertFalse(hasattr(self.m,'_advice_rows'))

    def test_exact_duplicates_keep_one_deterministic_copy(self):
        rows=self.historical(3);result=self.assess(rows)
        kept=[bid for bid,a in result.items() if a['code']=='keep']
        self.assertEqual(len(kept),1)
        for a in result.values():
            if a['code']=='cleanable':self.assertEqual(a['keeper_id'],kept[0])

    def test_retained_duplicate_is_always_the_keeper(self):
        rows=self.historical(2);recovery.retain(self.m,rows[0]['id'],True)
        result=self.assess(rows)
        self.assertEqual(result[rows[0]['id']]['code'],'keep')
        self.assertEqual(result[rows[1]['id']]['keeper_id'],rows[0]['id'])

    def test_modified_backup_does_not_become_disposable(self):
        rows=self.historical(2)
        (core.Path(rows[0]['path'])/'notes').write_text('unique work')
        result=self.assess(rows)
        self.assertTrue(all(a['code']!='cleanable' for a in result.values()))

    def test_different_permissions_are_not_complete_duplicates(self):
        rows=self.historical(2)
        (Path(rows[0]['path'])/'SKILL.md').chmod(0o600)
        with patch.object(recovery,'locations',return_value=[self.base/'historical']):recovery.discover(self.m)
        rows=[b for b in self.m.backups(False)['backups'] if b.get('external')]
        self.assertTrue(all(a['code']!='cleanable' for a in self.assess(rows).values()))

    def test_missing_installation_has_specific_reason(self):
        rows=self.historical(name='orphan');(core.Path(rows[0]['path'])/'SKILL.md').write_text('old content without metadata')
        with patch.object(recovery,'locations',return_value=[self.base/'historical']):recovery.discover(self.m)
        rows=[b for b in self.m.backups(False)['backups'] if b.get('external')]
        advice=self.assess(rows)[rows[0]['id']]
        self.assertEqual(advice['label'],'自行判断');self.assertEqual(advice['summary'],'未找到对应安装项')

    def test_different_contents_are_not_deleted_based_on_name_or_old_version(self):
        rows=self.historical();self.skill('a','new content')
        advice=self.assess(rows)[rows[0]['id']]
        self.assertEqual(advice['code'],'review');self.assertEqual(advice['summary'],'与安装项内容不同')

    def test_nested_backup_reference_is_preserved(self):
        rows=self.historical();(self.roots/'reference').symlink_to(core.Path(rows[0]['path']),target_is_directory=True)
        self.m.scan();advice=self.assess(rows)[rows[0]['id']]
        self.assertEqual(advice['code'],'keep');self.assertIn('引用',advice['summary'])


if __name__=='__main__':unittest.main()
