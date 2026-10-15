"""Calendar cell association needs geometric evidence before date inference."""
from datetime import date
import unittest
from unittest.mock import patch

from PIL import Image

from alimi.core import parse_schedule_text
from alimi.ocr import (OcrEngine, _TextFragment, _monthly_cell_text,
                       _weekly_cell_text)


def fragment(text, x, y, width=40, height=20):
    return _TextFragment(x, y, x + width, y + height, text)


def monthly_fixture(months=(10,), with_events=True, year=2026):
    fragments = []
    for panel, month in enumerate(months):
        left = 20 + panel * 800
        fragments.append(fragment(f"{year}년 {month}월" if year else f"{month}월",
                                  left + 190, 10, 200))
        for column, weekday in enumerate("일월화수목금토"):
            fragments.append(fragment(weekday, left + column * 100 + 40, 55, 20))
        first = (date(year or 2026, month, 1).weekday() + 1) % 7
        last = 31 if month == 10 else 30
        for day in range(1, last + 1):
            column, row = (first + day - 1) % 7, (first + day - 1) // 7
            x, y = left + column * 100, 90 + row * 100
            fragments.append(fragment(str(day), x + 10, y, 20))
            if with_events and day in (5, 9):
                fragments.append(fragment("회의" if day == 5 else "교육", x + 10, y + 34, 65))
                if day == 5:
                    fragments.append(fragment("14:00", x + 10, y + 61, 65))
    return fragments


def weekly_fixture():
    return [fragment("2026년 10월 주간 일정", 20, 10, 350)] + [
        fragment(f"{day}일", 30 + column * 180, 60, 50)
        for column, day in enumerate(range(19, 24))] + [
        fragment("독서 발표회", 570, 125, 130),
        fragment("15:00", 570, 165, 75),
        fragment("진로 체험", 750, 125, 130)]


def recognize(fragments, width=1700, height=800):
    engine = OcrEngine()
    with patch.object(engine, "_load"), patch.object(engine, "_read_fragments", return_value=fragments):
        return engine.recognize(Image.new("RGB", (width, height), "white"))


