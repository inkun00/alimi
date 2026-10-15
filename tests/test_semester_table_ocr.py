"""Coordinate evidence and conservative refinement for semester matrices."""

import unittest
from unittest.mock import patch

import numpy as np

from alimi.ocr import OcrEngine, _TextFragment, _semester_table, _semester_table_text


def fragment(text, x, y, width=50, height=16):
    return _TextFragment(x, y, x + width, y + height, text)


def semester_fixture():
    result = [fragment("2028학년도", 15, 10, 150, 30),
              fragment("주요 학사 일정", 15, 45, 180, 30),
              fragment("구분", 30, 100, 40),
              fragment("1학기", 150, 100), fragment("2학기", 330, 100)]
    for row, (label, note, first, second) in enumerate((
        ("개강", "(학기개시일)", "3.2(월)", "8.24(월)"),
        ("중간고사", "(8주차)", "4.20(월)~4.24(금)", "10.12(월)~10.16(금)"),
        ("기념 행사", "", "5.16(토)", ""),
        ("계절학기", "(하기,동기)", "6.22(월)~7.10(금)", "12.14(월)~'29.1.5.(화)"),
    )):
        y = 140 + row * 55
        result.append(fragment(label, 15, y, 70))
        if note:
            result.append(fragment(note, 10, y + 18, 90))
        result.append(fragment(first, 130, y + 9, 120))
        if second:
            result.append(fragment(second, 290, y + 9, 150))
    return result


