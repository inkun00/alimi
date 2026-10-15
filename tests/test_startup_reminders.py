"""Login-started reminders survive optional backup failures and shell startup."""

import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Match the executable's Windows DLL initialization order.
import onnxruntime

onnxruntime.disable_telemetry_events()

from PyQt5.QtCore import QEvent
from PyQt5.QtWidgets import QApplication, QMessageBox

from alimi.app import MainWindow, make_icon
from alimi.core import Event, Store


class StartupReminderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.data_dir = Path(self.directory.name)
        self.store = Store(self.data_dir / "alimi.sqlite3")
        self.app.setProperty("shutting_down", False)
        self.windows = []
        # Hold deferred calls so the tests can simulate backup completion and
        # elapsed retry time without sleeping or accessing a real user's data.
        self.single_shot_patch = patch("alimi.app.QTimer.singleShot")
        self.single_shot = self.single_shot_patch.start()
        self.tray_patch = patch("alimi.app.QSystemTrayIcon.show")
        self.tray_patch.start()

    def tearDown(self):
        self.app.setProperty("shutting_down", True)
        for window in self.windows:
            window.pool.waitForDone(10_000)
            window.timer.stop()
            window.midnight_timer.stop()
            window.startup_refresh_timer.stop()
            window.on_shutdown()
            window.capture_notepad.deleteLater()
            for note in list(window.notes.values()):
                note.finish()
            window.notes.clear()
            if window.desktop_list:
                window.desktop_list.finish()
                window.desktop_list = None
            for signal, callback in ((self.app.aboutToQuit, window.on_shutdown),
                                     (self.app.commitDataRequest, window.on_session_end)):
                try:
                    signal.disconnect(callback)
                except (TypeError, RuntimeError):
                    pass
            window.close()
            window.deleteLater()
        try:
            self.app.applicationStateChanged.disconnect()
        except (TypeError, RuntimeError):
            pass
        self.app.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()
        self.store.close()
        self.directory.cleanup()
        self.tray_patch.stop()
        self.single_shot_patch.stop()
        self.app.setProperty("shutting_down", False)

    def add_event(self, title="로그인 시 표시할 일정", days=0, status="open"):
        return self.store.upsert_event(Event(
            title=title, start_date=(date.today() + timedelta(days=days)).isoformat(),
            status=status))

    def make_window(self, *, mode="sticky", background=True, smoke=False):
        self.store.set_setting("desktop_mode", mode)
        args = SimpleNamespace(data_dir=self.data_dir, no_autostart=True,
                               smoke_test=smoke, background=background)
        window = MainWindow(self.store, args, make_icon())
        self.windows.append(window)
        return window

    def deferred_backups(self):
        return [call.args[1] for call in self.single_shot.call_args_list if call.args[0] == 500]

    def assert_backup_failure_preserves_reminders(self, mode):
        event = self.add_event()
        with patch.object(self.store, "backup", side_effect=PermissionError("백업 폴더 접근 거부")) as backup:
            window = self.make_window(mode=mode)
            self.assertTrue(window.capture_notepad.isVisible())
            self.assertFalse(window.isVisible(), "A login-started manager stays hidden.")
            if mode == "sticky":
                self.assertTrue(window.notes[event.id].isVisible())
            else:
                self.assertTrue(window.desktop_list.isVisible())
                self.assertEqual(1, window.desktop_list.list_widget.count())
            backup.assert_not_called()
            self.assertEqual(1, len(self.deferred_backups()))
            with patch("alimi.app.logging.exception") as log:
                self.deferred_backups()[0]()
            log.assert_called_once()
            backup.assert_called_once()
            self.assertIn("일정은 표시되어 있습니다", window.status_label.text())
            self.assertIsNone(self.store.get_setting("last_backup_date"))
            self.assertEqual("open", self.store.get_event(event.id).status)
            self.assertTrue(window.capture_notepad.isVisible())
            if mode == "sticky":
                self.assertTrue(window.notes[event.id].isVisible())
            else:
                self.assertTrue(window.desktop_list.isVisible())

    def test_background_sticky_is_shown_before_a_failed_daily_backup(self):
        self.assert_backup_failure_preserves_reminders("sticky")

    def test_background_list_is_shown_before_a_failed_daily_backup(self):
        self.assert_backup_failure_preserves_reminders("list")

    def test_multiple_refreshes_queue_only_one_pending_backup(self):
        self.add_event()
        window = self.make_window()
        with patch.object(self.store, "backup", return_value=self.data_dir / "backup.sqlite3") as backup:
            for _ in range(6):
                window.sync_notes()
            self.assertEqual(1, len(self.deferred_backups()))
            backup.assert_not_called()
            self.assertTrue(window._daily_backup_pending)
            self.deferred_backups()[0]()
            backup.assert_called_once()
            self.assertFalse(window._daily_backup_pending)
            self.assertEqual(date.today().isoformat(), self.store.get_setting("last_backup_date"))
            window.sync_notes()
            self.deferred_backups()[-1]()
            backup.assert_called_once()

    def test_failed_backup_retries_after_one_minute_without_clearing_saved_date(self):
        event = self.add_event()
        previous = (date.today() - timedelta(days=1)).isoformat()
        self.store.set_setting("last_backup_date", previous)
        with patch("alimi.app.time.monotonic", return_value=100.0) as clock:
            window = self.make_window()
            with patch.object(self.store, "backup", side_effect=[PermissionError("denied"),
                              self.data_dir / "backup.sqlite3"]) as backup:
                with patch("alimi.app.logging.exception"):
                    self.deferred_backups()[0]()
                self.assertEqual(160.0, window._daily_backup_retry_at)
                self.assertEqual(previous, self.store.get_setting("last_backup_date"))
                for _ in range(3):
                    window.sync_notes()
                clock.return_value = 159.999
                window.sync_notes()
                self.assertEqual(1, len(self.deferred_backups()))
                self.assertTrue(window.notes[event.id].isVisible())
                clock.return_value = 160.0
                window.sync_notes()
                self.assertEqual(2, len(self.deferred_backups()))
                self.deferred_backups()[-1]()
                self.assertEqual(2, backup.call_count)
                self.assertEqual(date.today().isoformat(), self.store.get_setting("last_backup_date"))
                self.assertEqual(0.0, window._daily_backup_retry_at)

    def test_existing_daily_backup_is_not_repeated(self):
        self.add_event()
        self.store.set_setting("last_backup_date", date.today().isoformat())
        window = self.make_window()
        with patch.object(self.store, "backup") as backup:
            self.deferred_backups()[0]()
            backup.assert_not_called()
        self.assertFalse(window._daily_backup_pending)

    def test_smoke_test_never_queues_a_daily_backup(self):
        self.add_event()
        window = self.make_window(smoke=True)
        window.sync_notes()
        window.queue_daily_backup()
        self.assertEqual([], self.deferred_backups())

    def test_startup_refresh_is_background_only_and_stops_after_five_ticks(self):
        self.add_event()
        foreground = self.make_window(background=False)
        self.assertFalse(foreground.startup_refresh_timer.isActive())
        background = self.make_window()
        self.assertTrue(background.startup_refresh_timer.isActive())
        self.assertEqual(2_000, background.startup_refresh_timer.interval())
        with patch.object(background, "sync_notes") as refresh:
            for _ in range(5):
                background.refresh_startup_reminders()
            self.assertEqual(5, refresh.call_count)
            self.assertFalse(background.startup_refresh_timer.isActive())
            background.refresh_startup_reminders()
            self.assertEqual(5, refresh.call_count)

    def test_startup_refresh_respects_saved_window_and_closed_sticky_and_icon(self):
        today = self.add_event()
        nearby = self.add_event("기간 안 미래 일정", days=3)
        outside = self.add_event("기간 밖 미래 일정", days=4)
        completed = self.add_event("완료 일정", status="done")
        self.store.set_setting("reminder_unlimited", "false")
        self.store.set_setting("reminder_past_days", "0")
        self.store.set_setting("reminder_future_days", "3")
        window = self.make_window()
        self.assertEqual({today.id, nearby.id}, set(window.notes))
        with patch("alimi.app.QMessageBox.warning", return_value=QMessageBox.Ok):
            window.notes[today.id].close()
            window.capture_notepad.close()
        window.refresh_startup_reminders()
        self.assertFalse(window.notes[today.id].isVisible())
        self.assertFalse(window.capture_notepad.isVisible())
        self.assertTrue(window.notes[nearby.id].isVisible())
        self.assertNotIn(outside.id, window.notes)
        self.assertNotIn(completed.id, window.notes)
        self.assertEqual("false", self.store.get_setting("reminder_unlimited"))
        self.assertEqual("3", self.store.get_setting("reminder_future_days"))

    def test_startup_refresh_does_not_reopen_a_closed_list(self):
        self.add_event()
        window = self.make_window(mode="list")
        with patch("alimi.app.QMessageBox.warning", return_value=QMessageBox.Ok):
            window.desktop_list.close()
        window.refresh_startup_reminders()
        self.assertTrue(window._desktop_list_dismissed)
        self.assertFalse(window.desktop_list.isVisible())

    def test_shutdown_stops_startup_refresh_and_discards_pending_backup(self):
        event = self.add_event()
        window = self.make_window()
        window.on_shutdown()
        window.notes[event.id].hide()
        self.assertFalse(window.startup_refresh_timer.isActive())
        self.assertEqual(0, window._startup_refresh_remaining)
        with patch.object(window, "sync_notes") as refresh, patch.object(self.store, "backup") as backup:
            window.refresh_startup_reminders()
            self.deferred_backups()[0]()
            refresh.assert_not_called()
            backup.assert_not_called()
        self.assertFalse(window.notes[event.id].isVisible())
        self.assertFalse(window.capture_notepad.isVisible())
        self.assertFalse(window._daily_backup_pending)
        self.assertEqual("open", self.store.get_event(event.id).status)


if __name__ == "__main__":
    unittest.main()
