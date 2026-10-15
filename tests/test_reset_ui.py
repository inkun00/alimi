"""Exercise confirmed schedule resets using isolated local data only."""

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import onnxruntime

onnxruntime.disable_telemetry_events()

from PyQt5 import sip
from PyQt5.QtCore import QDate, QEvent, Qt
from PyQt5.QtWidgets import QApplication, QDialog, QMessageBox

from alimi.app import MainWindow, make_icon
from alimi.core import Event, Store


class ScheduleResetUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.data_dir = Path(self.directory.name)
        self.store = Store(self.data_dir / "alimi.sqlite3")
        self.args = SimpleNamespace(data_dir=self.data_dir, no_autostart=True,
                                    smoke_test=True, background=False)
        self.app.setProperty("shutting_down", False)
        self.window = MainWindow(self.store, self.args, make_icon())
        self.errors = []
        self.window.show_error = self.errors.append
        self.window.calendar.setSelectedDate(QDate(2026, 10, 15))
        self.app.processEvents()

    def tearDown(self):
        window = self.window
        window.pool.waitForDone(10_000)
        window.timer.stop()
        window.midnight_timer.stop()
        window.on_shutdown()
        window.capture_notepad.deleteLater()
        for note in list(window.notes.values()):
            note.finish()
        window.notes.clear()
        if window.desktop_list is not None:
            window.desktop_list.finish()
            window.desktop_list = None
        for signal, callback in ((self.app.aboutToQuit, window.on_shutdown),
                                 (self.app.commitDataRequest, window.on_session_end)):
            try:
                signal.disconnect(callback)
            except (TypeError, RuntimeError):
                pass
        try:
            self.app.applicationStateChanged.disconnect()
        except (TypeError, RuntimeError):
            pass
        self.app.setProperty("shutting_down", True)
        window.close()
        window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()
        self.app.setProperty("shutting_down", False)
        self.store.close()
        self.directory.cleanup()

    def add(self, title, start="2026-10-15", **changes):
        return self.store.upsert_event(Event(title=title, start_date=start, **changes))

    def backups(self):
        return list((self.data_dir / "backups").glob("*.sqlite3"))

    def configure_range(self, first="2026-10-15", last="2026-10-15"):
        self.window.reset_from_date.setDate(QDate.fromString(first, "yyyy-MM-dd"))
        self.window.reset_to_date.setDate(QDate.fromString(last, "yyyy-MM-dd"))

    def assert_same_content(self, expected, actual):
        # Refresh may save a new desktop position when remaining notes move.
        self.assertIsNotNone(actual)
        self.assertEqual(expected, replace(actual, x=expected.x, y=expected.y))

    def test_cancel_is_default_and_never_backs_up_or_deletes(self):
        original = self.add("취소할 초기화")
        self.configure_range()
        with patch.object(QMessageBox, "warning", return_value=QMessageBox.Cancel) as warning, \
                patch.object(self.store, "reset_events", wraps=self.store.reset_events) as reset:
            self.window.reset_range_button.click()
        self.assertEqual(original, self.store.get_event(original.id))
        reset.assert_not_called()
        self.assertEqual([], self.backups())
        self.assertEqual(QMessageBox.Cancel, warning.call_args.args[-1])
        message = warning.call_args.args[2]
        for detail in ("2026-10-15", "1건", "완료", "확인 필요", "자동 백업", "초안"):
            self.assertIn(detail, message)
        self.assertEqual([], self.errors)

    def test_period_reset_overlaps_ranges_months_and_all_statuses(self):
        selected = [
            self.add("선택일 미완료"),
            self.add("선택일 완료", status="done", completed_at="2026-10-14T01:00:00Z"),
            self.add("선택일 확인 필요", status="review"),
            self.add("선택일을 포함한 기간", "2026-10-01", end_date="2026-10-20"),
            self.add("10월 일정", "2026-10-01", date_precision="month"),
        ]
        kept = [self.add("기간 앞", "2026-10-14"),
                self.add("기간 뒤", "2026-10-16"),
                self.add("날짜 없는 확인 필요", "", status="review")]
        self.window.refresh()
        self.configure_range()
        with patch.object(QMessageBox, "warning", return_value=QMessageBox.Ok):
            self.window.reset_range_button.click()
        self.assertEqual({event.id for event in kept}, {event.id for event in self.store.list_events()})
        self.assertTrue(all(self.store.get_event(event.id) is None for event in selected))
        for event in kept:
            self.assert_same_content(event, self.store.get_event(event.id))
        self.assertEqual({event.id for event in kept if event.status == "open"}, set(self.window.notes))
        self.assertEqual(0, self.window.history_table.rowCount())
        self.assertEqual(1, self.window.review_table.rowCount())
        self.assertIn("5건", self.window.reset_result_label.text())
        backup, = self.backups()
        self.assertIn(str(backup), self.window.reset_result_label.text())
        self.assertEqual([], self.errors)

    def test_all_reset_removes_visible_reminders_calendar_history_and_review(self):
        self.add("보이는 미완료")
        self.add("다른 날", "2027-01-01")
        self.add("완료", status="done")
        self.add("확인 필요", "", status="review")
        self.window.refresh()
        notes = list(self.window.notes.values())
        self.assertTrue(notes)
        self.assertTrue(all(note.isVisible() for note in notes))
        with patch.object(QMessageBox, "warning", return_value=QMessageBox.Ok):
            self.window.reset_all_button.click()
        self.assertEqual([], self.store.list_events())
        self.assertEqual({}, self.window.notes)
        self.assertTrue(all(sip.isdeleted(note) or not note.isVisible() for note in notes))
        self.assertEqual(0, self.window.review_table.rowCount())
        self.assertEqual(0, self.window.history_table.rowCount())
        self.assertFalse(self.window.edit_selected_button.isEnabled())
        self.assertIsNone(self.window.agenda.item(0).data(Qt.UserRole))
        self.assertIn("4건", self.window.reset_result_label.text())
        self.assertEqual([], self.errors)

    def test_all_reset_closes_desktop_list(self):
        self.add("목록에 보이는 일정")
        self.window.mode_combo.setCurrentIndex(1)
        self.app.processEvents()
        desktop = self.window.desktop_list
        self.assertIsNotNone(desktop)
        self.assertTrue(desktop.isVisible())
        with patch.object(QMessageBox, "warning", return_value=QMessageBox.Ok):
            self.window.reset_all_schedules()
        self.assertIsNone(self.window.desktop_list)
        self.assertTrue(sip.isdeleted(desktop) or not desktop.isVisible())
        self.assertEqual([], self.store.list_events())
        self.assertEqual([], self.errors)

    def test_reset_preserves_display_settings_and_flushes_both_unsaved_drafts_into_backup(self):
        original = self.add("복원할 일정", notes="일정 메모")
        self.store.set_setting("reminder_unlimited", "false")
        self.store.set_setting("reminder_past_days", "11")
        self.store.set_setting("reminder_future_days", "27")
        self.store.set_setting("font_size", "17")
        self.window.capture_notepad.editor.setPlainText("초기화 직전 메모장 초안")
        self.window.text_input.setPlainText("초기화 직전 인식한 본문 초안")
        with patch.object(QMessageBox, "warning", return_value=QMessageBox.Ok):
            self.window.reset_all_schedules()
        expected_settings = dict(reminder_unlimited="false", reminder_past_days="11",
                                 reminder_future_days="27", font_size="17",
                                 capture_draft="초기화 직전 메모장 초안",
                                 recognized_text_draft="초기화 직전 인식한 본문 초안")
        backup, = self.backups()
        saved = Store(backup)
        try:
            self.assertEqual(original, saved.get_event(original.id))
            for key, value in expected_settings.items():
                self.assertEqual(value, self.store.get_setting(key), key)
                self.assertEqual(value, saved.get_setting(key), key)
        finally:
            saved.close()
        self.assertEqual("초기화 직전 메모장 초안", self.window.capture_notepad.editor.toPlainText())
        self.assertEqual("초기화 직전 인식한 본문 초안", self.window.text_input.toPlainText())
        self.store.restore(backup)
        self.window.refresh()
        self.assert_same_content(original, self.store.get_event(original.id))
        self.assertEqual([], self.errors)

    def test_reset_rejects_stale_review_and_clears_queued_imports(self):
        original = self.add("열려 있던 기존 일정")
        self.window.queue_import_review([original], "기존 공지", "수정할 원문")
        dialog = self.window.import_dialog
        dialog.table.item(0, dialog.TITLE).setText("확인 중 수정한 기존 일정")
        selected = dialog.selected_events()
        self.assertEqual(original.id, selected[0].id)
        self.window.queue_import_review([Event(title="대기 중인 미등록 일정", start_date="2026-10-16")],
                                        "다음 공지", "대기 원문")
        self.assertEqual(1, len(self.window.import_queue))
        finished = []
        dialog.finished.connect(finished.append)
        self.window._dismissed_note_ids.add(original.id)
        with patch.object(QMessageBox, "warning", return_value=QMessageBox.Ok):
            self.window.reset_all_schedules()
        self.assertEqual([QDialog.Rejected], finished)
        self.assertIsNone(self.window.import_dialog)
        self.assertEqual([], self.window.import_queue)
        self.assertNotIn(original.id, self.window._dismissed_note_ids)
        self.app.processEvents()
        self.assertIsNone(self.window.import_dialog)
        with self.assertRaisesRegex(ValueError, "수정할 일정이 없습니다"):
            self.store.save_reviewed_events(selected)
        self.assertEqual([], self.store.list_events())
        self.assertEqual([], self.errors)

    def test_new_schedule_created_during_warning_is_not_part_of_confirmed_reset(self):
        original = self.add("확인한 일정")
        added = []

        def confirm(*_args):
            added.append(self.add("확인창 중 새로 생긴 일정"))
            return QMessageBox.Ok

        with patch.object(QMessageBox, "warning", side_effect=confirm):
            self.window.reset_all_schedules()
        self.assertIsNone(self.store.get_event(original.id))
        self.assertEqual(1, len(self.store.list_events()))
        self.assert_same_content(added[0], self.store.list_events()[0])
        self.assertIn(added[0].id, self.window.notes)
        self.assertIn("1건", self.window.reset_result_label.text())
        self.assertEqual([], self.errors)

    def test_reversed_range_reports_error_without_warning_or_backup(self):
        original = self.add("유효성 검증 일정")
        self.configure_range("2026-10-20", "2026-10-10")
        with patch.object(QMessageBox, "warning") as warning:
            self.window.reset_range_button.click()
        warning.assert_not_called()
        self.assertEqual([original], self.store.list_events())
        self.assertEqual([], self.backups())
        self.assertEqual(1, len(self.errors))
        self.assertIn("종료일", self.errors[0])

    def test_empty_period_explains_without_confirmation_or_backup(self):
        original = self.add("기간 밖 일정", "2026-11-01")
        self.configure_range()
        with patch.object(QMessageBox, "warning") as warning, \
                patch.object(QMessageBox, "information") as info:
            self.window.reset_range_button.click()
        warning.assert_not_called()
        self.assertIn("초기화할 일정이 없습니다", info.call_args.args[2])
        self.assertEqual([original], self.store.list_events())
        self.assertEqual([], self.backups())
        self.assertEqual([], self.errors)

    def test_running_analysis_blocks_reset_without_mutation(self):
        original = self.add("분석 중 보존할 일정")
        self.window.jobs.add(object())
        try:
            with patch.object(QMessageBox, "warning") as warning, \
                    patch.object(QMessageBox, "information") as info:
                self.window.reset_all_button.click()
            warning.assert_not_called()
            self.assertIn("분석이 끝난 뒤", info.call_args.args[2])
            self.assertEqual([original], self.store.list_events())
            self.assertEqual([], self.backups())
        finally:
            self.window.jobs.clear()

    def test_backup_failure_keeps_data_review_and_reminders_untouched(self):
        original = self.add("백업 실패 시 보존")
        self.window.refresh()
        note = self.window.notes[original.id]
        original = self.store.get_event(original.id)
        self.window.queue_import_review([original], "원문", "현재 확인 중")
        dialog = self.window.import_dialog
        with patch.object(QMessageBox, "warning", return_value=QMessageBox.Ok), \
                patch.object(self.store, "backup", side_effect=PermissionError("백업 쓰기 권한 없음")):
            self.window.reset_all_schedules()
        self.assertEqual([original], self.store.list_events())
        self.assertIs(note, self.window.notes[original.id])
        self.assertTrue(note.isVisible())
        self.assertIs(dialog, self.window.import_dialog)
        self.assertTrue(dialog.isVisible())
        self.assertIn("백업 쓰기 권한 없음", self.errors[0])
        self.assertEqual([], self.backups())

    def test_database_failure_keeps_ui_and_all_rows_after_transaction_rollback(self):
        originals = [self.add("첫 번째 보존"), self.add("두 번째 보존")]
        self.window.refresh()
        originals = self.store.list_events()
        with self.store._db:
            self.store._db.execute(
                f"CREATE TRIGGER stop_reset BEFORE DELETE ON events WHEN OLD.id={originals[1].id} "
                "BEGIN SELECT RAISE(ABORT, 'reset storage failure'); END")
        with patch.object(QMessageBox, "warning", return_value=QMessageBox.Ok):
            self.window.reset_all_schedules()
        self.assertEqual(originals, self.store.list_events())
        self.assertEqual({event.id for event in originals}, set(self.window.notes))
        self.assertIn("reset storage failure", self.errors[0])
        backup, = self.backups()
        with closing(sqlite3.connect(backup)) as saved:
            self.assertEqual(2, saved.execute("SELECT COUNT(*) FROM events").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
