"""Conservative update and backup advice without network or real user data."""
import json
import unittest
from unittest.mock import patch
import test_maintenance as fixtures
import maintenance as core
import recovery
import version_policy


class VersionPolicyTests(unittest.TestCase):
    setUp=fixtures.MaintenanceTests.setUp
    tearDown=fixtures.MaintenanceTests.tearDown
    skill=fixtures.MaintenanceTests.skill
    row=fixtures.MaintenanceTests.row

    def assess(self, local, remote, base, lv='1.0', rv='2.0'):
        return version_policy.assess(local,remote,base,lv,rv,self.a.version_gt)

    def test_clean_baseline_allows_remote_update(self):
        base=core.manifest(self.roots/'a');self.skill('a','remote')
        result=self.assess(base,core.manifest(self.roots/'a'),base)
        self.assertEqual(result['update_decision'],'update')

    def test_local_additions_edits_and_deletions_are_protected(self):
        for kind in ('added','changed','removed'):
            with self.subTest(kind=kind):
                self.skill('a','original');p=self.roots/'a';(p/'extra').write_text('base')
                base=core.manifest(p)
                if kind=='added':(p/'new').write_text('local')
                if kind=='changed':(p/'extra').write_text('local')
                if kind=='removed':(p/'extra').unlink()
                now=core.manifest(p)
                self.assertEqual(self.assess(now,base,base,rv='1.0')['update_decision'],'keep_local')
                remote=dict(base);remote.pop('SKILL.md')
                self.assertEqual(self.assess(now,remote,base)['update_decision'],'review')
                if (p/'new').exists():(p/'new').unlink()

    def test_unknown_ancestry_ahead_and_identical_content(self):
        a=core.manifest(self.roots/'a');b=core.manifest(self.roots/'b')
        self.assertEqual(self.assess(a,b,None)['update_decision'],'review')
        for base in (None,a):
            self.assertEqual(self.assess(a,b,base,'3.0','2.0')['update_decision'],'keep_local')
        self.assertEqual(self.assess(a,a,None)['status'],'latest')
        self.assertEqual(self.assess(a,b,a,'unknown','2.0')['update_decision'],'review')

    def test_remote_newer_remains_visible_with_local_changes_or_unknown_baseline(self):
        base=core.manifest(self.roots/'a')
        (self.roots/'a'/'notes').write_text('local work')
        now=core.manifest(self.roots/'a');remote=core.manifest(self.roots/'b')
        for baseline in (None,base):
            result=self.assess(now,remote,baseline)
            self.assertEqual(result['status'],'update_available')
            self.assertEqual(result['update_decision'],'review')
            self.assertTrue(result['remote_update_available'])

    def test_equal_unknown_or_ahead_versions_do_not_claim_new_release(self):
        a=core.manifest(self.roots/'a');b=core.manifest(self.roots/'b')
        for lv,rv in [('1.0','1.0.0'),('unknown','2.0'),('1.0','unknown'),('3.0','2.0')]:
            self.assertNotEqual(self.assess(a,b,a,lv,rv)['status'],'update_available')
        self.assertEqual(self.assess(a,b,a,'1.0+local.99','2.0')['status'],'update_available')

    def test_saved_checks_are_reinterpreted_without_modifying_database_or_files(self):
        row=self.row('a');before=core.manifest(self.roots/'a')
        saved={'status':'baseline_unknown','baseline_known':False,'local_changes':None,
               'local_version':'1.0','remote_version':'2.0','local_fingerprint':core.fingerprint(before)}
        self.m.state(row)
        self.m.db.execute('update maintenance_items set last_check=? where id=?',(json.dumps(saved),row['id']));self.m.db.commit()
        self.m.inventory()
        with patch.object(self.m,'remote',side_effect=AssertionError('Unexpected cloud query')):
            result=self.m.cached_inventory()
        check=next(i for i in result['items'] if i['id']==row['id'])['update_check']
        self.assertEqual(check['status'],'update_available');self.assertEqual(check['update_decision'],'review')
        stored=json.loads(self.m.db.execute('select last_check from maintenance_items where id=?',(row['id'],)).fetchone()[0])
        self.assertEqual(stored,saved);self.assertEqual(core.manifest(self.roots/'a'),before)
        (self.roots/'a'/'later').write_text('changed after comparison')
        current=next(i for i in self.m.inventory()['items'] if i['id']==row['id'])
        self.assertEqual(current['update_check']['status'],'stale')

    def test_old_checks_without_local_version_use_snapshot_only_when_not_stale(self):
        saved={'status':'update_available','remote_version':'2.0','local_fingerprint':'saved',
               'baseline_known':False,'local_changes':None}
        result=version_policy.normalize_check(saved,'1.0')
        self.assertEqual(result['status'],'update_available');self.assertEqual(result['update_decision'],'review')
        self.assertNotIn('local_version',saved)
        self.assertEqual(version_policy.normalize_check({**saved,'status':'stale'},'1.0')['status'],'stale')
        self.assertNotEqual(version_policy.normalize_check(saved,'unknown')['status'],'update_available')
        self.assertNotEqual(version_policy.normalize_check({**saved,'local_version':'unknown'},'1.0')['status'],'update_available')

    def test_check_and_force_never_apply_and_conflict_update_requires_confirmation(self):
        row=self.row('a');self.m.bind_source(row,'https://github.com/example/repo',path='a')
        self.trusted('1.0');(self.roots/'a'/'notes').write_text('local work')
        remote=self.base/'remote-new';remote.mkdir()
        (remote/'SKILL.md').write_text('---\nname: a\nversion: 2.0\n---\nnew release')
        before=core.manifest(self.roots/'a');self.m._use_check_cache=True
        with patch.object(self.m,'remote',return_value=(remote,self.m.source(row))):
            for force in (False,False,True):
                self.m._force_check=force;check=self.m.check(row)
                self.assertEqual(check['status'],'update_available');self.assertEqual(check['update_decision'],'review')
                self.assertEqual(core.manifest(self.roots/'a'),before)
                self.assertEqual(self.m.history()['operations'],[])
                self.assertEqual(self.m.backups()['backups'],[])
            with self.assertRaisesRegex(ValueError,'Local conflicts'):self.m.plan('update',row)
            plan=self.m.plan('update',row,allow_changes=True)
        self.assertEqual(core.manifest(self.roots/'a'),before)
        result=self.m.apply(plan['id'])
        self.assertEqual(len(result['backups']),1)
        self.assertTrue(self.m.verify_backup(result['backups'][0]['id'])['valid'])
        self.assertIn('version: 2.0',(self.roots/'a'/'SKILL.md').read_text())
        self.assertEqual((self.roots/'a'/'notes').read_text(),'local work')

    def trusted(self, value):
        p=self.roots/'a';(p/'SKILL.md').write_text('---\nname: a\ndescription: Test skill purpose\nversion: '+value+'\n---\ncontent '+value)
        row=self.row('a');self.m.state(row)
        self.m.db.execute('update maintenance_items set baseline=? where id=?',(json.dumps(core.manifest(p)),row['id']))
        self.m.db.commit()
        return row

    def setup_history(self):
        row=self.row('a');self.m.bind_source(row,'https://github.com/example/repo',path='a')
        ids=[]
        for v in ('1.0','1.1'):
            row=self.trusted(v);ids.append(self.m.backup(row)['id'])
        row=self.trusted('1.3')
        with patch.object(self.m,'remote',return_value=(self.roots/'a',self.m.source(row))):self.m.check(row)
        return ids

    def test_old_backup_cleanup_keeps_recent_rollback(self):
        old,recent=self.setup_history()
        advice=recovery.backup_detail(self.m,old)['cleanup_advice']
        self.assertEqual(advice['code'],'cleanable');self.assertEqual(advice['keeper_id'],recent)
        self.assertEqual(recovery.backup_detail(self.m,recent)['cleanup_advice']['code'],'keep')
        self.assertTrue(self.m.verify_backup(old)['valid'])

    def test_advice_works_on_readonly_connection(self):
        old,_=self.setup_history()
        self.m.db.execute('pragma query_only=on')
        try:self.assertEqual(recovery.backup_detail(self.m,old)['cleanup_advice']['code'],'cleanable')
        finally:self.m.db.execute('pragma query_only=off')

    def test_visible_row_advice_is_readonly_and_needs_no_detail_click(self):
        old,recent=self.setup_history();self.m.inventory()
        with patch.object(self.m,'remote',side_effect=AssertionError('Unexpected cloud query')):
            result=core.api_dispatch(self.a,{'action':'backup-advice','ids':[old,recent]})
        by_id={r['id']:r['cleanup_advice'] for r in result['items']}
        self.assertEqual(by_id[old]['label'],'建议删除')
        self.assertEqual(by_id[recent]['label'],'不建议删除')
        self.assertTrue(self.m.verify_backup(old)['valid'])

    def test_missing_provenance_still_compares_and_preserves_only_copy(self):
        bid=self.m.backup(self.row('a'))['id']
        with patch.object(recovery,'backup_detail',wraps=recovery.backup_detail) as compare:
            result=recovery.backup_advice(self.m,[bid])
        compare.assert_called_once()
        self.assertEqual(result['items'][0]['cleanup_advice']['label'],'不建议删除')

    def test_advice_batch_is_bounded_and_unknown_ids_are_conservative(self):
        for ids in (None,[],['x']*9,[None]):
            with self.assertRaises(ValueError):recovery.backup_advice(self.m,ids)
        result=recovery.backup_advice(self.m,['not-found'])
        self.assertIsNone(result['items'][0]['cleanup_advice'])
        self.assertIn('not found',result['items'][0]['error'])

    def test_retained_and_local_modifications_stay(self):
        old,recent=self.setup_history();recovery.retain(self.m,old,True)
        self.assertEqual(recovery.backup_detail(self.m,old)['cleanup_advice']['code'],'keep')
        (self.roots/'a'/'notes').write_text('unique local work')
        local=self.m.backup(self.row('a'))['id']
        detail=recovery.backup_detail(self.m,local)
        self.assertEqual(detail['cleanup_advice']['code'],'keep')
        self.assertIn('本地修改',detail['cleanup_advice']['reason'])

    def test_unknown_source_baseline_and_version_never_cleanable(self):
        bid=self.m.backup(self.row('a'))['id']
        self.assertEqual(recovery.backup_detail(self.m,bid)['cleanup_advice']['code'],'keep')
        self.m.bind_source(self.row('a'),'https://github.com/example/repo',path='a')
        self.skill('a','distinct content without baseline')
        bid=self.m.backup(self.row('a'))['id']
        self.assertEqual(recovery.backup_detail(self.m,bid)['cleanup_advice']['code'],'review')
        self.trusted('unknown');bid=self.m.backup(self.row('a'))['id']
        self.assertNotEqual(recovery.backup_detail(self.m,bid)['cleanup_advice']['code'],'cleanable')

    def test_unverified_or_modified_current_prevents_cleanup(self):
        old,_=self.setup_history()
        (self.roots/'a'/'notes').write_text('new work')
        self.assertEqual(recovery.backup_detail(self.m,old)['cleanup_advice']['code'],'review')
        (self.roots/'a'/'notes').unlink()
        self.m.db.execute('update maintenance_items set last_check=null');self.m.db.commit()
        self.assertEqual(recovery.backup_detail(self.m,old)['cleanup_advice']['code'],'review')

    def test_duplicate_recommendations_have_one_deterministic_keeper(self):
        self.setup_history();row=self.trusted('1.1')
        first=self.m.backup(row)['id'];second=self.m.backup(row)['id']
        row=self.trusted('1.3')
        with patch.object(self.m,'remote',return_value=(self.roots/'a',self.m.source(row))):self.m.check(row)
        details=[recovery.backup_detail(self.m,b) for b in (first,second)]
        cleanable=[d for d in details if d['cleanup_advice']['code']=='cleanable']
        self.assertTrue(cleanable)
        for d in cleanable:self.assertNotEqual(d['id'],d['cleanup_advice']['keeper_id'])

    def test_tampered_keeper_cannot_justify_cleanup(self):
        old,recent=self.setup_history();container,_=self.m.backup_record(recent)
        (container/'content'/'SKILL.md').write_text('tampered')
        self.assertEqual(recovery.backup_detail(self.m,old)['cleanup_advice']['code'],'keep')
        self.assertEqual(recovery.backup_detail(self.m,recent)['cleanup_advice']['code'],'review')

    def test_check_reads_actual_disk_version_not_registry_version(self):
        row=self.row('a');self.m.bind_source(row,'https://github.com/example/repo',path='a')
        self.trusted('3.0');remote=self.base/'remote';remote.mkdir()
        (remote/'SKILL.md').write_text('---\nname: a\nversion: 2.0\n---\nremote')
        with patch.object(self.m,'remote',return_value=(remote,self.m.source(row))):result=self.m.check(row)
        self.assertEqual(result['local_version'],'3.0');self.assertEqual(result['update_decision'],'keep_local')
        with patch.object(self.m,'remote',return_value=(remote,self.m.source(row))):
            with self.assertRaisesRegex(ValueError,'Local conflicts'):self.m.plan('update',row)

    def test_active_reference_into_backup_is_protected(self):
        old,_=self.setup_history();container,_=self.m.backup_record(old)
        (self.roots/'alias').symlink_to(container/'content',target_is_directory=True);self.m.scan()
        detail=recovery.backup_detail(self.m,old)
        self.assertEqual(detail['cleanup_advice']['code'],'keep')
        self.assertIn('扫描为安装项',detail['cleanup_advice']['reason'])


if __name__=='__main__':unittest.main()
