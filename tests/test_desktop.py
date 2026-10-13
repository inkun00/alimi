"""Check desktop title/detail behavior without displaying real user windows."""

import os
import unittest
from dataclasses import replace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Keep the same Windows DLL initialization order as the production entry point.
import onnxruntime

onnxruntime.disable_telemetry_events()

from PyQt5.QtCore import QEvent, QPoint, Qt
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication

from alimi.core import Event
from alimi.desktop import DesktopListWindow, NoteWindow, event_details, format_event_deadline


class DesktopReminderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.app.setProperty("shutting_down", False)
        self.windows = []
        self.event = Event(
            id=12, title="과학 · 생명 · 생물 요소들의 관계를 조사하여 먹이그물로 나타내기",
            start_date="2026-09-01", end_date="2026-09-30", date_precision="month",
            place="과학실", notes="평가내용: " + "원문 내용을 모두 보존합니다. " * 80,
            source="평가 계획.pdf",
        )

    def tearDown(self):
        for window in self.windows:
            window.finish()
        self.app.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()
        self.app.setProperty("shutting_down", False)

    def show(self, window):
        self.windows.append(window)
        window.show()
        self.app.processEvents()
        return window

    def test_note_stays_below_windows_and_starts_with_title_only(self):
        note = self.show(NoteWindow(self.event))
        self.assertIsNone(note.parent())
        self.assertTrue(note.windowFlags() & Qt.WindowStaysOnBottomHint)
        self.assertFalse(note.windowFlags() & Qt.WindowStaysOnTopHint)
        self.assertFalse(note.windowFlags() & Qt.WindowCloseButtonHint)
        self.assertFalse(note.expanded)
        self.assertFalse(note.details_scroll.isVisible())
        self.assertFalse(note.complete_button.isVisible())
        self.assertLess(note.height(), 105)
        self.assertEqual(self.event.title, note.title_label.accessibleName())

    def test_deadline_uses_range_end_and_real_korean_weekdays(self):
        event = replace(self.event, date_precision="day", start_date="2026-10-13", end_date=None)
        self.assertEqual("10/13(화)", format_event_deadline(event))
        self.assertEqual("2026-10-13(화)", format_event_deadline(event, include_year=True))
        self.assertEqual("10/14(수)", format_event_deadline(replace(event, end_date="2026-10-14")))
        self.assertEqual("2/29(목)", format_event_deadline(replace(event, start_date="2024-02-29")))
        for day, weekday in ((12, "월"), (13, "화"), (14, "수"), (15, "목"), (16, "금"), (17, "토"), (18, "일")):
            self.assertEqual(f"10/{day}({weekday})", format_event_deadline(replace(event, start_date=f"2026-10-{day}")))

    def test_month_deadline_preserves_month_precision(self):
        self.assertEqual("9월", format_event_deadline(self.event))
        self.assertEqual("2026년 9월", format_event_deadline(self.event, include_year=True))
        self.assertEqual("날짜 미정", format_event_deadline(replace(self.event, start_date=None, end_date=None)))
        note = self.show(NoteWindow(self.event))
        self.assertEqual("9월", note.date_label.text())
        self.assertEqual("2026년 9월", note.date_label.toolTip())
        self.assertNotIn("요일", note.date_label.toolTip())

    def test_deadline_is_left_of_title_and_refreshes_without_clipping(self):
        note = self.show(NoteWindow(replace(self.event, date_precision="day", end_date="2026-12-31")))
        self.assertEqual("12/31(목)", note.date_label.text())
        self.assertLess(note.date_label.geometry().right(), note.title_label.geometry().left())
        self.assertGreaterEqual(note.title_label.width(), 150)
        self.assertEqual(self.event.title, note.title_label.accessibleName())
        self.assertEqual(self.event.title, note.title_label.full_title)
        for size in (11, 12, 14, 16, 18):
            note.set_font_size(size)
            self.app.processEvents()
            metrics = note.date_label.fontMetrics()
            self.assertLessEqual(metrics.horizontalAdvance(note.date_label.text()), note.date_label.width())
            self.assertLess(note.date_label.geometry().right(), note.title_label.geometry().left())
            self.assertLess(note.title_label.geometry().right(), note.handle.geometry().left())
            self.assertTrue(note.rect().contains(note.date_label.geometry()))
            self.assertTrue(note.rect().contains(note.title_label.geometry()))
            self.assertTrue(note.rect().contains(note.handle.geometry()))
            self.assertLess(note.height(), 105)
        note.set_expanded(True)
        note.update_event(replace(self.event, title="변경된 일정", date_precision="day", end_date="2026-10-14"))
        self.app.processEvents()
        self.assertEqual("10/14(수)", note.date_label.text())
        self.assertEqual("2026-10-14(수)", note.date_label.toolTip())
        self.assertEqual("변경된 일정", note.title_label.full_title)
        self.assertTrue(note.expanded)
        note.update_event(self.event)
        self.assertEqual("9월", note.date_label.text())

    def test_note_title_click_and_keyboard_toggle_full_scrollable_details(self):
        note = self.show(NoteWindow(self.event))
        QTest.mouseClick(note.title_label, Qt.LeftButton)
        self.app.processEvents()
        self.assertTrue(note.expanded)
        self.assertTrue(note.complete_button.isVisible())
        self.assertIn(self.event.title, note.details_label.text())
        self.assertIn(self.event.notes, note.details_label.text())
        self.assertIn("2026년 9월", note.details_label.text())
        self.assertNotIn("2026-09-01", note.details_label.text())
        self.assertNotIn("2026-09-30", note.details_label.text())
        self.assertGreater(note.details_scroll.verticalScrollBar().maximum(), 0)
        QTest.keyClick(note.title_label, Qt.Key_Space)
        self.assertFalse(note.expanded)
        self.assertFalse(note.details_scroll.isVisible())

    def test_note_refresh_preserves_expansion_and_complete_waits_for_store_owner(self):
        note = self.show(NoteWindow(self.event))
        note.set_expanded(True)
        emitted = []
        note.completed.connect(emitted.append)
        updated = replace(self.event, notes="새 세부 내용")
        note.update_event(updated)
        self.assertTrue(note.expanded)
        self.assertIn("새 세부 내용", note.details_label.text())
        note.complete_button.click()
        self.assertEqual([12], emitted)
        self.assertTrue(note.isVisible(), "The store owner must commit completion before removing a note.")

    def test_expanded_note_keeps_foreground_during_refresh_and_restores_desktop_layer(self):
        note = self.show(NoteWindow(self.event))
        original_handle = int(note.winId())
        original_position = note.pos()
        with (
            patch("alimi.desktop._native_windows_surface", return_value=True),
            patch("alimi.desktop._native_is_topmost", return_value=True) as is_topmost,
            patch("alimi.desktop._native_set_z_order") as set_order,
            patch("alimi.desktop._above_desktop_insert_after", return_value=None),
        ):
            note.set_expanded(True)
            self.assertTrue(note.expanded)
            self.assertTrue(note.details_scroll.isVisible())
            self.assertEqual((original_handle, -1), set_order.call_args.args)
            self.assertEqual(original_handle, int(note.winId()))
            self.assertEqual(original_position, note.pos())
            self.assertTrue(note.isVisible())
            self.assertTrue(note.windowHandle().flags() & Qt.WindowStaysOnTopHint)
            self.assertFalse(note.windowHandle().flags() & Qt.WindowStaysOnBottomHint)

            # The manager's periodic refresh, alignment callback and queued
            # activation must not demote the card or reorder expanded cards.
            set_order.reset_mock()
            note.update_event(replace(self.event, notes="갱신된 세부 내용"))
            note.keep_below_windows()
            note._after_activation()
            self.app.processEvents()
            self.assertTrue(note.expanded)
            self.assertIn("갱신된 세부 내용", note.details_label.text())
            self.assertEqual(original_handle, int(note.winId()))
            set_order.assert_not_called()

            # Recover the expanded layer if something changes the native style.
            is_topmost.return_value = False
            note.keep_below_windows()
            self.assertEqual((original_handle, -1), set_order.call_args.args)

            set_order.reset_mock()
            note.set_expanded(False)
            self.assertFalse(note.details_scroll.isVisible())
            self.assertFalse(note.complete_button.isVisible())
            self.assertEqual(original_handle, int(note.winId()))
            self.assertEqual(original_position, note.pos())
            self.assertTrue(note.isVisible())
            self.assertTrue(note.windowHandle().flags() & Qt.WindowStaysOnBottomHint)
            self.assertFalse(note.windowHandle().flags() & Qt.WindowStaysOnTopHint)
            layers = [call.args[1] for call in set_order.call_args_list]
            self.assertIn(-2, layers, "Collapse must remove native topmost status.")
            self.assertIn(1, layers, "The compact card returns to the desktop layer.")
            self.assertNotIn(-1, layers)

    def test_both_reminder_modes_block_close_until_finish_or_shutdown(self):
        for window in (NoteWindow(self.event), DesktopListWindow([self.event])):
            self.show(window)
            self.assertFalse(window.close())
            self.assertTrue(window.isVisible())
            self.app.setProperty("shutting_down", True)
            self.assertTrue(window.close())
            self.assertFalse(window.isVisible())
            self.app.setProperty("shutting_down", False)

    def test_list_shows_titles_and_details_only_after_click(self):
        window = self.show(DesktopListWindow([self.event, replace(self.event, id=13, title="음악 평가")]))
        self.assertTrue(window.windowFlags() & Qt.WindowStaysOnBottomHint)
        self.assertFalse(window.windowFlags() & Qt.WindowStaysOnTopHint)
        self.assertFalse(window.details_panel.isVisible())
        self.assertIsNone(window.selected_event_id)
        self.assertEqual(self.event.title, window.list_widget.item(0).text())
        rectangle = window.list_widget.visualItemRect(window.list_widget.item(0))
        QTest.mouseClick(window.list_widget.viewport(), Qt.LeftButton, pos=rectangle.center())
        self.assertEqual(12, window.selected_event_id)
        self.assertTrue(window.details_panel.isVisible())
        self.assertIn("2026년 9월", window.details_label.text())
        self.assertIn(self.event.notes, window.details_label.text())

    def test_list_refresh_preserves_selection_scroll_and_full_details(self):
        events = [replace(self.event, id=index, title=f"일정 {index}") for index in range(1, 61)]
        window = self.show(DesktopListWindow(events))
        window.set_selected_event(30)
        self.app.processEvents()
        scroll = window.list_widget.verticalScrollBar()
        self.assertGreater(scroll.maximum(), 0)
        scroll.setValue(scroll.maximum() // 2)
        previous = scroll.value()
        updates = [replace(event, notes="변경된 원문") if event.id == 30 else event for event in events]
        window.update_events(updates)
        self.app.processEvents()
        self.assertEqual(30, window.selected_event_id)
        self.assertEqual(30, window.list_widget.currentItem().data(Qt.UserRole))
        self.assertTrue(window.details_panel.isVisible())
        self.assertEqual(previous, scroll.value())
        self.assertIn("변경된 원문", window.details_label.text())

    def test_list_detail_divider_is_dark_and_separates_the_visible_panels(self):
        window = self.show(DesktopListWindow([self.event]))
        self.assertFalse(window.details_divider.isVisible())
        window.set_selected_event(12)
        self.app.processEvents()
        divider = window.details_divider
        self.assertTrue(divider.isVisible())
        self.assertEqual(3, divider.height())
        self.assertEqual(window.list_widget.width(), divider.width())
        list_bottom = window.list_widget.mapTo(window, QPoint(0, window.list_widget.height())).y()
        divider_top = divider.mapTo(window, QPoint()).y()
        details_top = window.details_scroll.mapTo(window, QPoint()).y()
        self.assertLess(list_bottom, divider_top)
        self.assertLess(divider_top + divider.height(), details_top)
        image = divider.grab().toImage()
        for x in (0, image.width() // 2, image.width() - 1):
            self.assertEqual("#493e1e", image.pixelColor(x, 1).name())
            self.assertEqual(255, image.pixelColor(x, 1).alpha())
        window.update_events([])
        self.app.processEvents()
        self.assertFalse(divider.isVisible())

    def test_list_keyboard_activation_completion_and_removal_do_not_select_another_event(self):
        window = self.show(DesktopListWindow([self.event, replace(self.event, id=13, title="음악 평가")]))
        window.list_widget.setCurrentRow(1)
        self.assertFalse(window.details_panel.isVisible())
        QTest.keyClick(window.list_widget, Qt.Key_Return)
        self.assertEqual(13, window.selected_event_id)
        emitted = []
        window.completed.connect(emitted.append)
        window.complete_button.click()
        self.assertEqual([13], emitted)
        self.assertTrue(window.isVisible())
        window.update_events([self.event])
        self.app.processEvents()
        self.assertIsNone(window.selected_event_id)
        self.assertFalse(window.details_panel.isVisible())
        self.assertEqual("", window.details_label.text())

    def test_exact_day_details_keep_the_real_day_and_time(self):
        text = event_details(replace(self.event, date_precision="day", end_date=None, time="15:30"))
        self.assertIn("2026-09-01 · 15:30", text)
        self.assertIn("과학실", text)
        self.assertIn("평가 계획.pdf", text)


if __name__ == "__main__":
    unittest.main()
