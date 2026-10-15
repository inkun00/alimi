"""Import recognition never writes schedules before the user's final review."""

import os
import tempfile
import threading
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

# These tests must not display application windows or register autostart.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Match the application entry point's Windows DLL initialization order.
import onnxruntime

onnxruntime.disable_telemetry_events()

from PyQt5.QtCore import QDate, QEvent, QMimeData, Qt, QUrl
from PyQt5.QtGui import QImage
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QDialog, QMessageBox

from alimi.app import MainWindow, make_icon
from alimi.core import Event, Store, parse_schedule_text


class FileImportUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.data_dir = Path(self.directory.name)
        self.store = Store(self.data_dir / "alimi.sqlite3")
        args = SimpleNamespace(data_dir=self.data_dir, no_autostart=True,
                               smoke_test=True, background=False)
        self.app.setProperty("shutting_down", False)
        self.window = MainWindow(self.store, args, make_icon())
        self.errors = []
        self.window.show_error = self.errors.append

    def tearDown(self):
        self.dispose_window()
        self.store.close()
        self.directory.cleanup()
        self.app.setProperty("shutting_down", False)

    def dispose_window(self):
        """Dispose the current UI without removing the local data being tested."""
        self.window.pool.waitForDone(10_000)
        self.app.processEvents()
        self.window.timer.stop()
        self.window.midnight_timer.stop()
        # Flush the editable draft before its database and signal handlers vanish.
        self.window.on_shutdown()
        self.window.capture_notepad.deleteLater()
        for note in list(self.window.notes.values()):
            note.finish()
        self.window.notes.clear()
        if self.window.desktop_list is not None:
            self.window.desktop_list.finish()
            self.window.desktop_list = None
        self.window.tray.hide()
        for signal, callback in ((self.app.aboutToQuit, self.window.on_shutdown),
                                 (self.app.commitDataRequest, self.window.on_session_end)):
            try:
                signal.disconnect(callback)
            except (TypeError, RuntimeError):
                pass
        try:
            self.app.applicationStateChanged.disconnect()
        except (TypeError, RuntimeError):
            pass
        self.app.setProperty("shutting_down", True)
        self.window.close()
        self.window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()

    def wait_until(self, condition, message, timeout=10):
        deadline = time.monotonic() + timeout
        while not condition() and time.monotonic() < deadline:
            QTest.qWait(10)
        self.app.processEvents()
        self.assertTrue(condition(), message)

    def wait_for_import(self, allow_errors=False):
        self.wait_until(lambda: not self.window.jobs, "The document worker did not finish.")
        if not allow_errors:
            self.assertEqual([], self.errors)

    def preview(self):
        self.wait_until(lambda: self.window.import_dialog is not None,
                        "Recognized input did not open an editable review.")
        dialog = self.window.import_dialog
        self.assertTrue(dialog.isVisible())
        return dialog

    def register(self):
        self.preview().register_button.click()
        self.app.processEvents()
        QTest.qWait(1)

    def document(self, name, text):
        path = self.data_dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def open_notepad(self):
        notepad = self.window.capture_notepad
        if notepad.collapsed:
            QTest.mouseDClick(notepad.icon_button, Qt.LeftButton)
            self.app.processEvents()
        self.assertFalse(notepad.collapsed)
        self.assertTrue(notepad.editor.isVisible())
        return notepad

    def test_original_deadline_sentence_survives_confirmation_registration_and_modes(self):
        original = "10월 30일까지 수행평가 제출하세요"
        notepad = self.open_notepad()
        notepad.editor.setPlainText(original)
        notepad.review_button.click()
        dialog = self.preview()
        self.assertEqual(original, dialog.table.item(0, dialog.TITLE).text())
        self.assertEqual([], self.store.list_events())
        self.register()
        event = self.store.list_events()[0]
        self.assertEqual(original, event.title)
        self.assertEqual(f"{date.today().year}-10-30", event.start_date)
        self.assertIn(original, event.source_text)
        self.assertEqual(original, self.window.notes[event.id].title_label.full_title)
        self.window.mode_combo.setCurrentIndex(1)
        self.app.processEvents()
        self.assertEqual(original, self.window.desktop_list.list_widget.item(0).text())
        notepad.review_button.click()
        self.register()
        self.assertEqual(1, len(self.store.list_events()))
        self.assertEqual(original, self.store.get_event(event.id).title)
        self.assertEqual([], self.errors)

    def test_time_range_notice_is_one_confirmed_schedule_through_notepad_and_modes(self):
        original = "-부스 사전설치:2026.10.22.(목),15:00-19:00"
        notepad = self.open_notepad()
        notepad.editor.setPlainText(original)
        notepad.review_button.click()
        dialog = self.preview()
        self.assertEqual(1, dialog.table.rowCount())
        self.assertEqual(original, dialog.table.item(0, dialog.TITLE).text())
        self.assertEqual("2026-10-22", dialog.table.item(0, dialog.DATE).text())
        self.assertEqual("15:00", dialog.table.item(0, dialog.TIME).text())
        self.assertEqual([], self.store.list_events())
        self.register()
        event, = self.store.list_events()
        self.assertEqual((original, "2026-10-22", "15:00", "open"),
                         (event.title, event.start_date, event.time, event.status))
        self.assertEqual(original, event.source_text)
        self.assertEqual("일정 메모장", event.source)
        self.assertIn("15:00-19:00", self.window.notes[event.id].details_label.text())
        self.assertNotIn("출처:", self.window.notes[event.id].details_label.text())
        self.window.mode_combo.setCurrentIndex(1)
        self.app.processEvents()
        listing = self.window.desktop_list
        self.assertEqual(1, listing.list_widget.count())
        self.assertEqual(original, listing.list_widget.item(0).text())
        notepad.review_button.click()
        self.assertEqual(1, self.preview().table.rowCount())
        self.register()
        self.assertEqual([event.id], [saved.id for saved in self.store.list_events()])
        self.assertEqual([], self.errors)

    def test_calendar_and_record_tables_hide_source_but_keep_place_and_saved_origin(self):
        origin = "내부 출처 검사 전용.pdf"
        records = [self.store.upsert_event(Event(
            title=f"출처 숨김 {status}", start_date=date.today().isoformat(),
            source=origin, source_text="보존할 일정 원문", status=status,
            place="회의실" if status == "open" else ""))
            for status in ("open", "review", "done")]
        self.window.calendar.setSelectedDate(QDate.currentDate())
        self.window.refresh()
        visible_agenda = "\n".join(self.window.agenda.item(row).text()
                                   for row in range(self.window.agenda.count()))
        self.assertNotIn(origin, visible_agenda)
        self.assertIn("회의실", visible_agenda)
        for table in (self.window.review_table, self.window.history_table):
            headers = [table.horizontalHeaderItem(column).text() for column in range(table.columnCount())]
            self.assertEqual(["제목", "날짜", "시간"], headers)
            self.assertFalse(any(origin in table.item(row, column).text()
                                 for row in range(table.rowCount()) for column in range(table.columnCount())))
        self.assertTrue(all(self.store.get_event(event.id).source == origin for event in records))
        self.assertTrue(all(self.store.get_event(event.id).source_text == "보존할 일정 원문" for event in records))
        self.window.queue_import_review([Event(title="새 일정", start_date=date.today().isoformat(), source=origin)],
                                        origin, "원문")
        self.assertNotIn(origin, self.window.status_label.text())
        self.register()
        self.assertNotIn(origin, self.window.status_label.text())

    def test_file_button_previews_then_saves_edited_monthly_events(self):
        original = "2099학년도 평가 계획\n9월 국어 독서 평가\n10월 수학 연산 평가"
        document = self.document("monthly-plan.txt", original)
        with patch("alimi.app.QFileDialog.getOpenFileNames", return_value=([str(document)], "")) as chooser:
            self.window.analyze_button.click()
            chooser.assert_called_once()
            self.wait_for_import()
        self.assertEqual(original, self.window.text_input.toPlainText())
        self.assertEqual([], self.store.list_events(), "File recognition must not register schedules.")
        dialog = self.preview()
        self.assertEqual(2, dialog.table.rowCount())
        self.assertEqual("2099-09", dialog.table.item(0, 2).text())
        dialog.table.item(0, 1).setText("최종 수정한 국어 평가")
        dialog.register_button.click()
        self.app.processEvents()
        events = self.store.list_events()
        self.assertEqual(["2099-09-01", "2099-10-01"], [event.start_date for event in events])
        self.assertEqual("최종 수정한 국어 평가", events[0].title)
        self.assertTrue(all(event.source == document.name and event.status == "open" for event in events))
        self.assertTrue(all(event.date_precision == "month" for event in events))
        self.assertEqual("2099-09-30", events[0].end_date)
        self.assertIn("새 일정 2건", self.window.import_result.toPlainText())
        self.assertEqual([], self.errors)

    def test_calendar_image_import_joins_month_headers_to_days_before_registration(self):
        text = (
            "주요 학사일정\n2021. 3월\n일 월 화 수 목 금 토\n1 2 3 4 5 6\n"
            "1(월) 삼일절\n2(화)~5(금) 학교 적응 주간\n수업일수: 22일\n"
            "4월\n12(월)~16(금) 과학축제주간\n"
            "19(월)~23(금) 정보통신윤리교육주간\n19(월)~23(금) 인성실천 주간\n"
            "7월\n7.21~8.18. 여름방학(29일)\n8월\n15(일) 광복절"
        )
        image_path = self.data_dir / "calendar.png"
        image = QImage(80, 80, QImage.Format_RGB32)
        image.fill(Qt.white)
        self.assertTrue(image.save(str(image_path)))
        with patch.object(self.window.ocr, "recognize", return_value=text) as recognize:
            self.window.import_file_paths([str(image_path)])
            self.wait_for_import()
        recognize.assert_called_once()
        dialog = self.preview()
        self.assertEqual(7, dialog.table.rowCount())
        self.assertEqual([], self.store.list_events())
        self.assertEqual(text, self.window.text_input.toPlainText())
        dates = {(dialog.table.item(row, dialog.DATE).text(), dialog.table.item(row, dialog.END_DATE).text())
                 for row in range(dialog.table.rowCount())}
        self.assertIn(("2021-07-21", "2021-08-18"), dates)
        self.assertTrue(all(dialog.table.item(row, dialog.DATE).text().startswith("2021-")
                            for row in range(dialog.table.rowCount())))
        self.register()
        events = self.store.list_events()
        self.assertEqual(7, len(events))
        self.assertEqual(2, sum(event.start_date == "2021-04-19" and event.end_date == "2021-04-23"
                                for event in events))
        self.assertFalse(any("수업일수" in event.title for event in events))
        self.assertTrue(all(event.date_precision == "day" for event in events))
        self.window.import_text()
        self.assertEqual(7, self.preview().table.rowCount())
        self.register()
        self.assertEqual([event.id for event in events], [event.id for event in self.store.list_events()])
        self.assertEqual([], self.errors)

    def test_calendar_clipboard_image_keeps_month_and_original_text_for_review(self):
        text = "주요 학사일정\n2021. 6월\n6(일) 현충일\n7(월)~11(금) 환경교육주간"
        image = QImage(80, 80, QImage.Format_RGB32)
        image.fill(Qt.white)
        with patch.object(self.window.ocr, "recognize", return_value=text):
            self.window.receive_import_payload({"kind": "image", "image": image}, "desktop")
            self.wait_for_import()
        dialog = self.preview()
        self.assertEqual(2, dialog.table.rowCount())
        self.assertEqual("2021-06-06", dialog.table.item(0, dialog.DATE).text())
        self.assertEqual("2021-06-07", dialog.table.item(1, dialog.DATE).text())
        self.assertEqual("2021-06-11", dialog.table.item(1, dialog.END_DATE).text())
        self.assertEqual(text, dialog.recognized_editor.toPlainText())
        self.assertIn(text, self.window.capture_notepad.editor.toPlainText())
        dialog.reject()
        self.app.processEvents()
        self.assertEqual([], self.store.list_events())
        self.assertIn(text, self.store.get_setting("capture_draft"))

    def test_reanalyzed_document_still_requires_confirmation_and_preserves_done_duplicates(self):
        document = self.document("duplicate-plan.txt", "2099년 9월 10일 국어 평가")
        self.window.import_file_paths([str(document)])
        self.wait_for_import()
        self.register()
        event = self.store.list_events()[0]
        self.store.complete_event(event.id)
        completed_at = self.store.get_event(event.id).completed_at
        with patch("alimi.app.QFileDialog.getOpenFileNames") as chooser:
            self.window.analyze_button.click()
            chooser.assert_not_called()
        self.preview()
        self.assertEqual(1, len(self.store.list_events()))
        self.register()
        self.assertEqual(1, len(self.store.list_events()))
        saved = self.store.get_event(event.id)
        self.assertEqual(("done", completed_at), (saved.status, saved.completed_at))
        self.assertEqual(document.name, saved.source)
        self.assertNotIn(event.id, self.window.notes,
                         "Re-registering a completed duplicate must not reopen it.")
        self.assertIn("새 일정 0건 · 중복 1건", self.window.import_result.toPlainText())

    def test_existing_schedule_edits_in_import_review_update_same_reminder(self):
        original = "2099년 9월 10일 국어 평가"
        self.window.receive_import_text(original, "평가 계획.txt")
        self.register()
        saved = self.store.list_events()[0]
        note = self.window.notes[saved.id]
        note.set_expanded(True)
        self.window.receive_import_text(original, "평가 계획.txt")
        dialog = self.preview()
        dialog.notes_editor.setPlainText("수정한 세부 내용")
        dialog.table.item(0, dialog.END_DATE).setText("2099-09-12")
        self.assertEqual(saved.notes, self.store.get_event(saved.id).notes)
        self.register()
        updated = self.store.get_event(saved.id)
        self.assertEqual(1, len(self.store.list_events()))
        self.assertEqual("수정한 세부 내용", updated.notes)
        self.assertEqual("2099-09-12", updated.end_date)
        self.assertIs(note, self.window.notes[saved.id])
        self.assertTrue(note.expanded)
        self.assertIn(updated.notes, note.details_label.text())
        self.assertIn("수정 1건", self.window.import_result.toPlainText())

        self.window.mode_combo.setCurrentIndex(1)
        self.app.processEvents()
        desktop = self.window.desktop_list
        desktop.set_selected_event(saved.id)
        self.window.receive_import_text(original, "평가 계획.txt")
        dialog = self.preview()
        self.assertEqual(updated.notes, dialog.notes_editor.toPlainText())
        dialog.table.item(0, dialog.TITLE).setText("최종 변경한 평가")
        dialog.table.item(0, dialog.DATE).setText("2099-09-11")
        dialog.table.item(0, dialog.TIME).setText("14:30")
        dialog.table.item(0, dialog.PLACE).setText("도서관")
        self.register()
        updated = self.store.get_event(saved.id)
        self.assertEqual(1, len(self.store.list_events()))
        self.assertEqual(("최종 변경한 평가", "2099-09-11", "14:30", "도서관"),
                         (updated.title, updated.start_date, updated.time, updated.place))
        self.assertIs(desktop, self.window.desktop_list)
        self.assertEqual(saved.id, desktop.selected_event_id)
        self.assertEqual(updated.title, desktop.list_widget.item(0).text())
        self.assertIn(updated.notes, desktop.details_label.text())
        self.assertEqual([], self.errors)

    def test_existing_completed_schedule_review_preserves_state_and_cancelled_edits(self):
        original = "2099년 9월 10일 국어 평가"
        self.window.receive_import_text(original, "평가 계획.txt")
        self.register()
        saved = self.store.list_events()[0]
        self.store.complete_event(saved.id)
        before = self.store.get_event(saved.id)
        self.window.refresh()
        self.window.receive_import_text(original, "평가 계획.txt")
        dialog = self.preview()
        dialog.notes_editor.setPlainText("취소할 메모")
        dialog.reject()
        self.app.processEvents()
        self.assertEqual(before, self.store.get_event(saved.id))
        self.window.receive_import_text(original, "평가 계획.txt")
        dialog = self.preview()
        dialog.notes_editor.setPlainText("완료 일정의 수정한 메모")
        self.register()
        updated = self.store.get_event(saved.id)
        self.assertEqual(("done", before.completed_at), (updated.status, updated.completed_at))
        self.assertEqual("완료 일정의 수정한 메모", updated.notes)
        self.assertNotIn(saved.id, self.window.notes)
        self.assertIn("완료 상태를 유지", self.window.import_result.toPlainText())
        self.window.receive_import_text(original, "평가 계획.txt")
        dialog = self.preview()
        self.assertEqual(updated.notes, dialog.notes_editor.toPlainText())
        self.register()
        self.assertEqual(updated, self.store.get_event(saved.id))
        self.assertIn("새 일정 0건 · 중복 1건", self.window.import_result.toPlainText())

    def test_same_existing_schedule_in_multiple_files_is_edited_once(self):
        original = "2099년 9월 10일 국어 평가"
        first = self.document("first.txt", original)
        second = self.document("second.txt", original)
        self.window.import_file_paths([str(first)])
        self.wait_for_import()
        self.register()
        saved = self.store.list_events()[0]
        self.window.import_file_paths([str(first), str(second)])
        self.wait_for_import()
        dialog = self.preview()
        self.assertEqual(1, dialog.table.rowCount())
        dialog.table.item(0, dialog.TITLE).setText("최종 수정한 국어 평가")
        self.register()
        events = self.store.list_events()
        self.assertEqual(1, len(events))
        self.assertEqual((saved.id, "최종 수정한 국어 평가", first.name),
                         (events[0].id, events[0].title, events[0].source))

    def test_far_future_registration_immediately_shows_sticky_with_default_unlimited_range(self):
        self.assertTrue(self.window.reminder_unlimited.isChecked())
        notepad = self.open_notepad()
        pasted = QMimeData()
        pasted.setText("2099년 10월 22일까지 인사기록 제출")
        notepad.editor.insertFromMimeData(pasted)
        dialog = self.preview()
        self.assertEqual([], self.store.list_events())
        self.assertEqual({}, self.window.notes)
        dialog.register_button.click()
        self.app.processEvents()
        event = self.store.list_events()[0]
        self.assertEqual("2099-10-22", event.start_date)
        self.assertIn(event.id, self.window.notes,
                      "A successful registration must immediately appear with unlimited dates.")
        self.assertTrue(self.window.notes[event.id].isVisible())
        self.assertEqual("normal", self.window.notes[event.id].urgency_state.code)
        notepad.review_button.click()
        self.register()
        self.assertEqual(1, len(self.store.list_events()))
        self.assertEqual({event.id}, set(self.window.notes))
        self.assertTrue(self.window.notes[event.id].isVisible())
        self.assertIn("이미 등록된", notepad.status_label.text())
        self.assertIn("표시", notepad.status_label.text())
        self.assertIn("중복 1건", self.window.import_result.toPlainText())
        self.assertEqual([], self.errors)

    def test_reviewing_closed_tools_explains_tray_reopen_without_range_changes(self):
        original = "2099년 10월 15일 닫아 둔 평가 일정"
        self.window.receive_import_text(original, "학교 공지")
        self.register()
        saved = self.store.list_events()[0]
        for index, mode in ((0, "sticky"), (1, "list")):
            with self.subTest(mode=mode):
                self.window.mode_combo.setCurrentIndex(index)
                self.app.processEvents()
                tool = self.window.notes[saved.id] if mode == "sticky" else self.window.desktop_list
                with patch("alimi.desktop.QMessageBox.warning", return_value=QMessageBox.Ok):
                    tool.close_button.click()
                self.window.receive_import_text(original, "학교 공지")
                dialog = self.preview()
                dialog.notes_editor.setPlainText(mode + "에서 수정한 메모")
                self.register()
                message = self.window.capture_notepad.status_label.text()
                self.assertIn("1건은 바탕화면 도구를 닫아 숨겨져 있습니다", message)
                self.assertIn("바탕화면 알림 다시 열기", message)
                self.assertNotIn("설정한 표시 기간 밖", message)
                self.assertFalse(tool.isVisible())
                self.assertEqual("open", self.store.get_event(saved.id).status)
                self.assertEqual(mode + "에서 수정한 메모", self.store.get_event(saved.id).notes)
                self.assertEqual(1, len(self.store.list_events()))
                self.window.restore_desktop_tools()
                self.app.processEvents()
                self.assertTrue(tool.isVisible())

    def test_registration_counts_closed_tools_separately_from_outside_range_schedules(self):
        today = date.today()
        original = f"{today.year}년 {today.month}월 {today.day}일 닫아 둔 당일 일정"
        self.window.receive_import_text(original, "학교 공지")
        self.register()
        self.window.reminder_future_days.setValue(3)
        self.window.reminder_unlimited.setChecked(False)
        self.window.mode_combo.setCurrentIndex(1)
        self.app.processEvents()
        with patch("alimi.desktop.QMessageBox.warning", return_value=QMessageBox.Ok):
            self.window.desktop_list.close_button.click()
        future = today + timedelta(days=8)
        future_text = f"{future.year}년 {future.month}월 {future.day}일 범위 밖 일정"
        self.window.receive_import_text(original + "\n" + future_text, "학교 공지")
        self.register()
        message = self.window.capture_notepad.status_label.text()
        self.assertIn("1건은 바탕화면 도구를 닫아 숨겨져 있습니다", message)
        self.assertIn("바탕화면 알림 다시 열기", message)
        self.assertIn("1건은 설정한 표시 기간 밖입니다", message)
        self.assertIn("기간을 늘리거나", message)
        self.assertEqual(2, len(self.store.list_events()))
        self.assertTrue(all(event.status == "open" for event in self.store.list_events()))

    def test_future_registration_explains_limited_range_then_enabling_unlimited_displays_it(self):
        self.window.reminder_future_days.setValue(3)
        self.window.reminder_unlimited.setChecked(False)
        self.app.processEvents()
        deadline = date.today() + timedelta(days=8)
        self.window.process_import(
            f"{deadline.year}년 {deadline.month}월 {deadline.day}일 인사기록 제출", "일정 메모장")
        self.register()
        event = self.store.list_events()[0]
        self.assertEqual("open", event.status)
        self.assertNotIn(event.id, self.window.notes)
        status = self.window.capture_notepad.status_label.text()
        self.assertIn("표시", status)
        self.assertIn("설정", status,
                      "Registration must explain when the configured range hides the saved event.")
        self.window.reminder_unlimited.setChecked(True)
        self.app.processEvents()
        self.assertIn(event.id, self.window.notes)
        self.assertEqual(1, len(self.store.list_events()))
        self.assertEqual("open", self.store.get_event(event.id).status)

    def test_changing_range_during_confirmation_never_saves_unconfirmed_candidates(self):
        notepad = self.open_notepad()
        draft = "2099년 10월 22일 취소할 일정"
        notepad.editor.setPlainText(draft)
        notepad.review_button.click()
        dialog = self.preview()
        self.window.reminder_unlimited.setChecked(False)
        self.window.reminder_past_days.setValue(0)
        self.window.reminder_future_days.setValue(0)
        self.app.processEvents()
        self.assertEqual([], self.store.list_events())
        self.assertEqual({}, self.window.notes)
        self.assertIs(dialog, self.window.import_dialog)
        dialog.reject()
        self.app.processEvents()
        self.window.reminder_unlimited.setChecked(True)
        self.app.processEvents()
        self.assertEqual([], self.store.list_events())
        self.assertEqual({}, self.window.notes)
        self.assertEqual(draft, notepad.editor.toPlainText())
        self.assertEqual([], self.errors)

    def test_canceling_empty_analyze_file_chooser_is_harmless(self):
        preview = self.window.import_result.toPlainText()
        with patch("alimi.app.QFileDialog.getOpenFileNames", return_value=([], "")) as chooser:
            self.window.analyze_button.click()
            chooser.assert_called_once()
        self.assertEqual([], self.store.list_events())
        self.assertEqual("", self.window.text_input.toPlainText())
        self.assertEqual(preview, self.window.import_result.toPlainText())
        self.assertIsNone(self.window.import_dialog)
        self.assertEqual([], self.errors)
        self.assertFalse(self.window.jobs)

    def test_manager_pasted_text_opens_preview_without_registering(self):
        pasted = QMimeData()
        pasted.setText("2099년 10월 15일 15:30 교직원 협의회")
        with patch("alimi.app.QFileDialog.getOpenFileNames") as chooser:
            self.window.text_input.insertFromMimeData(pasted)
            self.preview()
            self.assertEqual([], self.store.list_events())
            self.register()
            chooser.assert_not_called()
        events = self.store.list_events()
        self.assertEqual(1, len(events))
        self.assertEqual("2099-10-15", events[0].start_date)
        self.assertEqual("15:30", events[0].time)
        self.assertEqual("붙여넣은 텍스트", events[0].source)
        self.assertEqual([], self.errors)

    def test_pasted_full_body_corrections_do_not_reanalyze_or_change_schedule_candidates(self):
        original = ("안내문을 읽고 확인해 주세요.\n\n"
                    "2099년 10월 15일 15:30 교직원 협의회\n\n"
                    "준비물은 개인 필기구입나다.\n문의는 행정실로 해 주세요.")
        corrected = original.replace("필기구입나다", "필기구입니다")
        pasted = QMimeData()
        pasted.setText(original)
        self.window.text_input.insertFromMimeData(pasted)
        dialog = self.preview()
        self.assertEqual(original, dialog.recognized_editor.toPlainText())
        self.assertFalse(dialog.recognized_editor.isReadOnly())
        self.assertEqual(1, dialog.table.rowCount())
        before_rows = [[dialog.table.item(row, column).text()
                        for column in range(dialog.table.columnCount())]
                       for row in range(dialog.table.rowCount())]
        before_candidates = [vars(event).copy() for event in dialog._events]
        before_queue = list(self.window.import_queue)
        original_source = dialog._events[0].source_text
        with patch("alimi.app.parse_schedule_text", wraps=parse_schedule_text) as parse, \
                patch.object(self.window.ocr, "recognize") as recognize:
            dialog.recognized_editor.setPlainText(corrected)
            QTest.qWait(350)
            self.assertEqual(before_rows, [[dialog.table.item(row, column).text()
                                           for column in range(dialog.table.columnCount())]
                                          for row in range(dialog.table.rowCount())])
            self.assertEqual(before_candidates, [vars(event).copy() for event in dialog._events])
            self.assertEqual(before_queue, self.window.import_queue)
            self.assertIs(dialog, self.window.import_dialog)
            self.assertEqual([], self.store.list_events())
            self.assertEqual(corrected, self.window.text_input.toPlainText())
            self.assertEqual(corrected, self.window.capture_notepad.editor.toPlainText())
            self.assertEqual(corrected, self.store.get_setting("capture_draft"))
            self.register()
            parse.assert_not_called()
            recognize.assert_not_called()
        saved = self.store.list_events()
        self.assertEqual(1, len(saved))
        self.assertEqual(("2099-10-15", "15:30", "open"),
                         (saved[0].start_date, saved[0].time, saved[0].status))
        self.assertEqual(original_source, saved[0].source_text)
        self.assertIn(corrected, self.window.import_result.toPlainText())
        self.assertNotIn("필기구입나다", self.window.import_result.toPlainText())

    def test_recognized_body_correction_keeps_completed_schedule_metadata_unchanged(self):
        original = "2099년 10월 15일 협의회\n담당자에게 자료를 전다래 주세요."
        corrected = original.replace("전다래", "전달해")
        self.window.receive_import_text(original, "안내문.txt")
        self.register()
        event = self.store.list_events()[0]
        self.store.complete_event(event.id)
        self.store.update_position(event.id, 123, 234)
        before = self.store.get_event(event.id)
        self.window.receive_import_text(original, "안내문.txt")
        dialog = self.preview()
        self.assertEqual(before.id, dialog._events[0].id)
        dialog.recognized_editor.setPlainText(corrected)
        self.assertEqual([before], self.store.list_events())
        self.assertEqual(before.source_text, dialog.source_text.toPlainText())
        self.register()
        self.assertEqual([before], self.store.list_events(),
                         "Editing the recognized body must not update or reopen an existing schedule.")
        self.assertNotIn(before.id, self.window.notes)
        self.assertIn(corrected, self.window.import_result.toPlainText())

    def test_cancel_preserves_corrected_body_but_discards_unregistered_schedule_edits(self):
        original = "2099년 10월 15일 협의회\n공통 안내 내용입나다."
        corrected = original.replace("입나다", "입니다")
        self.window.receive_import_text(original, "붙여넣은 텍스트")
        dialog = self.preview()
        original_title = dialog.table.item(0, dialog.TITLE).text()
        dialog.table.item(0, dialog.TITLE).setText("취소한 일정 제목 수정")
        dialog.recognized_editor.setPlainText(corrected)
        dialog.cancel_button.click()
        self.app.processEvents()
        self.assertIsNone(self.window.import_dialog)
        self.assertEqual([], self.store.list_events())
        self.window.save_capture_draft()
        self.assertEqual(corrected, self.store.get_setting("capture_draft"))
        self.window.review_capture_draft()
        reopened = self.preview()
        self.assertEqual(corrected, reopened.recognized_editor.toPlainText())
        self.assertEqual(original_title, reopened.table.item(0, reopened.TITLE).text())
        self.assertEqual([], self.store.list_events())

    def test_zero_schedule_body_is_editable_and_reloaded_after_shutdown(self):
        original = "날짜 없는 일반 안내입니다.\n맞춤뻡을 확인해 주세요."
        corrected = original.replace("맞춤뻡", "맞춤법")
        self.window.receive_import_text(original, "클립보드 / 드래그 이미지")
        dialog = self.preview()
        self.assertEqual(0, dialog.table.rowCount())
        self.assertEqual(original, dialog.recognized_editor.toPlainText())
        dialog.recognized_editor.setPlainText(corrected)
        self.assertEqual([], self.store.list_events())
        # Exit before the debounce timer fires: shutdown must flush the edit.
        self.dispose_window()
        self.store.close()
        self.store = Store(self.data_dir / "alimi.sqlite3")
        self.assertEqual(corrected, self.store.get_setting("capture_draft"))
        self.assertEqual(corrected, self.store.get_setting("recognized_text_draft"))
        self.app.setProperty("shutting_down", False)
        args = SimpleNamespace(data_dir=self.data_dir, no_autostart=True,
                               smoke_test=True, background=False)
        self.window = MainWindow(self.store, args, make_icon())
        self.window.show_error = self.errors.append
        self.assertEqual(corrected, self.window.text_input.toPlainText())
        self.assertEqual(corrected, self.window.capture_notepad.editor.toPlainText())
        self.window.review_capture_draft()
        reopened = self.preview()
        self.assertEqual(corrected, reopened.recognized_editor.toPlainText())
        self.assertEqual(0, reopened.table.rowCount())
        self.assertEqual([], self.store.list_events())

    def test_edited_recognition_and_unrelated_notepad_draft_persist_independently(self):
        manual = "사용자가 따로 작성한 메모입니다.\n인식한 문서와 바꾸면 안 됩니다."
        original = "2099년 10월 15일 협의회\n문서의 안내를 확인하새요."
        corrected = original.replace("확인하새요", "확인하세요")
        notepad = self.open_notepad()
        notepad.editor.setPlainText(manual)
        self.window.receive_import_text(original, "별도 안내문.txt")
        dialog = self.preview()
        self.assertEqual(manual, notepad.editor.toPlainText())
        dialog.recognized_editor.setPlainText(corrected)
        self.assertEqual(corrected, self.window.text_input.toPlainText())
        self.assertEqual(manual, notepad.editor.toPlainText(),
                         "Editing an imported body must not replace an unrelated manual memo.")
        self.assertEqual(1, dialog.table.rowCount())
        # Both independent drafts must survive exit, including pending edits.
        self.dispose_window()
        self.store.close()
        self.store = Store(self.data_dir / "alimi.sqlite3")
        self.assertEqual(manual, self.store.get_setting("capture_draft"))
        self.assertEqual(corrected, self.store.get_setting("recognized_text_draft"))
        self.app.setProperty("shutting_down", False)
        args = SimpleNamespace(data_dir=self.data_dir, no_autostart=True,
                               smoke_test=True, background=False)
        self.window = MainWindow(self.store, args, make_icon())
        self.window.show_error = self.errors.append
        self.assertEqual(manual, self.window.capture_notepad.editor.toPlainText())
        self.assertEqual(corrected, self.window.text_input.toPlainText())
        self.assertEqual([], self.store.list_events())
        self.window.analyze_button.click()
        reopened = self.preview()
        self.assertEqual(corrected, reopened.recognized_editor.toPlainText())
        self.assertEqual(1, reopened.table.rowCount())
        self.assertEqual(manual, self.window.capture_notepad.editor.toPlainText())
        self.assertEqual([], self.store.list_events())

    def test_corrected_file_body_is_used_on_explicit_reanalysis_instead_of_stale_records(self):
        first = self.document("first-edit.txt", "2099년 9월 10일 원래 첫 일정\n오타 안내문")
        second = self.document("second-edit.txt", "2099년 10월 15일 원래 둘째 일정")
        for files in ([first], [first, second]):
            with self.subTest(files=[path.name for path in files]):
                self.window.import_file_paths([str(path) for path in files])
                self.wait_for_import()
                dialog = self.preview()
                self.assertEqual(len(files), len(self.window.import_preview_records))
                corrected = "2099년 11월 20일 수정한 인식 내용의 일정\n일정 외 안내도 고쳤습니다."
                dialog.recognized_editor.setPlainText(corrected)
                self.assertEqual([], self.window.import_preview_records)
                self.assertEqual(len(files), dialog.table.rowCount(),
                                 "The current review stays unchanged until explicit reanalysis.")
                dialog.reject()
                self.app.processEvents()
                source = ", ".join(path.name for path in files)
                with patch("alimi.app.parse_schedule_text", wraps=parse_schedule_text) as parse:
                    self.window.analyze_button.click()
                    parse.assert_called_once_with(corrected, source=source)
                new_dialog = self.preview()
                self.assertEqual(corrected, new_dialog.recognized_editor.toPlainText())
                self.assertEqual(1, new_dialog.table.rowCount())
                self.assertEqual("2099-11-20", new_dialog.table.item(0, new_dialog.DATE).text())
                self.assertIn("수정한 인식 내용", new_dialog.table.item(0, new_dialog.TITLE).text())
                self.assertEqual([], self.store.list_events())
                new_dialog.reject()
                self.app.processEvents()

    def test_imminent_paste_stays_unregistered_and_hidden_until_final_confirmation(self):
        deadline = date.today() + timedelta(days=3)
        pasted = QMimeData()
        pasted.setText(f"{deadline.year}년 {deadline.month}월 {deadline.day}일 교직원 제출 마감")
        self.open_notepad().editor.insertFromMimeData(pasted)
        dialog = self.preview()
        self.assertEqual([], self.store.list_events())
        self.assertEqual({}, self.window.notes)
        self.assertIsNone(self.window.desktop_list)
        self.assertEqual(deadline.isoformat(), dialog.table.item(0, 2).text())
        dialog.table.item(0, 1).setText("확인 후 수정한 제출 마감")
        dialog.register_button.click()
        self.app.processEvents()
        events = self.store.list_events()
        self.assertEqual(1, len(events))
        event = events[0]
        self.assertEqual("확인 후 수정한 제출 마감", event.title)
        self.assertEqual(deadline.isoformat(), event.start_date)
        self.assertIn(event.id, self.window.notes)
        self.assertEqual("three_days", self.window.notes[event.id].urgency_state.code)
        self.assertEqual([], self.errors)

    def test_preview_edited_deadline_controls_reminder_appearance_after_save(self):
        self.window.reminder_future_days.setValue(3)
        self.window.reminder_unlimited.setChecked(False)
        self.app.processEvents()
        soon = date.today() + timedelta(days=2)
        later = date.today() + timedelta(days=4)
        self.window.process_import(
            f"{soon.year}년 {soon.month}월 {soon.day}일 수정할 일정", "검증 입력")
        dialog = self.preview()
        dialog.table.item(0, 2).setText(later.isoformat())
        self.assertEqual({}, self.window.notes)
        dialog.register_button.click()
        self.app.processEvents()
        events = self.store.list_events()
        self.assertEqual(1, len(events))
        self.assertEqual(later.isoformat(), events[0].start_date)
        self.assertNotIn(events[0].id, self.window.notes,
                         "Only the confirmed date determines whether a desktop reminder is due.")
        self.assertEqual([], self.errors)

    def test_edited_document_input_uses_new_text_source_after_final_confirmation(self):
        document = self.document("first-plan.txt", "2099년 9월 10일 국어 평가")
        self.window.import_file_paths([str(document)])
        self.wait_for_import()
        self.register()
        self.window.text_input.setPlainText("2099년 10월 15일 수학 평가")
        self.window.analyze_button.click()
        self.assertEqual(1, len(self.store.list_events()))
        self.register()
        events = self.store.list_events()
        self.assertEqual(2, len(events))
        self.assertEqual([document.name, "붙여넣은 텍스트"], [event.source for event in events])
        self.assertEqual([], self.errors)

    def test_desktop_typing_waits_for_review_and_cancel_preserves_local_draft(self):
        text = "2099년 10월 15일 직접 입력한 수학 평가"
        notepad = self.open_notepad()
        notepad.editor.insertPlainText(text)
        QTest.qWait(400)
        self.assertIsNone(self.window.import_dialog)
        self.assertEqual([], self.store.list_events())
        notepad.review_button.click()
        dialog = self.preview()
        dialog.reject()
        self.app.processEvents()
        self.assertEqual([], self.store.list_events())
        self.assertEqual(text, notepad.editor.toPlainText())
        self.window.save_capture_draft()
        self.assertEqual(text, self.store.get_setting("capture_draft"))
        notepad.review_button.click()
        self.register()
        self.assertEqual(1, len(self.store.list_events()))

    def test_desktop_paste_recognizes_only_inserted_input_and_cancel_does_not_save(self):
        original = "2099년 9월 10일 기존 메모 내용\n"
        notepad = self.open_notepad()
        notepad.editor.setPlainText(original)
        cursor = notepad.editor.textCursor()
        cursor.movePosition(cursor.End)
        notepad.editor.setTextCursor(cursor)
        pasted = QMimeData()
        pasted.setText("2099년 10월 15일 새로 붙인 일정")
        notepad.editor.insertFromMimeData(pasted)
        dialog = self.preview()
        self.assertEqual(1, dialog.table.rowCount(), "Pasting must not re-import the older draft.")
        self.assertIn("새로 붙인", dialog.table.item(0, 1).text())
        self.assertEqual([], self.store.list_events())
        dialog.reject()
        self.app.processEvents()
        self.assertEqual([], self.store.list_events())
        self.assertIn(original, notepad.editor.toPlainText())
        self.assertIn("새로 붙인 일정", notepad.editor.toPlainText())

    def test_explorer_clipboard_file_urls_take_precedence_over_image_and_text(self):
        document = self.document("clipboard-plan.txt", "2099년 10월 15일 파일 안의 일정")
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(document))])
        mime.setText("2099년 10월 20일 잘못된 파일 경로 텍스트")
        mime.setImageData(QImage(3, 3, QImage.Format_RGB32))
        self.open_notepad()
        with patch.object(self.window.ocr, "recognize", side_effect=AssertionError("File URLs should be read first.")):
            self.window.capture_notepad.editor.insertFromMimeData(mime)
            self.wait_for_import()
        dialog = self.preview()
        self.assertEqual(1, dialog.table.rowCount())
        self.assertIn("파일 안의", dialog.table.item(0, 1).text())
        self.assertEqual([], self.store.list_events())
        self.assertNotIn("잘못된 파일 경로", self.window.capture_notepad.editor.toPlainText())
        self.register()
        event = self.store.list_events()[0]
        self.assertEqual(document.name, event.source)
        self.assertEqual("2099-10-15", event.start_date)
        self.assertTrue(document.exists(), "Importing a copied file must leave the original in place.")

    def test_clipboard_image_ocr_previews_before_registering(self):
        mime = QMimeData()
        image = QImage(3, 3, QImage.Format_RGB32)
        image.fill(Qt.white)
        mime.setImageData(image)
        mime.setText("2099년 10월 20일 이미지 대신 읽으면 안 되는 텍스트")
        self.open_notepad()
        with patch.object(self.window.ocr, "recognize", return_value="2099년 10월 15일 이미지에서 읽은 일정") as recognize:
            self.window.capture_notepad.editor.insertFromMimeData(mime)
            self.wait_for_import()
            recognize.assert_called_once()
        dialog = self.preview()
        self.assertIn("이미지에서 읽은", dialog.table.item(0, 1).text())
        self.assertEqual([], self.store.list_events())
        self.assertIn("이미지에서 읽은", self.window.capture_notepad.editor.toPlainText())
        self.register()
        self.assertEqual("2099-10-15", self.store.list_events()[0].start_date)

    def test_image_ocr_body_can_be_corrected_without_running_ocr_again(self):
        recognized = "담당자 안내입나다.\n2099년 10월 15일 이미지에서 읽은 일정\n자료는 첨부를 참고하세요."
        corrected = recognized.replace("안내입나다", "안내입니다")
        mime = QMimeData()
        image = QImage(3, 3, QImage.Format_RGB32)
        image.fill(Qt.white)
        mime.setImageData(image)
        notepad = self.open_notepad()
        with patch.object(self.window.ocr, "recognize", return_value=recognized) as recognize:
            notepad.editor.insertFromMimeData(mime)
            self.wait_for_import()
            dialog = self.preview()
            self.assertEqual(recognized, dialog.recognized_editor.toPlainText())
            original_source = dialog._events[0].source_text
            dialog.recognized_editor.setPlainText(corrected)
            self.assertEqual(corrected, notepad.editor.toPlainText())
            self.assertEqual(1, dialog.table.rowCount())
            self.assertEqual([], self.store.list_events())
            self.register()
            recognize.assert_called_once()
        saved = self.store.list_events()
        self.assertEqual(1, len(saved))
        self.assertEqual("2099-10-15", saved[0].start_date)
        self.assertEqual(original_source, saved[0].source_text)
        self.assertIn(corrected, self.window.import_result.toPlainText())

    def test_multiple_files_share_one_preview_and_preserve_each_source(self):
        first = self.document("first.txt", "2099년 9월 10일 첫 번째 평가")
        second = self.document("second.txt", "2099년 10월 15일 두 번째 평가")
        self.window.import_file_paths([str(first), str(second)], origin="desktop")
        self.wait_for_import()
        dialog = self.preview()
        self.assertEqual(2, dialog.table.rowCount())
        self.assertFalse(self.window.import_queue)
        self.assertEqual([], self.store.list_events())
        self.register()
        self.assertEqual([first.name, second.name], [event.source for event in self.store.list_events()])
        # Editing a retried row updates its existing identity and must retain
        # that row's original per-file source association.
        first_id = self.store.list_events()[0].id
        self.window.analyze_button.click()
        retried = self.preview()
        self.assertEqual(2, retried.table.rowCount())
        retried.table.item(0, 1).setText("재분석에서 수정한 첫 번째 평가")
        self.assertEqual(2, len(self.store.list_events()))
        self.register()
        events = self.store.list_events()
        changed = next(event for event in events if "재분석에서 수정" in event.title)
        self.assertEqual(first.name, changed.source)
        self.assertEqual(first_id, changed.id)
        self.assertEqual(2, len(events))
        self.assertIn("새 일정 0건 · 수정 1건 · 중복 1건", self.window.import_result.toPlainText())

    def test_file_read_error_does_not_partially_register_successful_files(self):
        first = self.document("valid.txt", "2099년 9월 10일 정상 평가")
        missing = self.data_dir / "missing.txt"
        self.window.import_file_paths([str(first), str(missing)], origin="desktop")
        self.wait_for_import(allow_errors=True)
        dialog = self.preview()
        self.assertEqual(1, dialog.table.rowCount())
        self.assertIn("missing.txt", dialog.import_errors.text())
        self.assertEqual([], self.store.list_events())
        dialog.reject()
        self.app.processEvents()
        self.assertEqual([], self.store.list_events())
        self.assertIn("정상 평가", self.window.capture_notepad.editor.toPlainText())

    def test_later_paste_is_queued_without_losing_the_first_preview(self):
        first = QMimeData()
        first.setText("2099년 9월 10일 첫 붙여넣기")
        second = QMimeData()
        second.setText("2099년 10월 15일 두 번째 붙여넣기")
        editor = self.open_notepad().editor
        editor.insertFromMimeData(first)
        first_dialog = self.preview()
        editor.insertFromMimeData(second)
        self.app.processEvents()
        self.assertIs(first_dialog, self.window.import_dialog)
        self.assertEqual(1, len(self.window.import_queue))
        self.assertEqual([], self.store.list_events())
        self.register()
        self.wait_until(lambda: self.window.import_dialog is not None and self.window.import_dialog is not first_dialog,
                        "The queued paste was lost after confirming the first preview.")
        self.assertEqual(1, len(self.store.list_events()))
        self.assertIn("두 번째", self.window.import_dialog.table.item(0, 1).text())
        self.register()
        self.assertEqual(2, len(self.store.list_events()))
        self.assertFalse(self.window.import_queue)

    def test_save_failure_keeps_review_open_and_retry_saves_once(self):
        self.window.process_import("2099년 10월 15일 저장 재시도 일정", "검증 입력")
        dialog = self.preview()
        with patch.object(self.store, "save_reviewed_events", side_effect=OSError("검증용 디스크 저장 실패")):
            dialog.register_button.click()
            self.app.processEvents()
        self.assertEqual([], self.store.list_events())
        self.assertIs(dialog, self.window.import_dialog)
        self.assertTrue(dialog.isVisible())
        self.assertIn("검증용 디스크 저장 실패", dialog.error_label.text())
        dialog.register_button.click()
        self.app.processEvents()
        self.assertEqual(1, len(self.store.list_events()))

    def test_failed_batch_rolls_back_prior_rows_and_retry_uses_fresh_candidates(self):
        self.window.process_import("2099년 9월 10일 첫 번째 저장 일정\n2099년 10월 15일 두 번째 저장 일정", "검증 입력")
        dialog = self.preview()
        original_upsert = self.store._upsert
        calls = []

        def fail_second_insert(event):
            calls.append(event.title)
            if len(calls) == 2:
                raise OSError("검증용 두 번째 행 저장 실패")
            return original_upsert(event)

        with patch.object(self.store, "_upsert", side_effect=fail_second_insert):
            dialog.register_button.click()
            self.app.processEvents()
        self.assertEqual(2, len(calls))
        self.assertEqual([], self.store.list_events(), "A failed batch must roll back its earlier inserts.")
        self.assertIs(dialog, self.window.import_dialog)
        self.assertIn("검증용 두 번째 행 저장 실패", dialog.error_label.text())
        dialog.register_button.click()
        self.app.processEvents()
        self.assertEqual(2, len(self.store.list_events()), "Retry must not retain rolled-back candidate IDs.")

    def test_desktop_notepad_remains_visible_when_manager_hides_and_mode_changes(self):
        notepad = self.window.capture_notepad
        self.assertIsNone(notepad.parent())
        self.assertTrue(notepad.isVisible())
        self.assertFalse(notepad.windowFlags() & Qt.WindowStaysOnBottomHint)
        self.assertTrue(notepad.windowFlags() & Qt.WindowStaysOnTopHint)
        self.assertTrue(notepad.collapsed)
        self.assertTrue(notepad.icon_button.isVisible())
        self.assertFalse(notepad.editor.isVisible())
        self.window.show()
        with patch("alimi.app.QMessageBox.warning", return_value=QMessageBox.Ok):
            self.window.close()
        self.app.processEvents()
        self.assertFalse(self.window.isVisible())
        self.assertTrue(notepad.isVisible())
        self.window.sync_notes()
        self.app.processEvents()
        self.assertTrue(notepad.collapsed,
                        "Routine reminder updates must not reopen the desktop editor.")
        with patch("alimi.desktop.QMessageBox.warning", return_value=QMessageBox.Cancel):
            notepad.close()
        self.app.processEvents()
        self.assertTrue(notepad.isVisible())
        self.window.mode_combo.setCurrentIndex(1)
        self.app.processEvents()
        self.assertTrue(notepad.isVisible())
        self.assertTrue(notepad.collapsed)
        self.window.mode_combo.setCurrentIndex(0)
        self.app.processEvents()
        self.assertTrue(notepad.isVisible())
        self.assertTrue(notepad.collapsed)

    def test_icon_double_click_and_minimize_preserve_the_editable_draft(self):
        notepad = self.window.capture_notepad
        self.assertTrue(notepad.collapsed)
        self.assertTrue(notepad.isVisible())
        QTest.mouseClick(notepad.icon_button, Qt.LeftButton)
        self.app.processEvents()
        self.assertTrue(notepad.collapsed, "A single click must not open the editor.")
        self.open_notepad()
        text = "2099년 10월 15일 아이콘 전환 중 보존할 초안"
        notepad.editor.setPlainText(text)
        notepad.minimize_button.click()
        self.app.processEvents()
        self.assertTrue(notepad.collapsed)
        self.assertTrue(notepad.icon_button.isVisible())
        self.assertFalse(notepad.editor.isVisible())
        self.assertEqual(text, notepad.editor.toPlainText())
        self.open_notepad()
        QTest.mouseDClick(notepad.header, Qt.LeftButton)
        self.app.processEvents()
        self.assertTrue(notepad.collapsed)
        self.window.save_capture_draft()
        self.assertEqual(text, self.store.get_setting("capture_draft"))
        self.assertEqual([], self.store.list_events())

    def test_tray_notepad_action_expands_the_icon_despite_triggered_false_argument(self):
        notepad = self.window.capture_notepad
        self.assertTrue(notepad.collapsed)
        action = next(action for action in self.window.tray.contextMenu().actions()
                      if "일정 메모장" in action.text())
        action.trigger()
        self.app.processEvents()
        self.assertFalse(notepad.collapsed)
        self.assertTrue(notepad.editor.isVisible())
        self.assertFalse(self.window.isVisible(), "Opening the memo should not open the manager.")

    def test_minimize_during_confirmation_keeps_preview_and_never_registers_early(self):
        notepad = self.open_notepad()
        text = "2099년 10월 15일 확인 중 최소화할 일정"
        mime = QMimeData()
        mime.setText(text)
        notepad.editor.insertFromMimeData(mime)
        dialog = self.preview()
        notepad.minimize_button.click()
        self.app.processEvents()
        self.assertTrue(notepad.collapsed)
        self.assertTrue(notepad.isVisible())
        self.assertIs(dialog, self.window.import_dialog)
        self.assertTrue(dialog.isVisible())
        self.assertEqual([], self.store.list_events())
        self.window.sync_notes()
        self.app.processEvents()
        self.assertTrue(notepad.collapsed)
        dialog.register_button.click()
        self.app.processEvents()
        self.assertEqual(1, len(self.store.list_events()))
        self.assertTrue(notepad.collapsed, "Saving a schedule must retain the chosen icon view.")
        self.assertEqual(text, notepad.editor.toPlainText())

    def test_minimize_during_file_worker_keeps_icon_and_draft_when_preview_arrives(self):
        notepad = self.open_notepad()
        draft = "이미 작성한 메모\n"
        recognized = "2099년 10월 15일 파일에서 인식한 평가"
        document = self.document("worker-minimize.txt", recognized)
        notepad.editor.setPlainText(draft)
        release = threading.Event()
        entered = threading.Event()

        def read_document(_path, _ocr):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("The verification worker was not released.")
            return recognized

        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(document))])
        with patch("alimi.app.extract_file_text", side_effect=read_document):
            try:
                notepad.editor.insertFromMimeData(mime)
                self.wait_until(entered.is_set, "The file worker did not start.")
                self.assertTrue(self.window.jobs)
                notepad.minimize_button.click()
                self.app.processEvents()
                self.assertTrue(notepad.collapsed)
                self.assertTrue(notepad.isVisible())
                self.assertEqual([], self.store.list_events())
            finally:
                release.set()
            self.wait_for_import()
        dialog = self.preview()
        self.assertTrue(notepad.collapsed,
                        "A completed recognition worker must not reopen the minimized editor.")
        self.assertEqual([], self.store.list_events())
        self.assertIn(draft, notepad.editor.toPlainText())
        self.assertIn(recognized, notepad.editor.toPlainText())
        dialog.reject()
        self.app.processEvents()
        self.assertTrue(notepad.collapsed)
        self.assertEqual([], self.store.list_events())
        self.window.save_capture_draft()
        self.assertEqual(notepad.editor.toPlainText(), self.store.get_setting("capture_draft"))

    def test_shutdown_flushes_recent_draft_and_discards_unregistered_preview(self):
        text = "2099년 10월 15일 종료 직전 초안"
        self.open_notepad().editor.insertPlainText(text)
        self.window.review_capture_draft()
        self.preview()
        self.window.on_shutdown()
        self.app.processEvents()
        self.assertEqual(text, self.store.get_setting("capture_draft"))
        self.assertEqual([], self.store.list_events())
        self.assertFalse(self.window.capture_notepad.isVisible())

    def test_shutdown_is_repeatable_and_notepad_can_reopen_if_session_close_is_canceled(self):
        text = "2099년 10월 15일 보존할 메모장 초안"
        notepad = self.open_notepad()
        notepad.editor.insertPlainText(text)
        self.window.on_shutdown()
        self.app.processEvents()
        self.window.on_shutdown()
        self.app.processEvents()
        self.assertEqual(text, self.store.get_setting("capture_draft"))
        self.assertEqual(text, notepad.editor.toPlainText())
        self.app.setProperty("shutting_down", False)
        notepad.allow_close = False
        self.window.show_capture_notepad()
        self.app.processEvents()
        self.assertTrue(notepad.isVisible())
        self.assertFalse(notepad.collapsed,
                         "A canceled session close should restore the user's active editor view.")
        with patch("alimi.desktop.QMessageBox.warning", return_value=QMessageBox.Cancel):
            notepad.close()
        self.app.processEvents()
        self.assertTrue(notepad.isVisible())
        self.assertEqual([], self.store.list_events())

    def test_canceled_windows_session_restores_current_icon_or_editor_view_and_draft(self):
        notepad = self.window.capture_notepad
        text = "2099년 10월 15일 종료 취소 시 보존할 초안"
        notepad.editor.setPlainText(text)
        for collapsed in (True, False):
            with self.subTest(collapsed=collapsed):
                notepad.set_collapsed(collapsed)
                self.app.processEvents()
                position = notepad.pos()
                with patch("alimi.app.QTimer.singleShot") as scheduled:
                    self.window.on_session_end(None)
                self.assertFalse(notepad.isVisible())
                self.assertTrue(self.app.property("shutting_down"))
                self.assertEqual(text, self.store.get_setting("capture_draft"))
                milliseconds, recover = scheduled.call_args.args
                self.assertEqual(10_000, milliseconds)
                # Exercise the actual cancellation callback without waiting
                # ten seconds or carrying its timer into the next test.
                recover()
                self.app.processEvents()
                self.assertFalse(self.app.property("shutting_down"))
                self.assertTrue(notepad.isVisible())
                self.assertEqual(collapsed, notepad.collapsed)
                self.assertEqual(position, notepad.pos())
                self.assertEqual(text, notepad.editor.toPlainText())
                with patch("alimi.desktop.QMessageBox.warning", return_value=QMessageBox.Cancel):
                    notepad.close()
                self.app.processEvents()
                self.assertTrue(notepad.isVisible())
                self.assertEqual([], self.store.list_events())

    def test_successful_restore_cancels_existing_schedule_preview_and_queued_inputs(self):
        original = "2099년 10월 15일 복원 전 일정"
        self.window.receive_import_text(original, "복원 전.txt")
        self.register()
        before = self.store.list_events()[0]
        self.window.receive_import_text(original, "복원 전.txt")
        dialog = self.preview()
        self.assertEqual(before.id, dialog._events[0].id)
        dialog.table.item(0, dialog.TITLE).setText("복원 후 덮어쓰면 안 되는 제목")
        dialog.notes_editor.setPlainText("복원 전 확인 창의 수정 메모")
        finished = []
        dialog.finished.connect(finished.append)
        self.window.receive_import_text("2099년 10월 16일 대기 중인 입력", "대기 중.txt")
        self.assertEqual(1, len(self.window.import_queue))

        replacement_path = self.data_dir / "replacement.sqlite3"
        replacement = Store(replacement_path)
        try:
            restored = replacement.upsert_event(Event(
                title="복원된 다른 일정", start_date="2099-11-01", notes="복원된 메모", source="복원 백업"))
            self.assertEqual(before.id, restored.id, "The restored database reuses the preview's old id.")
        finally:
            replacement.close()
        with patch("alimi.app.QFileDialog.getOpenFileName", return_value=(str(replacement_path), "")), \
                patch("alimi.app.QMessageBox.question", return_value=QMessageBox.Yes):
            self.window.restore_data()
        self.app.processEvents()
        QTest.qWait(1)

        self.assertEqual([QDialog.Rejected], finished)
        self.assertIsNone(self.window.import_dialog)
        self.assertEqual([], self.window.import_queue)
        saved = self.store.get_event(restored.id)
        self.assertEqual((restored.title, restored.start_date, restored.notes, restored.source),
                         (saved.title, saved.start_date, saved.notes, saved.source))
        self.assertEqual(1, len(self.store.list_events()))
        self.assertEqual("백업을 복원했습니다.", self.window.status_label.text())
        self.assertEqual([], self.errors)

    def test_failed_restore_preserves_existing_schedule_preview_edits_and_queue(self):
        original = "2099년 10월 15일 복원 실패 시 유지할 일정"
        self.window.receive_import_text(original, "유지할 일정.txt")
        self.register()
        before = self.store.list_events()[0]
        self.window.receive_import_text(original, "유지할 일정.txt")
        dialog = self.preview()
        dialog.table.item(0, dialog.TITLE).setText("확인 창에서 수정한 제목")
        dialog.notes_editor.setPlainText("확인 창에서 수정한 메모")
        self.window.receive_import_text("2099년 10월 16일 유지할 대기 입력", "대기 중.txt")
        queued = list(self.window.import_queue)
        with patch("alimi.app.QFileDialog.getOpenFileName", return_value=("invalid-backup.sqlite3", "")), \
                patch("alimi.app.QMessageBox.question", return_value=QMessageBox.Yes), \
                patch.object(self.store, "restore", side_effect=ValueError("검증용 복원 실패")):
            self.window.restore_data()
        self.app.processEvents()

        self.assertIs(dialog, self.window.import_dialog)
        self.assertTrue(dialog.isVisible())
        self.assertEqual("확인 창에서 수정한 제목", dialog.table.item(0, dialog.TITLE).text())
        self.assertEqual("확인 창에서 수정한 메모", dialog.notes_editor.toPlainText())
        self.assertEqual(queued, self.window.import_queue)
        self.assertEqual(before, self.store.get_event(before.id))
        self.assertEqual(["검증용 복원 실패"], self.errors)

    def test_declined_restore_preserves_existing_schedule_preview_edits(self):
        original = "2099년 10월 15일 복원을 거절할 일정"
        self.window.receive_import_text(original, "유지할 일정.txt")
        self.register()
        before = self.store.list_events()[0]
        self.window.receive_import_text(original, "유지할 일정.txt")
        dialog = self.preview()
        dialog.notes_editor.setPlainText("취소하지 않을 수정 메모")
        with patch("alimi.app.QFileDialog.getOpenFileName", return_value=("unused-backup.sqlite3", "")), \
                patch("alimi.app.QMessageBox.question", return_value=QMessageBox.No), \
                patch.object(self.store, "restore") as restore:
            self.window.restore_data()
        self.app.processEvents()

        restore.assert_not_called()
        self.assertIs(dialog, self.window.import_dialog)
        self.assertTrue(dialog.isVisible())
        self.assertEqual("취소하지 않을 수정 메모", dialog.notes_editor.toPlainText())
        self.assertEqual(before, self.store.get_event(before.id))
        self.assertEqual([], self.errors)


if __name__ == "__main__":
    unittest.main()
