"""Semester date matrices keep shared row labels and independent date cells."""

from datetime import date
import unittest

from alimi.core import parse_schedule_text


SAMPLE = """2026학년도 주요 학사 일정
구분\t1학기\t2학기
개강 (학기개시일)\t3. 2(월)\t8.24(월)
중간고사 (8주차)\t4.20(월) ~ 4.24(금)\t10.12(월) ~ 10.16(금)
DYW (9주차)\t4.27(월) ~ 4.30(목)\t10.19(월) ~ 10.23(금)
35주년 개교기념일\t5.16(토)\t
기말고사 (16주차)\t6.15(월) ~ 6.19(금)\t12. 7(월) ~ 12.11(금)
종강\t6.19(금)\t12.11(금)
계절학기 (하계, 동계)\t6.22(월) ~ 7.10(금)\t12.14(월) ~ ’27. 1. 5.(화)
학적 변동기간\t7. 6(월) ~ 7.10(금)\t‘27. 1. 4.(월) ~ 1. 8.(금)
"""


class SemesterTableParserTests(unittest.TestCase):
    def parse(self, text):
        return parse_schedule_text(text, "학사일정 이미지", reference_date=date(2032, 1, 1))

    def test_actual_semester_matrix_has_fifteen_independent_events(self):
        events = self.parse(SAMPLE)
        self.assertEqual(15, len(events))
        self.assertEqual([
            ("2026-03-02", None), ("2026-08-24", None),
            ("2026-04-20", "2026-04-24"), ("2026-10-12", "2026-10-16"),
            ("2026-04-27", "2026-04-30"), ("2026-10-19", "2026-10-23"),
            ("2026-05-16", None),
            ("2026-06-15", "2026-06-19"), ("2026-12-07", "2026-12-11"),
            ("2026-06-19", None), ("2026-12-11", None),
            ("2026-06-22", "2026-07-10"), ("2026-12-14", "2027-01-05"),
            ("2026-07-06", "2026-07-10"), ("2027-01-04", "2027-01-08"),
        ], [(event.start_date, event.end_date) for event in events])
        self.assertTrue(all(event.status == "open" for event in events))
        self.assertTrue(all(event.date_precision == "day" and not event.place for event in events))

    def test_shared_titles_keep_week_annotations_and_two_day_end_events(self):
        events = self.parse(SAMPLE)
        self.assertEqual(2, sum(event.title == "기말고사 (16주차)" for event in events))
        self.assertEqual(2, sum(event.title == "종강" for event in events))
        self.assertEqual(1, sum(event.title == "35주년 개교기념일" for event in events))
        self.assertEqual(2, sum(event.title == "DYW (9주차)" for event in events))
        self.assertEqual({"2026-06-19", "2026-12-11"},
                         {event.start_date for event in events if event.title == "종강"})

    def test_unknown_ocr_weekdays_keep_range_and_require_review(self):
        text = """2026학년도
구분\t1학기\t2학기
중간고사 (8주차)\t4.20(월)~4.24(금)\t10.12()~10.16(금)
기말고사 (16주차)\t6.15(월)~6.19(금)\t12.7()~12.11(금)
계절학기\t6.22()-7.10(금)\t12.14()-‘27.1.5.(호)
학적변동기간\t7.6(월)-7.10(금)\t‘27.1.4.()-1.8.(금)
"""
        events = self.parse(text)
        self.assertEqual(8, len(events))
        self.assertEqual(("2026-12-14", "2027-01-05"),
                         (events[5].start_date, events[5].end_date))
        self.assertEqual(("2027-01-04", "2027-01-08"),
                         (events[7].start_date, events[7].end_date))
        for index in (1, 3, 4, 5, 7):
            self.assertEqual("review", events[index].status)
            self.assertIn("요일", events[index].notes)
        self.assertEqual("open", events[0].status)

    def test_empty_or_slashed_cell_never_invents_second_semester_event(self):
        for empty in ("", "/", "\\", "-", "해당 없음"):
            with self.subTest(empty=empty):
                events = self.parse(f"2026학년도\n구분\t1학기\t2학기\n기념일\t5.16(토)\t{empty}")
                self.assertEqual(1, len(events))
                self.assertEqual("2026-05-16", events[0].start_date)

    def test_empty_first_cell_does_not_move_second_date_into_first(self):
        events = self.parse("2026학년도\n구분\t1학기\t2학기\n행사\t\t12.11(금)")
        self.assertEqual(1, len(events))
        self.assertEqual("2026-12-11", events[0].start_date)

    def test_apostrophe_variants_and_bare_short_year_are_local_to_date_cell(self):
        for apostrophe in ("'", "‘", "’", "ʼ", ""):
            with self.subTest(apostrophe=apostrophe):
                events = self.parse(f"2026학년도\n행사\t1학기\t2학기\n계절학기\t\t12.14(월)~{apostrophe}27.1.5.(화)")
                self.assertEqual(1, len(events))
                self.assertEqual("2027-01-05", events[0].end_date)
                self.assertEqual("open", events[0].status)

    def test_academic_january_rollover_and_explicit_year_override(self):
        events = self.parse("2026학년도\n구분\t1학기\t2학기\n학적변동\t2026.1.5(월)\t1.4(월)~1.8(금)")
        self.assertEqual(["2026-01-05", "2027-01-04"], [event.start_date for event in events])
        self.assertTrue(all(event.status == "open" for event in events))

    def test_calendar_year_heading_does_not_assume_academic_rollover(self):
        events = self.parse("2026년\n구분\t1학기\t2학기\n행사\t1.5(월)\t12.11(금)")
        self.assertEqual(["2026-01-05", "2026-12-11"], [event.start_date for event in events])
        self.assertTrue(all(event.status == "open" for event in events))

    def test_missing_year_uses_reference_but_keeps_review(self):
        events = self.parse("구분\t1학기\t2학기\n등록\t3.2\t8.24")
        self.assertEqual(2, len(events))
        self.assertTrue(all(event.start_date.startswith("2032-") and event.status == "review" for event in events))
        self.assertTrue(all("연도가 없어" in event.notes for event in events))

    def test_malformed_or_invalid_date_is_retained_for_review(self):
        for cell in ("2.30(월)", "추후 공지"):
            with self.subTest(cell=cell):
                events = self.parse(f"2026학년도\n구분\t1학기\t2학기\n등록\t{cell}\t")
                self.assertEqual(1, len(events))
                self.assertTrue(not events[0].start_date and events[0].status == "review")

    def test_pipe_markdown_spaced_and_reordered_header_variants(self):
        samples = [
            "| 구분 | 1학기 | 2학기 |\n| --- | --- | --- |\n| 종강 | 6.19(금) | 12.11(금) |",
            "행사  제1학기  제2학기\n종강  6.19(금)  12.11(금)",
            "2학기\t행사명\t1학기\n12.11(금)\t종강\t6.19(금)",
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                events = self.parse("2026학년도\n" + sample)
                self.assertEqual(["2026-06-19", "2026-12-11"], [event.start_date for event in events])
                self.assertTrue(all(event.title == "종강" and event.status == "open" for event in events))

    def test_original_row_and_header_are_saved_and_reparse_without_lost_year(self):
        events = self.parse(SAMPLE)
        event = events[-1]
        self.assertIn("2026학년도\n구분\t1학기\t2학기\n", event.source_text)
        self.assertIn("‘27. 1. 4.(월)", event.source_text)
        reparsed = self.parse(event.source_text)
        self.assertEqual([("2026-07-06", "2026-07-10"), ("2027-01-04", "2027-01-08")],
                         [(candidate.start_date, candidate.end_date) for candidate in reparsed])

    def test_duplicate_rows_do_not_duplicate_events(self):
        events = self.parse(SAMPLE + "종강\t6.19(금)\t12.11(금)\n")
        self.assertEqual(15, len(events))

    def test_new_period_table_header_ends_semester_matrix(self):
        events = self.parse("2026학년도\n구분\t1학기\t2학기\n종강\t6.19(금)\t12.11(금)\n"
                            "제목\t시작일\t종료일\n회의\t2026.10.1\t2026.10.2")
        self.assertEqual(3, len(events))
        self.assertEqual(("회의", "2026-10-01", "2026-10-02"),
                         (events[-1].title, events[-1].start_date, events[-1].end_date))

    def test_header_requires_both_explicit_semesters(self):
        for header in ("구분\t날짜\t장소", "행사\t1차\t2차", "구분\t1학기\t비고"):
            with self.subTest(header=header):
                events = self.parse(header + "\n회의\t2026.3.2\t교무실")
                self.assertEqual(1, len(events))
                self.assertEqual("교무실", events[0].place)

    def test_following_plain_text_does_not_inherit_matrix_columns(self):
        events = self.parse("2026학년도\n구분\t1학기\t2학기\n종강\t6.19(금)\t12.11(금)\n"
                            "추가 일정\n회의\t2026.10.1\t교무실")
        self.assertEqual(3, len(events))
        self.assertEqual("교무실", events[-1].place)

    def test_blank_line_ends_matrix_before_a_normal_schedule_row(self):
        events = self.parse("2026학년도\n구분\t1학기\t2학기\n종강\t6.19(금)\t12.11(금)\n\n"
                            "회의\t2026.10.1\t교무실")
        self.assertEqual(3, len(events))
        self.assertEqual(("회의", "2026-10-01", "교무실", "open"),
                         (events[-1].title, events[-1].start_date, events[-1].place, events[-1].status))

    def test_general_table_headers_end_matrix_before_their_data(self):
        for header in ("기간\t내용\t장소", "날짜\t일정명\t장소"):
            with self.subTest(header=header):
                events = self.parse("2026학년도\n구분\t1학기\t2학기\n종강\t6.19(금)\t12.11(금)\n"
                                    + header + "\n2026.10.1\t회의\t교무실")
                self.assertEqual(3, len(events))
                self.assertEqual(("회의", "2026-10-01", "교무실", "open"),
                                 (events[-1].title, events[-1].start_date, events[-1].place, events[-1].status))


if __name__ == "__main__":
    unittest.main()
