"""Regression cases discovered by the independent document-format audit."""
import unittest
import csv
import tempfile
from datetime import date
from pathlib import Path

from alimi.core import parse_schedule_text
from alimi.importers import extract_file_text


class FormatParserTests(unittest.TestCase):
    def parse(self, text):
        return parse_schedule_text(text, reference_date=date(2026, 10, 15))

    def parse_table_file(self, suffix, row):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            path = Path(directory) / ("periods" + suffix)
            rows = [("시작일", "종료일", "내용"), row]
            if suffix == ".csv":
                with path.open("w", encoding="utf-8", newline="") as stream:
                    csv.writer(stream).writerows(rows)
            else:
                from openpyxl import Workbook
                workbook = Workbook()
                workbook.active.title = "기간표"
                for values in rows:
                    workbook.active.append(values)
                workbook.save(path)
                workbook.close()
            return self.parse(extract_file_text(path))

    def test_shortened_period_ends_and_year_rollover(self):
        for text, start, end in (
            ("2026.10.20.~23. 도서관 행사", "2026-10-20", "2026-10-23"),
            ("2026년 10월 20일~23일 독서 주간", "2026-10-20", "2026-10-23"),
            ("2026년 10월 22일부터24일까지 독서 주간", "2026-10-22", "2026-10-24"),
            ("2026.12.28.~1.3. 겨울 캠프", "2026-12-28", "2027-01-03"),
        ):
            with self.subTest(text=text):
                events = self.parse(text)
                self.assertEqual(1, len(events))
                self.assertEqual((start, end, "open"), (events[0].start_date, events[0].end_date, events[0].status))

    def test_explicit_reversed_or_impossible_end_stays_review(self):
        for text in ("2026.12.28.~2026.1.3. 겨울 캠프", "2026.10.22.~20. 상담", "2026.02.20.~30. 상담"):
            with self.subTest(text=text):
                event, = self.parse(text)
                self.assertEqual("review", event.status)
                self.assertIsNone(event.end_date)

    def test_unreadable_range_end_is_not_silently_one_day(self):
        event, = self.parse("2026.10.22.~미정 독서 캠프")
        self.assertEqual("review", event.status)
        self.assertIn("종료 날짜", event.notes)

    def test_explicit_range_end_weekday_is_checked(self):
        event, = self.parse("2026.10.22.(목)~2026.10.24.(일) 독서 캠프")
        self.assertEqual("2026-10-24", event.end_date)
        self.assertEqual("review", event.status)
        self.assertIn("종료 날짜와 요일", event.notes)

    def test_short_range_end_weekday_is_checked(self):
        event, = self.parse("2026.10.22.(목)~24(일) 독서 캠프")
        self.assertEqual("2026-10-24", event.end_date)
        self.assertEqual("review", event.status)

    def test_multiple_inline_events_do_not_share_first_time(self):
        first, second = self.parse("2026.10.22. 09:00 회의 / 2026.10.23. 14:00 연수")
        self.assertEqual(("2026-10-22", "09:00"), (first.start_date, first.time))
        self.assertEqual(("2026-10-23", "14:00"), (second.start_date, second.time))
        self.assertNotIn("회의", second.title)
        self.assertIn("연수", second.title)

    def test_descriptive_units_are_not_dates_or_clock_times(self):
        for detail in ("3시간", "2.5시간", "12.5%", "3.2kg", "24명, 3~5학년"):
            with self.subTest(detail=detail):
                event, = self.parse(f"2026.10.22. 진로 체험 ({detail})")
                self.assertEqual(("2026-10-22", None, "open"), (event.start_date, event.time, event.status))
                self.assertIn(detail, event.title)

    def test_valid_clock_followed_by_title_starting_with_gan_still_works(self):
        event, = self.parse("2026.10.22. 오후 3시 간담회")
        self.assertEqual("15:00", event.time)

    def test_date_before_korean_title_is_not_mistaken_for_quantity(self):
        for title in ("원어민 수업", "명상 체험", "점심 간담회"):
            with self.subTest(title=title):
                event, = self.parse(f"10.22 {title}")
                self.assertEqual("2026-10-22", event.start_date)
                self.assertIn(title, event.title)

    def test_invalid_times_require_review_in_all_date_modes(self):
        for text in ("2026.10.22. 25:70 운영 회의", "2026.10.22. 오전 0시 회의",
                     "2026년 11월 25:70 안전 점검", "2026년\n10월\n22 25:70 운영 회의"):
            with self.subTest(text=text):
                event, = self.parse(text)
                self.assertEqual("review", event.status)
                self.assertIsNone(event.time)
                self.assertIn("시간이 올바르지", event.notes)

    def test_month_document_headings_do_not_become_extra_month_events(self):
        for text in ("2026년 11월 업무 일정\n2026-11-02 교직원 회의\n2026-11-04 학부모 상담",
                     "[11월 일정]\n날짜\t제목\n2026-11-02\t교직원 회의\n2026-11-04\t학부모 상담"):
            with self.subTest(text=text):
                events = self.parse(text)
                self.assertEqual(["2026-11-02", "2026-11-04"], [event.start_date for event in events])
                self.assertTrue(all(event.date_precision == "day" for event in events))

    def test_horizontal_month_headings_are_not_schedules(self):
        events = self.parse("2026년 2학기 행사\n10월 일정  11월 일정\n10월 22일 독서 발표회  11월 2일 안전 교육\n10월 23일 진로 체험  11월 3일 소방 훈련")
        self.assertEqual(["2026-10-22", "2026-11-02", "2026-10-23", "2026-11-03"], [event.start_date for event in events])
        self.assertTrue(all(event.date_precision == "day" and event.status == "review" for event in events))

    def test_real_month_reminders_are_preserved(self):
        for text in ("2026년 11월 진로 상담\n2026.12.03. 독서 발표회", "2026년 11월 업무 일정"):
            with self.subTest(text=text):
                events = self.parse(text)
                self.assertEqual("month", events[0].date_precision)
                self.assertEqual("2026-11-01", events[0].start_date)

    def test_date_before_title_and_unlabelled_time(self):
        event, = self.parse("학교 일정 알림\n2026년 10월 22일\n학부모 상담\n오후3시\n장소:회의실")
        self.assertEqual(("학부모 상담", "2026-10-22", "15:00", "회의실"), (event.title, event.start_date, event.time, event.place))

    def test_following_instruction_does_not_replace_real_prior_title(self):
        event, = self.parse("교직원 협의회\n2026.10.22.\n참석 시 마스크 착용")
        self.assertEqual("교직원 협의회", event.title)

    def test_relative_dates_remain_review_without_assumed_source_date(self):
        for text in ("내일 오후 2시 서류 제출", "다음 주 월요일 오전 10시 학년 회의"):
            with self.subTest(text=text):
                event, = self.parse(text)
                self.assertEqual(("", "review"), (event.start_date, event.status))

    def test_start_end_table_headers_make_one_period_per_row(self):
        for separator in ("\t", " | ", "  "):
            with self.subTest(separator=repr(separator)):
                text = "\n".join(separator.join(row) for row in (
                    ("시작일", "종료일", "내용"),
                    ("2026-11-02", "2026-11-06", "진로 캠프"),
                    ("2026-12-28", "2027-01-05", "겨울 방학")))
                events = self.parse(text)
                self.assertEqual([("2026-11-02", "2026-11-06", "진로 캠프"), ("2026-12-28", "2027-01-05", "겨울 방학")],
                                 [(event.start_date, event.end_date, event.title) for event in events])

    def test_start_end_table_retains_time_and_place(self):
        event, = self.parse("시작일\t종료일\t제목\t시간\t장소\n2026-11-02\t2026-11-06\t진로 캠프\t09:00\t체육관")
        self.assertEqual(("진로 캠프", "09:00", "체육관"), (event.title, event.time, event.place))

    def test_start_end_table_empty_start_cell_requires_review(self):
        event, = self.parse("시작일\t종료일\t내용\n\t2026-11-06\t진로 캠프")
        self.assertEqual(("진로 캠프", "", None, "review"),
                         (event.title, event.start_date, event.end_date, event.status))
        self.assertIn("시작일이 없습니다", event.notes)

    def test_start_end_table_empty_title_cell_requires_review(self):
        event, = self.parse("시작일\t종료일\t내용\n2026-11-02\t2026-11-06\t")
        self.assertEqual(("제목 확인 필요", "2026-11-02", "2026-11-06", "review"),
                         (event.title, event.start_date, event.end_date, event.status))
        self.assertIn("일정 제목", event.notes)

    def test_imported_period_empty_start_cell_requires_review(self):
        for suffix in (".csv", ".xlsx"):
            with self.subTest(suffix=suffix):
                event, = self.parse_table_file(suffix, ("", "2026-11-06", "진로 캠프"))
                self.assertEqual(("진로 캠프", "", None, "review"),
                                 (event.title, event.start_date, event.end_date, event.status))
                self.assertIn("시작일이 없습니다", event.notes)

    def test_imported_period_empty_title_cell_requires_review(self):
        for suffix in (".csv", ".xlsx"):
            with self.subTest(suffix=suffix):
                event, = self.parse_table_file(suffix, ("2026-11-02", "2026-11-06", ""))
                self.assertEqual(("제목 확인 필요", "2026-11-02", "2026-11-06", "review"),
                                 (event.title, event.start_date, event.end_date, event.status))
                self.assertIn("일정 제목", event.notes)

    def test_period_table_context_ends_at_new_date_table_or_sheet(self):
        for divider in ("날짜\t제목\t장소", "[다음 학기]\n날짜\t제목\t장소"):
            with self.subTest(divider=divider):
                events = self.parse("시작일\t종료일\t내용\n2026-11-02\t2026-11-06\t진로 캠프\n" + divider
                                    + "\n2026-11-10\t독서 토론\t도서관")
                self.assertEqual(2, len(events))
                self.assertEqual(("2026-11-10", None, "독서 토론", "도서관"),
                                 (events[1].start_date, events[1].end_date, events[1].title, events[1].place))

    def test_unlabelled_two_date_columns_are_not_assumed_to_be_range(self):
        events = self.parse("2026-11-02\t2026-11-06\t진로 캠프")
        self.assertEqual(2, len(events))
        self.assertTrue(all(event.end_date is None for event in events))

    def test_ocr_clock_touches_date_and_audience_numbers(self):
        events = self.parse("2026년 행사 안내\n2026.10.22.15:00-19:003학년 진로 체험120명\n2026.10.23.09:00-10:301,2학년 안전 교육")
        self.assertEqual([("2026-10-22", "15:00"), ("2026-10-23", "09:00")],
                         [(event.start_date, event.time) for event in events])

    def test_unlabelled_time_and_place_can_share_following_line(self):
        event, = self.parse("학교 일정 알림\n2026년 10월 22일\n학부모 상담\n오후3시/장소:회의실")
        self.assertEqual(("학부모 상담", "15:00", "회의실"), (event.title, event.time, event.place))


if __name__ == "__main__":
    unittest.main()
