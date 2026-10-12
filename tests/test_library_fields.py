"""Library date provenance and read-window semantics, using disposable state."""
import datetime as dt
import os
from pathlib import Path
import unittest
from zoneinfo import ZoneInfo
import test_maintenance as fixtures

class LibraryFieldsTests(unittest.TestCase):
    setUp = fixtures.MaintenanceTests.setUp
    tearDown = fixtures.MaintenanceTests.tearDown
    skill = fixtures.MaintenanceTests.skill
    row = fixtures.MaintenanceTests.row

    def record(self, item, at, session='session'):
        self.m.db.execute('insert into usage_events(capability_id,occurred_at,source,confidence,session_id,evidence) values (?,?,?,?,?,?)', (item['id'],at.isoformat(),'fixture','read',session,'SKILL.md'))
        self.m.db.commit()

    def test_local_today_rolling_windows_future_and_duplicate_aliases(self):
        now = dt.datetime(2026,10,8,12,tzinfo=ZoneInfo('America/Los_Angeles'))
        ref = self.roots/'alias'; ref.symlink_to(self.roots/'a',target_is_directory=True)
        self.m.scan();a=self.m.select(str(self.roots/'a'));alias=self.m.select(str(ref))
        midnight=now.replace(hour=0)
        for at in (midnight, midnight-dt.timedelta(seconds=1), now-dt.timedelta(days=7), now-dt.timedelta(days=30), now-dt.timedelta(days=90)):
            self.record(a,at)
        self.record(alias,midnight)  # Same physical read mirrored at another Agent entry.
        self.record(a,now+dt.timedelta(seconds=1),'future')
        result=self.m.library_frequencies(self.m.r.all_capabilities(),now)
        self.assertEqual(result[str(self.roots/'a')],{'today':1,'7':3,'14':3,'30':4,'90':5})
        self.assertNotIn(str(self.roots/'b'),result)

    def test_date_is_registration_not_filesystem_mtime_and_survives_rescan(self):
        os.utime(self.roots/'a',(946684800,946684800))
        first=next(i for i in self.m.inventory()['items'] if i['id']==self.row('a')['id'])
        self.assertIn(first['installation']['basis'],('directory-created','first-recorded'))
        if first['installation']['basis']=='first-recorded':
            self.assertFalse(first['installation']['at'].startswith('2000'))
        else:
            from library_metadata import directory_date
            self.assertEqual(first['installation']['at'],directory_date(self.roots/'a'))
        self.assertEqual(first['usage_frequency'],{'today':None,'7':None,'14':None,'30':None,'90':None})
        self.m.scan()
        second=next(i for i in self.m.inventory()['items'] if i['id']==first['id'])
        self.assertEqual(second['installation'],first['installation'])

    def test_shared_aliases_use_confirmed_install_date(self):
        ref=self.roots/'alias';ref.symlink_to(self.roots/'a',target_is_directory=True);self.m.scan()
        self.m.db.execute('update maintenance_item_dates set installed_at=? where id=?',('2026-10-01T12:00:00+00:00',self.m.select(str(self.roots/'a'))['id']))
        items=[i for i in self.m.inventory()['items'] if i['real_path']==str(self.roots/'a')]
        self.assertEqual(len(items),2)
        self.assertTrue(all(i['installation']=={'at':'2026-10-01T12:00:00+00:00','basis':'installed'} for i in items))

    def test_recency_retains_old_evidence_and_ignores_future_reads(self):
        now=dt.datetime(2026,10,8,12,tzinfo=dt.timezone.utc)
        self.record(self.row('a'),now-dt.timedelta(days=120))
        self.record(self.row('a'),now+dt.timedelta(days=1),'future')
        result=self.m.library_last_reads(self.m.r.all_capabilities(),now)
        self.assertEqual(result[str(self.roots/'a')],(now-dt.timedelta(days=120)).isoformat())
        self.assertNotIn(str(self.roots/'b'),result)
