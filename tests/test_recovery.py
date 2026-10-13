"""Recovery catalog safety: retain, review, copy, restore and batch cleanup."""
import json
import sqlite3
import unittest
from unittest.mock import patch
import test_maintenance as fixtures
import maintenance as core
import recovery

class RecoveryTests(unittest.TestCase):
    setUp=fixtures.MaintenanceTests.setUp
    tearDown=fixtures.MaintenanceTests.tearDown
    skill=fixtures.MaintenanceTests.skill
    row=fixtures.MaintenanceTests.row

    def historical(self):
        root=self.base/'old-backups';root.mkdir()
        p=root/'a-20200101';p.mkdir();(p/'SKILL.md').write_text('---\nname: a\n---\nold content')
        with patch.object(recovery,'locations',return_value=[root]):result=recovery.discover(self.m)
        return p,result['candidates'][0]

    def test_retained_backup_blocks_preview_and_already_prepared_apply(self):
        b=self.m.backup(self.row('a'))['id'];plan=self.m.plan('purge',backup_id=b)
        recovery.retain(self.m,b,True)
        with self.assertRaisesRegex(ValueError,'retain'):self.m.apply(plan['id'])
        self.m.discard_plan(plan['id'])
        with self.assertRaisesRegex(ValueError,'retain'):self.m.plan('purge',backup_id=b)
        recovery.retain(self.m,b,False)
        self.m.apply(self.m.plan('purge',backup_id=b)['id'])
        self.assertEqual(self.m.backups()['backups'][0]['state'],'purged')

    def test_historical_copy_requires_confirmation_and_preserves_source(self):
        p,c=self.historical();before=core.manifest(p)
        with self.assertRaisesRegex(ValueError,'Confirmation'):recovery.import_candidate(self.m,c['id'],str(self.roots/'recovered'),'backup')
        b=recovery.import_candidate(self.m,c['id'],str(self.roots/'recovered'),'backup',True)
        self.assertEqual(core.manifest(p),before)
        self.assertFalse((self.roots/'recovered').exists())
        self.assertTrue(self.m.verify_backup(b['id'])['valid'])
        self.m.apply(self.m.plan('restore',backup_id=b['id'])['id'])
        self.assertEqual((self.roots/'recovered'/'SKILL.md').read_text(),(p/'SKILL.md').read_text())
        self.assertEqual(recovery.candidates(self.m)[0]['state'],'imported')
        with self.assertRaises(ValueError):recovery.import_candidate(self.m,c['id'],str(self.roots/'other'),'backup',True)

    def test_changed_files_links_and_unsafe_original_are_rejected(self):
        p,c=self.historical();(p/'SKILL.md').write_text('changed')
        with self.assertRaisesRegex(ValueError,'changed'):recovery.import_candidate(self.m,c['id'],str(self.roots/'restored'),'backup',True)
        with patch.object(recovery,'locations',return_value=[p.parent]):c=recovery.discover(self.m)['candidates'][0]
        with self.assertRaises(ValueError):recovery.import_candidate(self.m,c['id'],str(self.base/'outside'),'backup',True)
        (p/'link').symlink_to(self.roots/'a')
        with patch.object(recovery,'locations',return_value=[p.parent]):c=recovery.discover(self.m)['candidates'][0]
        with self.assertRaisesRegex(ValueError,'links'):recovery.import_candidate(self.m,c['id'],str(self.roots/'restored'),'backup',True)
        self.assertFalse([b for b in self.m.backups()['backups'] if not b.get('external')])

    def test_archives_only_listed_ignored_candidates_stay_ignored(self):
        import zipfile
        p,c=self.historical();archive=p.parent/'old.zip'
        with zipfile.ZipFile(archive,'w') as z:z.writestr('old/SKILL.md','name: old')
        with patch.object(recovery,'locations',return_value=[p.parent]):result=recovery.discover(self.m)
        a=next(i for i in result['candidates'] if i['format']=='archive')
        with self.assertRaisesRegex(ValueError,'Archive'):recovery.import_candidate(self.m,a['id'],str(self.roots/'restored'),'backup',True)
        recovery.ignore(self.m,c['id'])
        with patch.object(recovery,'locations',return_value=[p.parent]):again=recovery.discover(self.m)
        self.assertEqual(next(i['state'] for i in again['candidates'] if i['id']==c['id']),'ignored')

    def test_restore_alternative_path_leaves_existing_target_untouched(self):
        b=self.m.backup(self.row('a'))['id'];before=core.manifest(self.roots/'a')
        dest=self.roots/'alternative'
        self.m.apply(self.m.plan('restore',backup_id=b,restore_path=str(dest))['id'])
        self.assertEqual(core.manifest(self.roots/'a'),before)
        self.assertEqual(core.manifest(dest),before)
        with self.assertRaises(ValueError):self.m.plan('restore',backup_id=b,restore_path=str(self.base/'outside'))

    def test_batch_purge_confirmation_retention_and_partial_failure(self):
        ids=[self.m.backup(self.row(name))['id'] for name in ('a','b')]
        v=recovery.preview_purge(self.m,ids);plans=[p['id'] for p in v['plans']]
        with self.assertRaisesRegex(ValueError,'Confirmation'):recovery.apply_purge(self.m,plans)
        recovery.retain(self.m,ids[1],True)
        with self.assertRaisesRegex(ValueError,'retain'):recovery.apply_purge(self.m,plans,True)
        self.assertTrue(all(self.m.verify_backup(i)['valid'] for i in ids))
        recovery.retain(self.m,ids[1],False)
        real=self.m.apply
        def apply(pid):
            if pid==plans[1]:raise OSError('test failure')
            return real(pid)
        with patch.object(self.m,'apply',side_effect=apply):r=recovery.apply_purge(self.m,plans,True)
        self.assertEqual(r['state'],'partial');self.assertEqual(r['completed'],1)
        self.assertTrue(self.m.verify_backup(ids[1])['valid'])

    def test_readonly_backup_listing_supports_old_database_schema(self):
        self.m.db.execute('drop table maintenance_backup_flags');self.m.db.execute('drop table maintenance_recovery_candidates');self.m.db.commit()
        self.assertEqual(core.api_dispatch(self.a,{'action':'backups'})['candidates'],[])

    def test_discovered_entries_share_list_without_copy_or_registration(self):
        source,c=self.historical()
        row=next(b for b in self.m.backups()['backups'] if b['id']==c['id'])
        self.assertTrue(row['external']);self.assertEqual(row['restore_status'],'needs_target')
        self.assertIsNone(row['created_at']);self.assertIsNone(row['original_path'])
        self.assertEqual(self.m.db.execute('select count(*) from maintenance_backups').fetchone()[0],0)
        self.assertEqual(list((self.home/'backups').iterdir()),[])
        self.assertTrue(source.exists())

    def test_historical_direct_restore_pins_content_without_import(self):
        source,c=self.historical();before=core.manifest(source)
        with self.assertRaisesRegex(ValueError,'location'):self.m.plan('restore',backup_id=c['id'])
        dest=self.roots/'restored-direct'
        plan=self.m.plan('restore',backup_id=c['id'],restore_path=str(dest))
        self.m.apply(plan['id'])
        self.assertEqual(core.manifest(dest),before);self.assertEqual(core.manifest(source),before)
        self.assertEqual(self.m.db.execute('select count(*) from maintenance_backups').fetchone()[0],0)
        self.assertEqual(recovery.candidates(self.m)[0]['state'],'pending')

    def test_historical_purge_checks_retention_and_file_changes(self):
        source,c=self.historical();pid=self.m.plan('purge',backup_id=c['id'])['id']
        recovery.retain(self.m,c['id'],True)
        with self.assertRaisesRegex(ValueError,'retain'):self.m.apply(pid)
        recovery.retain(self.m,c['id'],False)
        (source/'extra').write_text('new')
        with self.assertRaisesRegex(ValueError,'changed'):self.m.apply(pid)
        self.m.discard_plan(pid)
        self.m.apply(self.m.plan('purge',backup_id=c['id'])['id'])
        self.assertFalse(source.exists());self.assertEqual(recovery.candidates(self.m)[0]['state'],'purged')
        self.assertFalse(self.m.backups()['backups'])

    def test_date_clues_are_validated_and_never_replace_recorded_time(self):
        self.assertEqual(recovery.backup_date('/backups/batch-20260702-223618/skill')['value'],'2026-07-02T22:36:18')
        self.assertEqual(recovery.backup_date('/backups/a-2020-01-02')['value'],'2020-01-02')
        self.assertEqual(recovery.backup_date('/backups/a-20261340')['kind'],'unknown')
        self.assertEqual(recovery.backup_date('/backups/a-20200101','2026-10-08T12:00:00Z')['kind'],'recorded')
        source,c=self.historical()
        b=next(b for b in self.m.backups()['backups'] if b['id']==c['id'])
        self.assertEqual(b['saved_time']['value'],'2020-01-01');self.assertIsNone(b['created_at'])

    def test_detail_compares_same_name_without_assigning_restore_destination(self):
        source,c=self.historical()
        d=core.api_dispatch(self.a,{'action':'backup-detail','id':c['id']})
        self.assertEqual(d['comparison_state'],'different');self.assertIsNone(d['original_path'])
        self.assertEqual(d['comparisons'][0]['basis'],'name')
        self.assertIn('SKILL.md',d['comparisons'][0]['changes']['changed'])
        self.assertTrue(d['valid']);self.assertEqual(d['file_count'],1)
        self.assertEqual(self.m.db.execute('select count(*) from maintenance_backups').fetchone()[0],0)

    def test_native_detail_identical_then_changed_and_missing(self):
        b=self.m.backup(self.row('a'))
        d=recovery.backup_detail(self.m,b['id'])
        self.assertEqual(d['comparison_state'],'identical');self.assertEqual(d['skill_info']['version'],'1.0')
        (self.roots/'a'/'new.txt').write_text('current only')
        d=recovery.backup_detail(self.m,b['id'])
        self.assertEqual(d['comparison_state'],'different');self.assertIn('new.txt',d['comparisons'][0]['changes']['removed'])
        core.remove_path(self.roots/'a')
        self.assertEqual(recovery.backup_detail(self.m,b['id'])['comparison_state'],'not_installed')

    def test_detail_reports_multiple_names_and_stale_backup(self):
        source,c=self.historical();self.skill('second-a','two','a');self.m.scan()
        d=recovery.backup_detail(self.m,c['id'])
        self.assertEqual(d['comparison_state'],'multiple');self.assertEqual(len(d['comparisons']),2)
        (source/'SKILL.md').write_text('changed since discovery')
        d=recovery.backup_detail(self.m,c['id'])
        self.assertFalse(d['valid']);self.assertEqual(d['comparison_state'],'unavailable')

    def test_archive_detail_does_not_extract(self):
        import zipfile
        source,c=self.historical();p=source.parent/'skill-20200102.zip'
        with zipfile.ZipFile(p,'w') as z:z.writestr('old/SKILL.md','old')
        with patch.object(recovery,'locations',return_value=[source.parent]):v=recovery.discover(self.m)
        a=next(i for i in v['candidates'] if i['format']=='archive')
        d=recovery.backup_detail(self.m,a['id'])
        self.assertEqual(d['comparison_state'],'unsupported');self.assertFalse(d['files'])

    def test_old_registered_backup_directories_are_not_current_installations(self):
        source,c=self.historical()
        hidden=self.roots/'.cmm-skill-backups'/'20260101'/'a';hidden.mkdir(parents=True)
        (hidden/'SKILL.md').write_text('---\nname: a\n---\nnot installed')
        self.m.r.upsert_capability(self.a.capability_from_skill_md('shared',hidden,hidden/'SKILL.md'))
        d=recovery.backup_detail(self.m,c['id'])
        self.assertEqual(len(d['comparisons']),1)
        self.assertEqual(d['comparison_state'],'different')
