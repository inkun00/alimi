"""Deadline color boundaries and painted desktop reminders."""

import os
import unittest
from dataclasses import asdict, replace
from datetime import date, timedelta
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# The production launcher initializes these DLLs before creating Qt surfaces.
import onnxruntime

onnxruntime.disable_telemetry_events()

from PyQt5.QtCore import QEvent, Qt
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import QApplication

from alimi.core import Event
from alimi.desktop import DesktopListWindow, NoteWindow
from alimi.urgency import URGENCY_PALETTES, event_urgency


def luminance(hex_color):
    color = QColor(hex_color)
    values = [color.redF(), color.greenF(), color.blueF()]
    linear = [value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4
              for value in values]
    return sum(value * weight for value, weight in zip(linear, (.2126, .7152, .0722)))


class UrgencyTests(unittest.TestCase):
    def setUp(self):
        self.today = date(2026, 10, 14)
        self.event = Event(id=7, title="교직원 회의", start_date="2026-10-17")

    def test_three_days_before_through_overdue_have_distinct_expected_colors(self):
        expected = [(4, "normal", "#fff0ad"), (3, "three_days", "#ffe08a"),
                    (2, "two_days", "#ffc75f"), (1, "one_day", "#ffad3d"),
                    (0, "due", "#ff8d3a"), (-1, "due", "#ff8d3a")]
        for days, code, color in expected:
            with self.subTest(days=days):
                event = replace(self.event, start_date=(self.today + timedelta(days=days)).isoformat())
                urgency = event_urgency(event, self.today)
                self.assertEqual(code, urgency.code)
                self.assertEqual(color, urgency.background)
                self.assertEqual(days, urgency.days_remaining)
        self.assertEqual("오늘 마감", event_urgency(self.event, date(2026, 10, 17)).label)
        self.assertEqual("기한 지남", event_urgency(self.event, date(2026, 10, 18)).label)

    def test_date_ranges_use_the_actual_deadline_not_the_start(self):
        event = replace(self.event, start_date="2026-10-01", end_date="2026-10-17")
        self.assertEqual("three_days", event_urgency(event, self.today).code)
        self.assertEqual("due", event_urgency(event, date(2026, 10, 17)).code)

    def test_month_schedules_never_invent_deadlines(self):
        event = replace(self.event, date_precision="month", start_date="2026-10-01",
                        end_date="2026-10-31")
        for day in (date(2026, 9, 28), date(2026, 10, 1), date(2026, 10, 28),
                    date(2026, 10, 31), date(2026, 11, 1)):
            with self.subTest(day=day):
                urgency = event_urgency(event, day)
                self.assertEqual("month", urgency.code)
                self.assertEqual("#fff0ad", urgency.background)
                self.assertIsNone(urgency.days_remaining)

    def test_missing_and_invalid_dates_stay_neutral(self):
        for start, end in ((None, None), ("bad", "bad"), ("2026-02-30", None)):
            urgency = event_urgency(replace(self.event, start_date=start, end_date=end), self.today)
            self.assertEqual("undated", urgency.code)
            self.assertEqual("#fff0ad", urgency.background)
        fallback = replace(self.event, end_date="bad")
        self.assertEqual("three_days", event_urgency(fallback, self.today).code)

    def test_leap_year_and_year_rollover_use_calendar_days(self):
        leap = replace(self.event, start_date="2024-03-01")
        self.assertEqual(1, event_urgency(leap, date(2024, 2, 29)).days_remaining)
        new_year = replace(self.event, start_date="2027-01-02")
        self.assertEqual("three_days", event_urgency(new_year, date(2026, 12, 30)).code)

    def test_default_clock_and_clock_rollback_refresh_without_mutating_event(self):
        class FixedDate(date):
            @classmethod
            def today(cls):
                return cls(2026, 10, 17)

        original = asdict(self.event)
        with patch("alimi.urgency.date", FixedDate):
            self.assertEqual("due", event_urgency(self.event).code)
        self.assertEqual("three_days", event_urgency(self.event, self.today).code)
        self.assertEqual(original, asdict(self.event))

    def test_progressive_palette_darkens_and_all_text_has_usable_contrast(self):
        lights = [luminance(state.background) for state in URGENCY_PALETTES]
        self.assertEqual(sorted(lights, reverse=True), lights)
        for state in URGENCY_PALETTES:
            ratio = (luminance(state.background) + .05) / (luminance(state.foreground) + .05)
            self.assertGreaterEqual(ratio, 4.5, state.code)


class UrgencyWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.windows = []
        self.app.setProperty("shutting_down", False)
        self.event = Event(id=9, title="교직원 회의", start_date="2026-10-17", notes="회의 안건")

    def tearDown(self):
        for window in self.windows:
            window.finish()
        self.app.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()

    def show(self, window):
        self.windows.append(window)
        window.show()
        self.app.processEvents()
        return window

    def test_note_painted_colors_refresh_without_geometry_or_expansion_changes(self):
        note = self.show(NoteWindow(self.event))
        note.move(70, 80)
        note.set_expanded(True)
        self.app.processEvents()
        geometry = note.geometry()
        original_title = note.title_label.text()
        original_date = note.date_label.text()
        for day, code, color in ((13, "normal", "#fff0ad"), (14, "three_days", "#ffe08a"),
                                 (15, "two_days", "#ffc75f"), (16, "one_day", "#ffad3d"),
                                 (17, "due", "#ff8d3a"), (18, "due", "#ff8d3a"),
                                 (14, "three_days", "#ffe08a")):
            with self.subTest(day=day):
                note.update_event(self.event, date(2026, 10, day))
                self.app.processEvents()
                self.assertEqual(code, note.property("urgency"))
                self.assertEqual(color, note.grab().toImage().pixelColor(5, 5).name())
                self.assertEqual(geometry, note.geometry())
                self.assertEqual(original_title, note.title_label.text())
                self.assertEqual(original_date, note.date_label.text())
                self.assertTrue(note.expanded)
                self.assertFalse(note.windowHandle().flags() & Qt.WindowStaysOnBottomHint)
                self.assertTrue(note.windowHandle().flags() & Qt.WindowStaysOnTopHint)

    def test_same_stage_refresh_does_not_repolish_again(self):
        note = self.show(NoteWindow(self.event))
        note.update_event(self.event, date(2026, 10, 14))
        with patch.object(type(note.style()), "polish", wraps=note.style().polish) as polish:
            note.update_event(self.event, date(2026, 10, 14))
            self.assertEqual(0, polish.call_count)

    def test_note_month_replaces_orange_with_neutral_without_day_label(self):
        note = self.show(NoteWindow(self.event))
        note.update_event(self.event, date(2026, 10, 17))
        monthly = replace(self.event, date_precision="month", start_date="2026-10-01", end_date="2026-10-31")
        note.update_event(monthly, date(2026, 10, 31))
        self.app.processEvents()
        self.assertEqual("month", note.urgency_state.code)
        self.assertEqual("normal", note.property("urgency"))
        self.assertEqual("10월", note.date_label.text())
        self.assertEqual("#fff0ad", note.grab().toImage().pixelColor(5, 5).name())

    def test_list_rows_match_palette_keep_titles_and_selected_detail(self):
        monthly = replace(self.event, id=10, title="10월 평가", date_precision="month",
                          start_date="2026-10-01", end_date="2026-10-31")
        events = [self.event, monthly]
        window = self.show(DesktopListWindow(events))
        window.set_selected_event(9)
        for day, color in ((14, "#ffe08a"), (15, "#ffc75f"), (16, "#ffad3d"),
                           (17, "#ff8d3a"), (18, "#ff8d3a")):
            window.update_events(events, date(2026, 10, day))
            self.assertEqual(color, window.list_widget.item(0).background().color().name())
            self.assertEqual("#fff0ad", window.list_widget.item(1).background().color().name())
            self.assertEqual(self.event.title, window.list_widget.item(0).text())
            self.assertEqual(9, window.selected_event_id)
            self.assertEqual(9, window.list_widget.currentItem().data(Qt.UserRole))
            self.assertTrue(window.details_panel.isVisible())
            self.assertIn("회의 안건", window.details_label.text())
        self.assertEqual("기한 지남", window.list_widget.item(0).data(Qt.AccessibleDescriptionRole))

    def test_list_unselected_rows_actually_paint_the_deadline_color(self):
        window = self.show(DesktopListWindow([self.event]))
        for day, color in ((14, "#ffe08a"), (15, "#ffc75f"), (16, "#ffad3d"), (17, "#ff8d3a")):
            window.update_events([self.event], date(2026, 10, day))
            window.list_widget.setCurrentRow(-1)
            window.list_widget.clearSelection()
            self.app.processEvents()
            rectangle = window.list_widget.visualItemRect(window.list_widget.item(0))
            actual = window.list_widget.viewport().grab().toImage().pixelColor(
                rectangle.right() - 4, rectangle.center().y()).name()
            self.assertEqual(color, actual)

    def test_list_selected_rows_keep_deadline_color_with_a_dark_selection_edge(self):
        window = self.show(DesktopListWindow([self.event]))
        for day, color in ((14, "#ffe08a"), (15, "#ffc75f"), (16, "#ffad3d"), (17, "#ff8d3a")):
            window.update_events([self.event], date(2026, 10, day))
            window.set_selected_event(9)
            self.app.processEvents()
            rectangle = window.list_widget.visualItemRect(window.list_widget.item(0))
            pixels = window.list_widget.viewport().grab().toImage()
            self.assertEqual(color, pixels.pixelColor(rectangle.right() - 5, rectangle.center().y()).name())
            self.assertEqual("#493e1e", pixels.pixelColor(rectangle.left() + 1, rectangle.center().y()).name())
            self.assertTrue(window.list_widget.item(0).isSelected())


if __name__ == "__main__":
    unittest.main()
