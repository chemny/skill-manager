"""Saved confirmations remain separate from failed and interrupted operations."""
import json
import unittest
import datetime as dt
import test_maintenance as fixture
import maintenance as core

class PendingSettingsTests(unittest.TestCase):
    setUp=fixture.MaintenanceTests.setUp
    tearDown=fixture.MaintenanceTests.tearDown
    skill=fixture.MaintenanceTests.skill
    row=fixture.MaintenanceTests.row

    def test_saved_preview_does_not_execute_and_survives_reading(self):
        plan=self.m.plan('delete',self.row('a'))
        listed=core.api_dispatch(self.a,{'action':'plans'})['plans']
        self.assertEqual(listed[0]['state'],'preview')
        self.assertEqual(listed[0]['name'],'a')
        self.assertFalse(listed[0]['expired'])
        self.assertTrue((self.roots/'a'/'SKILL.md').is_file())
        self.assertEqual(core.api_dispatch(self.a,{'action':'get-plan','id':plan['id']})['state'],'preview')
        self.assertFalse(self.m.history()['operations'])

    def test_expired_plan_is_marked_and_rejected_without_writing(self):
        plan=self.m.plan('delete',self.row('a'))
        old=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(hours=25)).isoformat()
        plan['created_at']=old
        self.m.db.execute('update maintenance_plans set payload=? where id=?',(json.dumps(plan),plan['id']));self.m.db.commit()
        self.assertTrue(self.m.plans()['plans'][0]['expired'])
        self.assertTrue(self.m.get_plan(plan['id'])['expired'])
        with self.assertRaisesRegex(ValueError,'expired'):self.m.apply(plan['id'])
        self.assertTrue((self.roots/'a').exists())

    def test_cancel_removes_staging_only_and_preserves_skill(self):
        plan=self.m.plan('delete',self.row('a'))
        self.m.discard_plan(plan['id'])
        self.assertFalse((self.home/'plans'/plan['id']).exists())
        self.assertTrue((self.roots/'a'/'SKILL.md').is_file())
        self.assertEqual(self.m.plans()['plans'],[])

    def test_running_plan_cannot_be_cancelled(self):
        plan=self.m.plan('delete',self.row('a'))
        self.m.db.execute("update maintenance_plans set state='running' where id=?",(plan['id'],));self.m.db.commit()
        self.assertEqual(self.m.plans()['plans'][0]['state'],'running')
        with self.assertRaisesRegex(ValueError,'cannot be discarded'):self.m.discard_plan(plan['id'])
        self.assertTrue((self.roots/'a').exists())

    def test_old_interrupted_operation_remains_visible_beyond_history_limit(self):
        self.m.operation('old-interrupted','sync','running',{'target':str(self.roots/'a')})
        for n in range(55):self.m.operation('recent-'+str(n),'backup','succeeded',{'target':str(self.roots/'b')})
        history=self.m.history(limit=2)
        self.assertEqual(len(history['operations']),2)
        self.assertEqual(history['interrupted_operations'][0]['id'],'old-interrupted')

    def test_internal_logs_do_not_push_user_actions_out_of_history(self):
        self.m.r.log_operation('agent-add','codex',{'roots':['/example/skills']})
        for n in range(55):self.m.r.log_operation('source-channel-search','github',{'state':'success','count':n})
        before=self.m.db.execute('select count(*) from operation_logs').fetchone()[0]
        self.m.db.execute('pragma query_only=on')
        try:history=self.m.history(limit=2)
        finally:self.m.db.execute('pragma query_only=off')
        self.assertEqual(history['user_legacy_operations'][0]['action'],'agent-add')
        self.assertEqual(len(history['legacy_operations']),2)
        self.assertEqual(history['legacy_operations'][0]['action'],'source-channel-search')
        self.assertEqual(self.m.db.execute('select count(*) from operation_logs').fetchone()[0],before)