class CalendarCellLayoutTests(unittest.TestCase):
    def test_single_month_cells_keep_their_own_printed_dates_and_clock(self):
        text = recognize(monthly_fixture())
        events = parse_schedule_text(text, reference_date="2026-10-15")
        self.assertEqual([(e.start_date, e.time) for e in events],
                         [("2026-10-05", "14:00"), ("2026-10-09", None)])
        self.assertIn("회의", events[0].title)
        self.assertNotIn("교육", events[0].title)
        self.assertTrue(all(e.status == "open" and e.date_precision == "day" for e in events))

    def test_adjacent_months_do_not_mix_titles(self):
        events = parse_schedule_text(recognize(monthly_fixture((10, 11))), reference_date="2026-10-15")
        self.assertEqual([e.start_date for e in events],
                         ["2026-10-05", "2026-10-09", "2026-11-05", "2026-11-09"])

    def test_monthly_coordinates_can_be_scaled_and_shifted(self):
        values = monthly_fixture()
        moved = [_TextFragment(item.left * 1.6 + 15, item.top * 1.6 + 11,
                               item.right * 1.6 + 15, item.bottom * 1.6 + 11, item.text)
                 for item in values]
        self.assertEqual(recognize(values), recognize(moved, 2800, 1400))

    def test_missing_day_number_does_not_guess_a_cell_date(self):
        values = [item for item in monthly_fixture() if item.text != "8"]
        self.assertIsNone(_monthly_cell_text(values, 1700, 800))

    def test_duplicate_day_number_does_not_guess(self):
        values = monthly_fixture() + [fragment("1", 25, 700, 20)]
        self.assertIsNone(_monthly_cell_text(values, 1700, 800))

    def test_next_month_spillover_numbers_are_ambiguous(self):
        values = monthly_fixture() + [fragment("30", 30, 90, 20)]
        self.assertIsNone(_monthly_cell_text(values, 1700, 800))

    def test_missing_weekday_anchor_rejects_numeric_tables(self):
        values = [item for item in monthly_fixture() if item.text != "수"]
        self.assertIsNone(_monthly_cell_text(values, 1700, 800))

    def test_day_grid_must_follow_weekday_columns_in_order(self):
        values = monthly_fixture()
        day = next(item for item in values if item.text == "8")
        values[values.index(day)] = fragment(day.text, day.left - 100, day.top, 20)
        self.assertIsNone(_monthly_cell_text(values, 1700, 800))

    def test_explicit_year_and_month_must_match_printed_weekday_grid(self):
        values = monthly_fixture()
        values[0] = fragment("2025년 10월", values[0].left, values[0].top, 200)
        self.assertIsNone(_monthly_cell_text(values, 1700, 800))

    def test_title_banner_spanning_multiple_days_is_not_assigned_to_one_day(self):
        values = monthly_fixture()
        meeting = next(item for item in values if item.text == "회의")
        values[values.index(meeting)] = fragment(meeting.text, meeting.left, meeting.top, 230)
        self.assertIsNone(_monthly_cell_text(values, 1700, 800))

    def test_empty_month_calendar_does_not_create_a_monthly_reminder(self):
        self.assertEqual(recognize(monthly_fixture(with_events=False)), "")

    def test_list_outside_calendar_cells_is_retained_for_existing_layout_reader(self):
        values = monthly_fixture() + [fragment("22일 별도 일정 목록", 30, 700, 300)]
        self.assertIsNone(_monthly_cell_text(values, 1700, 800))

    def test_empty_neighboring_month_does_not_create_extra_event(self):
        values = monthly_fixture((10, 11))
        values = [item for item in values if not (item.left > 800 and item.text in ("회의", "교육", "14:00"))]
        events = parse_schedule_text(recognize(values), reference_date="2026-10-15")
        self.assertEqual(len(events), 2)
        self.assertTrue(all(e.start_date.startswith("2026-10") for e in events))

    def test_missing_year_is_reviewed_instead_of_silently_confirmed(self):
        events = parse_schedule_text(recognize(monthly_fixture(year=None)), reference_date="2026-10-15")
        self.assertEqual(len(events), 2)
        self.assertTrue(all(e.status == "review" for e in events))

    def test_weekly_explicit_day_columns_keep_title_and_time_together(self):
        events = parse_schedule_text(recognize(weekly_fixture()), reference_date="2026-10-15")
        self.assertEqual([(e.start_date, e.time) for e in events],
                         [("2026-10-22", "15:00"), ("2026-10-23", None)])
        self.assertIn("독서 발표회", events[0].title)
        self.assertNotIn("진로", events[0].title)

    def test_weekly_numbers_need_explicit_month_schedule_heading(self):
        values = weekly_fixture()
        values[0] = fragment("학생별 과제 계획", 20, 10, 300)
        self.assertIsNone(_weekly_cell_text(values, 1700, 800))

    def test_weekly_numbers_need_day_suffix(self):
        values = [_TextFragment(item.left, item.top, item.right, item.bottom, item.text.removesuffix("일"))
                  if item.text.endswith("일") else item for item in weekly_fixture()]
        self.assertIsNone(_weekly_cell_text(values, 1700, 800))

    def test_weekly_nonconsecutive_dates_are_not_inferred(self):
        values = weekly_fixture()
        value = next(item for item in values if item.text == "21일")
        values[values.index(value)] = fragment("25일", value.left, value.top, 50)
        self.assertIsNone(_weekly_cell_text(values, 1700, 800))

    def test_weekly_irregular_columns_are_not_inferred(self):
        values = weekly_fixture()
        value = next(item for item in values if item.text == "21일")
        values[values.index(value)] = fragment(value.text, value.left + 90, value.top, 50)
        self.assertIsNone(_weekly_cell_text(values, 1700, 800))

    def test_weekly_impossible_month_days_are_not_inferred(self):
        values = [fragment("2026년 2월 주간 일정", 20, 10, 350)] + [
            fragment(f"{day}일", 30 + column * 180, 60, 50) for column, day in enumerate(range(27, 30))]
        self.assertIsNone(_weekly_cell_text(values, 1700, 800))

    def test_weekly_empty_cells_and_clock_only_cells_are_not_events(self):
        values = [item for item in weekly_fixture() if item.text not in ("독서 발표회", "진로 체험")]
        self.assertEqual(recognize(values), "")

    def test_weekly_title_banner_spanning_columns_is_not_assigned_to_one_day(self):
        values = weekly_fixture()
        value = next(item for item in values if item.text == "독서 발표회")
        values[values.index(value)] = fragment(value.text, value.left, value.top, 350)
        self.assertIsNone(_weekly_cell_text(values, 1700, 800))

    def test_weekly_text_outside_day_columns_keeps_original_reading_path(self):
        values = weekly_fixture() + [fragment("별도 안내", 1500, 200, 150)]
        self.assertIsNone(_weekly_cell_text(values, 1700, 800))


if __name__ == "__main__":
    unittest.main()