class SemesterTableOcrTests(unittest.TestCase):
    def test_multiline_labels_remain_with_both_date_cells(self):
        text = _semester_table_text(semester_fixture(), 460, 400)
        self.assertIn("구분\t1학기\t2학기", text)
        self.assertIn("개강 (학기개시일)\t3.2(월)\t8.24(월)", text)
        self.assertIn("중간고사 (8주차)\t4.20(월)~4.24(금)\t10.12(월)~10.16(금)", text)
        self.assertIn("계절학기 (하기,동기)\t6.22(월)~7.10(금)\t12.14(월)~'29.1.5.(화)", text)

    def test_empty_semester_stays_empty_and_year_title_is_preserved(self):
        text = _semester_table_text(semester_fixture(), 460, 400)
        self.assertTrue(text.startswith("2028학년도\n주요 학사 일정\n"))
        self.assertIn("기념 행사\t5.16(토)\t\n", text)
        self.assertEqual(text.count("5.16"), 1)

    def test_missing_left_header_keeps_existing_reader(self):
        values = [item for item in semester_fixture() if item.text != "구분"]
        self.assertIsNone(_semester_table(values, 460, 400))

    def test_semester_words_without_repeated_dates_are_not_a_table(self):
        values = [fragment("구분", 30, 100), fragment("1학기", 150, 100),
                  fragment("2학기", 330, 100), fragment("행사", 15, 150),
                  fragment("3.2(월)", 150, 150)]
        self.assertIsNone(_semester_table(values, 460, 400))

    def test_duplicate_or_nonconsecutive_semesters_are_not_invented(self):
        values = [fragment("3학기", item.left, item.top, item.right-item.left, item.height)
                  if item.text == "2학기" else item for item in semester_fixture()]
        self.assertIsNone(_semester_table(values, 460, 400))

    def test_three_semesters_keep_the_existing_document_path(self):
        values = semester_fixture() + [fragment("3학기", 460, 100)]
        self.assertIsNone(_semester_table(values, 560, 400))

    def test_unlabelled_date_row_prevents_reassociation(self):
        values = [item for item in semester_fixture() if item.text != "기념 행사"]
        self.assertIsNone(_semester_table(values, 460, 400))

    def test_combined_neighboring_date_box_does_not_get_split_by_guess(self):
        values = [item for item in semester_fixture() if item.text != "3.2(월)"]
        values.append(fragment("3.2(월) 8.24(월)", 150, 149, 265))
        self.assertIsNone(_semester_table(values, 460, 400))

    def test_geometry_scales_with_observed_boxes(self):
        values = [_TextFragment(item.left*2, item.top*2, item.right*2, item.bottom*2, item.text)
                  for item in semester_fixture()]
        self.assertEqual(_semester_table_text(values, 920, 800),
                         _semester_table_text(semester_fixture(), 460, 400))

    def test_optional_crop_failure_preserves_every_original_cell(self):
        engine = OcrEngine()
        with patch.object(engine, "_read_fragments", side_effect=RuntimeError("optional crop failed")):
            text = engine._calendar_text(np.zeros((400, 460, 3), dtype=np.uint8), semester_fixture())
        self.assertEqual(text, _semester_table_text(semester_fixture(), 460, 400))

    def test_refinement_never_changes_date_numbers(self):
        engine = OcrEngine()
        original = [fragment("4.20()~4.24(금)", 100, 100, 120)]
        with patch.object(engine, "_read_fragments", return_value=[fragment("4.21(월)~4.24(금)", 20, 20, 200)]):
            text = engine._semester_cell_text(np.zeros((200, 300, 3), dtype=np.uint8),
                                              original, (90, 90, 250, 140), date_cell=True)
        self.assertEqual(text, original[0].text)

    def test_missing_weekday_can_use_second_scale_with_identical_numbers(self):
        engine = OcrEngine()
        original = [fragment("12.14()~27.1.5.()", 100, 100, 120)]
        candidates = [[fragment("12.14(월)~27.1.5.()", 20, 20, 220)],
                      [fragment("12.14(월)~27.1.5.(화)", 20, 20, 220)]]
        with patch.object(engine, "_read_fragments", side_effect=candidates) as read:
            text = engine._semester_cell_text(np.zeros((200, 300, 3), dtype=np.uint8),
                                              original, (90, 90, 250, 140), date_cell=True)
        self.assertEqual(text, "12.14(월)~27.1.5.(화)")
        self.assertEqual(read.call_count, 2)

    def test_equally_complete_weekday_crop_keeps_original(self):
        engine = OcrEngine()
        original = [fragment("3.2(월)", 100, 100)]
        with patch.object(engine, "_read_fragments", return_value=[fragment("3.2(화)", 20, 20)]):
            text = engine._semester_cell_text(np.zeros((200, 300, 3), dtype=np.uint8),
                                              original, (90, 90, 250, 140), date_cell=True)
        self.assertEqual(text, "3.2(월)")

    def test_label_refinement_preserves_week_numbers_and_recovers_suffix(self):
        engine = OcrEngine()
        original = [fragment("증간고산", 10, 50, 70), fragment("(8주)", 10, 68, 70)]
        corrected = [fragment("중간고사", 20, 20, 130), fragment("(8주차)", 20, 65, 130)]
        with patch.object(engine, "_read_fragments", return_value=corrected):
            text = engine._semester_cell_text(np.zeros((150, 150, 3), dtype=np.uint8),
                                              original, (0, 40, 100, 100), date_cell=False)
        self.assertEqual(text, "중간고사 (8주차)")

    def test_equal_length_continuation_keeps_original_recognition(self):
        engine = OcrEngine()
        original = [fragment("계절학기", 10, 50, 70), fragment("(하기,동기)", 10, 68, 70)]
        changed = [fragment("계절학기", 20, 20, 130), fragment("(하기,등기)", 20, 65, 130)]
        with patch.object(engine, "_read_fragments", return_value=changed):
            text = engine._semester_cell_text(np.zeros((150, 150, 3), dtype=np.uint8),
                                              original, (0, 40, 100, 100), date_cell=False)
        self.assertEqual(text, "계절학기 (하기,동기)")

    def test_lost_label_continuation_keeps_original(self):
        engine = OcrEngine()
        original = [fragment("개강", 10, 50, 70), fragment("(학기개시일)", 10, 68, 70)]
        with patch.object(engine, "_read_fragments", return_value=[fragment("개강", 20, 20)]):
            text = engine._semester_cell_text(np.zeros((150, 150, 3), dtype=np.uint8),
                                              original, (0, 40, 100, 100), date_cell=False)
        self.assertEqual(text, "개강 (학기개시일)")


if __name__ == "__main__":
    unittest.main()
