"""Shared-directory comparisons must not depend on the selected Agent reference."""
import copy
import json
from pathlib import Path
import subprocess
from unittest.mock import patch
import unittest

import test_maintenance as fixtures
import maintenance as core
import update_checks


class ConsistencyTests(unittest.TestCase):
    setUp = fixtures.MaintenanceTests.setUp
    tearDown = fixtures.MaintenanceTests.tearDown
    skill = fixtures.MaintenanceTests.skill
    row = fixtures.MaintenanceTests.row

    def snapshot(self, first_status='check_failed', next_status='latest'):
        row = self.row('a')
        self.m.bind_source(row, 'https://github.com/example/skills', 'main', 'a')
        value = self.m.inventory()
        first = next(i for i in value['items'] if i['id'] == row['id'])
        first['update_check'] = {'id':first['id'], 'status':first_status,
                                 'checked_at':'2026-10-12T12:00:00+00:00'}
        if first_status == 'latest': first['update_check']['local_fingerprint'] = 'same'
        other = copy.deepcopy(first)
        other.update(id='codex:fixture', platform='codex')
        other['update_check'] = {'id':other['id'], 'status':next_status,
                                 'checked_at':'2026-10-12T13:00:00+00:00',
                                 'source':other['source'], 'local_fingerprint':'same'}
        value['items'] = [first, other]
        return value

    def read(self, value):
        self.m.db.execute('update maintenance_inventory_cache set payload=? where id=1', (json.dumps(value),))
        self.m.db.commit()
        changes = self.m.db.total_changes
        result = self.m.cached_inventory()['items']
        self.assertEqual(self.m.db.total_changes, changes)
        return result

    def test_new_success_replaces_older_failure_for_all_agents(self):
        rows = self.read(self.snapshot())
        self.assertEqual([i['update_check']['status'] for i in rows], ['latest','latest'])
        self.assertEqual([i['update_check']['id'] for i in rows], [i['id'] for i in rows])

    def test_new_failure_is_not_hidden_by_older_success(self):
        rows = self.read(self.snapshot('latest','check_failed'))
        self.assertEqual([i['update_check']['status'] for i in rows], ['check_failed','check_failed'])

    def test_later_attempt_takes_precedence_over_original_success_time(self):
        value = self.snapshot('latest','latest')
        value['items'][0]['update_check'].update(attempt_failed=True, attempted_at='2026-10-12T14:00:00+00:00')
        rows = self.read(value)
        self.assertTrue(all(i['update_check'].get('attempt_failed') for i in rows))

    def test_independent_copies_do_not_share_checks(self):
        value = self.snapshot()
        value['items'][1]['real_path'] += '-other'
        self.assertEqual([i['update_check']['status'] for i in self.read(value)], ['check_failed','latest'])

    def test_different_bindings_do_not_share_checks(self):
        value = self.snapshot()
        value['items'][1]['source']['ref'] = 'dev'
        self.assertEqual([i['update_check']['status'] for i in self.read(value)], ['check_failed','latest'])

    def test_different_local_fingerprints_are_not_reconciled(self):
        value = self.snapshot()
        value['items'][0]['update_check']['local_fingerprint'] = 'different'
        self.assertEqual([i['update_check']['status'] for i in self.read(value)], ['check_failed','latest'])

    def test_conflicted_source_is_not_reconciled(self):
        value = self.snapshot()
        value['items'][0]['source_readiness'] = 'source_conflict'
        self.assertEqual([i['update_check']['status'] for i in self.read(value)], ['check_failed','latest'])

    def test_stale_fingerprint_never_turns_into_latest(self):
        value = self.snapshot('stale','latest')
        value['items'][0]['update_check']['local_fingerprint'] = 'same'
        self.assertEqual([i['update_check']['status'] for i in self.read(value)], ['stale','latest'])

    def test_completed_check_is_visible_without_rescanning_cached_inventory(self):
        value = self.snapshot()
        self.read(value)
        latest = {**value['items'][1]['update_check'], 'status':'update_available',
                  'local_version':'1.0', 'remote_version':'2.0',
                  'checked_at':'2026-10-12T15:00:00+00:00'}
        self.m.db.execute('update maintenance_items set last_check=? where id=?',
                          (json.dumps(latest), value['items'][0]['id']))
        self.m.invalidate_cache()
        rows = self.m.cached_inventory()['items']
        self.assertEqual([i['update_check']['status'] for i in rows], ['update_available']*2)

    def test_cleared_check_is_not_restored_from_list_cache(self):
        value=self.snapshot(); self.read(value)
        value['items']=value['items'][:1]
        self.m.db.execute('update maintenance_inventory_cache set payload=? where id=1',(json.dumps(value),))
        self.m.db.execute('update maintenance_items set last_check=null where id=?',(value['items'][0]['id'],))
        self.m.invalidate_cache()
        self.assertFalse(self.m.cached_inventory()['items'][0]['update_check'])

    def test_changed_binding_cannot_recover_old_result_from_another_agent(self):
        value=self.snapshot();self.read(value)
        self.m.bind_source(self.row('a'),'https://github.com/example/different','dev','a')
        rows=self.m.cached_inventory()['items']
        self.assertFalse(rows[0]['update_check'])
        self.assertIn('/different',rows[0]['source']['url'])
        self.assertTrue(all(i['source_readiness']=='source_conflict' for i in rows))

    def test_conflicting_legacy_timestamps_do_not_arbitrarily_choose_an_agent(self):
        value=self.snapshot()
        value['items'][1]['update_check']['checked_at']=value['items'][0]['update_check']['checked_at']
        self.assertEqual([i['update_check']['status'] for i in self.read(value)],['check_failed','latest'])

    def test_frontend_does_not_propagate_results_between_different_channels(self):
        text=(Path(__file__).resolve().parents[1]/'assets/maintenance.html').read_text()
        function='function sourceIdentity'+text.split('function sourceIdentity',1)[1].split('let skillGroupInventory',1)[0]
        script=function+'''
const assert=require('node:assert/strict');
const a={name:'fixture',source:{provider:'github',url:'https://github.com/example/skills',ref:'main',path:'a',channel:'one'}};
const b={...a,source:{...a.source,channel:'two'}};
assert.notEqual(sourceIdentity(a),sourceIdentity(b));
assert.equal(sourceIdentity(a),sourceIdentity({...a,id:'another-agent'}));
'''
        subprocess.run(['node','-e',script],check=True,capture_output=True,text=True)

    def test_verified_result_persists_to_aliases_without_changing_baselines(self):
        row=self.row('a');alias=self.roots/'alias';alias.symlink_to(row['path'],target_is_directory=True)
        self.m.scan();other=self.m.select(str(alias))
        for ref in (row,other):self.m.bind_source(ref,'https://github.com/example/skills','main','a')
        self.m.db.execute('update maintenance_items set baseline=? where id=?',('alias baseline',other['id']))
        self.m.db.commit()
        checked={'id':row['id'],'status':'latest','checked_at':core.stamp(),'local_fingerprint':core.fingerprint(core.manifest(self.roots/'a',ignore_git=True))}
        with patch.object(self.m,'_check_remote',return_value=checked):update_checks.compare(self.m,row)
        value=json.loads(self.m.state(other)['last_check'])
        self.assertEqual(value['status'],'latest');self.assertEqual(value['id'],other['id'])
        self.assertEqual(self.m.state(other)['baseline'],'alias baseline')

    def test_persistence_does_not_cross_alias_source_bindings(self):
        row=self.row('a');alias=self.roots/'alias';alias.symlink_to(row['path'],target_is_directory=True)
        self.m.scan();other=self.m.select(str(alias))
        self.m.bind_source(row,'https://github.com/example/skills','main','a')
        self.m.bind_source(other,'https://github.com/example/skills','dev','a')
        checked={'id':row['id'],'status':'latest','checked_at':core.stamp(),'local_fingerprint':core.fingerprint(core.manifest(self.roots/'a',ignore_git=True))}
        with patch.object(self.m,'_check_remote',return_value=checked):update_checks.compare(self.m,row)
        self.assertFalse(self.m.state(other)['last_check'])
