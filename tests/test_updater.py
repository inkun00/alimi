"""Exercise update safety with private workspace data and mocked processes."""

import hashlib
import ctypes
import sqlite3
import tempfile
import unittest
from contextlib import closing, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from ctypes import wintypes

from alimi import updater
from alimi.core import Event, Store


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.root = Path(self.directory.name)
        self.data_dir = self.root / "local-data"
        self.data_dir.mkdir()
        self.source = self.root / "SchoolAlimi-0.1.4.exe"
        # Deliberately non-executable fixture bytes: no test can start an app.
        self.source.write_bytes(b"School Alimi update fixture\x00new-version")

    def tearDown(self):
        self.directory.cleanup()

    def install(self, version="0.1.4", payload=None):
        if payload is not None:
            self.source.write_bytes(payload)
        return updater.install_versioned(self.source, self.data_dir, version)

    def process(self, path, pid=1234):
        return updater.ManagedProcess(pid=pid, parent_pid=1, path=path, started_at=100)

    def test_version_validation_blocks_path_traversal_and_incomplete_versions(self):
        self.assertEqual((0, 1, 4), updater.version_key("0.1.4"))
        self.assertGreater(updater.version_key("0.10.0"), updater.version_key("0.9.99"))
        for version in ("../0.1.4", "..\\0.1.4", "0.1", "0.1.4/escape", "0.1.4-beta", "C:\\bin", ""):
            with self.subTest(version=version), self.assertRaises(updater.UpgradeError):
                updater.managed_executable(self.data_dir, version)

    def test_install_uses_exact_versioned_path_and_verified_hash(self):
        installed = self.install()
        expected = self.data_dir / "bin" / "0.1.4" / "SchoolAlimi.exe"
        self.assertEqual(expected.resolve(), installed.resolve())
        digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.assertEqual(self.source.read_bytes(), installed.read_bytes())
        self.assertEqual(digest, installed.with_suffix(".sha256").read_text().strip())
        self.assertEqual(digest, updater.hash_file(installed))

    def test_only_exact_stable_or_versioned_app_paths_are_managed(self):
        allowed = (self.data_dir / "bin" / "SchoolAlimi.exe",
                   self.data_dir / "bin" / "0.1.4" / "SchoolAlimi.exe")
        for path in allowed:
            with self.subTest(path=path):
                self.assertTrue(updater.is_managed_executable(path, self.data_dir))
        rejected = (self.root / "SchoolAlimi.exe",
                    self.data_dir / "SchoolAlimi.exe",
                    self.data_dir / "bin" / "SchoolAlimi-old.exe",
                    self.data_dir / "bin" / "SchoolAlimi-0.1.4.exe",
                    self.data_dir / "bin" / "unknown" / "SchoolAlimi.exe",
                    self.data_dir / "bin" / "0.1.4" / "nested" / "SchoolAlimi.exe",
                    self.data_dir / "bin" / "0.1.4" / "another.exe")
        for path in rejected:
            with self.subTest(path=path):
                self.assertFalse(updater.is_managed_executable(path, self.data_dir))

    def test_same_version_same_hash_is_reused_without_copying(self):
        installed = self.install()
        with patch.object(updater.shutil, "copyfile", side_effect=AssertionError("existing version must be reused")):
            self.assertEqual(installed, self.install())

    def test_same_version_different_bytes_cannot_overwrite_installed_binary(self):
        installed = self.install()
        original = installed.read_bytes()
        original_manifest = installed.with_suffix(".sha256").read_bytes()
        self.source.write_bytes(b"unexpected same-version replacement")
        with self.assertRaises(updater.UpgradeError):
            self.install()
        self.assertEqual(original, installed.read_bytes())
        self.assertEqual(original_manifest, installed.with_suffix(".sha256").read_bytes())

    def test_partial_copy_failure_never_publishes_target(self):
        def interrupted_copy(source, target, *args, **kwargs):
            Path(target).write_bytes(b"partial binary")
            raise OSError("copy interrupted")

        with patch.object(updater.shutil, "copyfile", side_effect=interrupted_copy):
            with self.assertRaises(updater.UpgradeError):
                self.install()
        target = updater.managed_executable(self.data_dir, "0.1.4")
        self.assertFalse(target.exists())
        self.assertFalse(target.with_suffix(".sha256").exists())

    def test_copy_hash_mismatch_never_publishes_corrupt_binary(self):
        def corrupt_copy(source, target, *args, **kwargs):
            Path(target).write_bytes(b"different bytes from source")
            return str(target)

        with patch.object(updater.shutil, "copyfile", side_effect=corrupt_copy):
            with self.assertRaises(updater.UpgradeError):
                self.install()
        self.assertFalse(updater.managed_executable(self.data_dir, "0.1.4").exists())

    def test_new_version_is_not_visible_until_copy_and_hash_complete(self):
        stable = self.data_dir / "bin" / "SchoolAlimi.exe"
        stable.parent.mkdir(parents=True)
        stable.write_bytes(b"running legacy fixture")
        target = updater.managed_executable(self.data_dir, "0.1.4")
        real_copy = updater.shutil.copyfile
        observed = []

        def inspect_copy(source, staging, *args, **kwargs):
            self.assertNotEqual(target.resolve(), Path(staging).resolve())
            self.assertFalse(target.exists())
            result = real_copy(source, staging, *args, **kwargs)
            self.assertFalse(target.exists())
            observed.append(True)
            return result

        with patch.object(updater.shutil, "copyfile", side_effect=inspect_copy):
            self.assertEqual(target, self.install())
        self.assertEqual([True], observed)
        self.assertEqual(b"running legacy fixture", stable.read_bytes())

    def test_readonly_backup_includes_committed_wal_and_preserves_live_store(self):
        live = Store(self.data_dir / "alimi.sqlite3")
        try:
            live._db.execute("PRAGMA wal_autocheckpoint=0")
            event = live.upsert_event(Event(title="월별 평가", start_date="2026-10-01"))
            live.complete_event(event.id)
            live.update_position(event.id, 450, 125)
            live.set_setting("autostart", "1")
            self.assertTrue(Path(str(live.path) + "-wal").exists())
            backup = updater.backup_database(self.data_dir, "before-update")
            self.assertIsNotNone(backup)
            self.assertTrue(backup.is_relative_to(self.data_dir / "backups"))
            with closing(sqlite3.connect(backup)) as restored:
                self.assertEqual("ok", restored.execute("PRAGMA integrity_check").fetchone()[0])
                saved = restored.execute("SELECT title,status,x,y FROM events").fetchone()
                self.assertEqual(("월별 평가", "done", 450, 125), saved)
                self.assertEqual("1", restored.execute("SELECT value FROM settings WHERE key='autostart'").fetchone()[0])
            self.assertEqual("done", live.get_event(event.id).status)
            live.upsert_event(Event(title="백업 후 저장", start_date="2026-11-01"))
            self.assertEqual(2, len(live.list_events()))
        finally:
            live.close()

    def test_missing_database_is_not_created_by_update_backup(self):
        self.assertIsNone(updater.backup_database(self.data_dir, "before-update"))
        self.assertFalse((self.data_dir / "alimi.sqlite3").exists())

    def test_corrupt_database_backup_fails_explicitly(self):
        (self.data_dir / "alimi.sqlite3").write_bytes(b"not a sqlite database")
        with self.assertRaises(updater.UpgradeError):
            updater.backup_database(self.data_dir, "before-update")

    def test_update_takes_backup_before_and_after_stopping_old_instance(self):
        previous = self.data_dir / "bin" / "SchoolAlimi.exe"
        previous.parent.mkdir(parents=True)
        previous.write_bytes(b"old fixture")
        old_process = self.process(previous)
        same_version = self.process(updater.managed_executable(self.data_dir, "0.1.4"), pid=1235)
        calls = []

        def backup(_data_dir, label):
            calls.append(label)
            return self.root / (label + ".sqlite3")

        def shutdown(processes):
            self.assertEqual([old_process], processes)
            calls.append("shutdown")

        with patch.object(updater, "install_lock", return_value=nullcontext()), \
             patch.object(updater, "discover_managed_processes", return_value=[old_process, same_version]), \
             patch.object(updater, "backup_database", side_effect=backup), \
             patch.object(updater, "_wake_legacy_manager", side_effect=lambda *_args: calls.append("wake-manager")), \
             patch.object(updater, "shutdown_managed_processes", side_effect=shutdown):
            target = updater.ensure_installed(self.source, self.data_dir, "0.1.4")
        self.assertEqual(["before-update", "wake-manager", "shutdown", "after-update"], calls)
        self.assertEqual(updater.managed_executable(self.data_dir, "0.1.4"), target)

    def test_backup_failure_prevents_process_shutdown(self):
        process = self.process(self.data_dir / "bin" / "SchoolAlimi.exe")
        with patch.object(updater, "install_lock", return_value=nullcontext()), \
             patch.object(updater, "discover_managed_processes", return_value=[process]), \
             patch.object(updater, "backup_database", side_effect=updater.UpgradeError("backup failed")), \
             patch.object(updater, "_wake_legacy_manager") as wake, \
             patch.object(updater, "shutdown_managed_processes") as shutdown:
            with self.assertRaisesRegex(updater.UpgradeError, "backup failed"):
                updater.ensure_installed(self.source, self.data_dir, "0.1.4")
        shutdown.assert_not_called()
        wake.assert_not_called()

    def test_shutdown_failure_never_returns_a_launch_target(self):
        process = self.process(self.data_dir / "bin" / "SchoolAlimi.exe")
        with patch.object(updater, "install_lock", return_value=nullcontext()), \
             patch.object(updater, "discover_managed_processes", return_value=[process]), \
             patch.object(updater, "backup_database", return_value=self.root / "backup.sqlite3") as backup, \
             patch.object(updater, "_wake_legacy_manager", return_value=None), \
             patch.object(updater, "shutdown_managed_processes", side_effect=updater.UpgradeError("old process refused shutdown")):
            with self.assertRaisesRegex(updater.UpgradeError, "refused shutdown"):
                updater.ensure_installed(self.source, self.data_dir, "0.1.4")
        backup.assert_called_once_with(self.data_dir, "before-update")

    def test_fresh_install_does_not_stop_any_process_or_backup_absent_old_instance(self):
        with patch.object(updater, "install_lock", return_value=nullcontext()), \
             patch.object(updater, "discover_managed_processes", return_value=[]), \
             patch.object(updater, "backup_database") as backup, \
             patch.object(updater, "shutdown_managed_processes") as shutdown:
            target = updater.ensure_installed(self.source, self.data_dir, "0.1.4")
        self.assertTrue(target.is_file())
        backup.assert_not_called()
        shutdown.assert_not_called()

    def test_verified_newer_install_prevents_downgrade(self):
        newer = self.install("0.2.0", b"newer release fixture")
        self.source.write_bytes(b"older release fixture")
        with patch.object(updater, "install_lock", return_value=nullcontext()), \
             patch.object(updater, "discover_managed_processes", return_value=[]):
            selected = updater.ensure_installed(self.source, self.data_dir, "0.1.4")
        self.assertEqual(newer, selected)
        self.assertEqual(b"newer release fixture", newer.read_bytes())

    def test_invalid_newer_manifest_is_not_selected(self):
        corrupted = self.install("9.9.9", b"corrupt manifest fixture")
        corrupted.write_bytes(b"tampered installed bytes")
        self.source.write_bytes(b"current release fixture")
        with patch.object(updater, "install_lock", return_value=nullcontext()), \
             patch.object(updater, "discover_managed_processes", return_value=[]):
            selected = updater.ensure_installed(self.source, self.data_dir, "0.1.4")
        self.assertEqual(updater.managed_executable(self.data_dir, "0.1.4"), selected)

    def fake_windows_api(self, shutdown_result=0):
        class UniqueProcess(ctypes.Structure):
            _fields_ = [("dwProcessId", wintypes.DWORD),
                        ("ProcessStartTime", wintypes.FILETIME)]

        kernel = SimpleNamespace(OpenProcess=Mock(return_value=123),
                                 CloseHandle=Mock(return_value=1),
                                 WaitForSingleObject=Mock(return_value=0))
        restart = SimpleNamespace(RmStartSession=Mock(return_value=0),
                                  RmRegisterResources=Mock(return_value=0),
                                  RmShutdown=Mock(return_value=shutdown_result),
                                  RmEndSession=Mock(return_value=0))
        api = (ctypes, wintypes, kernel, None, restart, None, UniqueProcess, None)
        return api, kernel, restart

    def test_restart_manager_never_uses_force_flag(self):
        process = self.process(self.data_dir / "bin" / "SchoolAlimi.exe")
        api, kernel, restart = self.fake_windows_api()
        with patch.object(updater, "_win_api", return_value=api), \
             patch.object(updater, "_graceful_windows_shutdown", return_value=False), \
             patch.object(updater, "_process_path_time", return_value=(process.path.resolve(), process.started_at)):
            updater.shutdown_managed_processes([process])
        restart.RmShutdown.assert_called_once()
        self.assertEqual(0, restart.RmShutdown.call_args.args[1])
        kernel.WaitForSingleObject.assert_called_once()
        restart.RmEndSession.assert_called_once()
        kernel.CloseHandle.assert_called_once_with(123)

    def test_restart_manager_failure_raises_and_releases_all_handles(self):
        process = self.process(self.data_dir / "bin" / "SchoolAlimi.exe")
        api, kernel, restart = self.fake_windows_api(shutdown_result=1234)
        with patch.object(updater, "_win_api", return_value=api), \
             patch.object(updater, "_graceful_windows_shutdown", return_value=False), \
             patch.object(updater, "_process_path_time", return_value=(process.path.resolve(), process.started_at)):
            with self.assertRaisesRegex(updater.UpgradeError, "1234"):
                updater.shutdown_managed_processes([process])
        kernel.WaitForSingleObject.assert_not_called()
        restart.RmEndSession.assert_called_once()
        kernel.CloseHandle.assert_called_once_with(123)

    def test_approved_qt_session_shutdown_skips_restart_manager(self):
        process = self.process(self.data_dir / "bin" / "SchoolAlimi.exe")
        api, kernel, restart = self.fake_windows_api()
        with patch.object(updater, "_win_api", return_value=api), \
             patch.object(updater, "_graceful_windows_shutdown", return_value=True) as graceful, \
             patch.object(updater, "_process_path_time", return_value=(process.path.resolve(), process.started_at)):
            updater.shutdown_managed_processes([process])
        graceful.assert_called_once_with([process])
        restart.RmStartSession.assert_not_called()
        restart.RmShutdown.assert_not_called()
        restart.RmEndSession.assert_not_called()
        kernel.WaitForSingleObject.assert_called_once()
        kernel.CloseHandle.assert_called_once_with(123)

    def test_denied_qt_shutdown_does_not_fall_back_to_restart_manager(self):
        process = self.process(self.data_dir / "bin" / "SchoolAlimi.exe")
        api, kernel, restart = self.fake_windows_api()
        with patch.object(updater, "_win_api", return_value=api), \
             patch.object(updater, "_graceful_windows_shutdown", side_effect=updater.UpgradeError("Qt refused session close")), \
             patch.object(updater, "_process_path_time", return_value=(process.path.resolve(), process.started_at)):
            with self.assertRaisesRegex(updater.UpgradeError, "refused session close"):
                updater.shutdown_managed_processes([process])
        restart.RmStartSession.assert_not_called()
        restart.RmShutdown.assert_not_called()
        restart.RmEndSession.assert_not_called()
        kernel.WaitForSingleObject.assert_not_called()
        kernel.CloseHandle.assert_called_once_with(123)

    def test_qt_shutdown_timeout_fails_without_forcing_or_rm_fallback(self):
        process = self.process(self.data_dir / "bin" / "SchoolAlimi.exe")
        api, kernel, restart = self.fake_windows_api()
        kernel.WaitForSingleObject.return_value = 258  # WAIT_TIMEOUT
        with patch.object(updater, "_win_api", return_value=api), \
             patch.object(updater, "_graceful_windows_shutdown", return_value=True), \
             patch.object(updater, "_process_path_time", return_value=(process.path.resolve(), process.started_at)):
            with self.assertRaises(updater.UpgradeError):
                updater.shutdown_managed_processes([process])
        restart.RmStartSession.assert_not_called()
        restart.RmShutdown.assert_not_called()
        kernel.CloseHandle.assert_called_once_with(123)

    def test_reused_pid_does_not_shutdown_an_unrelated_process(self):
        process = self.process(self.data_dir / "bin" / "SchoolAlimi.exe")
        api, kernel, restart = self.fake_windows_api()
        with patch.object(updater, "_win_api", return_value=api), \
             patch.object(updater, "_process_path_time", return_value=(process.path.resolve(), process.started_at + 1)):
            with self.assertRaises(updater.UpgradeError):
                updater.shutdown_managed_processes([process])
        restart.RmStartSession.assert_not_called()
        restart.RmShutdown.assert_not_called()
        kernel.CloseHandle.assert_called_once_with(123)


if __name__ == "__main__":
    unittest.main()
