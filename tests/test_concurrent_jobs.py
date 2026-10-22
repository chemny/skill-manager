"""Queries and refresh remain independent; aliases and overlapping writes conflict."""
import json
import sqlite3
import threading
import time
import unittest
from unittest.mock import patch
import test_maintenance as fixtures
import maintenance
import task_scopes
import web_jobs
import diagnostics
from skill_locations import default_agent_roots, add_preset_agent


class ConcurrentJobsTests(unittest.TestCase):
    setUp=fixtures.MaintenanceTests.setUp
    tearDown=fixtures.MaintenanceTests.tearDown
    skill=fixtures.MaintenanceTests.skill
    row=fixtures.MaintenanceTests.row

    def test_scope_connections_close_on_success_and_failure(self):
        connect = sqlite3.connect
        for fail in (False, True):
            connections = []
            class TrackedConnection(sqlite3.Connection):
                def execute(self, sql, *args, **kwargs):
                    if fail and sql.startswith('select path'):
                        raise sqlite3.OperationalError('injected read failure')
                    return super().execute(sql, *args, **kwargs)
            def tracked(*args, **kwargs):
                db = connect(*args, **kwargs, factory=TrackedConnection)
                connections.append(db)
                return db
            with patch.object(task_scopes.sqlite3, 'connect', tracked):
                try:
                    if fail:
                        with self.assertRaises(sqlite3.OperationalError):
                            task_scopes.describe(self.a, {'action':'check', 'target':'a'})
                    else:
                        task_scopes.describe(self.a, {'action':'check', 'target':'a'})
                    self.assertEqual(len(connections), 1)
                    with self.assertRaises(sqlite3.ProgrammingError):
                        connections[0].execute('select 1')
                finally:
                    for db in connections:
                        db.close()

    def finish(self,jobs):
        deadline=time.monotonic()+5
        while jobs.busy() and time.monotonic()<deadline:time.sleep(.01)
        self.assertFalse(jobs.busy())

    def test_disk_scan_reuses_job_blocks_moves_and_allows_stop(self):
        jobs = web_jobs.Jobs(self.a)
        self.m.db.execute('insert into maintenance_space_catalogs values(?,?,?)', ('concurrency-catalog',maintenance.stamp(),json.dumps({'projects':[{'id':'project','path':str(self.base/'work')}]})))
        self.m.db.commit()
        request = {'action':'disk-scan','catalog_id':'concurrency-catalog','ids':['project'],'agents':['codex']}
        started, release = threading.Event(), threading.Event()
        def delayed(m, request):
            if request['action'] == 'disk-scan':
                started.set()
                release.wait(4)
            return {}
        with patch.object(maintenance, 'dispatch', delayed):
            first = jobs.start(request)
            try:
                self.assertTrue(started.wait(2))
                self.assertEqual(jobs.start(request)['id'], first['id'])
                with self.assertRaises(web_jobs.BusyError):
                    jobs.start({**request,'agents':['claude_code']})
                with self.assertRaises(web_jobs.BusyError):
                    jobs.start({'action': 'space-trash', 'confirmed': True})
                stop = jobs.start({'action': 'disk-stop'})
                self.assertNotEqual(stop['id'], first['id'])
            finally:
                release.set()
                self.finish(jobs)

    def test_check_refresh_and_discovery_run_together(self):
        jobs=web_jobs.Jobs(self.a); release=threading.Event(); gates={a:threading.Event() for a in ('check','inventory','discover-sources')}
        def delayed(m,p):
            gates[p['action']].set();release.wait(4);return {'action':p['action']}
        with patch.object(maintenance,'dispatch',delayed):
            records=[jobs.start({'action':a,'target':self.row('a')['id']}) for a in gates]
            try:
                self.assertTrue(all(g.wait(2) for g in gates.values()))
                self.assertEqual(len(jobs.snapshot()['jobs']),3)
            finally:release.set();self.finish(jobs)
        self.assertTrue(all(jobs.snapshot(r['id'])['state']=='succeeded' for r in records))

    def test_shared_alias_is_locked_but_other_skill_is_available(self):
        alias=self.roots/'alias';alias.symlink_to(self.roots/'a');self.m.scan()
        jobs=web_jobs.Jobs(self.a); gate=threading.Event();release=threading.Event()
        def delayed(m,p):gate.set();release.wait(4);return {}
        with patch.object(maintenance,'dispatch',delayed):
            first=jobs.start({'action':'source-bind','target':self.m.select(str(alias))['id']})
            try:
                self.assertTrue(gate.wait(2))
                with self.assertRaises(web_jobs.BusyError):jobs.start({'action':'check','target':self.m.select(str(self.roots/'a'))['id']})
                second=jobs.start({'action':'check','target':self.row('b')['id']})
                self.assertNotEqual(first['id'],second['id'])
            finally:release.set();self.finish(jobs)

    def test_classification_waits_for_check_and_reuses_queued_job(self):
        jobs=web_jobs.Jobs(self.a);started=threading.Event();release=threading.Event()
        calls=[]
        def delayed(m,p):
            calls.append(p['action'])
            if p['action']=='check':
                started.set();release.wait(4)
            return {'action':p['action']}
        with patch.object(maintenance,'dispatch',delayed):
            check=jobs.start({'action':'check','target':self.row('a')['id']})
            try:
                self.assertTrue(started.wait(2))
                classify=jobs.start({'action':'classification-apply'})
                self.assertEqual(jobs.snapshot(classify['id'])['state'],'queued')
                self.assertEqual(jobs.snapshot(classify['id'])['message'],'等待其他任务完成')
                self.assertEqual(jobs.start({'action':'classification-apply'})['id'],classify['id'])
                with self.assertRaises(web_jobs.BusyError):
                    jobs.start({'action':'classification-apply','field':'tags'})
                self.assertEqual(calls,['check'])
            finally:
                release.set();self.finish(jobs)
        self.assertEqual(jobs.snapshot(check['id'])['state'],'succeeded')
        self.assertEqual(jobs.snapshot(classify['id'])['state'],'succeeded')
        self.assertEqual(calls,['check','classification-apply'])

    def test_busy_error_has_specific_diagnostic(self):
        self.assertEqual(diagnostics.classify(web_jobs.BusyError('相关 Skill 或目录正在操作')),
                         ('busy','相关 Skill 或目录正在操作'))

    def test_failure_of_one_query_does_not_end_another(self):
        jobs=web_jobs.Jobs(self.a);gate=threading.Event();release=threading.Event()
        def dispatch(m,p):
            if p['action']=='inventory':gate.set();release.wait(4);return {}
            raise ValueError('isolated failure')
        with patch.object(maintenance,'dispatch',dispatch):
            running=jobs.start({'action':'inventory'});self.assertTrue(gate.wait(2))
            bad=jobs.start({'action':'discover-sources','target':self.row('b')['id']})
            try:
                for _ in range(100):
                    if jobs.snapshot(bad['id'])['state']=='failed':break
                    time.sleep(.01)
                self.assertEqual(jobs.snapshot(bad['id'])['state'],'failed')
                self.assertEqual(jobs.snapshot(running['id'])['state'],'running')
            finally:release.set();self.finish(jobs)

    def test_network_jobs_are_bounded_and_queued_records_survive_pruning(self):
        jobs=web_jobs.Jobs(self.a);release=threading.Event();count=[0];lock=threading.Lock()
        def dispatch(m,p):
            with lock:count[0]+=1
            release.wait(4);return {}
        with patch.object(maintenance,'dispatch',dispatch):
            records=[jobs.start({'action':'discover-sources'}) for _ in range(5)]
            try:
                for _ in range(100):
                    if count[0]==3:break
                    time.sleep(.01)
                self.assertEqual(count[0],3)
                self.assertEqual(sum(jobs.snapshot(r['id'])['state']=='queued' for r in records),2)
            finally:release.set();self.finish(jobs)

    def test_nested_paths_conflict_but_siblings_do_not(self):
        a={'write':True,'resources':['/skills/a'],'global':False,'catalog':False}
        b={**a,'write':False,'resources':['/skills/a/nested']}
        self.assertTrue(task_scopes.conflicts(a,b))
        self.assertFalse(task_scopes.conflicts(a,{**b,'resources':['/skills/ab']}))

    def test_identical_inventory_requests_share_job_but_different_scopes_do_not(self):
        jobs = web_jobs.Jobs(self.a)
        gate, release = threading.Event(), threading.Event()
        def delayed(manager, request):
            gate.set(); release.wait(4); return {}
        aid, bid = self.row('a')['id'], self.row('b')['id']
        with patch.object(maintenance, 'dispatch', delayed):
            first = jobs.start({'action':'inventory', 'ids':[aid], 'lightweight':True})
            try:
                self.assertTrue(gate.wait(2))
                duplicate = jobs.start({'action':'inventory', 'ids':[aid, aid], 'lightweight':True})
                other = jobs.start({'action':'inventory', 'ids':[bid], 'lightweight':True})
                deep = jobs.start({'action':'inventory', 'ids':[aid]})
                self.assertEqual(first['id'], duplicate['id'])
                self.assertNotEqual(first['id'], other['id'])
                self.assertNotEqual(first['id'], deep['id'])
            finally:
                release.set(); self.finish(jobs)

    def test_file_commits_queue_without_stopping_unrelated_queries(self):
        self.skill('c','three');self.m.scan()
        jobs=web_jobs.Jobs(self.a);release=threading.Event();backup=threading.Event();query=threading.Event()
        def dispatch(manager,request):
            (backup if request['action']=='backup' else query).set()
            release.wait(4)
            return {}
        with patch.object(maintenance,'dispatch',dispatch):
            first=jobs.start({'action':'backup','target':self.row('a')['id']})
            try:
                self.assertTrue(backup.wait(2))
                second=jobs.start({'action':'backup','target':self.row('b')['id']})
                third=jobs.start({'action':'check','target':self.row('c')['id']})
                self.assertTrue(query.wait(2))
                self.assertEqual(jobs.snapshot(first['id'])['state'],'running')
                self.assertEqual(jobs.snapshot(second['id'])['state'],'queued')
                self.assertEqual(jobs.snapshot(third['id'])['state'],'running')
            finally:release.set();self.finish(jobs)

    def test_agent_presets_use_service_os_and_environment_without_creating_dirs(self):
        self.assertEqual(default_agent_roots('codex','win32',{'USERPROFILE':r'C:\fixture-home'}),[r'C:\fixture-home\.codex\skills'])
        self.assertEqual(default_agent_roots('claude_code','win32',{'USERPROFILE':r'C:\fixture-home','CLAUDE_CONFIG_DIR':r'D:\Claude'}),[r'D:\Claude\skills'])
        self.assertEqual(default_agent_roots('codex','darwin',{'HOME':'/fixture-home','CODEX_HOME':'/custom/codex'}),['/custom/codex/skills'])
        with patch('skill_locations.default_agent_roots',return_value=[str(self.base/'missing'/'skills')]):
            add_preset_agent(self.a,'workbuddy');add_preset_agent(self.a,'workbuddy')
        roots=self.a.load_config()['platforms']['workbuddy']['roots']
        self.assertEqual(roots,[str(self.base/'missing'/'skills')])
        self.assertFalse((self.base/'missing').exists())
        with self.assertRaises(ValueError):add_preset_agent(self.a,'arbitrary-agent')
