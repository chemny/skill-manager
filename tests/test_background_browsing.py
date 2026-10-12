"""Browsing must remain readable without competing with a worker's writes."""
import threading
import unittest
from unittest.mock import patch
import test_maintenance as fixtures
import maintenance as core
import web_jobs

class BackgroundBrowsingTests(unittest.TestCase):
    setUp=fixtures.MaintenanceTests.setUp
    tearDown=fixtures.MaintenanceTests.tearDown
    skill=fixtures.MaintenanceTests.skill
    row=fixtures.MaintenanceTests.row

    def test_browse_uses_readonly_connection_during_uncommitted_worker_write(self):
        self.m.inventory()
        plan=self.m.plan('delete',self.row('a'))
        backup=self.m.backup(self.row('a'))
        entered=threading.Event();release=threading.Event();finished=threading.Event()
        def delayed(manager,*args):
            manager.db.execute('update maintenance_items set protected=1')
            entered.set();release.wait(timeout=8);manager.db.rollback();finished.set()
            return {'count':2}
        jobs=web_jobs.Jobs(self.a)
        with patch.object(core.Manager,'scan',delayed):
            job=jobs.start({'action':'scan'})
            self.assertTrue(entered.wait(timeout=2))
            try:
                with patch.object(self.a.Registry,'__init__',side_effect=AssertionError('Read initialized schema')), patch('maintenance.subprocess.run'):
                    for action in core.BROWSE_ACTIONS:
                        payload={'action':action,'id':backup['id'] if action=='backup-detail' else plan['id'],'ids':[backup['id']]}
                        if action=='restore-targets':payload['entries']=[{'id':backup['id']}]
                        if action=='open-directory':payload['path']=str(self.roots)
                        value=core.api_dispatch(self.a,payload)
                        self.assertIsInstance(value,dict)
                self.assertTrue(jobs.busy())
                with self.assertRaises(web_jobs.BusyError):jobs.start({'action':'backup','target':self.row('a')['id']})
            finally:
                release.set();self.assertTrue(finished.wait(timeout=2))

    def test_read_views_do_not_change_protection_or_plan_state(self):
        self.m.inventory();plan=self.m.plan('delete',self.row('a'))
        core.api_dispatch(self.a,{'action':'history'})
        core.api_dispatch(self.a,{'action':'backups'})
        core.api_dispatch(self.a,{'action':'get-plan','id':plan['id']})
        self.assertFalse(self.m.state(self.row('a'))['protected'])
        self.assertEqual(self.m.get_plan(plan['id'])['state'],'preview')
