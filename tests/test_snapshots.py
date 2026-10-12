import json
import zipfile
from pathlib import Path
import test_maintenance as fixtures
import unittest
import snapshots
import library_actions
import maintenance as core

class SnapshotTests(unittest.TestCase):
    setUp=fixtures.MaintenanceTests.setUp
    tearDown=fixtures.MaintenanceTests.tearDown
    skill=fixtures.MaintenanceTests.skill
    row=fixtures.MaintenanceTests.row
    def test_roundtrip_and_metadata(self):
        self.m.set_labels([self.row('a')['id']],'编程开发',['代码'])
        v=snapshots.dispatch(self.m,{'action':'create-snapshot','scope':'all'})
        self.assertEqual(v['count'],2)
        self.assertTrue(self.m.verify_backup(v['id'])['valid'])
        listed=next(r for r in self.m.backups(False)['backups'] if r['id']==v['id'])
        self.assertTrue(listed['bundle']);self.assertEqual(listed['entry_count'],2)
        exported=snapshots.dispatch(self.m,{'action':'export-backup','id':v['id']})
        archive=self.home/'exports'/exported['download_id']
        data=snapshots.read_package(archive)
        self.assertEqual(data['items'][0]['labels']['category'],'编程开发')
        imported=snapshots.dispatch(self.m,{'action':'import-snapshot','path':str(archive)})
        target=self.base/'new-device';target.mkdir()
        config=self.a.load_config();config['platforms']['shared']['roots'].append(str(target));self.a.save_config(config)
        entries=[{'id':imported['id'],'key':i['key']} for i in data['items']]
        plans=snapshots.preview_restore(self.m,{'entries':entries,'target_root':str(target)})['plans']
        self.assertFalse((target/'a').exists())
        r=snapshots.apply_restore(self.m,[p['id'] for p in plans],True)
        self.assertEqual(r['completed'],2)
        self.assertEqual((target/'a'/'SKILL.md').read_text(),(self.roots/'a'/'SKILL.md').read_text())
        self.assertEqual(json.loads(self.m.db.execute('select tags from maintenance_labels where real_path=?',(str(target/'a'),)).fetchone()[0]),['代码'])

    def test_snapshot_deduplicates_shared_directory(self):
        (self.roots/'alias').symlink_to(self.roots/'a',target_is_directory=True);self.m.scan()
        v=snapshots.dispatch(self.m,{'action':'create-snapshot','scope':'all'})
        self.assertEqual(v['count'],2)
        data=snapshots.read_package(snapshots.package_path(self.m,v['id']))
        self.assertEqual(len(next(i for i in data['items'] if i['name']=='a')['references']),2)

    def test_bundle_requires_explicit_entry(self):
        v=snapshots.dispatch(self.m,{'action':'create-snapshot','scope':'all'})
        with self.assertRaisesRegex(ValueError,'请选择'):self.m.plan('restore',backup_id=v['id'],restore_path=str(self.roots/'new'))

    def test_package_tamper_and_traversal_rejected(self):
        v=snapshots.dispatch(self.m,{'action':'create-snapshot','scope':'all'})
        path=snapshots.package_path(self.m,v['id']);data=snapshots.read_package(path)
        bad=self.base/'bad.zip'
        with zipfile.ZipFile(path) as src,zipfile.ZipFile(bad,'w') as dst:
            for i in src.infolist():dst.writestr(i.filename,src.read(i.filename) if i.filename=='snapshot.json' else b'bad')
        with self.assertRaises(ValueError):snapshots.read_package(bad)
        with zipfile.ZipFile(bad,'w') as z:z.writestr('../escape','bad')
        with self.assertRaisesRegex(ValueError,'Unsafe'):snapshots.extract_zip(bad,self.base/'extract')
        self.assertFalse((self.base/'escape').exists())

    def test_adopt_in_place_without_agent(self):
        external=self.base/'external';folder=external/'example';folder.mkdir(parents=True)
        (folder/'SKILL.md').write_text('---\nname: example\ndescription: Example task\n---\nBody')
        v=library_actions.preview(self.m,str(external))
        with self.assertRaisesRegex(ValueError,'Confirmation'):library_actions.dispatch(self.m,{'action':'directory-add','path':str(external),'fingerprint':v['fingerprint']})
        library_actions.dispatch(self.m,{'action':'directory-add','path':str(external),'fingerprint':v['fingerprint'],'confirmed':True})
        self.assertTrue((folder/'SKILL.md').exists())
        row=self.m.select('example');self.assertEqual(row['path'],str(folder))
        self.assertEqual(next(i for i in self.m.inventory()['items'] if i['id']==row['id'])['known_agents'],[])

    def test_local_install_deployment_and_no_remote_binding(self):
        source=self.base/'local';source.mkdir();(source/'SKILL.md').write_text('---\nname: local\ndescription: A local task\n---\nHello')
        v=library_actions.dispatch(self.m,{'action':'install-local-identify','path':str(source),'target_root':str(self.roots)})
        p=self.m.plan_install(v['id'],v['candidates'][0]['remote_path']);r=self.m.apply(p['id'])
        self.assertEqual(r['deployment']['status'],'deployed');self.assertTrue(r['deployment']['description_valid'])
        self.assertIsNone(self.m.state(self.m.select('local'))['source'])

    def test_empty_trash_skips_retained_and_other_backups(self):
        import recovery
        normal=self.m.backup(self.row('a'))['id']
        trash=self.m.apply(self.m.plan('delete',self.row('b'))['id'])['backups'][0]['id']
        recovery.retain(self.m,trash,True)
        v=snapshots.dispatch(self.m,{'action':'preview-empty-trash'})
        self.assertEqual(v['plans'],[]);self.assertEqual(v['skipped_retained'],1)
        recovery.retain(self.m,trash,False)
        v=snapshots.dispatch(self.m,{'action':'preview-empty-trash'})
        self.assertEqual([p['backup_id'] for p in v['plans']],[trash])
        self.assertTrue(self.m.verify_backup(normal)['valid'])

    def test_export_download_requires_token_and_serves_zip(self):
        import http.server, threading, urllib.request, urllib.error
        v=snapshots.dispatch(self.m,{'action':'create-snapshot','scope':'all'})
        export=snapshots.dispatch(self.m,{'action':'export-backup','id':v['id']})
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),self.a.AdminHandler)
        server.api_token='test-token'
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            url='http://127.0.0.1:'+str(server.server_address[1])+'/api/backup-file?id='+export['download_id']
            with self.assertRaises(urllib.error.HTTPError) as e:urllib.request.urlopen(url)
            self.assertEqual(e.exception.code,403)
            req=urllib.request.Request(url,headers={'X-ASM-Token':'test-token'})
            with urllib.request.urlopen(req) as response:
                self.assertEqual(response.headers['Content-Type'],'application/zip')
                self.assertTrue(response.read().startswith(b'PK'))
        finally:server.shutdown();server.server_close();thread.join()

    def test_single_backup_export_keeps_original_folder_name(self):
        bid=self.m.backup(self.row('a'))['id']
        result=snapshots.dispatch(self.m,{'action':'export-backup','id':bid})
        data=snapshots.read_package(self.home/'exports'/result['download_id'])
        self.assertEqual(data['items'][0]['folder'],'a')

    def test_adoption_detects_stale_preview(self):
        v=library_actions.preview(self.m,str(self.roots))
        (self.roots/'a'/'SKILL.md').write_text('changed')
        with self.assertRaisesRegex(ValueError,'变化'):
            library_actions.dispatch(self.m,{'action':'directory-add','path':str(self.roots),'fingerprint':v['fingerprint'],'confirmed':True})

    def test_restore_accepts_native_permission_differences(self):
        from unittest.mock import patch
        v=snapshots.dispatch(self.m,{'action':'create-snapshot','scope':'all'})
        data=snapshots.read_package(snapshots.package_path(self.m,v['id']))
        dest=self.base/'staged';actual=core.manifest
        def native_manifest(path):
            files=actual(path)
            if Path(path)==dest:
                for meta in files.values():
                    if 'mode' in meta:meta['mode']^=0o100
            return files
        with patch.object(core,'manifest',side_effect=native_manifest):
            snapshots.stage_item(self.m,v['id'],data['items'][0]['key'],dest)
        self.assertTrue((dest/'SKILL.md').is_file())

    def test_restore_rejects_case_insensitive_target_collision(self):
        v=snapshots.dispatch(self.m,{'action':'create-snapshot','scope':'all'})
        path=snapshots.package_path(self.m,v['id']);data=snapshots.read_package(path)
        data['items'][1]['folder']='A'
        modified=self.base/'case.zip'
        with zipfile.ZipFile(path) as src,zipfile.ZipFile(modified,'w') as dst:
            for info in src.infolist():dst.writestr(info.filename,json.dumps(data) if info.filename=='snapshot.json' else src.read(info.filename))
        imported=snapshots.dispatch(self.m,{'action':'import-snapshot','path':str(modified)})
        with self.assertRaisesRegex(ValueError,'同一位置'):
            snapshots.preview_restore(self.m,{'entries':[{'id':imported['id'],'key':i['key']} for i in data['items']], 'target_root':str(self.roots),'allow_overwrite':True})
        self.assertEqual(self.m.plans()['plans'],[])
