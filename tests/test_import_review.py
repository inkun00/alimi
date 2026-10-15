"""Validate reviewed imports without ever opening application storage."""

import os
import unittest
from dataclasses import replace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Match the production DLL initialization order on Windows.
import onnxruntime

onnxruntime.disable_telemetry_events()

from PyQt5.QtCore import QEvent, Qt
from PyQt5.QtWidgets import QApplication, QDialog, QLabel, QTabWidget

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

    def dialog(self, candidates=None, errors=None, *, recognized_text=None):
        window = ImportReviewDialog([self.event] if candidates is None else candidates, errors,
                                    recognized_text=recognized_text)
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

    def test_source_metadata_is_hidden_while_original_text_is_preserved(self):
        window = self.dialog()
        labels = [label.text() for label in window.findChildren(QLabel)]
        self.assertFalse(any("출처:" in text or self.event.source in text for text in labels))
        self.assertFalse(window.event_status_label.isVisible())
        self.assertEqual(self.event.source_text, window.source_text.toPlainText())
        event = window.selected_events()[0]
        self.assertEqual((self.event.source, self.event.source_text), (event.source, event.source_text))

    def test_existing_event_hint_keeps_completion_status_without_source_metadata(self):
        window = self.dialog([replace(self.event, id=17, status="done")])
        self.assertTrue(window.event_status_label.isVisible())
        self.assertEqual("기존 완료 일정 (수정해도 완료 상태 유지)", window.event_status_label.text())
        window.append_candidates([replace(self.event, id=18, status="open")])
        window.table.setCurrentCell(1, window.TITLE)
        self.assertEqual("기존 일정 수정", window.event_status_label.text())
        self.assertNotIn(self.event.source, window.event_status_label.text())

    def test_full_recognized_text_is_the_default_editable_tab(self):
        full_text = "가정통신문\n인식한 안내 문구입니다.\n2026년 10월 13일 교직원 회의\n회신은 담당자에게 전달하세요."
        window = self.dialog(recognized_text=full_text)
        tabs = window.findChild(QTabWidget)
        self.assertEqual("인식한 텍스트", tabs.tabText(tabs.currentIndex()))
        self.assertTrue(window.recognized_editor.isVisible())
        self.assertFalse(window.recognized_editor.isReadOnly())
        self.assertEqual(full_text, window.recognized_editor.toPlainText())
        self.assertEqual("원래 공지 원문", window.source_text.toPlainText())
        self.assertEqual("일정 원문 (참고)", tabs.tabText(tabs.indexOf(window.source_text)))

    def test_recognized_body_edits_do_not_change_schedules_or_input_events(self):
        existing = replace(self.event, id=17, status="done", completed_at="2026-10-14T00:00:00Z")
        original = replace(existing)
        window = self.dialog([existing], recognized_text="날짜 없는 전체 안내문")
        selected_before = window.selected_events()
        changed = []
        window.recognized_text_changed.connect(changed.append)
        window.recognized_editor.selectAll()
        window.recognized_editor.insertPlainText("일정 이외 안내문과 오타를 수정했습니다.")
        self.assertEqual(["일정 이외 안내문과 오타를 수정했습니다."], changed)
        self.assertEqual(selected_before, window.selected_events())
        self.assertIsNone(window.selected_events()[0].id, "Body edits must not request an existing schedule update.")
        self.assertEqual(original, existing)
        self.assertEqual(original.source_text, window.source_text.toPlainText())

    def test_empty_schedule_result_keeps_all_recognized_content_editable(self):
        body = "안내드립니다.\n별도의 일정 없이 참고만 해주세요."
        window = self.dialog([], recognized_text=body)
        changed, confirmed = [], []
        window.recognized_text_changed.connect(changed.append)
        window.confirmed.connect(confirmed.append)
        self.assertTrue(window.recognized_editor.isEnabled())
        self.assertEqual(body, window.recognized_editor.toPlainText())
        window.recognized_editor.moveCursor(window.recognized_editor.textCursor().End)
        window.recognized_editor.insertPlainText("\n추가 안내입니다.")
        self.assertEqual([body + "\n추가 안내입니다."], changed)
        self.assertEqual(0, window.table.rowCount())
        window.register_button.click()
        self.assertEqual([], confirmed)
        self.assertEqual(0, window.table.rowCount())

    def test_recognized_body_is_preserved_when_rows_switch_append_and_delete(self):
        window = self.dialog(recognized_text="전체 원문")
        changed = []
        window.recognized_text_changed.connect(changed.append)
        window.recognized_editor.setPlainText("수정한 전체 원문")
        window.append_candidates([replace(self.event, title="두 번째 일정", source_text="두 번째 행 원문")])
        window.table.setCurrentCell(1, window.TITLE)
        self.assertEqual("두 번째 행 원문", window.source_text.toPlainText())
        window.table.setCurrentCell(0, window.TITLE)
        window.delete_current_event()
        window.delete_current_event()
        self.assertEqual(0, window.table.rowCount())
        self.assertEqual("수정한 전체 원문", window.recognized_editor.toPlainText())
        self.assertTrue(window.recognized_editor.isEnabled())
        self.assertEqual(["수정한 전체 원문"], changed)

    def test_recognized_fallback_deduplicates_sources_from_one_shot_iterable(self):
        events = (event for event in [self.event, replace(self.event, title="같은 자료"),
                  replace(self.event, title="다른 자료", source_text="두 번째 원문")])
        window = self.dialog(events)
        self.assertEqual(3, window.table.rowCount())
        self.assertEqual("원래 공지 원문\n\n두 번째 원문", window.recognized_editor.toPlainText())
        explicit_empty = self.dialog([self.event], recognized_text="")
        self.assertEqual("", explicit_empty.recognized_editor.toPlainText())

    def test_cancel_keeps_recognized_draft_edit_signal_without_registering_schedules(self):
        window = self.dialog(recognized_text="인식한 안내문")
        changed, confirmed = [], []
        window.recognized_text_changed.connect(changed.append)
        window.confirmed.connect(confirmed.append)
        window.recognized_editor.setPlainText("고친 안내문")
        window.table.item(0, window.TITLE).setText("저장하지 않을 일정 수정")
        window.cancel_button.click()
        self.assertEqual(["고친 안내문"], changed)
        self.assertEqual([], confirmed)
        self.assertEqual([], window.confirmed_events)
        self.assertEqual(QDialog.Rejected, window.result())
        self.assertEqual("교직원 회의", self.event.title)
        self.assertEqual("원래 공지 원문", self.event.source_text)

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
