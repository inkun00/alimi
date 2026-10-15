"""Calendar reading order using disposable, coordinate-aware OCR fragments."""

import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

from alimi.ocr import OcrEngine, _TextFragment, _calendar_panels, _rows_text


def fragment(text, x, y, width=40, height=14):
    return _TextFragment(x, y, x + width, y + height, text)


def calendar_fixture(months=(3, 4, 5, 6, 7, 8), columns=3, body_height=14):
    result = [fragment("주요 학사일정", 65, 0, 150)]
    for index, month in enumerate(months):
        column, row = index % columns, index // columns
        x, y = 20 + column * 190, 30 + row * 310
        heading = f"2021. {month}월" if index == 0 else f"{month}월"
        result.append(fragment(heading, x + 70, y, 50))
        for cell, weekday in enumerate("일월화수목금토"):
            result.append(fragment(weekday, x + cell * 25, y + 24, 15))
        for day in range(1, 32):
            result.append(fragment(str(day), x + ((day - 1) % 7) * 25,
                                   y + 44 + ((day - 1) // 7) * 18, 15))
        result.extend((fragment(f"1(월) {month}월 첫 행사", x, y + 148, 170, body_height),
                       fragment(f"2(화) {month}월 둘째 행사", x, y + 184, 170, body_height),
                       fragment(":", x, y + 220, 10),
                       fragment("수업일수: 22일", x + 50, y + 278, 90)))
    return result


def panel_text(fragments, width=640, height=680):
    engine = OcrEngine()
    with patch.object(engine, "_read_fragments", return_value=[]):
        return engine._calendar_text(np.zeros((height, width, 3), dtype=np.uint8), fragments)


class CalendarOcrLayoutTests(unittest.TestCase):
    def test_month_columns_are_read_down_each_panel(self):
        text = panel_text(calendar_fixture())
        self.assertEqual(text.count("첫 행사"), 6)
        self.assertLess(text.index("3월 둘째 행사"), text.index("4월 첫 행사"))
        self.assertLess(text.index("5월 둘째 행사"), text.index("6월 첫 행사"))
        self.assertEqual(len([line for line in text.splitlines() if "행사" in line]), 12)

    def test_original_year_and_title_are_preserved(self):
        text = panel_text(calendar_fixture())
        self.assertTrue(text.startswith("주요 학사일정\n2021. 3월\n"))
        self.assertEqual(text.count("2021"), 1)
        self.assertNotIn("2026", text)

    def test_weekdays_grid_numbers_punctuation_and_totals_are_not_events(self):
        text = panel_text(calendar_fixture())
        self.assertNotIn("수업일수", text)
        self.assertNotIn("일  월  화", text)
        self.assertNotIn("\n:\n", text)
        self.assertFalse(any(line.isdecimal() for line in text.splitlines()))

    def test_normal_month_table_keeps_its_row_order(self):
        ordinary = [fragment("3월", 100, 20), fragment("4월", 300, 20),
                    fragment("회의", 40, 70), fragment("3월 1일", 100, 70),
                    fragment("교육", 240, 70), fragment("4월 2일", 300, 70)]
        self.assertEqual(_calendar_panels(ordinary, 500, 300), [])
        self.assertIsNone(panel_text(ordinary, 500, 300))
        self.assertIn("회의  3월 1일  교육  4월 2일", _rows_text(ordinary))

    def test_numeric_table_without_weekdays_is_not_a_calendar(self):
        values = [item for item in calendar_fixture((3, 4), 2)
                  if item.text not in "일월화수목금토"]
        self.assertIsNone(panel_text(values))

    def test_incomplete_numeric_grid_does_not_change_order(self):
        values = [item for item in calendar_fixture((3, 4), 2)
                  if not item.text.isdecimal() or int(item.text) <= 10]
        self.assertIsNone(panel_text(values))

    def test_each_month_needs_calendar_evidence(self):
        values = [item for item in calendar_fixture((3, 4), 2)
                  if not (item.left > 200 and item.text in "일월화수목금토")]
        self.assertIsNone(panel_text(values))

    def test_duplicate_month_headings_are_not_a_calendar(self):
        self.assertIsNone(panel_text(calendar_fixture((3, 3), 2)))

    def test_no_template_coordinates_are_required(self):
        values = calendar_fixture()
        moved = [_TextFragment(item.left * 2 + 17, item.top * 2 + 11,
                               item.right * 2 + 17, item.bottom * 2 + 11, item.text)
                 for item in values]
        self.assertEqual(panel_text(values), panel_text(moved, 1350, 1400))

    def test_small_panels_are_enlarged_and_reread(self):
        values = calendar_fixture((3, 4), 2)
        engine = OcrEngine()
        refined = [[fragment("1(월) 개선된 첫 일정", 0, 0), fragment("2(화) 개선된 둘째 일정", 0, 35)],
                   [fragment("1(목) 다음 달 첫 일정", 0, 0), fragment("2(금) 다음 달 둘째 일정", 0, 35)]]
        with patch.object(engine, "_read_fragments", side_effect=refined) as reader:
            text = engine._calendar_text(np.zeros((360, 450, 3), dtype=np.uint8), values)
        self.assertEqual(reader.call_count, 2)
        self.assertIn("개선된 첫 일정", text)
        self.assertIn("다음 달 첫 일정", text)
        self.assertNotIn("3월 첫 행사", text)
        for args in reader.call_args_list:
            self.assertGreater(args.args[0].shape[1], 300)

    def test_reread_cannot_drop_a_previously_detected_line(self):
        values = calendar_fixture((3, 4), 2)
        engine = OcrEngine()
        with patch.object(engine, "_read_fragments", return_value=[fragment("1(월) 한 줄만 재인식", 0, 0)]):
            text = engine._calendar_text(np.zeros((360, 450, 3), dtype=np.uint8), values)
        self.assertNotIn("한 줄만 재인식", text)
        self.assertIn("3월 둘째 행사", text)
        self.assertIn("4월 둘째 행사", text)

    def test_failed_optional_reread_preserves_the_original_schedule_lists(self):
        values = calendar_fixture((3, 4), 2)
        engine = OcrEngine()
        with patch.object(engine, "_read_fragments", side_effect=RuntimeError("optional OCR failed")):
            text = engine._calendar_text(np.zeros((360, 450, 3), dtype=np.uint8), values)
        self.assertIn("3월 첫 행사", text)
        self.assertIn("3월 둘째 행사", text)
        self.assertIn("4월 첫 행사", text)
        self.assertIn("4월 둘째 행사", text)

    def test_reread_with_equal_line_count_cannot_lose_detected_dates(self):
        values = calendar_fixture((3, 4), 2)
        engine = OcrEngine()
        undated = [fragment("날짜를 읽지 못한 첫 행", 0, 0),
                   fragment("달력 아래쪽의 안내 문구", 0, 35)]
        with patch.object(engine, "_read_fragments", return_value=undated):
            text = engine._calendar_text(np.zeros((360, 450, 3), dtype=np.uint8), values)
        self.assertNotIn("날짜를 읽지 못한", text)
        self.assertIn("1(월) 3월 첫 행사", text)
        self.assertIn("2(화) 3월 둘째 행사", text)
        self.assertIn("1(월) 4월 첫 행사", text)
        self.assertIn("2(화) 4월 둘째 행사", text)

    def test_optional_resize_failure_also_preserves_the_original_lists(self):
        engine = OcrEngine()
        with patch("cv2.resize", side_effect=ValueError("resize failed")):
            text = engine._calendar_text(np.zeros((360, 450, 3), dtype=np.uint8),
                                         calendar_fixture((3, 4), 2))
        self.assertIn("3월 첫 행사", text)
        self.assertIn("4월 둘째 행사", text)

    def test_large_print_does_not_need_another_detection_pass(self):
        values = calendar_fixture((3, 4), 2, body_height=28)
        engine = OcrEngine()
        with patch.object(engine, "_read_fragments") as reader:
            text = engine._calendar_text(np.zeros((360, 450, 3), dtype=np.uint8), values)
        reader.assert_not_called()
        self.assertIn("3월 첫 행사", text)

    def test_recognize_uses_calendar_order_in_the_public_image_path(self):
        engine = OcrEngine()
        values = calendar_fixture((3, 4), 2, body_height=28)
        with patch.object(engine, "_load"), patch.object(engine, "_read_fragments", return_value=values):
            text = engine.recognize(Image.new("RGB", (450, 360), "white"))
        self.assertLess(text.index("3월 둘째 행사"), text.index("4월 첫 행사"))

    def test_calendar_analysis_does_not_mutate_original_fragments(self):
        values = calendar_fixture()
        before = list(values)
        panel_text(values)
        self.assertEqual(values, before)


if __name__ == "__main__":
    unittest.main()
