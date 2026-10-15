"""Complete schedules only after explicit confirmation in isolated Qt fixtures."""

import os
import tempfile
import unittest
from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import onnxruntime

onnxruntime.disable_telemetry_events()

from PyQt5.QtCore import QEvent, Qt
from PyQt5.QtWidgets import QApplication, QMessageBox

from alimi.app import MainWindow, make_icon
from alimi.core import Event, Store


class CompletionConfirmationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.data_dir = Path(self.directory.name)
        self.store = Store(self.data_dir / "alimi.sqlite3")
        args = SimpleNamespace(data_dir=self.data_dir, no_autostart=True,
                               smoke_test=True, background=True)
        self.app.setProperty("shutting_down", False)
        self.window = MainWindow(self.store, args, make_icon())
        self.errors = []
        self.window.show_error = self.errors.append
        self.window.timer.stop()
        self.window.startup_refresh_timer.stop()

    def tearDown(self):
        self.app.setProperty("shutting_down", True)
        window = self.window
        window.pool.waitForDone(10_000)
        window.timer.stop()
        window.midnight_timer.stop()
        window.startup_refresh_timer.stop()
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
        window.close()
        window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()
        self.store.close()
        self.directory.cleanup()
        self.app.setProperty("shutting_down", False)
        self.assertEqual([], self.errors)

    def add(self, title="완료 확인 대상", **changes):
        return self.store.upsert_event(Event(
            title=title, start_date=date.today().isoformat(), **changes))

    def make_note(self, event):
        self.window.refresh()
        note = self.window.notes[event.id]
        note.set_expanded(True)
        self.app.processEvents()
        return note

    def make_list(self, selected):
        self.window.mode_combo.setCurrentIndex(1)
        self.window.refresh()
        listing = self.window.desktop_list
        listing.set_selected_event(selected.id)
        self.app.processEvents()
        return listing

    @contextmanager
    def answer(self, value=QMessageBox.Cancel, inspect=None):
        dialogs = []

        def execute(dialog):
            dialogs.append(dialog)
            if inspect is not None:
                inspect(dialog)
            return value

        with patch.object(QMessageBox, "exec_", new=execute):
            yield dialogs

    def test_note_cancel_keeps_storage_reminder_and_safe_popup_defaults(self):
        event = self.add("<b>보고서</b> & 발표", time="15:00")
        note = self.make_note(event)
        original = self.store.get_event(event.id)
        status = self.window.status_label.text()

        def inspect(dialog):
            self.assertFalse(self.window.isVisible())
            self.assertIs(note, dialog.parentWidget())
            self.assertEqual("일정 완료 확인", dialog.windowTitle())
            self.assertEqual(Qt.PlainText, dialog.textFormat())
            self.assertIn(original.title, dialog.text())
            self.assertIn(original.start_date, dialog.text())
            self.assertIn("15:00", dialog.text())
            self.assertEqual(QMessageBox.Question, dialog.icon())
            self.assertEqual(QMessageBox.Ok | QMessageBox.Cancel, dialog.standardButtons())
            self.assertIs(dialog.button(QMessageBox.Cancel), dialog.defaultButton())
            self.assertIs(dialog.button(QMessageBox.Cancel), dialog.escapeButton())
            self.assertEqual("완료", dialog.button(QMessageBox.Ok).text())
            self.assertEqual("취소", dialog.button(QMessageBox.Cancel).text())
            self.assertTrue(dialog.windowFlags() & Qt.WindowStaysOnTopHint)
            self.assertEqual(Qt.ApplicationModal, dialog.windowModality())

        with self.answer(inspect=inspect) as dialogs, \
                patch.object(self.store, "complete_event", wraps=self.store.complete_event) as complete:
            note.complete_button.click()
        self.assertEqual(1, len(dialogs))
        complete.assert_not_called()
        self.assertEqual(original, self.store.get_event(event.id))
        self.assertIs(note, self.window.notes[event.id])
        self.assertTrue(note.isVisible())
        self.assertTrue(note.expanded)
        self.assertEqual(status, self.window.status_label.text())
        self.assertFalse(self.window._confirming_completion)

    def test_only_ok_can_complete_including_dismissed_dialog_return(self):
        event = self.add()
        note = self.make_note(event)
        original = self.store.get_event(event.id)
        for answer in (QMessageBox.Cancel, 0, QMessageBox.No):
            with self.subTest(answer=answer), self.answer(answer), \
                    patch.object(self.store, "complete_event") as complete:
                note.complete_button.click()
            complete.assert_not_called()
            self.assertEqual(original, self.store.get_event(event.id))
            self.assertIs(note, self.window.notes[event.id])

    def test_confirmed_note_is_persisted_once_and_removed_from_reminders(self):
        event = self.add()
        note = self.make_note(event)
        with self.answer(QMessageBox.Ok) as dialogs, \
                patch.object(self.store, "complete_event", wraps=self.store.complete_event) as complete:
            note.complete_button.click()
        complete.assert_called_once_with(event.id)
        self.assertEqual(1, len(dialogs))
        saved = self.store.get_event(event.id)
        self.assertEqual("done", saved.status)
        self.assertIsNotNone(saved.completed_at)
        self.assertNotIn(event.id, self.window.notes)
        self.assertEqual(1, self.window.history_table.rowCount())
        with closing(Store(self.data_dir / "alimi.sqlite3")) as reopened:
            self.assertEqual(saved, reopened.get_event(event.id))
        self.assertEqual([], self.errors)

    def test_list_cancel_keeps_selected_details_and_all_schedules(self):
        event = self.add("목록의 첫 일정")
        other = self.add("목록의 다른 일정")
        listing = self.make_list(event)
        original = self.store.list_events()
        details = listing.details_label.text()
        selected_row = listing.list_widget.currentRow()
        with self.answer(inspect=lambda dialog: self.assertIs(listing, dialog.parentWidget())), \
                patch.object(self.store, "complete_event") as complete:
            listing.complete_button.click()
        complete.assert_not_called()
        self.assertEqual(original, self.store.list_events())
        self.assertEqual(event.id, listing.selected_event_id)
        self.assertEqual(selected_row, listing.list_widget.currentRow())
        self.assertEqual(details, listing.details_label.text())
        self.assertTrue(listing.details_panel.isVisible())
        self.assertEqual({event.id, other.id}, set(listing.events))

    def test_list_confirmation_completes_initial_id_after_selection_changes(self):
        event = self.add("처음 선택한 일정")
        other = self.add("확인 중 선택이 바뀐 일정")
        listing = self.make_list(event)
        with self.answer(QMessageBox.Ok, inspect=lambda _: listing.set_selected_event(other.id)), \
                patch.object(self.store, "complete_event", wraps=self.store.complete_event) as complete:
            listing.complete_button.click()
        complete.assert_called_once_with(event.id)
        self.assertEqual("done", self.store.get_event(event.id).status)
        self.assertEqual("open", self.store.get_event(other.id).status)
        self.assertNotIn(event.id, listing.events)
        self.assertIn(other.id, listing.events)
        self.assertEqual(other.id, listing.selected_event_id)

    def test_reentrant_completion_is_ignored_until_first_dialog_closes(self):
        event = self.add()
        other = self.add("재진입 대상")
        self.make_note(event)
        with self.answer(QMessageBox.Ok, inspect=lambda _: self.window.complete_event(other.id)) as dialogs, \
                patch.object(self.store, "complete_event", wraps=self.store.complete_event) as complete:
            self.window.complete_event(event.id)
        self.assertEqual(1, len(dialogs))
        complete.assert_called_once_with(event.id)
        self.assertEqual("open", self.store.get_event(other.id).status)
        self.assertFalse(self.window._confirming_completion)

    def test_missing_and_done_schedules_do_not_prompt_or_write(self):
        done = self.add(status="done", completed_at="2026-10-01T12:00:00")
        original = self.store.get_event(done.id)
        with self.answer() as dialogs, patch.object(self.store, "complete_event") as complete:
            self.window.complete_event(done.id)
            self.window.complete_event(999_999)
        self.assertEqual([], dialogs)
        complete.assert_not_called()
        self.assertEqual(original, self.store.get_event(done.id))

    def test_shutdown_before_request_does_not_prompt_read_or_write(self):
        self.app.setProperty("shutting_down", True)
        with self.answer() as dialogs, patch.object(self.store, "get_event") as get, \
                patch.object(self.store, "complete_event") as complete:
            self.window.complete_event(1)
        self.assertEqual([], dialogs)
        get.assert_not_called()
        complete.assert_not_called()

    def test_shutdown_during_confirmation_does_not_write(self):
        event = self.add()
        note = self.make_note(event)
        original = self.store.get_event(event.id)
        with self.answer(QMessageBox.Ok, inspect=lambda _: self.app.setProperty("shutting_down", True)), \
                patch.object(self.store, "complete_event") as complete:
            note.complete_button.click()
        complete.assert_not_called()
        self.assertEqual(original, self.store.get_event(event.id))
        self.assertIs(note, self.window.notes[event.id])

    def test_schedule_deleted_during_confirmation_is_not_written(self):
        event = self.add()
        self.make_note(event)
        with self.answer(QMessageBox.Ok, inspect=lambda _: self.store.reset_events([event.id])), \
                patch.object(self.store, "complete_event") as complete:
            self.window.complete_event(event.id)
        complete.assert_not_called()
        self.assertIsNone(self.store.get_event(event.id))
        self.assertFalse(self.window._confirming_completion)

    def test_schedule_completed_elsewhere_keeps_existing_completion_timestamp(self):
        event = self.add()
        self.make_note(event)

        def complete_elsewhere(_):
            self.store.upsert_event(replace(
                self.store.get_event(event.id), status="done", completed_at="2026-10-01T10:00:00"))

        with self.answer(QMessageBox.Ok, inspect=complete_elsewhere), \
                patch.object(self.store, "complete_event") as complete:
            self.window.complete_event(event.id)
        complete.assert_not_called()
        self.assertEqual("2026-10-01T10:00:00", self.store.get_event(event.id).completed_at)

    def test_schedule_changed_during_confirmation_requires_new_confirmation(self):
        event = self.add()
        note = self.make_note(event)
        with self.answer(QMessageBox.Ok, inspect=lambda _: self.store.upsert_event(replace(
                self.store.get_event(event.id), title="확인 중 바뀐 제목"))), \
                patch.object(self.store, "complete_event") as complete:
            note.complete_button.click()
        complete.assert_not_called()
        self.assertEqual("open", self.store.get_event(event.id).status)
        self.assertIn("변경", self.window.status_label.text())
        self.assertIs(note, self.window.notes[event.id])

    def test_position_changes_during_confirmation_do_not_block_completion(self):
        event = self.add()
        self.make_note(event)
        with self.answer(QMessageBox.Ok, inspect=lambda _: self.store.update_position(event.id, 77, 88)):
            self.window.complete_event(event.id)
        self.assertEqual("done", self.store.get_event(event.id).status)

    def test_save_failure_keeps_list_selection_and_open_schedule(self):
        event = self.add()
        listing = self.make_list(event)
        original = self.store.get_event(event.id)
        with self.answer(QMessageBox.Ok), \
                patch.object(self.store, "complete_event", side_effect=OSError("디스크 쓰기 실패")):
            listing.complete_button.click()
        self.assertEqual(original, self.store.get_event(event.id))
        self.assertEqual(event.id, listing.selected_event_id)
        self.assertIn(event.id, listing.events)
        self.assertTrue(listing.details_panel.isVisible())
        self.assertEqual(1, len(self.errors))
        self.assertIn("메모를 유지", self.errors[0])
        self.errors.clear()
        self.assertFalse(self.window._confirming_completion)

    def test_month_confirmation_does_not_invent_a_specific_day(self):
        event = self.store.upsert_event(Event(
            title="월 단위 일정", start_date=date.today().replace(day=1).isoformat(),
            date_precision="month"))
        note = self.make_note(event)

        def inspect(dialog):
            self.assertIn(f"{date.today().year}년 {date.today().month}월", dialog.text())
            self.assertNotIn(event.start_date, dialog.text())

        with self.answer(inspect=inspect):
            note.complete_button.click()

    def test_date_range_and_time_are_in_confirmation(self):
        event = self.add(end_date="2099-12-31", time="15:00")
        note = self.make_note(event)

        def inspect(dialog):
            self.assertIn(f"{event.start_date} ~ 2099-12-31 · 15:00", dialog.text())

        with self.answer(inspect=inspect):
            note.complete_button.click()


if __name__ == "__main__":
    unittest.main()
