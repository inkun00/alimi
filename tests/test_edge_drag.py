"""Exercise real mouse routing at tool edges without touching user data."""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Match the production Windows DLL initialization order.
import onnxruntime

onnxruntime.disable_telemetry_events()

from PyQt5.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt5.QtGui import QMouseEvent
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QMessageBox

from alimi.capture import CaptureNotepad
from alimi.core import Event
from alimi.desktop import DesktopListWindow, NoteWindow


class ToolEdgeDragTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.app.setProperty("shutting_down", False)
        self.windows = []
        self.events = [
            Event(id=12, title="교직원 회의", start_date="2026-10-15",
                  notes="상세 내용은 드래그로 선택할 수 있어야 합니다. " * 100),
            Event(id=13, title="과학 발표", start_date="2026-10-16"),
        ]

    def tearDown(self):
        for window in self.windows:
            window.finish()
        self.app.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()
        self.app.setProperty("shutting_down", False)

    def make_window(self, kind, expanded=False):
        if kind == "note":
            window = NoteWindow(self.events[0])
            window.set_expanded(expanded)
        elif kind == "list":
            window = DesktopListWindow(self.events)
            if expanded:
                window.set_selected_event(12)
        else:
            window = CaptureNotepad("가장자리는 이동하고 이 본문은 선택합니다. " * 100)
            window.set_collapsed(not expanded)
        self.windows.append(window)
        window.show()
        self.app.processEvents()
        window.move(240, 180)
        self.app.processEvents()
        return window

    def variants(self):
        for kind in ("note", "list", "capture"):
            for expanded in (False, True):
                yield kind, expanded

    @staticmethod
    def edge_points(window):
        right, bottom = window.width() - 2, window.height() - 2
        middle_x, middle_y = window.width() // 2, window.height() // 2
        return {
            "left": QPoint(1, middle_y), "right": QPoint(right, middle_y),
            "top": QPoint(middle_x, 1), "bottom": QPoint(middle_x, bottom),
            "top_left": QPoint(1, 1), "top_right": QPoint(right, 1),
            "bottom_left": QPoint(1, bottom), "bottom_right": QPoint(right, bottom),
        }

    def send_mouse(self, target, event_type, global_position, button, buttons):
        event = QMouseEvent(
            event_type, QPointF(target.mapFromGlobal(global_position)),
            QPointF(global_position), button, buttons, Qt.NoModifier,
        )
        self.app.sendEvent(target, event)

    def gesture(self, window, point, delta=QPoint(37, 29), button=Qt.LeftButton,
                target=None):
        # Route to the widget that receives a physical click, including the
        # notepad's paper panel and compact icon rather than the parent alone.
        target = target or window.childAt(point) or window
        start = window.mapToGlobal(point)
        end = start + delta
        self.send_mouse(target, QEvent.MouseButtonPress, start, button, button)
        self.send_mouse(target, QEvent.MouseMove, end, Qt.NoButton, button)
        self.send_mouse(target, QEvent.MouseButtonRelease, end, button, Qt.NoButton)
        self.app.processEvents()

    def assert_drag_cleared(self, window):
        self.assertIsNone(window.drag_offset)
        self.assertIsNone(window._frame_drag_start)
        self.assertIsNone(window._frame_drag_origin)
        self.assertFalse(window._frame_dragging)

    def test_every_edge_and_corner_moves_each_tool_view_once_without_resizing(self):
        for kind, expanded in self.variants():
            window = self.make_window(kind, expanded)
            moves = []
            window.moved.connect(lambda *args, output=moves: output.append(args))
            for edge, point in self.edge_points(window).items():
                with self.subTest(kind=kind, expanded=expanded, edge=edge):
                    window.move(240, 180)
                    origin, size = window.pos(), window.size()
                    moves.clear()
                    self.gesture(window, point)
                    self.assertEqual(origin + QPoint(37, 29), window.pos())
                    self.assertEqual(size, window.size())
                    expected = ((12, window.x(), window.y()) if kind == "note"
                                else (window.x(), window.y()))
                    self.assertEqual([expected], moves)
                    self.assert_drag_cleared(window)

    def test_edge_click_without_motion_never_saves_or_moves(self):
        for kind, expanded in self.variants():
            window = self.make_window(kind, expanded)
            moves = []
            window.moved.connect(lambda *args, output=moves: output.append(args))
            for edge, point in self.edge_points(window).items():
                with self.subTest(kind=kind, expanded=expanded, edge=edge):
                    origin = window.pos()
                    self.gesture(window, point, delta=QPoint())
                    self.assertEqual(origin, window.pos())
                    self.assertEqual([], moves)
                    self.assert_drag_cleared(window)

    def test_right_button_edges_never_move_any_tool_view(self):
        for kind, expanded in self.variants():
            window = self.make_window(kind, expanded)
            moves = []
            window.moved.connect(lambda *args, output=moves: output.append(args))
            for edge, point in self.edge_points(window).items():
                with self.subTest(kind=kind, expanded=expanded, edge=edge):
                    origin = window.pos()
                    self.gesture(window, point, button=Qt.RightButton)
                    self.assertEqual(origin, window.pos())
                    self.assertEqual([], moves)
                    self.assert_drag_cleared(window)

    def test_one_pixel_edge_jitter_is_a_click_and_does_not_save(self):
        for kind, expanded in self.variants():
            with self.subTest(kind=kind, expanded=expanded):
                window = self.make_window(kind, expanded)
                moves = []
                window.moved.connect(lambda *args, output=moves: output.append(args))
                origin = window.pos()
                self.gesture(window, QPoint(1, 1), delta=QPoint(1, 0))
                self.assertEqual(origin, window.pos())
                self.assertEqual([], moves)
                self.assert_drag_cleared(window)

    def test_right_release_during_left_drag_does_not_finish_or_save_early(self):
        for kind, expanded in self.variants():
            with self.subTest(kind=kind, expanded=expanded):
                window = self.make_window(kind, expanded)
                moves = []
                window.moved.connect(lambda *args, output=moves: output.append(args))
                point = QPoint(1, 1)
                target = window.childAt(point) or window
                origin = window.pos()
                start = window.mapToGlobal(point)
                self.send_mouse(target, QEvent.MouseButtonPress, start,
                                Qt.LeftButton, Qt.LeftButton)
                self.send_mouse(target, QEvent.MouseMove, start + QPoint(20, 15),
                                Qt.NoButton, Qt.LeftButton)
                self.send_mouse(target, QEvent.MouseButtonRelease, start + QPoint(20, 15),
                                Qt.RightButton, Qt.LeftButton)
                self.assertEqual([], moves)
                self.send_mouse(target, QEvent.MouseMove, start + QPoint(37, 29),
                                Qt.NoButton, Qt.LeftButton)
                self.send_mouse(target, QEvent.MouseButtonRelease, start + QPoint(37, 29),
                                Qt.LeftButton, Qt.NoButton)
                self.assertEqual(origin + QPoint(37, 29), window.pos())
                self.assertEqual(1, len(moves))
                self.assert_drag_cleared(window)

    def test_lost_mouse_button_finishes_once_and_prevents_ghost_drag(self):
        for kind, expanded in self.variants():
            with self.subTest(kind=kind, expanded=expanded):
                window = self.make_window(kind, expanded)
                moves = []
                window.moved.connect(lambda *args, output=moves: output.append(args))
                point = QPoint(1, 1)
                target = window.childAt(point) or window
                start = window.mapToGlobal(point)
                self.send_mouse(target, QEvent.MouseButtonPress, start,
                                Qt.LeftButton, Qt.LeftButton)
                self.send_mouse(target, QEvent.MouseMove, start + QPoint(37, 29),
                                Qt.NoButton, Qt.LeftButton)
                final_position = window.pos()
                self.send_mouse(target, QEvent.MouseMove, start + QPoint(70, 50),
                                Qt.NoButton, Qt.NoButton)
                self.send_mouse(target, QEvent.MouseMove, start + QPoint(95, 80),
                                Qt.NoButton, Qt.NoButton)
                self.send_mouse(target, QEvent.MouseButtonRelease, start + QPoint(95, 80),
                                Qt.LeftButton, Qt.NoButton)
                self.assertEqual(final_position, window.pos())
                self.assertEqual(1, len(moves))
                self.assert_drag_cleared(window)

    def test_hide_and_ungrab_finish_once_without_leaving_drag_state(self):
        for kind, expanded in self.variants():
            for interruption in ("hide", "ungrab"):
                with self.subTest(kind=kind, expanded=expanded, interruption=interruption):
                    window = self.make_window(kind, expanded)
                    moves = []
                    window.moved.connect(lambda *args, output=moves: output.append(args))
                    point = QPoint(1, 1)
                    target = window.childAt(point) or window
                    start = window.mapToGlobal(point)
                    self.send_mouse(target, QEvent.MouseButtonPress, start,
                                    Qt.LeftButton, Qt.LeftButton)
                    self.send_mouse(target, QEvent.MouseMove, start + QPoint(37, 29),
                                    Qt.NoButton, Qt.LeftButton)
                    final_position = window.pos()
                    if interruption == "hide":
                        window.hide()
                        window.show()
                        self.app.processEvents()
                    else:
                        self.app.sendEvent(target, QEvent(QEvent.UngrabMouse))
                    self.send_mouse(target, QEvent.MouseMove, start + QPoint(90, 70),
                                    Qt.NoButton, Qt.LeftButton)
                    self.send_mouse(target, QEvent.MouseButtonRelease, start + QPoint(90, 70),
                                    Qt.LeftButton, Qt.NoButton)
                    self.assertEqual(final_position, window.pos())
                    self.assertEqual(1, len(moves))
                    self.assert_drag_cleared(window)

    def test_title_button_within_top_edge_band_keeps_its_click_action(self):
        window = self.make_window("note")
        moves = []
        window.moved.connect(lambda *args: moves.append(args))
        origin = window.pos()
        point = QPoint(window.title_label.geometry().center().x(), 9)
        self.assertIs(window.childAt(point), window.title_label)
        self.gesture(window, point, delta=QPoint())
        self.assertTrue(window.expanded)
        point = window.close_button.geometry().center()
        self.assertIs(window.childAt(point), window.close_button)
        with patch("alimi.desktop.QMessageBox.warning",
                   return_value=QMessageBox.Cancel) as warning:
            self.gesture(window, point, delta=QPoint())
        warning.assert_called_once()
        self.assertTrue(window.isVisible())
        self.assertEqual(origin, window.pos())
        self.assertEqual([], moves)

    def test_original_six_dot_handles_still_move_all_expanded_tools(self):
        for kind in ("note", "list", "capture"):
            with self.subTest(kind=kind):
                window = self.make_window(kind, expanded=True)
                moves = []
                window.moved.connect(lambda *args, output=moves: output.append(args))
                origin = window.pos()
                point = window.handle.mapTo(window, window.handle.rect().center())
                self.gesture(window, point, target=window.handle)
                self.assertEqual(origin + QPoint(37, 29), window.pos())
                self.assertEqual(1, len(moves))
                self.assert_drag_cleared(window)

    def test_note_title_and_completion_buttons_keep_click_actions(self):
        window = self.make_window("note")
        moves, completed = [], []
        window.moved.connect(lambda *args: moves.append(args))
        window.completed.connect(completed.append)
        origin = window.pos()
        QTest.mouseClick(window.title_label, Qt.LeftButton)
        self.app.processEvents()
        self.assertTrue(window.expanded)
        QTest.mouseClick(window.complete_button, Qt.LeftButton)
        self.assertEqual([12], completed)
        self.assertEqual([], moves)
        self.assertEqual(origin, window.pos())

    def test_close_buttons_keep_confirmation_and_cancel_does_not_move(self):
        for kind in ("note", "list", "capture"):
            with self.subTest(kind=kind):
                window = self.make_window(kind, expanded=True)
                moves = []
                window.moved.connect(lambda *args, output=moves: output.append(args))
                origin = window.pos()
                with patch("alimi.desktop.QMessageBox.warning",
                           return_value=QMessageBox.Cancel) as warning:
                    QTest.mouseClick(window.close_button, Qt.LeftButton)
                warning.assert_called_once()
                self.assertTrue(window.isVisible())
                self.assertEqual([], moves)
                self.assertEqual(origin, window.pos())

    def test_list_item_selection_and_completion_stay_interactive(self):
        window = self.make_window("list")
        moves, completed = [], []
        window.moved.connect(lambda *args: moves.append(args))
        window.completed.connect(completed.append)
        origin = window.pos()
        item = window.list_widget.item(1)
        QTest.mouseClick(window.list_widget.viewport(), Qt.LeftButton,
                         pos=window.list_widget.visualItemRect(item).center())
        self.app.processEvents()
        self.assertTrue(window.expanded)
        self.assertEqual(13, window.selected_event_id)
        QTest.mouseClick(window.complete_button, Qt.LeftButton)
        self.assertEqual([13], completed)
        self.assertEqual([], moves)
        self.assertEqual(origin, window.pos())

    def test_notepad_text_selection_and_scroll_are_not_window_drags(self):
        window = self.make_window("capture", expanded=True)
        moves = []
        window.moved.connect(lambda *args: moves.append(args))
        origin = window.pos()
        viewport = window.editor.viewport()
        cursor = window.editor.textCursor()
        cursor.setPosition(2)
        start = window.editor.cursorRect(cursor).center()
        cursor.setPosition(12)
        end = window.editor.cursorRect(cursor).center()
        global_start, global_end = viewport.mapToGlobal(start), viewport.mapToGlobal(end)
        self.send_mouse(viewport, QEvent.MouseButtonPress, global_start,
                        Qt.LeftButton, Qt.LeftButton)
        self.send_mouse(viewport, QEvent.MouseMove, global_end,
                        Qt.NoButton, Qt.LeftButton)
        self.send_mouse(viewport, QEvent.MouseButtonRelease, global_end,
                        Qt.LeftButton, Qt.NoButton)
        self.assertTrue(window.editor.textCursor().hasSelection())
        bar = window.editor.verticalScrollBar()
        self.assertGreater(bar.maximum(), 0)
        QTest.mouseClick(bar, Qt.LeftButton, pos=QPoint(bar.width() // 2, bar.height() - 3))
        self.assertGreater(bar.value(), 0)
        self.assertEqual(origin, window.pos())
        self.assertEqual([], moves)
        self.assert_drag_cleared(window)

    def test_notepad_minimize_and_icon_double_click_keep_view_controls(self):
        window = self.make_window("capture", expanded=True)
        moves = []
        window.moved.connect(lambda *args: moves.append(args))
        origin = window.pos()
        QTest.mouseClick(window.minimize_button, Qt.LeftButton)
        self.assertTrue(window.collapsed)
        QTest.mouseDClick(window.icon_button, Qt.LeftButton)
        self.assertFalse(window.collapsed)
        self.assertEqual(origin, window.pos())
        self.assertEqual([], moves)
        self.assert_drag_cleared(window)


if __name__ == "__main__":
    unittest.main()
