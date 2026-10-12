from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import test_maintenance as fixtures


class LocalSourcePickerTests(unittest.TestCase):
    setUp = fixtures.MaintenanceTests.setUp
    tearDown = fixtures.MaintenanceTests.tearDown
    skill = fixtures.MaintenanceTests.skill

    def test_folder(self):
        with patch.object(self.a, 'pick_directory', return_value={'path': str(self.roots)}):
            self.assertEqual(self.a.pick_install_source('folder')['path'], str(self.roots))

    def test_cancel_folder(self):
        with patch.object(self.a, 'pick_directory', side_effect=ValueError('Directory selection was cancelled.')):
            self.assertTrue(self.a.pick_install_source('folder')['cancelled'])

    def test_zip(self):
        archive = self.base / 'skill.zip'
        archive.touch()
        with patch.object(self.a.sys, 'platform', 'darwin'), patch.object(self.a.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=str(archive), stderr='')):
            self.assertEqual(self.a.pick_install_source('file')['path'], str(archive))

    def test_cancel_file(self):
        with patch.object(self.a.sys, 'platform', 'darwin'), patch.object(self.a.subprocess, 'run', return_value=SimpleNamespace(returncode=1, stdout='', stderr='User canceled. (-128)')):
            self.assertTrue(self.a.pick_install_source('file')['cancelled'])

    def test_invalid_file_and_kind(self):
        invalid = self.base / 'skill.txt'
        invalid.touch()
        with patch.object(self.a.sys, 'platform', 'darwin'), patch.object(self.a.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=str(invalid), stderr='')):
            with self.assertRaises(ValueError):
                self.a.pick_install_source('file')
        with self.assertRaises(ValueError):
            self.a.pick_install_source('unknown')
