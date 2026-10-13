"""Validate reviewed imports without ever opening application storage."""

import os
import unittest
from dataclasses import replace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Match the production DLL initialization order on Windows.
import onnxruntime

onnxruntime.disable_telemetry_events()

from PyQt5.QtCore import QEvent, Qt
from PyQt5.QtWidgets import QApplication, QDialog

from alimi.core import Event
from alimi.review import ImportReviewDialog


class ImportReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.windows = []
        self.event = Event(
            title="교직원 회의", start_date="2026-10-13", time="15:30", place="2층 회의실",
            notes="원래 세부 내용", source="학교 공지.pdf", source_text="원래 공지 원문",
        )

    def tearDown(self):
        for window in self.windows:
            window.reject()
            window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()

    def dialog(self, candidates=None, errors=None):
        window = ImportReviewDialog([self.event] if candidates is None else candidates, errors)
        self.windows.append(window)
        window.show()
        self.app.processEvents()
        return window

    def test_parentless_popup_is_normal_foreground_window(self):
        window = self.dialog()
        self.assertIsNone(window.parent())
        self.assertFalse(window.windowFlags() & Qt.WindowStaysOnBottomHint)
        self.assertFalse(window.windowFlags() & Qt.WindowStaysOnTopHint)
        self.assertEqual(Qt.NonModal, window.windowModality())
        self.assertEqual("원래 공지 원문", window.source_text.toPlainText())
        self.assertTrue(window.source_text.isReadOnly())

    def test_edits_are_cloned_and_confirm_only_requests_owner_commit(self):
        window = self.dialog()
        emitted = []
        window.confirmed.connect(emitted.append)
        window.table.item(0, window.TITLE).setText("수정한 회의")
        window.table.item(0, window.TIME).setText("16:40")
        window.table.item(0, window.PLACE).setText("도서관")
        window.notes_editor.setPlainText("수정한 세부 내용")
        window.register_button.click()
        self.assertEqual(1, len(emitted))
        event = emitted[0][0]
        self.assertEqual("수정한 회의", event.title)
        self.assertEqual("16:40", event.time)
        self.assertEqual("도서관", event.place)
        self.assertEqual("수정한 세부 내용", event.notes)
        self.assertEqual(self.event.source, event.source)
        self.assertEqual(self.event.source_text, event.source_text)
        self.assertEqual("교직원 회의", self.event.title)
        self.assertEqual("원래 세부 내용", self.event.notes)
        self.assertEqual(window.confirmed_events, emitted[0])
        self.assertTrue(window.isVisible(), "The storage owner must commit before accepting.")
        self.assertEqual(QDialog.Rejected, window.result())
        window.accept()
        self.assertEqual(QDialog.Accepted, window.result())

    def test_cancel_keeps_original_and_emits_no_registration(self):
        window = self.dialog()
        emitted = []
        window.confirmed.connect(emitted.append)
        window.table.item(0, window.TITLE).setText("취소한 수정")
        window.notes_editor.setPlainText("취소한 세부 내용")
        window.cancel_button.click()
        self.assertEqual([], emitted)
        self.assertEqual([], window.confirmed_events)
        self.assertEqual(QDialog.Rejected, window.result())
        self.assertEqual("교직원 회의", self.event.title)
        self.assertEqual("원래 세부 내용", self.event.notes)

    def test_existing_identity_is_retained_only_for_explicit_edits(self):
        existing = replace(self.event, id=17, status="done", completed_at="2026-10-14T00:00:00Z")
        window = self.dialog([existing])
        self.assertIn("완료 상태", window.summary_label.text())
        self.assertIsNone(window.selected_events()[0].id)
        window.notes_editor.setPlainText("변경한 메모")
        self.assertEqual(17, window.selected_events()[0].id)
        self.assertEqual("원래 세부 내용", existing.notes)
        window.notes_editor.setPlainText(existing.notes)
        self.assertIsNone(window.selected_events()[0].id)
        window.table.item(0, window.END_DATE).setText("2026-10-15")
        self.assertEqual(17, window.selected_events()[0].id)
        window.table.item(0, window.TITLE).setText("변경한 제목")
        self.assertEqual(17, window.selected_events()[0].id)

    def test_deleting_existing_row_keeps_remaining_edit_identity(self):
        window = self.dialog([replace(self.event, id=17), replace(self.event, id=18, title="두 번째 일정")])
        window.delete_current_event()
        window.notes_editor.setPlainText("남은 일정의 수정")
        selected = window.selected_events()
        self.assertEqual([(18, "남은 일정의 수정")], [(event.id, event.notes) for event in selected])

    def test_existing_review_schedule_is_confirmed_without_changing_fields(self):
        existing = replace(self.event, id=17, status="review")
        window = self.dialog([existing])
        selected = window.selected_events()[0]
        self.assertEqual((17, "open"), (selected.id, selected.status))
        self.assertEqual(existing.title, selected.title)

    def test_month_is_displayed_without_fake_day_and_maps_to_month_bounds(self):
        event = replace(self.event, start_date="2026-09-01", end_date="2026-09-30", date_precision="month")
        window = self.dialog([event])
        self.assertEqual("2026-09", window.table.item(0, window.DATE).text())
        self.assertEqual("", window.table.item(0, window.END_DATE).text())
        window.table.item(0, window.DATE).setText("2028-02")
        saved = window.selected_events()[0]
        self.assertEqual("2028-02-01", saved.start_date)
        self.assertEqual("2028-02-29", saved.end_date)
        self.assertEqual("month", saved.date_precision)

    def test_exact_first_day_remains_a_day_and_range_is_preserved(self):
        event = replace(self.event, start_date="2026-10-01", end_date="2026-10-03")
        window = self.dialog([event])
        self.assertEqual("2026-10-01", window.table.item(0, window.DATE).text())
        saved = window.selected_events()[0]
        self.assertEqual("day", saved.date_precision)
        self.assertEqual("2026-10-03", saved.end_date)
        window.table.item(0, window.END_DATE).setText("2026-10-05")
        self.assertEqual("2026-10-05", window.selected_events()[0].end_date)

    def test_unknown_date_must_be_resolved_and_becomes_open_after_confirmation(self):
        window = self.dialog([replace(self.event, start_date="", status="review")])
        emitted = []
        window.confirmed.connect(emitted.append)
        window.confirm_registration()
        self.assertEqual([], emitted)
        self.assertTrue(window.isVisible())
        self.assertIn("1행", window.error_label.text())
        window.table.item(0, window.DATE).setText("2026-11")
        window.confirm_registration()
        self.assertEqual("open", emitted[0][0].status)
        self.assertEqual("month", emitted[0][0].date_precision)

    def test_unchecked_invalid_candidate_is_not_registered(self):
        window = self.dialog([self.event, Event(title="날짜 없는 공지", source="memo.txt")])
        window.table.item(1, window.INCLUDE).setCheckState(Qt.Unchecked)
        selected = window.selected_events()
        self.assertEqual(1, len(selected))
        self.assertEqual(self.event.title, selected[0].title)
        window.table.item(0, window.INCLUDE).setCheckState(Qt.Unchecked)
        with self.assertRaisesRegex(ValueError, "하나 이상"):
            window.selected_events()

    def test_validation_checks_entire_selection_before_emitting(self):
        window = self.dialog([self.event, replace(self.event, title="행사", time="25:30")])
        emitted = []
        window.confirmed.connect(emitted.append)
        window.confirm_registration()
        self.assertEqual([], emitted)
        self.assertIn("2행", window.error_label.text())
        self.assertIn("HH:mm", window.error_label.text())
        window.table.item(1, window.TIME).setText("12:00")
        window.table.item(1, window.DATE).setText("2026-02-30")
        window.confirm_registration()
        self.assertEqual([], emitted)
        self.assertIn("올바른 날짜", window.error_label.text())
        window.table.item(1, window.DATE).setText("2026-02-03")
        window.table.item(1, window.TITLE).setText(" ")
        window.confirm_registration()
        self.assertEqual([], emitted)
        self.assertIn("제목", window.error_label.text())

    def test_empty_result_can_add_manual_event_and_delete_it(self):
        window = self.dialog([])
        self.assertIn("인식된 일정이 없습니다", window.summary_label.text())
        window.add_button.click()
        self.assertEqual(1, window.table.rowCount())
        window.table.item(0, window.TITLE).setText("직접 입력한 일정")
        window.table.item(0, window.DATE).setText("2026-12-01")
        self.assertEqual("직접 입력", window.selected_events()[0].source)
        window.delete_button.click()
        self.assertEqual(0, window.table.rowCount())
        with self.assertRaisesRegex(ValueError, "하나 이상"):
            window.selected_events()

    def test_append_results_and_switch_rows_preserve_edits_and_original_metadata(self):
        window = self.dialog(errors=["읽을 수 없는 파일: broken.pdf"])
        window.table.item(0, window.TITLE).setText("수정한 제목")
        window.notes_editor.setPlainText("첫 번째 행 수정")
        next_event = replace(self.event, title="두 번째 일정", source="other.txt", source_text="다른 원문")
        window.append_candidates([next_event], ["다른 파일 오류"])
        self.assertEqual("수정한 제목", window.table.item(0, window.TITLE).text())
        self.assertIn("broken.pdf", window.import_errors.text())
        self.assertIn("다른 파일 오류", window.import_errors.text())
        window.table.setCurrentCell(1, window.TITLE)
        self.assertEqual("다른 원문", window.source_text.toPlainText())
        window.notes_editor.setPlainText("두 번째 행 수정")
        selected = window.selected_events()
        self.assertEqual("첫 번째 행 수정", selected[0].notes)
        self.assertEqual("두 번째 행 수정", selected[1].notes)
        self.assertEqual("other.txt", selected[1].source)
        window.delete_current_event()
        self.assertEqual("첫 번째 행 수정", window.notes_editor.toPlainText())


if __name__ == "__main__":
    unittest.main()
