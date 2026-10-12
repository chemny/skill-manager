import unittest
import test_maintenance as fixtures
import taxonomy


class TaxonomyTests(unittest.TestCase):
    setUp = fixtures.MaintenanceTests.setUp
    tearDown = fixtures.MaintenanceTests.tearDown
    skill = fixtures.MaintenanceTests.skill
    row = fixtures.MaintenanceTests.row

    def test_rename_and_delete_update_references_without_skill_edits(self):
        row=self.row('a');before=(self.roots/'a/SKILL.md').read_bytes()
        for kind,name in [('categories','研究'),('tags','常用')]:
            taxonomy.update(self.m,{'operation':'add','kind':kind,'name':name})
        self.m.set_labels([row['id']],category='研究',tags=['常用'])
        for kind,name,new in [('categories','研究','调研'),('tags','常用','精选')]:
            taxonomy.update(self.m,{'operation':'rename','kind':kind,'name':name,'new_name':new})
        info=next(i for i in self.m.inventory()['items'] if i['id']==row['id'])
        self.assertEqual(info['classification'],{'category':'调研','tags':['精选']})
        self.assertEqual(next(r for r in taxonomy.overview(self.m)['categories'] if r['name']=='调研')['count'],1)
        for kind,name in [('categories','调研'),('tags','精选')]:
            taxonomy.update(self.m,{'operation':'delete','kind':kind,'name':name})
        info=next(i for i in self.m.inventory()['items'] if i['id']==row['id'])
        self.assertEqual(info['classification'],{'category':'','tags':[]})
        self.assertEqual((self.roots/'a/SKILL.md').read_bytes(),before)

    def test_deleted_defaults_are_not_reintroduced(self):
        name=taxonomy.names(self.m)['categories'][0]
        taxonomy.update(self.m,{'operation':'delete','kind':'categories','name':name})
        taxonomy.initialize(self.m)
        self.assertNotIn(name,taxonomy.names(self.m)['categories'])
        with self.assertRaises(ValueError):self.m.set_labels([self.row('a')['id']],category=name)

    def test_duplicate_name_does_not_rewrite_labels(self):
        names=taxonomy.names(self.m)['tags']
        with self.assertRaises(ValueError):taxonomy.update(self.m,{'operation':'rename','kind':'tags','name':names[0],'new_name':names[1]})
        self.assertEqual(taxonomy.names(self.m)['tags'],names)
