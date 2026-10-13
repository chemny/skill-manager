"""Release boundary checks use an isolated home and mocked OS adapters."""
import argparse
import contextlib
import io
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_maintenance as fixtures
import maintenance as core


class ReleaseBoundaryTests(unittest.TestCase):
    setUp = fixtures.MaintenanceTests.setUp
    tearDown = fixtures.MaintenanceTests.tearDown
    skill = fixtures.MaintenanceTests.skill

    def test_one_shot_registry_closes_success_and_error(self):
        for fail in (False, True):
            registry = self.a.Registry()
            with patch.object(self.a, 'Registry', return_value=registry):
                if fail:
                    with self.assertRaises(ValueError):
                        self.a.registry_call('update_channel', 'add', {'name':'bad', 'kind':'skillhub', 'base_url':'http://invalid'})
                else:
                    self.assertIsInstance(self.a.registry_call('update_channels'), list)
            with self.assertRaises(sqlite3.ProgrammingError): registry.conn.execute('select 1')

    def test_initialization_failure_closes_connection(self):
        db = sqlite3.connect(':memory:')
        with patch.object(self.a.sqlite3, 'connect', return_value=db), patch.object(self.a.Registry, 'init_schema', side_effect=RuntimeError('schema failure')):
            with self.assertRaises(RuntimeError): self.a.Registry()
        with self.assertRaises(sqlite3.ProgrammingError): db.execute('select 1')

    def test_list_cli_closes_connection(self):
        registry = self.a.Registry()
        with patch.object(self.a, 'Registry', return_value=registry), contextlib.redirect_stdout(io.StringIO()):
            self.a.list_command(argparse.Namespace(platform='', status=''))
        with self.assertRaises(sqlite3.ProgrammingError): registry.conn.execute('select 1')

    def test_windows_lock_releases_on_exception(self):
        windows = SimpleNamespace(LK_NBLCK=1, LK_UNLCK=2, locking=Mock())
        with patch.object(core, 'os', SimpleNamespace(name='nt')), patch.dict(sys.modules, msvcrt=windows):
            with self.assertRaisesRegex(RuntimeError, 'operation'):
                with self.m.lock(): raise RuntimeError('operation')
        self.assertEqual([c.args[1:] for c in windows.locking.call_args_list], [(1, 1), (2, 1)])

    def test_windows_busy_lock_rejects_operation(self):
        windows = SimpleNamespace(LK_NBLCK=1, LK_UNLCK=2, locking=Mock(side_effect=OSError('busy')))
        entered = False
        with patch.object(core, 'os', SimpleNamespace(name='nt')), patch.dict(sys.modules, msvcrt=windows):
            with self.assertRaisesRegex(ValueError, 'Another maintenance'):
                with self.m.lock(): entered = True
        self.assertFalse(entered)
        self.assertEqual(windows.locking.call_count, 1)

    def test_windows_wrappers_preserve_child_exit_status(self):
        scripts=Path(self.a.__file__).parent
        for name in ('skill-manager.ps1', 'start-manager.ps1'):
            self.assertTrue((scripts/name).read_text().rstrip().endswith('exit $LASTEXITCODE'))
        self.assertTrue((scripts/'start-manager.cmd').read_text().rstrip().endswith('endlocal & exit /b %errorlevel%'))

    def test_windows_open_directory_uses_registered_root_only(self):
        with patch.object(core, 'sys', SimpleNamespace(platform='win32')), patch.object(core.os, 'startfile', create=True) as start:
            self.assertTrue(core.dispatch(self.m, {'action':'open-directory', 'path':str(self.roots)})['opened'])
            start.assert_called_once_with(str(self.roots.resolve()))
            with self.assertRaises(ValueError):core.dispatch(self.m, {'action':'open-directory', 'path':str(self.base)})
            self.assertEqual(start.call_count, 1)
