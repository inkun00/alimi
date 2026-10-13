"""Verify ordered grids using work areas rather than physical screen bounds."""

import unittest

# Match the Windows application entry point before any Qt DLL is loaded.
import onnxruntime

onnxruntime.disable_telemetry_events()

from PyQt5.QtCore import QPoint, QRect, QSize

from alimi.layout import arrange_rectangles


class AlignmentLayoutTests(unittest.TestCase):
    def assert_contained_and_separated(self, area, sizes, positions):
        rectangles = [QRect(position, QSize(*size)) for position, size in zip(positions, sizes)]
        for rectangle in rectangles:
            self.assertTrue(area.contains(rectangle), (area, rectangle))
        for index, rectangle in enumerate(rectangles):
            for other in rectangles[index + 1:]:
                self.assertFalse(rectangle.intersects(other), (rectangle, other))

    def test_single_item_anchors_to_each_requested_edge(self):
        area = QRect(100, 200, 800, 600)
        expected = {
            "top": QPoint(112, 212), "bottom": QPoint(112, 708),
            "left": QPoint(112, 212), "right": QPoint(768, 212),
        }
        for alignment, position in expected.items():
            with self.subTest(alignment=alignment):
                self.assertEqual([position], arrange_rectangles(area, [QSize(120, 80)], alignment))

    def test_top_fills_rows_left_to_right_then_down(self):
        positions = arrange_rectangles(QRect(0, 0, 600, 500), [(180, 70)] * 8, "top")
        self.assertEqual([QPoint(12, 12), QPoint(202, 12), QPoint(392, 12)], positions[:3])
        self.assertEqual(QPoint(12, 92), positions[3])
        self.assertEqual(QPoint(202, 172), positions[7])
        self.assert_contained_and_separated(QRect(0, 0, 600, 500), [(180, 70)] * 8, positions)

    def test_bottom_fills_rows_left_to_right_then_up(self):
        positions = arrange_rectangles(QRect(0, 0, 600, 500), [(180, 70)] * 8, "bottom")
        self.assertEqual([QPoint(12, 418), QPoint(202, 418), QPoint(392, 418)], positions[:3])
        self.assertEqual(QPoint(12, 338), positions[3])
        self.assertEqual(QPoint(202, 258), positions[7])
        self.assert_contained_and_separated(QRect(0, 0, 600, 500), [(180, 70)] * 8, positions)

    def test_left_fills_columns_top_to_bottom_then_right(self):
        area, sizes = QRect(0, 0, 900, 500), [(180, 100)] * 8
        positions = arrange_rectangles(area, sizes, "left")
        self.assertEqual([QPoint(12, 12), QPoint(12, 122), QPoint(12, 232), QPoint(12, 342)], positions[:4])
        self.assertEqual(QPoint(202, 12), positions[4])
        self.assert_contained_and_separated(area, sizes, positions)

    def test_right_fills_columns_top_to_bottom_then_left(self):
        area, sizes = QRect(0, 0, 900, 500), [(180, 100)] * 8
        positions = arrange_rectangles(area, sizes, "right")
        self.assertEqual([QPoint(708, 12), QPoint(708, 122), QPoint(708, 232), QPoint(708, 342)], positions[:4])
        self.assertEqual(QPoint(518, 12), positions[4])
        self.assert_contained_and_separated(area, sizes, positions)

    def test_expanded_heights_use_tallest_row_and_bottom_edges(self):
        area, sizes = QRect(0, 0, 600, 800), [(180, 70), (180, 300), (180, 100), (180, 70)]
        positions = arrange_rectangles(area, sizes, "bottom")
        self.assertEqual([QPoint(12, 718), QPoint(202, 488), QPoint(392, 688)], positions[:3])
        self.assertEqual(QPoint(12, 408), positions[3])
        self.assert_contained_and_separated(area, sizes, positions)

    def test_varying_widths_use_widest_column_and_right_edges(self):
        area, sizes = QRect(0, 0, 900, 400), [(120, 100), (250, 100), (180, 100), (150, 100)]
        positions = arrange_rectangles(area, sizes, "right")
        self.assertEqual([QPoint(768, 12), QPoint(638, 122), QPoint(708, 232)], positions[:3])
        self.assertEqual(QPoint(478, 12), positions[3])
        self.assert_contained_and_separated(area, sizes, positions)

    def test_negative_monitor_origin_and_taskbar_exclusion(self):
        area, sizes = QRect(-1920, 40, 1920, 1000), [(320, 72)] * 20
        for alignment in ("top", "bottom", "right", "left"):
            with self.subTest(alignment=alignment):
                positions = arrange_rectangles(area, sizes, alignment)
                self.assert_contained_and_separated(area, sizes, positions)
                self.assertTrue(all(position.x() < 0 for position in positions))
                self.assertTrue(all(position.y() >= 52 for position in positions))
                self.assertTrue(all(position.y() + size[1] <= 1028 for position, size in zip(positions, sizes)))

    def test_twenty_notes_with_one_expanded_fit_all_four_directions(self):
        area = QRect(50, 75, 1920, 1000)
        sizes = [(320, 300 if index == 5 else 72) for index in range(20)]
        for alignment in ("top", "bottom", "right", "left"):
            with self.subTest(alignment=alignment):
                self.assert_contained_and_separated(area, sizes, arrange_rectangles(area, sizes, alignment))

    def test_exact_primary_fit_does_not_wrap_early(self):
        area, sizes = QRect(0, 0, 380, 240), [(180, 60)] * 3
        positions = arrange_rectangles(area, sizes, "top", margin=0, gap=20)
        self.assertEqual([QPoint(0, 0), QPoint(200, 0), QPoint(0, 80)], positions)
        self.assert_contained_and_separated(area, sizes, positions)

    def test_empty_and_invalid_work_areas_are_safe(self):
        self.assertEqual([], arrange_rectangles(QRect(), [], "top"))
        self.assertEqual([QPoint(70, 90)] * 2, arrange_rectangles(QRect(70, 90, 0, 0), [(50, 50)] * 2, "right"))

    def test_unknown_alignment_preserves_bottom_default(self):
        area, sizes = QRect(10, 20, 400, 500), [(120, 60)] * 5
        self.assertEqual(arrange_rectangles(area, sizes, "bottom"), arrange_rectangles(area, sizes, "unexpected"))

    def test_tiny_area_and_oversized_item_anchor_without_inverted_coordinates(self):
        area = QRect(-8, 4, 5, 3)
        for alignment in ("top", "bottom", "right", "left"):
            with self.subTest(alignment=alignment):
                self.assertEqual([QPoint(-6, 5)], arrange_rectangles(area, [(200, 100)], alignment))

    def test_overflow_is_deterministic_and_positions_remain_on_screen(self):
        area, sizes = QRect(-100, 50, 300, 150), [(120, 70)] * 20
        for alignment in ("top", "bottom", "right", "left"):
            with self.subTest(alignment=alignment):
                positions = arrange_rectangles(area, sizes, alignment)
                self.assertEqual(positions, arrange_rectangles(area, sizes, alignment))
                self.assertEqual(20, len(positions))
                self.assertTrue(all(area.contains(QRect(position, QSize(*size))) for position, size in zip(positions, sizes)))

    def test_negative_spacing_and_invalid_sizes_are_normalized(self):
        positions = arrange_rectangles(QRect(10, 20, 30, 40), [QSize(-1, 0), (4, 5)], "top", margin=-5, gap=-10)
        self.assertEqual([QPoint(10, 20), QPoint(11, 20)], positions)


if __name__ == "__main__":
    unittest.main()
