"""Exercise desktop mode switching and preservation of source date precision."""

import os
import tempfile
import unittest
from calendar import monthrange
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# The executable initializes ONNX before Qt to avoid Windows DLL order issues.
import onnxruntime

onnxruntime.disable_telemetry_events()

from PyQt5 import sip
from PyQt5.QtCore import QDate, QEvent, QRect, Qt, QTimer
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QDialog, QDialogButtonBox, QMessageBox

from alimi.app import EventDialog, MainWindow, TEST_NOTE_SOURCE, make_icon
from alimi.core import Event, Store
from alimi.urgency import event_urgency


class DesktopModeIntegrationTests(unittest.TestCase):
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
        self.windows = []
        self.errors = []
        self.window = self.make_window()

    def make_window(self):
        window = MainWindow(self.store, self.args, make_icon())
        window.show_error = self.errors.append
        self.windows.append(window)
        return window

    def destroy_window(self, window):
        window.pool.waitForDone(10_000)
        self.app.processEvents()
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
        window.tray.hide()
        for signal, callback in ((self.app.aboutToQuit, window.on_shutdown),
                                 (self.app.commitDataRequest, window.on_session_end)):
            try:
                signal.disconnect(callback)
            except (TypeError, RuntimeError):
                pass
        # The application-state callback is currently an anonymous lambda.
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
        self.windows.remove(window)

    def tearDown(self):
        for window in list(self.windows):
            self.destroy_window(window)
        self.store.close()
        self.directory.cleanup()

    def pump(self):
        self.app.processEvents()
        QTest.qWait(1)

    def edit_schedule(self, event_id, changes=None, cancel=False):
        original_exec = EventDialog.exec_
        dialogs = []

        def execute(dialog):
            dialogs.append(dialog)

            def choose():
                for name, value in (changes or {}).items():
                    field = getattr(dialog, name + "_field")
                    if name == "notes":
                        field.setPlainText(value)
                    else:
                        field.setText(value)
                buttons = dialog.findChild(QDialogButtonBox)
                buttons.button(QDialogButtonBox.Cancel if cancel else QDialogButtonBox.Save).click()

            QTimer.singleShot(0, choose)
            return original_exec(dialog)

        with patch.object(EventDialog, "exec_", execute):
            self.window.edit_event(event_id)
        self.pump()
        self.assertEqual(1, len(dialogs))
        self.assertEqual(QDialog.Rejected if cancel else QDialog.Accepted, dialogs[0].result())

    def test_edit_event_refreshes_details_and_preserves_identity_in_both_modes(self):
        event = self.store.upsert_event(Event(
            title="원래 일정", start_date="2026-10-14", time="10:00", place="원래 장소",
            notes="원래 메모", source="학교 공지", source_text="가져온 원문"))
        self.store.upsert_event(Event(title="같은 날 다른 일정", start_date=event.start_date, time="09:00"))
        self.window.calendar.setSelectedDate(QDate(2026, 10, 14))
        self.window.refresh()
        original_created = self.store.get_event(event.id).created_at

        for mode in ("sticky", "list"):
            with self.subTest(mode=mode):
                if mode == "list":
                    desktop = self.switch_to_list()
                    desktop.set_selected_event(event.id)
                else:
                    desktop = self.window.notes[event.id]
                    desktop.toggle_details()
                changes = dict(title=f"수정한 일정 {mode}", time="11:30", place="수정한 장소",
                               notes=f"수정한 메모 {mode}")
                self.edit_schedule(event.id, changes)
                saved = self.store.get_event(event.id)
                self.assertEqual(2, len(self.store.list_events()))
                self.assertEqual(event.id, saved.id)
                self.assertEqual(original_created, saved.created_at)
                self.assertEqual((changes["title"], event.start_date, "11:30", changes["place"], changes["notes"]),
                                 (saved.title, saved.start_date, saved.time, saved.place, saved.notes))
                self.assertEqual(("학교 공지", "가져온 원문", "open"),
                                 (saved.source, saved.source_text, saved.status))
                self.assertTrue(desktop.expanded)
                for value in (saved.title, saved.start_date, saved.time, saved.place, saved.notes):
                    self.assertIn(value, desktop.details_label.text())
                if mode == "sticky":
                    self.assertIs(desktop, self.window.notes[event.id])
                    self.assertEqual(saved.title, desktop.title_label.full_title)
                else:
                    self.assertIs(desktop, self.window.desktop_list)
                    self.assertEqual(event.id, desktop.selected_event_id)
                    self.assertEqual(saved.title, desktop.list_widget.currentItem().text())
                self.assertEqual(event.id, self.window.agenda.currentItem().data(Qt.UserRole))
                self.assertEqual("일정을 수정했습니다.", self.window.status_label.text())
                self.assertEqual([], self.errors)

        reopened = Store(self.store.path)
        try:
            self.assertEqual(saved, reopened.get_event(event.id))
            self.assertEqual(2, len(reopened.list_events()))
        finally:
            reopened.close()

    def test_edit_event_notes_only_refreshes_the_existing_reminder(self):
        event = self.store.upsert_event(Event(title="메모만 수정", start_date="2026-10-14", notes="원래 메모"))
        self.window.calendar.setSelectedDate(QDate(2026, 10, 14))
        self.window.refresh()
        note = self.window.notes[event.id]
        note.toggle_details()
        self.edit_schedule(event.id, {"notes": "새 메모"})
        saved = self.store.get_event(event.id)
        self.assertEqual(1, len(self.store.list_events()))
        self.assertEqual((event.id, event.title, event.start_date, "새 메모"),
                         (saved.id, saved.title, saved.start_date, saved.notes))
        self.assertIs(note, self.window.notes[event.id])
        self.assertIn("새 메모", note.details_label.text())
        self.assertNotIn("원래 메모", note.details_label.text())
        self.assertEqual([], self.errors)

    def test_manager_omits_source_metadata_but_keeps_places_status_and_saved_provenance(self):
        today = date.today().isoformat()
        events = [self.store.upsert_event(Event(
            title=f"출처 숨김 {status}", start_date=today, status=status,
            place="과학실" if status == "open" else "", notes="일정 세부 안내",
            source="내부 자료 이름.pdf", source_text="가져온 일정 원문"))
            for status in ("open", "done", "review")]
        self.window.calendar.setSelectedDate(QDate.fromString(today, "yyyy-MM-dd"))
        self.window.refresh()
        self.assertEqual(3, self.window.agenda.count())
        agenda = "\n".join(self.window.agenda.item(row).text() for row in range(self.window.agenda.count()))
        self.assertIn("과학실", agenda)
        self.assertIn("미완료", agenda)
        self.assertIn("완료", agenda)
        self.assertIn("확인 필요", agenda)
        self.assertNotIn("내부 자료 이름.pdf", agenda)
        for table in (self.window.review_table, self.window.history_table):
            self.assertEqual(3, table.columnCount())
            self.assertEqual(["제목", "날짜", "시간"],
                             [table.horizontalHeaderItem(column).text() for column in range(table.columnCount())])
            self.assertEqual(1, table.rowCount())
            self.assertFalse(any("내부 자료 이름.pdf" in table.item(0, column).text()
                                 for column in range(table.columnCount())))
        for original in events:
            saved = self.store.get_event(original.id)
            self.assertEqual((original.source, original.source_text), (saved.source, saved.source_text))
        self.assertEqual([], self.errors)

    def test_edit_event_moves_calendar_and_selects_the_saved_schedule(self):
        event = self.store.upsert_event(Event(title="날짜를 옮길 일정", start_date="2026-10-14", time="10:00"))
        self.store.upsert_event(Event(title="옮길 날짜의 다른 일정", start_date="2026-10-15", time="09:00"))
        self.window.calendar.setSelectedDate(QDate(2026, 10, 14))
        self.window.refresh()
        self.edit_schedule(event.id, {"date": "2026-10-15"})
        self.assertEqual("2026-10-15", self.store.get_event(event.id).start_date)
        self.assertEqual(QDate(2026, 10, 15), self.window.calendar.selectedDate())
        self.assertEqual(event.id, self.window.agenda.currentItem().data(Qt.UserRole))
        self.assertTrue(self.window.edit_selected_button.isEnabled())
        self.assertEqual(2, self.window.agenda.count())
        self.assertIn("2026-10-15", self.window.notes[event.id].details_label.text())
        self.assertEqual(2, len(self.store.list_events()))
        self.assertEqual([], self.errors)

    def test_edit_event_keeps_selected_calendar_day_inside_range_or_month(self):
        for precision, start, end, edited_date, edited_end in (
                ("day", "2026-10-14", "2026-10-20", "2026-10-12", "2026-10-21"),
                ("month", "2026-10-01", "2026-10-31", "2026-10", "")):
            with self.subTest(precision=precision):
                event = self.store.upsert_event(Event(
                    title=f"기간 안에서 수정 {precision}", start_date=start, end_date=end,
                    date_precision=precision))
                self.window.calendar.setSelectedDate(QDate(2026, 10, 16))
                self.window.refresh()
                self.edit_schedule(event.id, {"date": edited_date, "end": edited_end, "notes": "기간 메모 수정"})
                self.assertEqual(QDate(2026, 10, 16), self.window.calendar.selectedDate())
                self.assertEqual(event.id, self.window.agenda.currentItem().data(Qt.UserRole))
                self.assertEqual(precision, self.store.get_event(event.id).date_precision)
                self.assertEqual("기간 메모 수정", self.store.get_event(event.id).notes)
                self.assertEqual([], self.errors)

    def test_edit_event_preserves_done_status_and_cancellation_leaves_data_unchanged(self):
        event = self.store.upsert_event(Event(title="완료한 일정 수정", start_date="2026-10-14"))
        self.store.complete_event(event.id)
        completed_at = self.store.get_event(event.id).completed_at
        self.window.calendar.setSelectedDate(QDate(2026, 10, 14))
        self.window.refresh()
        self.edit_schedule(event.id, {"title": "수정한 완료 일정", "notes": "완료 후 메모"})
        saved = self.store.get_event(event.id)
        self.assertEqual(("done", completed_at), (saved.status, saved.completed_at))
        self.assertEqual("수정한 완료 일정", self.window.history_table.item(0, 0).text())
        self.assertNotIn(event.id, self.window.notes)
        self.assertEqual(event.id, self.window.agenda.currentItem().data(Qt.UserRole))
        status = self.window.status_label.text()
        self.edit_schedule(event.id, {"title": "취소한 제목", "date": "2026-11-01", "notes": "취소한 메모"}, cancel=True)
        self.assertEqual(saved, self.store.get_event(event.id))
        self.assertEqual(QDate(2026, 10, 14), self.window.calendar.selectedDate())
        self.assertEqual(status, self.window.status_label.text())
        self.assertEqual(1, len(self.store.list_events()))
        self.assertEqual([], self.errors)

    def test_history_edit_reorders_rows_and_completion_cancel_keeps_edited_identity(self):
        edited = self.store.upsert_event(Event(title="완료 기록에서 날짜 수정", start_date="2026-10-14"))
        other = self.store.upsert_event(Event(title="그대로 둘 완료 일정", start_date="2026-10-15"))
        for event in (edited, other):
            self.store.complete_event(event.id)
        other_before = self.store.get_event(other.id)
        self.window.refresh()
        table = self.window.history_table
        table.setCurrentCell(0, 0)
        self.assertEqual(edited.id, table.item(table.currentRow(), 0).data(Qt.UserRole))

        self.edit_schedule(edited.id, {"date": "2026-10-16", "notes": "날짜를 옮긴 완료 일정"})
        self.assertEqual(1, table.currentRow())
        self.assertEqual(edited.id, table.item(table.currentRow(), 0).data(Qt.UserRole))
        self.window.table_action(table, "done")
        self.pump()

        saved = self.store.get_event(edited.id)
        self.assertEqual(("2026-10-16", "open", None),
                         (saved.start_date, saved.status, saved.completed_at))
        self.assertEqual(other_before, self.store.get_event(other.id))
        self.assertEqual(1, table.rowCount())
        self.assertEqual(other.id, table.item(0, 0).data(Qt.UserRole))
        self.assertEqual(-1, table.currentRow())
        self.assertEqual([], table.selectedItems())
        self.assertEqual([], self.errors)

    def test_confirmed_review_edit_clears_selection_instead_of_selecting_next_schedule(self):
        edited = self.store.upsert_event(Event(
            title="확정할 일정", start_date="2026-10-14", status="review"))
        other = self.store.upsert_event(Event(
            title="확인을 기다릴 다른 일정", start_date="2026-10-15", status="review"))
        other_before = self.store.get_event(other.id)
        self.window.refresh()
        table = self.window.review_table
        table.setCurrentCell(0, 0)
        self.assertEqual(edited.id, table.item(table.currentRow(), 0).data(Qt.UserRole))

        self.edit_schedule(edited.id, {"notes": "확정한 세부 내용"})

        self.assertEqual("open", self.store.get_event(edited.id).status)
        self.assertEqual(1, table.rowCount())
        self.assertEqual(other.id, table.item(0, 0).data(Qt.UserRole))
        self.assertEqual(-1, table.currentRow())
        self.assertEqual([], table.selectedItems())
        with patch.object(self.window, "edit_event") as open_editor:
            self.window.table_action(table, "review")
        open_editor.assert_not_called()
        self.assertEqual(other_before, self.store.get_event(other.id))
        self.assertEqual([], self.errors)

    def test_review_selection_survives_refresh_when_an_earlier_schedule_is_added(self):
        selected = self.store.upsert_event(Event(
            title="선택한 확인 필요 일정", start_date="2026-10-15", status="review"))
        self.window.refresh()
        table = self.window.review_table
        table.setCurrentCell(0, 0)
        earlier = self.store.upsert_event(Event(
            title="먼저 표시될 확인 필요 일정", start_date="2026-10-14", status="review"))

        self.window.refresh()

        self.assertEqual(2, table.rowCount())
        self.assertEqual(earlier.id, table.item(0, 0).data(Qt.UserRole))
        self.assertEqual(1, table.currentRow())
        self.assertEqual(selected.id, table.item(table.currentRow(), 0).data(Qt.UserRole))
        self.assertTrue(table.item(1, 0).isSelected())
        self.assertEqual([], self.errors)

    @contextmanager
    def freeze_today(self, reference):
        """Advance one calendar day without replacing real reminder selection."""
        class FixedDate(date):
            @classmethod
            def today(cls):
                return cls(reference.year, reference.month, reference.day)

        with ExitStack() as patches:
            for module in ("alimi.core", "alimi.app", "alimi.urgency"):
                patches.enter_context(patch(f"{module}.date", FixedDate))
            yield

    def configure_reminder_range(self, past_days=30, future_days=14, unlimited=False):
        self.window.reminder_past_days.setValue(past_days)
        self.window.reminder_future_days.setValue(future_days)
        self.window.reminder_unlimited.setChecked(unlimited)
        self.pump()

    def test_default_unlimited_shows_old_future_and_monthly_open_events_in_both_modes(self):
        events = [self.store.upsert_event(Event(title=title, start_date=when))
                  for title, when in (("오래 지난 미완료 일정", "2000-01-01"),
                                      ("먼 미래 미완료 일정", "2099-12-31"))]
        month = self.store.upsert_event(Event(
            title="먼 미래 월 일정", start_date="2099-12-01", end_date="2099-12-31",
            date_precision="month"))
        done = self.store.upsert_event(Event(title="이미 완료", start_date="2000-01-01"))
        self.store.complete_event(done.id)
        completed_at = self.store.get_event(done.id).completed_at
        review = self.store.upsert_event(Event(title="날짜 미확정", status="review"))
        self.window.refresh()
        self.pump()
        expected = {month.id, *(event.id for event in events)}
        self.assertTrue(self.window.reminder_unlimited.isChecked())
        self.assertFalse(self.window.reminder_past_days.isEnabled())
        self.assertFalse(self.window.reminder_future_days.isEnabled())
        self.assertEqual(expected, set(self.window.notes))
        self.assertEqual("month", self.window.notes[month.id].urgency_state.code)
        desktop = self.switch_to_list()
        self.assertEqual(expected, set(desktop.events))
        self.window.mode_combo.setCurrentIndex(0)
        self.pump()
        self.assertEqual(expected, set(self.window.notes))
        self.assertEqual(("done", completed_at),
                         (self.store.get_event(done.id).status,
                          self.store.get_event(done.id).completed_at))
        self.assertEqual("review", self.store.get_event(review.id).status)

    def test_settings_range_inclusive_boundaries_and_deadline_selection_in_both_modes(self):
        today = date(2026, 10, 14)
        events = {}
        for offset in (-4, -3, 0, 5, 6):
            events[offset] = self.store.upsert_event(Event(
                title=f"기준일 {offset:+}일 일정",
                start_date=(today + timedelta(days=offset)).isoformat()))
        range_with_later_end = self.store.upsert_event(Event(
            title="시작은 지난 기간 일정", start_date="2026-10-01", end_date="2026-10-19"))
        range_outside = self.store.upsert_event(Event(
            title="마감일이 범위 밖인 기간 일정", start_date="2026-10-14", end_date="2026-10-20"))
        with self.freeze_today(today):
            self.configure_reminder_range(past_days=3, future_days=5)
            expected = {events[-3].id, events[0].id, events[5].id, range_with_later_end.id}
            self.assertEqual(expected, set(self.window.notes))
            self.assertTrue(self.window.reminder_past_days.isEnabled())
            self.assertTrue(self.window.reminder_future_days.isEnabled())
            desktop = self.switch_to_list()
            self.assertEqual(expected, set(desktop.events))
            self.window.reminder_future_days.setValue(6)
            self.pump()
            expected.update((events[6].id, range_outside.id))
            self.assertEqual(expected, set(desktop.events))
            self.window.reminder_past_days.setValue(4)
            self.pump()
            expected.add(events[-4].id)
            self.assertEqual(expected, set(desktop.events))
            self.window.mode_combo.setCurrentIndex(0)
            self.pump()
            self.assertEqual(expected, set(self.window.notes))
        self.assertEqual(7, len(self.store.list_events()))
        self.assertTrue(all(event.status == "open" for event in self.store.list_events()))

    def test_today_only_month_overlap_and_unlimited_toggle_preserve_records_and_capture_view(self):
        today = date(2026, 10, 14)
        current_month = self.store.upsert_event(Event(
            title="이달 월 일정", start_date="2026-10-01", end_date="2026-10-31",
            date_precision="month"))
        previous_month = self.store.upsert_event(Event(
            title="지난달 월 일정", start_date="2026-09-01", end_date="2026-09-30",
            date_precision="month"))
        day_events = [self.store.upsert_event(Event(
            title=f"주변 날짜 {offset:+}일", start_date=(today + timedelta(days=offset)).isoformat()))
            for offset in (-1, 0, 1)]
        capture = self.window.capture_notepad
        capture.editor.setPlainText("설정 중에도 보존할 초안")
        capture_position = capture.pos()
        with self.freeze_today(today):
            self.configure_reminder_range(past_days=0, future_days=0)
            self.assertEqual({current_month.id, day_events[1].id}, set(self.window.notes))
            self.assertEqual("month", self.window.notes[current_month.id].urgency_state.code)
            self.window.reminder_unlimited.setChecked(True)
            self.pump()
            self.assertEqual({current_month.id, previous_month.id,
                              *(event.id for event in day_events)}, set(self.window.notes))
            self.assertFalse(self.window.reminder_past_days.isEnabled())
            self.assertFalse(self.window.reminder_future_days.isEnabled())
            self.assertEqual(0, self.window.reminder_past_days.value())
            self.assertEqual(0, self.window.reminder_future_days.value())
            self.window.reminder_unlimited.setChecked(False)
            self.pump()
            self.assertEqual({current_month.id, day_events[1].id}, set(self.window.notes))
        self.assertTrue(capture.collapsed)
        self.assertEqual(capture_position, capture.pos())
        self.assertEqual("설정 중에도 보존할 초안", capture.editor.toPlainText())
        self.assertEqual(5, len(self.store.list_events()))
        self.assertTrue(all(event.status == "open" and not event.completed_at
                            for event in self.store.list_events()))

    def test_reminder_range_and_disabled_values_persist_when_application_restarts(self):
        self.configure_reminder_range(past_days=7, future_days=23)
        self.assertEqual("7", self.store.get_setting("reminder_past_days"))
        self.assertEqual("23", self.store.get_setting("reminder_future_days"))
        self.assertEqual("false", self.store.get_setting("reminder_unlimited").lower())
        self.destroy_window(self.window)
        self.window = self.make_window()
        self.pump()
        self.assertFalse(self.window.reminder_unlimited.isChecked())
        self.assertEqual(7, self.window.reminder_past_days.value())
        self.assertEqual(23, self.window.reminder_future_days.value())
        self.assertTrue(self.window.reminder_past_days.isEnabled())
        self.assertTrue(self.window.reminder_future_days.isEnabled())
        self.window.reminder_unlimited.setChecked(True)
        self.pump()
        self.destroy_window(self.window)
        self.window = self.make_window()
        self.pump()
        self.assertTrue(self.window.reminder_unlimited.isChecked())
        self.assertEqual(7, self.window.reminder_past_days.value())
        self.assertEqual(23, self.window.reminder_future_days.value())
        self.assertFalse(self.window.reminder_past_days.isEnabled())
        self.assertFalse(self.window.reminder_future_days.isEnabled())

    def test_backup_restore_reloads_range_controls_and_recomputes_desktop_selection(self):
        today = date(2026, 10, 14)
        today_event = self.store.upsert_event(Event(title="복원할 오늘 일정", start_date="2026-10-14"))
        future = self.store.upsert_event(Event(title="복원할 미래 일정", start_date="2026-10-22"))
        backup_path = self.data_dir / "range-backup.sqlite3"
        with self.freeze_today(today):
            self.configure_reminder_range(past_days=2, future_days=0)
            self.store.backup(backup_path)
            self.window.reminder_unlimited.setChecked(True)
            self.window.reminder_past_days.setValue(10)
            self.window.reminder_future_days.setValue(10)
            self.pump()
            self.assertEqual({today_event.id, future.id}, set(self.window.notes))
            with patch("alimi.app.QFileDialog.getOpenFileName", return_value=(str(backup_path), "")), \
                 patch("alimi.app.QMessageBox.question", return_value=QMessageBox.Yes):
                self.window.restore_data()
            self.pump()
            self.assertFalse(self.window.reminder_unlimited.isChecked())
            self.assertEqual(2, self.window.reminder_past_days.value())
            self.assertEqual(0, self.window.reminder_future_days.value())
            self.assertTrue(self.window.reminder_past_days.isEnabled())
            self.assertTrue(self.window.reminder_future_days.isEnabled())
            self.assertEqual({today_event.id}, set(self.window.notes))
        self.assertEqual("open", self.store.get_event(future.id).status)
        self.assertEqual([], self.errors)

    def test_midnight_recomputes_limited_range_and_keeps_filtered_events_open(self):
        first_day = date(2026, 10, 14)
        yesterday = self.store.upsert_event(Event(title="어제 일정", start_date="2026-10-13"))
        today_event = self.store.upsert_event(Event(title="오늘 일정", start_date="2026-10-14"))
        tomorrow = self.store.upsert_event(Event(title="내일 일정", start_date="2026-10-15"))
        with self.freeze_today(first_day):
            self.configure_reminder_range(past_days=0, future_days=0)
            self.assertEqual({today_event.id}, set(self.window.notes))
        with self.freeze_today(first_day + timedelta(days=1)):
            self.window.sync_notes()
            self.pump()
            self.assertEqual({tomorrow.id}, set(self.window.notes))
            self.assertEqual("due", self.window.notes[tomorrow.id].urgency_state.code)
        self.assertTrue(all(self.store.get_event(event.id).status == "open"
                            for event in (yesterday, today_event, tomorrow)))

    def test_limited_three_day_reminders_and_overlapping_months_match_in_both_modes(self):
        today = date(2026, 10, 29)
        events = {}
        for days in (3, 4):
            events[days] = self.store.upsert_event(Event(
                title=f"{days}일 뒤 교직원 협의회",
                start_date=(today + timedelta(days=days)).isoformat()))
        current_month = self.store.upsert_event(Event(
            title="이번 달 과학 평가", start_date="2026-10-01",
            end_date="2026-10-31", date_precision="month"))
        next_month = self.store.upsert_event(Event(
            title="다음 달 평가", start_date="2026-11-01",
            end_date="2026-11-30", date_precision="month"))
        distant_month = self.store.upsert_event(Event(
            title="범위 밖 월 평가", start_date="2026-12-01",
            end_date="2026-12-31", date_precision="month"))
        done = self.store.upsert_event(Event(
            title="완료한 임박 일정", start_date="2026-11-01", status="done"))
        unresolved = self.store.upsert_event(Event(
            title="검토 중인 임박 일정", start_date="2026-11-01", status="review"))
        with self.freeze_today(today):
            self.configure_reminder_range(future_days=3)
            self.window.refresh()
            self.pump()
            expected = {events[3].id, current_month.id, next_month.id}
            self.assertEqual(expected, set(self.window.notes))
            self.assertEqual("three_days", self.window.notes[events[3].id].urgency_state.code)
            self.assertEqual("month", self.window.notes[current_month.id].urgency_state.code)
            desktop = self.switch_to_list()
            self.assertEqual(expected, set(desktop.events))
            self.window.mode_combo.setCurrentIndex(0)
            self.pump()
            self.assertEqual(expected, set(self.window.notes))
        self.assertEqual("open", self.store.get_event(events[4].id).status)
        self.assertEqual("open", self.store.get_event(next_month.id).status)
        self.assertEqual("open", self.store.get_event(distant_month.id).status)
        self.assertEqual("done", self.store.get_event(done.id).status)
        self.assertEqual("review", self.store.get_event(unresolved.id).status)

    def test_daily_color_refresh_preserves_expansion_alignment_and_completion_records(self):
        first_day = date(2026, 10, 14)
        deadline = first_day + timedelta(days=3)
        event = self.store.upsert_event(Event(
            title="제출 마감 임박 일정", start_date=deadline.isoformat(),
            notes="기한까지 평가 자료를 제출합니다."))
        month = self.store.upsert_event(Event(
            title="월만 정해진 평가", start_date="2026-10-01",
            end_date="2026-10-31", date_precision="month"))
        area = QRect(100, 50, 1920, 1080)
        primary_patch, screens_patch = self.screen_patch(area)
        with primary_patch, screens_patch, self.freeze_today(first_day):
            self.window.refresh()
            self.pump()
            self.select_alignment("right")
            note = self.window.notes[event.id]
            note.title_label.click()
            self.pump()
            original_date = note.date_label.text()
            normal_month_color = self.window.notes[month.id].urgency_state.background
        colors = []
        for elapsed, code in enumerate(("three_days", "two_days", "one_day", "due", "due")):
            with self.subTest(elapsed=elapsed), primary_patch, screens_patch, \
                 self.freeze_today(first_day + timedelta(days=elapsed)):
                self.window.sync_notes()
                self.pump()
                self.assertIs(note, self.window.notes[event.id])
                self.assertEqual(code, note.urgency_state.code)
                self.assertEqual(code, note.property("urgency"))
                self.assertIn(note.urgency_state.background, note.styleSheet())
                self.assertTrue(note.expanded)
                self.assertEqual(original_date, note.date_label.text())
                self.assertIn(event.notes, note.details_label.text())
                self.assert_at_edge(note, area, "right")
                self.assertFalse(note.windowHandle().flags() & Qt.WindowStaysOnBottomHint)
                self.assertTrue(note.windowHandle().flags() & Qt.WindowStaysOnTopHint)
                self.assertEqual(normal_month_color,
                                 self.window.notes[month.id].urgency_state.background)
                colors.append(note.urgency_state.background)
        self.assertEqual(4, len(set(colors[:4])), "Each day before the deadline needs a distinct color.")
        self.assertEqual(colors[3], colors[4], "Overdue reminders retain the deadline color until completion.")
        with self.freeze_today(deadline + timedelta(days=1)):
            note.complete_button.click()
            self.pump()
            self.assertNotIn(event.id, self.window.notes)
            self.assertEqual("done", self.store.get_event(event.id).status)
            self.assertTrue(self.store.get_event(event.id).completed_at)
            self.window.sync_notes()
            self.pump()
            self.assertNotIn(event.id, self.window.notes)

    def test_list_colors_refresh_without_losing_selected_details_or_scroll(self):
        first_day = date(2026, 10, 14)
        upcoming = self.store.upsert_event(Event(
            title="곧 마감할 자료 제출", start_date="2026-10-16",
            notes="선택한 일정의 세부 내용 보존 검증"))
        for index in range(30):
            self.store.upsert_event(Event(
                title=f"미완료 지난 일정 {index:02}", start_date="2026-10-10"))
        with self.freeze_today(first_day):
            self.window.refresh()
            self.pump()
            desktop = self.switch_to_list()
            desktop.set_selected_event(upcoming.id)
            self.pump()
            scrollbar = desktop.list_widget.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())
            self.pump()
            original_scroll = scrollbar.value()
            self.assertGreater(original_scroll, 0)
            row = next(desktop.list_widget.item(index)
                       for index in range(desktop.list_widget.count())
                       if desktop.list_widget.item(index).data(Qt.UserRole) == upcoming.id)
            self.assertEqual(event_urgency(upcoming, first_day).background,
                             row.background().color().name())
            self.assertTrue(row.data(Qt.AccessibleDescriptionRole))
        with self.freeze_today(first_day + timedelta(days=1)):
            self.window.sync_notes()
            self.pump()
            self.assertIs(desktop, self.window.desktop_list)
            self.assertEqual(upcoming.id, desktop.selected_event_id)
            self.assertTrue(desktop.expanded)
            self.assertEqual(original_scroll, scrollbar.value())
            self.assertIn(upcoming.notes, desktop.details_label.text())
            row = desktop.list_widget.currentItem()
            self.assertEqual(upcoming.id, row.data(Qt.UserRole))
            self.assertEqual(event_urgency(upcoming, first_day + timedelta(days=1)).background,
                             row.background().color().name())
            desktop.complete_button.click()
            self.pump()
            self.assertNotIn(upcoming.id, desktop.events)
            self.assertEqual("done", self.store.get_event(upcoming.id).status)

    def test_new_three_day_reminders_in_free_layout_do_not_cover_existing_notes(self):
        today = date(2026, 10, 14)
        area = QRect(0, 0, 1920, 1080)
        primary_patch, screens_patch = self.screen_patch(area)
        with primary_patch, screens_patch, self.freeze_today(today):
            self.configure_reminder_range(future_days=3)
            existing = self.store.upsert_event(Event(
                title="위치를 정해 놓은 기존 메모", start_date=today.isoformat()))
            self.window.refresh()
            self.pump()
            original_note = self.window.notes[existing.id]
            original_note.moved.emit(existing.id, original_note.x(), original_note.y())
            self.pump()
            original_geometry = QRect(original_note.geometry())
            self.assertEqual("free", self.store.get_setting("desktop_layout"))
            upcoming = [self.store.upsert_event(Event(
                title=f"새로 나타날 임박 일정 {index + 1}",
                start_date=(today + timedelta(days=4)).isoformat())) for index in range(2)]
            self.window.sync_notes()
            self.pump()
            self.assertEqual({existing.id}, set(self.window.notes))
        with primary_patch, screens_patch, self.freeze_today(today + timedelta(days=1)):
            self.window.sync_notes()
            self.pump()
            self.assertEqual({existing.id, *(event.id for event in upcoming)}, set(self.window.notes))
            self.assertEqual(original_geometry, original_note.geometry(),
                             "New reminders must preserve the user's existing manual positions.")
            self.assert_no_overlapping_notes(area)
            first_positions = {}
            for event in upcoming:
                note = self.window.notes[event.id]
                saved = self.store.get_event(event.id)
                self.assertEqual((note.x(), note.y()), (saved.x, saved.y),
                                 "Initial placement must be saved before the next periodic refresh.")
                first_positions[event.id] = QRect(note.geometry())
            self.window.sync_notes()
            self.pump()
            self.assertEqual(original_geometry, original_note.geometry())
            self.assert_no_overlapping_notes(area)
            self.assertEqual(first_positions,
                             {event.id: QRect(self.window.notes[event.id].geometry()) for event in upcoming})

    def add_due(self, count=1):
        today = date.today().isoformat()
        result = [self.store.upsert_event(Event(
            title=f"교과 평가 일정 {index + 1}", start_date=today,
            notes="세부 내용 전체: " + ("평가 방법과 준비물 설명. " * 40),
            source="통합 검증")) for index in range(count)]
        self.window.refresh()
        self.pump()
        return result

    def add_monthly(self, status="open"):
        month = (date.today().replace(day=1) - timedelta(days=1)).replace(day=1)
        end = date(month.year, month.month, monthrange(month.year, month.month)[1])
        event = self.store.upsert_event(Event(
            title=f"과학 생명 평가 {status}", start_date=month.isoformat(),
            end_date=end.isoformat(), date_precision="month", status=status,
            notes="평가내용: 먹고 먹히는 관계를 조사해서 먹이그물로 나타내기",
            source="교수·학습 및 평가 계획"))
        self.window.refresh()
        self.pump()
        return event

    def switch_to_list(self):
        self.window.mode_combo.setCurrentIndex(1)
        self.pump()
        self.assertEqual("list", self.window.desktop_mode)
        self.assertIsNotNone(self.window.desktop_list)
        return self.window.desktop_list

    def click_list_row(self, desktop, row):
        item = desktop.list_widget.item(row)
        desktop.list_widget.scrollToItem(item)
        self.pump()
        point = desktop.list_widget.visualItemRect(item).center()
        QTest.mouseClick(desktop.list_widget.viewport(), Qt.LeftButton, pos=point)
        self.pump()
        return item.data(Qt.UserRole)

    def screen_patch(self, area):
        """Match a real translated work area, including a reserved taskbar."""
        screen = SimpleNamespace(availableGeometry=lambda: QRect(area),
                                 geometry=lambda: QRect(area.adjusted(-5, -5, 5, 40)),
                                 name=lambda: "alignment-verification-screen")
        return (patch("alimi.app.QApplication.primaryScreen", return_value=screen),
                patch("alimi.app.QApplication.screens", return_value=[screen]))

    def select_alignment(self, alignment, settings=False):
        combo = (self.window.settings_alignment_combo if settings else
                 self.window.alignment_combo)
        index = combo.findData(alignment)
        self.assertGreaterEqual(index, 0, f"Missing alignment choice: {alignment}")
        combo.setCurrentIndex(index)
        self.pump()
        self.assertEqual(alignment, self.store.get_setting("desktop_layout"))
        self.assertEqual(alignment, self.window.alignment_combo.currentData())
        self.assertEqual(alignment, self.window.settings_alignment_combo.currentData())

    def assert_at_edge(self, widget, area, alignment):
        rectangle = widget.geometry()
        self.assertTrue(area.contains(rectangle), (alignment, rectangle, area))
        distance = {"bottom": area.bottom() - rectangle.bottom(),
                    "top": rectangle.top() - area.top(),
                    "left": rectangle.left() - area.left(),
                    "right": area.right() - rectangle.right()}[alignment]
        self.assertGreaterEqual(distance, 0)
        self.assertLessEqual(distance, 15, (alignment, rectangle, area))

    def assert_no_overlapping_notes(self, area):
        rectangles = [QRect(note.geometry()) for note in self.window.notes.values()]
        self.assertTrue(all(area.contains(rectangle) for rectangle in rectangles), rectangles)
        for index, rectangle in enumerate(rectangles):
            self.assertFalse(any(rectangle.intersects(other)
                                 for other in rectangles[index + 1:]), rectangles)

    def test_four_alignments_pack_many_notes_at_the_chosen_desktop_edge(self):
        area = QRect(100, 50, 1920, 1080)
        primary_patch, screens_patch = self.screen_patch(area)
        with primary_patch, screens_patch:
            events = self.add_due(17)
            for alignment in ("top", "right", "left", "bottom"):
                with self.subTest(alignment=alignment):
                    self.select_alignment(alignment)
                    self.window.arrange_desktop()
                    self.pump()
                    first, second = [self.window.notes[event.id] for event in events[:2]]
                    self.assert_at_edge(first, area, alignment)
                    self.assert_no_overlapping_notes(area)
                    if alignment in ("top", "bottom"):
                        self.assertEqual(first.y(), second.y())
                    else:
                        self.assertEqual(first.x(), second.x())

    def test_initial_default_bottom_alignment_needs_no_apply_or_saved_setting(self):
        area = QRect(100, 50, 1920, 1080)
        primary_patch, screens_patch = self.screen_patch(area)
        with primary_patch, screens_patch:
            self.assertIsNone(self.store.get_setting("desktop_layout"))
            events = self.add_due(8)
            self.assertEqual("bottom", self.window.alignment_combo.currentData())
            self.assert_at_edge(self.window.notes[events[0].id], area, "bottom")
            self.assert_no_overlapping_notes(area)
            self.switch_to_list()
            self.window.mode_combo.setCurrentIndex(0)
            self.pump()
            self.assert_at_edge(self.window.notes[events[0].id], area, "bottom")
            self.assert_no_overlapping_notes(area)

    def test_expanding_details_keeps_the_chosen_edge_and_other_notes_separate(self):
        area = QRect(100, 50, 1920, 1080)
        primary_patch, screens_patch = self.screen_patch(area)
        with primary_patch, screens_patch:
            events = self.add_due(17)
            first = self.window.notes[events[0].id]
            for alignment in ("top", "right", "left", "bottom"):
                with self.subTest(alignment=alignment):
                    first.set_expanded(False)
                    self.select_alignment(alignment)
                    first.title_label.click()
                    self.pump()
                    self.assertTrue(first.expanded)
                    self.assertEqual(alignment, self.store.get_setting("desktop_layout"))
                    self.assert_at_edge(first, area, alignment)
                    self.assert_no_overlapping_notes(area)
                    self.window.sync_notes()
                    self.pump()
                    self.assertTrue(first.expanded)
                    self.assert_at_edge(first, area, alignment)
                    self.assert_no_overlapping_notes(area)

    def test_settings_alignment_is_synchronized_and_survives_recreation(self):
        self.add_due(3)
        for alignment in ("top", "right", "left", "bottom"):
            with self.subTest(alignment=alignment):
                self.select_alignment(alignment, settings=True)
                self.destroy_window(self.window)
                self.window = self.make_window()
                self.pump()
                self.assertEqual(alignment, self.store.get_setting("desktop_layout"))
                self.assertEqual(alignment, self.window.alignment_combo.currentData())
                self.assertEqual(alignment, self.window.settings_alignment_combo.currentData())
                self.assertEqual(3, len(self.window.notes))

    def test_drag_preserves_free_position_and_selection_restores_automatic_alignment(self):
        area = QRect(100, 50, 1920, 1080)
        primary_patch, screens_patch = self.screen_patch(area)
        with primary_patch, screens_patch:
            event = self.add_due()[0]
            self.select_alignment("top")
            note = self.window.notes[event.id]
            wanted = (area.left() + 300, area.top() + 300)
            note.move(*wanted)
            note.moved.emit(event.id, *wanted)
            self.pump()
            self.assertEqual("free", self.window.alignment_combo.currentData())
            self.assertEqual("free", self.window.settings_alignment_combo.currentData())
            self.window.sync_notes()
            self.pump()
            self.assertEqual(wanted, (note.x(), note.y()))
            self.select_alignment("right")
            self.assert_at_edge(note, area, "right")
            desktop = self.switch_to_list()
            desktop.move(*wanted)
            desktop.moved.emit(*wanted)
            self.pump()
            self.assertEqual("free", self.window.alignment_combo.currentData())
            self.assertEqual("free", self.window.settings_alignment_combo.currentData())
            self.window.sync_notes()
            self.pump()
            self.assertEqual(wanted, (desktop.x(), desktop.y()))
            self.select_alignment("left", settings=True)
            self.assert_at_edge(desktop, area, "left")

    def test_all_alignments_anchor_the_list_and_keep_expanded_details_visible(self):
        area = QRect(100, 50, 1920, 1080)
        primary_patch, screens_patch = self.screen_patch(area)
        with primary_patch, screens_patch:
            self.add_due(17)
            desktop = self.switch_to_list()
            for alignment in ("top", "right", "left", "bottom"):
                with self.subTest(alignment=alignment):
                    self.select_alignment(alignment)
                    self.assert_at_edge(desktop, area, alignment)
                    selected = self.click_list_row(desktop, 0)
                    self.assertTrue(desktop.expanded)
                    self.assert_at_edge(desktop, area, alignment)
                    self.window.sync_notes()
                    self.pump()
                    self.assertEqual(selected, desktop.selected_event_id)
                    self.assert_at_edge(desktop, area, alignment)

    def test_alignment_and_mode_changes_do_not_move_input_notepad_or_change_records(self):
        self.add_due(8)
        done = self.store.upsert_event(Event(title="완료 상태를 유지할 일정",
                                            start_date=date.today().isoformat()))
        self.store.complete_event(done.id)
        before = [(event.id, event.title, event.start_date, event.end_date,
                   event.status, event.completed_at, event.notes, event.date_precision)
                  for event in self.store.list_events()]
        capture = self.window.capture_notepad
        capture.move(capture.x() + 23, capture.y() + 31)
        capture.moved.emit(capture.x(), capture.y())
        for collapsed in (True, False):
            capture.set_collapsed(collapsed)
            self.pump()
            original = QRect(capture.geometry())
            for alignment in ("top", "right", "left", "bottom"):
                with self.subTest(collapsed=collapsed, alignment=alignment):
                    self.select_alignment(alignment)
                    self.assertEqual(original, capture.geometry())
                    self.assertEqual(collapsed, capture.collapsed)
                    self.switch_to_list()
                    self.assertEqual(original, capture.geometry())
                    self.assertEqual(collapsed, capture.collapsed)
                    self.window.mode_combo.setCurrentIndex(0)
                    self.pump()
                    self.assertEqual(original, capture.geometry())
                    self.assertEqual(collapsed, capture.collapsed)
        after = [(event.id, event.title, event.start_date, event.end_date,
                  event.status, event.completed_at, event.notes, event.date_precision)
                 for event in self.store.list_events()]
        self.assertEqual(before, after)
        self.assertEqual("done", self.store.get_event(done.id).status)

    def test_restart_keeps_draft_and_icon_anchor_but_always_starts_collapsed(self):
        area = QRect(100, 50, 1920, 1080)
        primary_patch, screens_patch = self.screen_patch(area)
        with primary_patch, screens_patch:
            capture = self.window.capture_notepad
            self.assertTrue(capture.collapsed)
            capture.move(area.left() + 200, area.top() + 120)
            capture.moved.emit(capture.x(), capture.y())
            anchor = capture.pos()
            QTest.mouseDClick(capture.icon_button, Qt.LeftButton)
            self.pump()
            self.assertFalse(capture.collapsed)
            text = "2099년 10월 15일 재시작해도 남는 초안"
            capture.editor.setPlainText(text)
            # Shutdown must flush even when the 300 ms draft debounce has
            # not elapsed and the large editor is still open.
            self.destroy_window(self.window)
            self.window = self.make_window()
            self.pump()
            reopened = self.window.capture_notepad
            self.assertTrue(reopened.isVisible())
            self.assertTrue(reopened.collapsed)
            self.assertTrue(reopened.icon_button.isVisible())
            self.assertFalse(reopened.editor.isVisible())
            self.assertEqual(anchor, reopened.pos())
            self.assertEqual(text, reopened.editor.toPlainText())

            self.assertEqual(text, self.store.get_setting("capture_draft"))
            self.assertEqual([], self.store.list_events())
            QTest.mouseDClick(reopened.icon_button, Qt.LeftButton)
            self.pump()
            self.assertTrue(reopened.editor.isVisible())
            self.assertEqual(text, reopened.editor.toPlainText())

    def test_tray_quit_preserves_all_schedule_states_and_recent_draft_on_database_restart(self):
        events = [self.store.upsert_event(Event(
            title=title, start_date=when, notes="종료와 재실행 후에도 보존", source="종료 검증"))
            for title, when in (("지난 미완료 일정", "2000-01-01"),
                                ("미래 미완료 일정", "2099-12-31"))]
        month = self.store.upsert_event(Event(
            title="미완료 월 일정", start_date="2099-12-01", end_date="2099-12-31",
            date_precision="month", source="월 일정 원문", source_text="2099년 12월 평가"))
        review = self.store.upsert_event(Event(title="날짜 확인 필요", status="review"))
        done = self.store.upsert_event(Event(title="이미 완료한 일정", start_date="2000-01-01"))
        self.store.complete_event(done.id)
        expected_ids = {month.id, *(event.id for event in events)}
        expected = [replace(event, x=None, y=None) for event in self.store.list_events()]

        for mode_index, mode in ((0, "sticky"), (1, "list")):
            with self.subTest(mode=mode):
                self.window.mode_combo.setCurrentIndex(mode_index)
                self.window.refresh()
                self.pump()
                capture = self.window.capture_notepad
                draft = f"2099년 10월 15일 {mode} 모드 종료 직전 작성한 초안"
                capture.editor.setPlainText(draft)
                self.assertTrue(self.window.draft_timer.isActive())
                self.assertNotEqual(draft, self.store.get_setting("capture_draft", ""))
                anchor = self.window.capture_icon_position
                action = next(action for action in self.window.tray.contextMenu().actions()
                              if action.text() == "알리미 종료")
                with patch.object(self.app, "quit") as quit_app, \
                     patch("alimi.app.QMessageBox.information") as information, \
                     patch("alimi.app.QMessageBox.warning") as warning:
                    action.trigger()
                quit_app.assert_called_once_with()
                information.assert_not_called()
                warning.assert_not_called()
                self.assertTrue(self.app.property("shutting_down"))
                self.assertEqual(draft, self.store.get_setting("capture_draft"))
                self.assertEqual(str(anchor.x()), self.store.get_setting("capture_x"))
                self.assertEqual(str(anchor.y()), self.store.get_setting("capture_y"))

                # Reopen the actual SQLite file through a fresh connection;
                # recreating widgets on the old Store alone would miss lost commits.
                self.destroy_window(self.window)
                self.store.close()
                self.store = Store(self.data_dir / "alimi.sqlite3")
                self.window = self.make_window()
                self.pump()
                self.assertEqual(expected, [replace(event, x=None, y=None)
                                            for event in self.store.list_events()])
                self.assertEqual(mode, self.window.desktop_mode)
                self.assertEqual(draft, self.window.capture_notepad.editor.toPlainText())
                self.assertEqual(anchor, self.window.capture_notepad.pos())
                if mode == "sticky":
                    self.assertEqual(expected_ids, set(self.window.notes))
                    self.assertTrue(all(note.isVisible() for note in self.window.notes.values()))
                else:
                    self.assertEqual(expected_ids, set(self.window.desktop_list.events))
                    self.assertTrue(self.window.desktop_list.isVisible())
                self.assertEqual(review.id, self.window.review_table.item(0, 0).data(Qt.UserRole))
                self.assertEqual(done.id, self.window.history_table.item(0, 0).data(Qt.UserRole))
                self.assertEqual([], self.errors)

    def test_tray_quit_aborts_if_recent_draft_cannot_be_saved(self):
        event = self.add_due(1)[0]
        draft = "2099년 10월 15일 저장에 실패한 초안"
        self.window.capture_notepad.editor.setPlainText(draft)
        with patch.object(self.window, "save_capture_draft", side_effect=OSError("저장 검증 실패")), \
             patch.object(self.app, "quit") as quit_app:
            self.window.request_quit()
        quit_app.assert_not_called()
        self.assertFalse(self.app.property("shutting_down"))
        self.assertTrue(self.window.capture_notepad.isVisible())
        self.assertEqual(draft, self.window.capture_notepad.editor.toPlainText())
        self.assertEqual("open", self.store.get_event(event.id).status)
        self.assertEqual(1, len(self.errors))
        self.assertIn("종료하지 않았습니다", self.errors[0])

    def test_tray_quit_aborts_if_icon_position_cannot_be_saved(self):
        draft = "2099년 10월 15일 종료 실패 후에도 남는 초안"
        self.window.capture_notepad.editor.setPlainText(draft)
        with patch.object(self.window, "save_capture_position", side_effect=OSError("위치 저장 검증 실패")), \
             patch.object(self.app, "quit") as quit_app:
            self.window.request_quit()
        quit_app.assert_not_called()
        self.assertFalse(self.app.property("shutting_down"))
        self.assertTrue(self.window.capture_notepad.isVisible())
        self.assertEqual(draft, self.store.get_setting("capture_draft"))
        self.assertEqual(1, len(self.errors))
        self.assertIn("종료하지 않았습니다", self.errors[0])

    def test_tray_quit_still_waits_for_active_document_analysis(self):
        task = object()
        self.window.jobs.add(task)
        try:
            with patch.object(self.app, "quit") as quit_app, \
                 patch("alimi.app.QMessageBox.information") as information:
                self.window.request_quit()
            quit_app.assert_not_called()
            self.assertFalse(self.app.property("shutting_down"))
            information.assert_called_once()
            self.assertEqual("자료 분석 중", information.call_args.args[1])
        finally:
            self.window.jobs.discard(task)

    def test_expansion_near_bottom_right_clamps_editor_and_restores_saved_icon_anchor(self):
        area = QRect(100, 50, 1920, 1080)
        primary_patch, screens_patch = self.screen_patch(area)
        with primary_patch, screens_patch:
            capture = self.window.capture_notepad
            anchor_x = area.right() - capture.width() + 1
            anchor_y = area.bottom() - capture.height() + 1
            capture.move(anchor_x, anchor_y)
            capture.moved.emit(anchor_x, anchor_y)
            self.pump()
            self.assertTrue(area.contains(capture.geometry()))
            for _ in range(3):
                QTest.mouseDClick(capture.icon_button, Qt.LeftButton)
                self.pump()
                self.assertFalse(capture.collapsed)
                self.assertTrue(area.contains(capture.geometry()),
                                "An expanded editor at a desktop edge must fit the working area.")
                self.assertLess(capture.x(), anchor_x)
                self.assertLess(capture.y(), anchor_y)
                self.assertEqual(str(anchor_x), self.store.get_setting("capture_x"))
                self.assertEqual(str(anchor_y), self.store.get_setting("capture_y"))
                capture.minimize_button.click()
                self.pump()
                self.assertTrue(capture.collapsed)
                self.assertEqual((anchor_x, anchor_y), (capture.x(), capture.y()),
                                 "Temporary expansion clamping must not move the desktop icon.")
                self.assertTrue(area.contains(capture.geometry()))
            capture.expand_notepad()
            self.pump()
            self.destroy_window(self.window)
            self.window = self.make_window()
            self.pump()
            reopened = self.window.capture_notepad
            self.assertTrue(reopened.collapsed)
            self.assertEqual((anchor_x, anchor_y), (reopened.x(), reopened.y()))
            self.assertTrue(area.contains(reopened.geometry()))

    def test_periodic_sync_and_manager_hide_never_expand_the_desktop_icon(self):
        capture = self.window.capture_notepad
        self.assertTrue(capture.collapsed)
        initial = QRect(capture.geometry())
        self.window.show()
        with patch("alimi.app.QMessageBox.warning", return_value=QMessageBox.Ok):
            self.window.close()
        self.pump()
        self.assertFalse(self.window.isVisible())
        for _ in range(3):
            self.window.timer.timeout.emit()
            self.pump()
            self.assertTrue(capture.isVisible())
            self.assertTrue(capture.collapsed)
            self.assertEqual(initial, capture.geometry())
            self.assertFalse(capture.editor.isVisible())
        with patch("alimi.desktop.QMessageBox.warning", return_value=QMessageBox.Cancel):
            capture.close()
        self.pump()
        self.assertTrue(capture.isVisible(), "Canceling the warning keeps the desktop icon visible.")
        self.assertTrue(capture.collapsed)

    def test_manager_close_warning_can_cancel_or_hide_without_changing_schedules(self):
        events = self.add_due(2)
        before = self.store.list_events()
        self.window.show()
        with patch("alimi.app.QMessageBox.warning", return_value=QMessageBox.Cancel) as warning:
            self.window.close()
        self.pump()
        self.assertTrue(self.window.isVisible())
        self.assertEqual(QMessageBox.Ok | QMessageBox.Cancel, warning.call_args.args[-2])
        self.assertEqual(QMessageBox.Cancel, warning.call_args.args[-1])
        with patch("alimi.app.QMessageBox.warning", return_value=QMessageBox.Ok):
            self.window.close()
        self.pump()
        self.assertFalse(self.window.isVisible())
        self.assertTrue(self.window.capture_notepad.isVisible())
        self.assertTrue(all(self.window.notes[event.id].isVisible() for event in events))
        self.assertEqual(before, self.store.list_events())
        self.window.show_manager()
        self.pump()
        self.assertTrue(self.window.isVisible())

    def test_notepad_close_flushes_draft_stays_hidden_and_can_explicitly_reopen(self):
        capture = self.window.capture_notepad
        self.window.show_capture_notepad(expand=True)
        capture.editor.setPlainText("2099년 10월 15일 닫아도 남아야 하는 초안")
        draft = capture.editor.toPlainText()
        with patch("alimi.desktop.QMessageBox.warning", return_value=QMessageBox.Cancel):
            capture.close_button.click()
        self.assertTrue(capture.isVisible())
        self.assertFalse(capture.dismissed)
        with patch("alimi.desktop.QMessageBox.warning", return_value=QMessageBox.Ok):
            capture.close_button.click()
        self.assertFalse(capture.isVisible())
        self.assertTrue(capture.dismissed)
        self.assertEqual(draft, self.store.get_setting("capture_draft"),
                         "Confirmed close flushes the draft before the debounce timer fires.")
        self.add_due(2)
        for index in (1, 0, 1):
            self.window.mode_combo.setCurrentIndex(index)
            self.window.timer.timeout.emit()
            self.pump()
            self.assertFalse(capture.isVisible(), "Reminder refresh must respect a confirmed close.")
        self.window.show_capture_notepad(expand=True)
        self.pump()
        self.assertTrue(capture.isVisible())
        self.assertFalse(capture.dismissed)
        self.assertFalse(capture.collapsed)
        self.assertEqual(draft, capture.editor.toPlainText())
        self.assertEqual(2, len(self.store.list_events()))

    def test_closed_sticky_remains_unfinished_and_explicit_restore_reopens_it(self):
        events = self.add_due(2)
        closed, other = events
        note = self.window.notes[closed.id]
        # Repacking the remaining cards may persist a new display position.
        # Every schedule field apart from those coordinates must be preserved.
        before = [replace(event, x=None, y=None) for event in self.store.list_events()]
        with patch("alimi.desktop.QMessageBox.warning", return_value=QMessageBox.Cancel):
            note.close_button.click()
        self.assertTrue(note.isVisible())
        self.assertFalse(note.dismissed)
        with patch("alimi.desktop.QMessageBox.warning", return_value=QMessageBox.Ok):
            note.close_button.click()
        selected = self.window.agenda.currentItem()
        self.assertEqual(closed.id, selected.data(Qt.UserRole))
        self.assertIn("바탕화면 도구 닫힘", selected.text())
        self.assertIn("바탕화면 알림 다시 열기", self.window.note_reason.text())
        self.assertNotIn("표시 기간 밖", self.window.note_reason.text())
        self.pump()
        self.assertTrue(sip.isdeleted(note) or not note.isVisible())
        self.window.timer.timeout.emit()
        self.pump()
        closed_note = self.window.notes.get(closed.id)
        self.assertTrue(closed_note is None or not closed_note.isVisible())
        self.assertTrue(self.window.notes[other.id].isVisible())
        desktop = self.switch_to_list()
        self.assertEqual({closed.id, other.id}, set(desktop.events),
                         "Closing a postit keeps its schedule available in the list.")
        self.assertIn("알림 표시 중", self.window.agenda.currentItem().text())
        self.window.mode_combo.setCurrentIndex(0)
        self.pump()
        closed_note = self.window.notes.get(closed.id)
        self.assertTrue(closed_note is None or not closed_note.isVisible())
        self.assertEqual(before, [replace(event, x=None, y=None) for event in self.store.list_events()])
        self.window.restore_desktop_tools()
        self.pump()
        self.assertEqual({closed.id, other.id}, set(self.window.notes))
        self.assertTrue(all(note.isVisible() for note in self.window.notes.values()))
        self.assertIn("알림 표시 중", self.window.agenda.currentItem().text())
        self.assertNotIn("바탕화면 알림 다시 열기", self.window.note_reason.text())
        self.assertEqual(before, [replace(event, x=None, y=None) for event in self.store.list_events()])

    def test_closed_list_stays_hidden_across_refresh_and_mode_switch_then_restores(self):
        self.add_due(2)
        desktop = self.switch_to_list()
        before = self.store.list_events()
        with patch("alimi.desktop.QMessageBox.warning", return_value=QMessageBox.Cancel):
            desktop.close_button.click()
        self.assertTrue(desktop.isVisible())
        with patch("alimi.desktop.QMessageBox.warning", return_value=QMessageBox.Ok):
            desktop.close_button.click()
        self.assertTrue(all("바탕화면 도구 닫힘" in self.window.agenda.item(row).text()
                            for row in range(self.window.agenda.count())))
        self.assertIn("바탕화면 알림 다시 열기", self.window.note_reason.text())
        self.pump()
        self.assertTrue(sip.isdeleted(desktop) or not desktop.isVisible())
        self.window.timer.timeout.emit()
        self.pump()
        self.assertTrue(self.window.desktop_list is None or not self.window.desktop_list.isVisible())
        self.window.mode_combo.setCurrentIndex(0)
        self.pump()
        self.assertEqual(2, len(self.window.notes))
        self.window.mode_combo.setCurrentIndex(1)
        self.pump()
        self.assertTrue(self.window.desktop_list is None or not self.window.desktop_list.isVisible())
        self.assertEqual(before, self.store.list_events())
        self.window.restore_desktop_tools()
        self.pump()
        self.assertTrue(self.window.desktop_list.isVisible())
        self.assertEqual({event.id for event in before}, set(self.window.desktop_list.events))
        self.assertTrue(all("알림 표시 중" in self.window.agenda.item(row).text()
                            for row in range(self.window.agenda.count())))
        self.assertEqual(before, self.store.list_events())

    def test_test_postit_button_reopens_only_closed_test_after_switching_modes(self):
        real = self.add_due()[0]
        self.window.test_postit_button.click()
        self.pump()
        test_event = next(event for event in self.store.list_events() if event.source == TEST_NOTE_SOURCE)
        with patch("alimi.desktop.QMessageBox.warning", return_value=QMessageBox.Ok):
            self.window.notes[real.id].close_button.click()
            self.window.notes[test_event.id].close_button.click()
        self.window.mode_combo.setCurrentIndex(1)
        self.window.mode_combo.setCurrentIndex(0)
        self.pump()
        self.assertNotIn(test_event.id, self.window.notes)
        self.window.test_postit_button.click()
        self.pump()
        self.assertTrue(self.window.notes[test_event.id].isVisible())
        self.assertFalse(self.window.notes[test_event.id].dismissed)
        self.assertNotIn(test_event.id, self.window._dismissed_note_ids)
        closed_real = self.window.notes.get(real.id)
        self.assertTrue(closed_real is None or not closed_real.isVisible())
        self.assertIn(real.id, self.window._dismissed_note_ids)
        self.assertEqual(2, len(self.store.list_events()))
        self.assertTrue(all(event.status == "open" for event in self.store.list_events()))

    def test_test_postit_button_reopens_closed_list_and_completed_test_without_duplicates(self):
        real = self.add_due()[0]
        desktop = self.switch_to_list()
        self.window.test_postit_button.click()
        self.pump()
        test_event = next(event for event in self.store.list_events() if event.source == TEST_NOTE_SOURCE)
        self.window.complete_event(test_event.id)
        with patch("alimi.desktop.QMessageBox.warning", return_value=QMessageBox.Ok):
            desktop.close_button.click()
        self.assertFalse(desktop.isVisible())
        self.window.test_postit_button.click()
        self.pump()
        self.assertIs(desktop, self.window.desktop_list)
        self.assertTrue(desktop.isVisible())
        self.assertFalse(desktop.dismissed)
        self.assertFalse(self.window._desktop_list_dismissed)
        self.assertEqual({real.id, test_event.id}, set(desktop.events))
        self.assertEqual("open", self.store.get_event(test_event.id).status)
        self.assertEqual(2, len(self.store.list_events()))

    def test_notepad_icon_and_editor_keep_top_layer_during_modes_and_refresh(self):
        self.add_due(2)
        capture = self.window.capture_notepad
        for collapsed in (True, False):
            capture.set_collapsed(collapsed)
            for index in (1, 0):
                self.window.mode_combo.setCurrentIndex(index)
                self.window.timer.timeout.emit()
                self.pump()
                self.assertTrue(capture.isVisible())
                self.assertEqual(collapsed, capture.collapsed)
                self.assertTrue(capture.windowHandle().flags() & Qt.WindowStaysOnTopHint)
                self.assertFalse(capture.windowHandle().flags() & Qt.WindowStaysOnBottomHint)

    def test_apply_alignment_button_restores_the_last_selected_edge_after_a_drag(self):
        area = QRect(100, 50, 1920, 1080)
        primary_patch, screens_patch = self.screen_patch(area)
        with primary_patch, screens_patch:
            event = self.add_due()[0]
            self.select_alignment("right")
            note = self.window.notes[event.id]
            note.move(area.left() + 300, area.top() + 300)
            note.moved.emit(event.id, note.x(), note.y())
            self.pump()
            self.assertEqual("free", self.window.alignment_combo.currentData())
            self.assertEqual("right", self.store.get_setting("desktop_alignment"))
            self.window.align_bottom_button.click()
            self.pump()
            self.assertEqual("right", self.store.get_setting("desktop_layout"))
            self.assertEqual("right", self.window.alignment_combo.currentData())
            self.assertEqual("right", self.window.settings_alignment_combo.currentData())
            self.assert_at_edge(note, area, "right")
            self.assertEqual("open", self.store.get_event(event.id).status)

    def test_compact_sticky_is_independent_bottom_window_and_completion_is_saved(self):
        event = self.add_due()[0]
        note = self.window.notes[event.id]
        self.assertEqual("sticky", self.window.desktop_mode)
        self.assertIsNone(note.parent())
        self.assertTrue(note.windowFlags() & Qt.WindowStaysOnBottomHint)
        self.assertFalse(note.windowFlags() & Qt.WindowStaysOnTopHint)
        self.assertFalse(note.expanded)
        self.assertFalse(note.details_scroll.isVisible())
        self.assertFalse(note.complete_button.isVisible())
        self.assertIn(event.title, note.title_label.toolTip())
        self.window.show()
        with patch("alimi.app.QMessageBox.warning", return_value=QMessageBox.Ok):
            self.window.close()
        self.pump()
        self.assertFalse(self.window.isVisible())
        self.assertTrue(note.isVisible())
        with patch("alimi.desktop.QMessageBox.warning", return_value=QMessageBox.Cancel):
            note.close()
        self.pump()
        self.assertTrue(note.isVisible(), "Canceling the warning leaves the reminder visible.")
        QTest.mouseClick(note.title_label, Qt.LeftButton)
        self.pump()
        self.assertTrue(note.expanded)
        self.assertTrue(note.complete_button.isVisible())
        self.assertIn(event.notes, note.details_label.text())
        self.window.sync_notes()
        self.pump()
        self.assertTrue(note.expanded, "A periodic refresh must preserve expanded details.")
        note.complete_button.click()
        self.pump()
        self.assertEqual("done", self.store.get_event(event.id).status)
        self.assertNotIn(event.id, self.window.notes)

    def test_bottom_alignment_fits_many_notes_and_expanded_details_without_overlaps(self):
        # Use a representative desktop working area; offscreen Qt defaults to
        # an 800x600 surface even on a physical full-HD Windows desktop.
        area = QRect(0, 0, 1920, 1080)
        screen = SimpleNamespace(availableGeometry=lambda: QRect(area),
                                 geometry=lambda: QRect(area), name=lambda: "verification-screen")
        with patch("alimi.app.QApplication.primaryScreen", return_value=screen), \
             patch("alimi.app.QApplication.screens", return_value=[screen]):
            events = self.add_due(17)
            self.window.align_bottom_button.click()
            self.pump()
            self.assertEqual("bottom", self.store.get_setting("desktop_layout"))
            QTest.mouseClick(self.window.notes[events[0].id].title_label, Qt.LeftButton)
            self.pump()
            self.window.arrange_desktop_bottom()
            self.pump()
            rectangles = [QRect(note.geometry()) for note in self.window.notes.values()]
            self.assertEqual(17, len(rectangles))
            self.assertTrue(all(area.contains(rectangle) for rectangle in rectangles))
            for index, rectangle in enumerate(rectangles):
                self.assertFalse(any(rectangle.intersects(other) for other in rectangles[index + 1:]))
            self.assertGreaterEqual(max(rectangle.bottom() for rectangle in rectangles), area.bottom() - 40)
            self.assertTrue(self.window.notes[events[0].id].expanded)

    def test_manual_move_switches_to_free_layout_and_button_restores_bottom(self):
        event = self.add_due()[0]
        note = self.window.notes[event.id]
        note.move(note.x() + 20, note.y() - 20)
        note.moved.emit(event.id, note.x(), note.y())
        self.pump()
        self.assertEqual("free", self.store.get_setting("desktop_layout"))
        saved = self.store.get_event(event.id)
        self.assertEqual((note.x(), note.y()), (saved.x, saved.y))
        self.window.align_bottom_button.click()
        self.pump()
        self.assertEqual("bottom", self.store.get_setting("desktop_layout"))

    def test_list_click_shows_month_only_details_and_completion_updates_list(self):
        monthly = self.add_monthly()
        self.add_due(16)
        desktop = self.switch_to_list()
        self.assertEqual({}, self.window.notes)
        self.assertIsNone(desktop.parent())
        self.assertTrue(desktop.isVisible())
        self.assertTrue(desktop.windowFlags() & Qt.WindowStaysOnBottomHint)
        self.assertFalse(desktop.windowFlags() & Qt.WindowStaysOnTopHint)
        self.assertEqual(17, desktop.list_widget.count())
        self.assertFalse(desktop.details_panel.isVisible())
        self.assertEqual(Qt.ScrollBarAlwaysOff, desktop.list_widget.horizontalScrollBarPolicy())
        self.assertGreater(desktop.list_widget.verticalScrollBar().maximum(), 0)
        for row in range(desktop.list_widget.count()):
            item = desktop.list_widget.item(row)
            self.assertEqual(self.store.get_event(item.data(Qt.UserRole)).title, item.text())
            self.assertNotIn("\n", item.text())
        monthly_row = next(row for row in range(desktop.list_widget.count())
                           if desktop.list_widget.item(row).data(Qt.UserRole) == monthly.id)
        self.click_list_row(desktop, monthly_row)
        self.assertEqual(monthly.id, desktop.selected_event_id)
        self.assertTrue(desktop.details_panel.isVisible())
        when = date.fromisoformat(monthly.start_date)
        self.assertIn(f"{when.year}년 {when.month}월", desktop.details_label.text())
        self.assertNotIn(monthly.start_date, desktop.details_label.text())
        self.assertIn(monthly.notes, desktop.details_label.text())
        self.window.sync_notes()
        self.pump()
        self.assertEqual(monthly.id, desktop.selected_event_id)
        self.assertTrue(desktop.expanded)
        self.window.show()
        with patch("alimi.app.QMessageBox.warning", return_value=QMessageBox.Ok):
            self.window.close()
        self.pump()
        self.assertTrue(desktop.isVisible())
        with patch("alimi.desktop.QMessageBox.warning", return_value=QMessageBox.Cancel):
            desktop.close()
        self.pump()
        self.assertTrue(desktop.isVisible())
        desktop.complete_button.click()
        self.pump()
        self.assertEqual("done", self.store.get_event(monthly.id).status)
        self.assertEqual(16, desktop.list_widget.count())
        self.assertNotIn(monthly.id, desktop.events)

    def test_completion_save_failure_keeps_list_reminder(self):
        event = self.add_due()[0]
        desktop = self.switch_to_list()
        self.click_list_row(desktop, 0)
        with patch.object(self.store, "complete_event", side_effect=OSError("검증용 저장 실패")):
            desktop.complete_button.click()
            self.pump()
        self.assertEqual("open", self.store.get_event(event.id).status)
        self.assertEqual(1, desktop.list_widget.count())
        self.assertTrue(desktop.isVisible())
        self.assertTrue(self.errors)

    def test_mode_changes_preserve_events_and_persist_on_recreation(self):
        events = self.add_due(3)
        old_notes = list(self.window.notes.values())
        desktop = self.switch_to_list()
        self.assertTrue(all(sip.isdeleted(note) or not note.isVisible() for note in old_notes))
        self.assertEqual("list", self.store.get_setting("desktop_mode"))
        self.window.mode_combo.setCurrentIndex(0)
        self.pump()
        self.assertTrue(sip.isdeleted(desktop) or not desktop.isVisible())
        self.assertIsNone(self.window.desktop_list)
        self.assertEqual({event.id for event in events}, set(self.window.notes))
        self.assertEqual(3, len(self.store.list_events()))
        self.assertTrue(all(event.status == "open" for event in self.store.list_events()))
        self.switch_to_list()
        self.destroy_window(self.window)
        self.window = self.make_window()
        self.pump()
        self.assertEqual("list", self.window.desktop_mode)
        self.assertEqual(1, self.window.mode_combo.currentIndex())
        self.assertEqual({}, self.window.notes)
        self.assertEqual(3, self.window.desktop_list.list_widget.count())
        self.assertEqual(3, len(self.store.list_events()))

    def test_list_scroll_position_and_selection_survive_refresh(self):
        self.add_due(40)
        desktop = self.switch_to_list()
        selected = self.click_list_row(desktop, 25)
        scrollbar = desktop.list_widget.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
        self.pump()
        before = scrollbar.value()
        self.assertGreater(before, 0)
        self.window.sync_notes()
        self.pump()
        self.assertEqual(selected, desktop.selected_event_id)
        self.assertEqual(before, scrollbar.value())
        self.assertTrue(desktop.details_panel.isVisible())

    def test_month_dates_in_history_and_review_are_not_artificial_first_days(self):
        completed = self.add_monthly("done")
        pending = self.add_monthly("review")
        for table, event in ((self.window.history_table, completed),
                             (self.window.review_table, pending)):
            row = next(row for row in range(table.rowCount())
                       if table.item(row, 0).data(Qt.UserRole) == event.id)
            label = table.item(row, 1).text()
            self.assertIn(f"{date.fromisoformat(event.start_date).month}월", label)
            self.assertNotIn(event.start_date, label)


class MonthEventDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def test_month_dialog_preserves_month_precision_and_hidden_internal_boundaries(self):
        event = Event(title="월별 과학 평가", start_date="2026-09-01",
                      end_date="2026-09-30", date_precision="month")
        dialog = EventDialog(event)
        try:
            self.assertEqual("2026-09", dialog.date_field.text())
            self.assertEqual("", dialog.end_field.text())
            dialog.date_field.setText("2028-02")
            dialog.accept()
            self.assertEqual(QDialog.Accepted, dialog.result())
            self.assertEqual("month", dialog.event.date_precision)
            self.assertEqual("2028-02-01", dialog.event.start_date)
            self.assertEqual("2028-02-29", dialog.event.end_date)
        finally:
            dialog.deleteLater()
            self.app.sendPostedEvents(None, QEvent.DeferredDelete)

    def test_explicit_first_day_is_kept_as_real_day_instead_of_month(self):
        dialog = EventDialog()
        try:
            dialog.title_field.setText("실제 1일 일정")
            dialog.date_field.setText("2026-09-01")
            dialog.accept()
            self.assertEqual(QDialog.Accepted, dialog.result())
            self.assertEqual("day", dialog.event.date_precision)
            self.assertEqual("2026-09-01", dialog.event.start_date)
            self.assertFalse(dialog.event.end_date)
        finally:
            dialog.deleteLater()
            self.app.sendPostedEvents(None, QEvent.DeferredDelete)


if __name__ == "__main__":
    unittest.main()
