"""Exercise MIME input routing and the independent editable desktop notepad."""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import onnxruntime

onnxruntime.disable_telemetry_events()

from PyQt5.QtCore import QEvent, QMimeData, QPoint, QPointF, Qt, QUrl
from PyQt5.QtGui import QDragEnterEvent, QDropEvent, QImage, QMouseEvent, QTextCursor
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QLabel, QMessageBox

from alimi.capture import CaptureNotepad


class DesktopCaptureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.app.setProperty("shutting_down", False)
        warning_patch = patch("alimi.desktop.QMessageBox.warning",
                              return_value=QMessageBox.Cancel)
        self.warning = warning_patch.start()
        self.addCleanup(warning_patch.stop)
        self.window = CaptureNotepad("기존 메모")
        self.payloads = []
        self.drafts = []
        self.reviews = []
        self.window.payload_received.connect(self.payloads.append)
        self.window.draft_changed.connect(self.drafts.append)
        self.window.review_requested.connect(lambda: self.reviews.append(True))
        self.window.expand_notepad()
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.finish()
        self.app.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()
        self.app.setProperty("shutting_down", False)

    def text_mime(self, text):
        mime = QMimeData()
        mime.setText(text)
        return mime

    def test_notepad_is_independent_editable_and_always_above_ordinary_windows(self):
        self.assertIsNone(self.window.parent())
        self.assertEqual("학교 알리미 · 일정 메모장", self.window.windowTitle())
        self.assertFalse(self.window.windowFlags() & Qt.WindowStaysOnBottomHint)
        self.assertTrue(self.window.windowFlags() & Qt.WindowStaysOnTopHint)
        self.assertFalse(self.window.windowFlags() & Qt.WindowCloseButtonHint)
        self.assertFalse(self.window.editor.isReadOnly())
        self.assertFalse(self.window.close())
        self.assertTrue(self.window.isVisible())
        self.warning.assert_called_once()

    def test_new_notepad_starts_as_visible_desktop_icon(self):
        window = CaptureNotepad("다음에도 남아야 할 초안")
        try:
            window.show()
            self.app.processEvents()
            self.assertTrue(window.collapsed)
            self.assertEqual((72, 96), (window.width(), window.height()))
            self.assertTrue(window.icon_button.isVisible())
            self.assertFalse(window.editor.isVisible())
            self.assertFalse(window.isMinimized())
            self.assertFalse(window.close())
            self.assertEqual("다음에도 남아야 할 초안", window.editor.toPlainText())
        finally:
            window.finish()

    def test_icon_keeps_paper_hitbox_with_visible_close_button_and_opaque_memo(self):
        self.window.set_collapsed(True)
        self.app.processEvents()
        original_handle = int(self.window.winId())
        self.assertTrue(self.window.testAttribute(Qt.WA_TranslucentBackground))
        self.assertEqual("", self.window.icon_button.text())
        self.assertEqual([], self.window.compact_panel.findChildren(QLabel))
        self.assertTrue(self.window.icon_close_button.isVisible())
        self.assertFalse(self.window.close_button.isVisible())
        self.assertIn("일정 메모장", self.window.icon_button.accessibleName())
        self.assertIn("더블클릭", self.window.icon_button.toolTip())
        self.window.icon_button.setFocus()
        QTest.mouseMove(self.window.icon_button, QPoint(2, 2))
        self.app.processEvents()
        icon_origin = self.window.icon_button.mapTo(self.window, QPoint(0, 0))
        compact = self.window.grab().toImage().copy(icon_origin.x(), icon_origin.y(), 72, 70)
        for x, y in ((0, 0), (2, 2), (71, 0), (0, 69), (71, 69), (35, 68)):
            self.assertEqual(0, compact.pixelColor(x, y).alpha(), (x, y))
        paper = compact.pixelColor(36, 22)
        self.assertEqual(255, paper.alpha())
        self.assertEqual("#fffbee", paper.name())

        self.window.expand_notepad()
        self.app.processEvents()
        self.assertTrue(self.window.close_button.isVisible())
        self.assertFalse(self.window.icon_close_button.isVisible())
        expanded = self.window.grab().toImage()
        self.assertEqual(original_handle, int(self.window.winId()))
        self.assertEqual(255, expanded.pixelColor(2, 2).alpha())
        self.assertEqual("#fff2b6", expanded.pixelColor(2, 2).name())
        self.assertEqual(255, expanded.pixelColor(expanded.width() - 3,
                                                expanded.height() - 3).alpha())
        self.window.set_collapsed(True)
        self.app.processEvents()
        self.assertEqual(original_handle, int(self.window.winId()))
        self.assertEqual(0, self.window.grab().toImage().pixelColor(2, 2).alpha())

    def test_first_icon_is_uncropped_under_application_button_styles(self):
        from alimi.app import STYLE

        previous_style = self.app.styleSheet()
        window = None
        try:
            self.app.setStyleSheet(STYLE)
            window = CaptureNotepad("배경 없이 전체 아이콘 표시")
            self.assertEqual((72, 96), (window.width(), window.height()))
            window.show()
            self.app.processEvents()
            icon_origin = window.icon_button.mapTo(window, QPoint(0, 0))
            image = window.grab().toImage().copy(icon_origin.x(), icon_origin.y(), 72, 70)
            self.assertEqual((72, 70), (image.width(), image.height()))
            opaque_points = [(x, y) for y in range(image.height()) for x in range(image.width())
                             if image.pixelColor(x, y).alpha()]
            self.assertTrue(opaque_points)
            left, right = min(x for x, _ in opaque_points), max(x for x, _ in opaque_points)
            top, bottom = min(y for _, y in opaque_points), max(y for _, y in opaque_points)
            self.assertGreater(top, 0)
            self.assertLess(bottom, image.height() - 1)
            self.assertGreaterEqual(bottom - top + 1, 51, "The complete paper icon must fit at first show.")
            self.assertGreaterEqual(right - left + 1, 38)
            self.assertEqual(0, image.pixelColor(2, 2).alpha())
            window.expand_notepad()
            self.app.processEvents()
            window.set_collapsed(True)
            self.app.processEvents()
            self.assertEqual((72, 96), (window.width(), window.height()))
            self.assertEqual((72, 70), (window.icon_button.width(), window.icon_button.height()))
            self.assertTrue(window.icon_close_button.isVisible())
            self.assertFalse(window.icon_close_button.geometry().intersects(window.icon_button.geometry()))
        finally:
            if window is not None:
                window.finish()
            self.app.setStyleSheet(previous_style)

    def test_topmost_refresh_preserves_handle_geometry_and_never_activates(self):
        original_handle = int(self.window.winId())
        geometry = self.window.geometry()
        with patch("alimi.capture._native_windows_surface", return_value=True), \
                patch("alimi.capture._native_is_topmost", side_effect=[False, True]) as is_topmost, \
                patch("alimi.capture._native_set_z_order") as set_z_order, \
                patch.object(self.window, "lower") as lower, \
                patch.object(self.window, "raise_") as raise_window, \
                patch.object(self.window, "activateWindow") as activate:
            self.window.keep_below_windows()
            self.window.keep_below_windows()
            self.assertEqual(2, is_topmost.call_count)
            set_z_order.assert_called_once_with(original_handle, -1)
            lower.assert_not_called()
            raise_window.assert_not_called()
            activate.assert_not_called()
        self.assertEqual(original_handle, int(self.window.winId()))
        self.assertEqual(geometry, self.window.geometry())
        for collapsed in (True, False, True):
            self.window.set_collapsed(collapsed)
            self.window.keep_below_windows()
            self.assertEqual(original_handle, int(self.window.winId()))
            self.assertTrue(self.window.windowHandle().flags() & Qt.WindowStaysOnTopHint)
            self.assertFalse(self.window.windowHandle().flags() & Qt.WindowStaysOnBottomHint)

    def test_each_close_button_warns_cancel_preserves_and_ok_dismisses(self):
        dismissed = []
        self.window.dismissed_by_user.connect(lambda: dismissed.append(True))
        for collapsed in (True, False):
            self.window.set_collapsed(collapsed)
            button = self.window.icon_close_button if collapsed else self.window.close_button
            self.assertTrue(button.isVisible())
            self.assertEqual("닫기", button.accessibleName())
            self.assertEqual("닫기", button.toolTip())
            self.assertEqual((28, 26), (button.width(), button.height()))
            self.warning.reset_mock()
            button.click()
            self.warning.assert_called_once()
            self.assertEqual(QMessageBox.Cancel, self.warning.call_args.args[-1])
            self.assertTrue(self.window.isVisible())
            self.assertFalse(self.window.dismissed)
            self.assertEqual("기존 메모", self.window.editor.toPlainText())
            self.assertEqual([], dismissed)
        self.warning.return_value = QMessageBox.Ok
        self.window.close_button.click()
        self.assertFalse(self.window.isVisible())
        self.assertTrue(self.window.dismissed)
        self.assertEqual([True], dismissed)
        self.assertEqual("기존 메모", self.window.editor.toPlainText())

    def test_programmatic_finish_never_prompts_or_marks_user_dismissal(self):
        window = CaptureNotepad("종료할 때도 보존")
        dismissed = []
        window.dismissed_by_user.connect(lambda: dismissed.append(True))
        window.show()
        self.warning.reset_mock()
        window.finish()
        self.warning.assert_not_called()
        self.assertFalse(window.isVisible())
        self.assertFalse(window.dismissed)
        self.assertEqual([], dismissed)

    def test_double_click_and_minimize_preserve_editor_cursor_and_undo(self):
        editor = self.window.editor
        self.window.set_draft("draft")
        editor.moveCursor(QTextCursor.End)
        editor.insertPlainText(" saved")
        cursor_position = editor.textCursor().position()
        self.window.resize(420, 390)
        changes = []
        self.window.view_changed.connect(changes.append)
        self.window.minimize_button.click()
        self.assertTrue(self.window.collapsed)
        self.assertIs(editor, self.window.editor)
        self.assertTrue(self.window.isVisible())
        self.assertFalse(self.window.isMinimized())
        QTest.mouseDClick(self.window.icon_button, Qt.LeftButton)
        self.assertFalse(self.window.collapsed)
        self.assertEqual((420, 390), (self.window.width(), self.window.height()))
        self.assertEqual(cursor_position, editor.textCursor().position())
        self.assertEqual("draft saved", editor.toPlainText())
        editor.undo()
        self.assertEqual("draft", editor.toPlainText())
        QTest.mouseDClick(self.window.title_label, Qt.LeftButton)
        self.assertTrue(self.window.collapsed)
        self.assertEqual([True, False, True], changes)
        self.assertEqual([], self.payloads)

    def test_header_double_click_collapses_but_editor_double_click_selects_word(self):
        self.window.set_draft("alpha beta")
        cursor = self.window.editor.textCursor()
        cursor.setPosition(2)
        self.window.editor.setTextCursor(cursor)
        position = self.window.editor.cursorRect(cursor).center()
        QTest.mouseDClick(self.window.editor.viewport(), Qt.LeftButton, pos=position)
        self.assertFalse(self.window.collapsed)
        self.assertTrue(self.window.editor.textCursor().hasSelection())
        QTest.mouseDClick(self.window.header, Qt.LeftButton, pos=QPoint(2, 2))
        self.assertTrue(self.window.collapsed)

    def test_icon_keyboard_opens_and_hidden_draft_review_is_disabled(self):
        self.window.set_collapsed(True)
        self.window.activateWindow()
        self.window.icon_button.setFocus()
        self.app.processEvents()
        QTest.keyClick(self.window.icon_button, Qt.Key_Return, Qt.ControlModifier)
        self.assertTrue(self.window.collapsed)
        self.assertEqual([], self.reviews)
        QTest.keyClick(self.window.icon_button, Qt.Key_Return)
        self.assertFalse(self.window.collapsed)
        self.window.set_collapsed(True)
        QTest.keyClick(self.window.icon_button, Qt.Key_Space)
        self.assertFalse(self.window.collapsed)

    def test_icon_drop_expands_and_routes_file_input_once(self):
        self.window.set_collapsed(True)
        mime = self.text_mime("file:///C:/School/plan.pdf")
        mime.setUrls([QUrl.fromLocalFile("C:/School/plan.pdf")])
        enter = QDragEnterEvent(QPoint(10, 10), Qt.CopyAction | Qt.MoveAction,
                                mime, Qt.LeftButton, Qt.NoModifier)
        self.app.sendEvent(self.window.icon_button, enter)
        self.assertTrue(enter.isAccepted())
        self.assertTrue(self.window.collapsed)
        drop = QDropEvent(QPointF(10, 10), Qt.CopyAction | Qt.MoveAction,
                          mime, Qt.LeftButton, Qt.NoModifier)
        self.app.sendEvent(self.window.icon_button, drop)
        self.assertTrue(drop.isAccepted())
        self.assertEqual(Qt.CopyAction, drop.dropAction())
        self.assertFalse(self.window.collapsed)
        self.assertEqual([{"kind": "files", "paths": ["C:\\School\\plan.pdf"]}], self.payloads)
        self.assertEqual("기존 메모", self.window.editor.toPlainText())

    def test_icon_paste_opens_and_preserves_busy_state_after_roundtrip(self):
        self.window.set_busy(True)
        self.window.set_collapsed(True)
        self.window.activateWindow()
        self.window.icon_button.setFocus()
        self.app.clipboard().setMimeData(self.text_mime("10월 15일 회의"))
        self.app.processEvents()
        QTest.keyClick(self.window.icon_button, Qt.Key_V, Qt.ControlModifier)
        self.assertFalse(self.window.collapsed)
        self.assertFalse(self.window.review_button.isEnabled())
        self.assertIn("인식하고 있습니다", self.window.status_label.text())
        self.assertEqual([{"kind": "text", "text": "10월 15일 회의"}], self.payloads)
        self.window.set_collapsed(True)
        self.window.expand_notepad()
        self.assertFalse(self.window.review_button.isEnabled())
        self.assertEqual([], self.reviews)

    def test_icon_drag_emits_one_move_and_plain_click_stays_collapsed(self):
        self.window.set_collapsed(True)
        self.window.move(200, 160)
        moves = []
        self.window.moved.connect(lambda x, y: moves.append((x, y)))
        QTest.mouseClick(self.window.icon_button, Qt.LeftButton)
        self.assertTrue(self.window.collapsed)
        self.assertEqual([], moves)
        start = self.window.icon_button.mapToGlobal(QPoint(20, 20))
        end = start + QPoint(35, 24)
        press = QMouseEvent(QEvent.MouseButtonPress, QPointF(20, 20), QPointF(start),
                            Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
        move = QMouseEvent(QEvent.MouseMove, QPointF(55, 44), QPointF(end),
                           Qt.NoButton, Qt.LeftButton, Qt.NoModifier)
        release = QMouseEvent(QEvent.MouseButtonRelease, QPointF(20, 20), QPointF(end),
                              Qt.LeftButton, Qt.NoButton, Qt.NoModifier)
        self.app.sendEvent(self.window.icon_button, press)
        self.app.sendEvent(self.window.icon_button, move)
        self.app.sendEvent(self.window.icon_button, release)
        self.assertTrue(self.window.collapsed)
        self.assertEqual([(235, 184)], moves)
        QTest.mouseDClick(self.window.icon_button, Qt.LeftButton)
        self.assertFalse(self.window.collapsed)
        self.assertEqual([(235, 184)], moves)
        self.assertEqual([], self.payloads)

    def test_text_paste_inserts_at_cursor_and_emits_only_current_input(self):
        cursor = self.window.editor.textCursor()
        cursor.setPosition(2)
        self.window.editor.setTextCursor(cursor)
        self.window.editor.insertFromMimeData(self.text_mime("10월 15일 교직원 회의"))
        self.assertEqual("기존10월 15일 교직원 회의 메모", self.window.editor.toPlainText())
        self.assertEqual([{"kind": "text", "text": "10월 15일 교직원 회의"}], self.payloads)
        self.assertEqual(self.window.editor.toPlainText(), self.drafts[-1])

    def test_context_style_paste_and_paste_button_use_same_mime_route(self):
        self.app.clipboard().setMimeData(self.text_mime("9월 상담"))
        self.window.editor.paste()
        self.assertEqual({"kind": "text", "text": "9월 상담"}, self.payloads[-1])
        self.app.clipboard().setMimeData(self.text_mime("10월 평가"))
        self.window.paste_button.click()
        self.assertEqual({"kind": "text", "text": "10월 평가"}, self.payloads[-1])
        self.assertEqual(2, len(self.payloads))

    def test_local_file_urls_take_precedence_over_text_and_image(self):
        mime = self.text_mime("file:///C:/School/plan.pdf")
        mime.setUrls([QUrl.fromLocalFile("C:/School/plan.pdf"),
                      QUrl.fromLocalFile("C:/School/plan.pdf"),
                      QUrl.fromLocalFile("C:/School/image.png")])
        image = QImage(2, 2, QImage.Format_RGB32)
        image.fill(Qt.yellow)
        mime.setImageData(image)
        self.assertTrue(self.window.editor.canInsertFromMimeData(mime))
        self.window.editor.insertFromMimeData(mime)
        self.assertEqual("files", self.payloads[0]["kind"])
        self.assertEqual(["C:\\School\\plan.pdf", "C:\\School\\image.png"], self.payloads[0]["paths"])
        self.assertEqual("기존 메모", self.window.editor.toPlainText())
        self.assertEqual([], self.drafts)

    def test_image_precedes_text_and_emits_an_independent_snapshot(self):
        original = QImage(3, 3, QImage.Format_RGB32)
        original.fill(Qt.red)
        mime = self.text_mime("image alt text")
        mime.setImageData(original)
        self.window.editor.insertFromMimeData(mime)
        emitted = self.payloads[0]["image"]
        self.assertEqual("image", self.payloads[0]["kind"])
        original.fill(Qt.blue)
        self.assertEqual(0xFFFF0000, emitted.pixel(0, 0))
        self.assertEqual("기존 메모", self.window.editor.toPlainText())

    def test_typing_restore_and_extracted_text_never_emit_input_payload(self):
        self.window.set_draft("직접 작성")
        self.assertEqual([], self.drafts)
        self.window.editor.moveCursor(QTextCursor.End)
        QTest.keyClicks(self.window.editor, " 15:30")
        self.assertEqual("직접 작성 15:30", self.window.editor.toPlainText())
        self.assertEqual("직접 작성 15:30", self.drafts[-1])
        self.window.append_recognized_text("11월 학년 행사")
        self.assertEqual("직접 작성 15:30\n\n11월 학년 행사", self.window.editor.toPlainText())
        self.assertEqual([], self.payloads)
        self.assertEqual([], self.reviews)

    def test_editor_drop_copies_instead_of_moving_and_emits_once(self):
        mime = self.text_mime("12월 체험학습")
        enter = QDragEnterEvent(QPoint(10, 10), Qt.CopyAction | Qt.MoveAction,
                                mime, Qt.LeftButton, Qt.NoModifier)
        self.window.editor.dragEnterEvent(enter)
        self.assertTrue(enter.isAccepted())
        self.assertEqual(Qt.CopyAction, enter.dropAction())
        drop = QDropEvent(QPointF(10, 10), Qt.CopyAction | Qt.MoveAction,
                          mime, Qt.LeftButton, Qt.NoModifier)
        drop.setDropAction(Qt.MoveAction)
        self.window.editor.dropEvent(drop)
        self.assertTrue(drop.isAccepted())
        self.assertEqual(Qt.CopyAction, drop.dropAction())
        self.assertEqual([{"kind": "text", "text": "12월 체험학습"}], self.payloads)

    def test_top_level_drop_forwards_file_input_without_inserting_url_text(self):
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile("C:/School/plan.pdf")])
        drop = QDropEvent(QPointF(10, 10), Qt.CopyAction | Qt.MoveAction,
                          mime, Qt.LeftButton, Qt.NoModifier)
        self.window.dropEvent(drop)
        self.assertTrue(drop.isAccepted())
        self.assertEqual(Qt.CopyAction, drop.dropAction())
        self.assertEqual("files", self.payloads[0]["kind"])
        self.assertEqual("기존 메모", self.window.editor.toPlainText())

    def test_remote_urls_are_plain_text_only_and_no_remote_file_payload(self):
        mime = self.text_mime("https://school.example/plan.pdf")
        mime.setUrls([QUrl("https://school.example/plan.pdf")])
        self.window.editor.insertFromMimeData(mime)
        self.assertEqual("text", self.payloads[0]["kind"])
        self.assertNotIn("paths", self.payloads[0])
        empty = QMimeData()
        empty.setData("application/octet-stream", b"unsupported")
        self.assertFalse(self.window.editor.accept_mime(empty))
        self.assertEqual(1, len(self.payloads))
        self.assertIn("이 PC의 파일", self.window.status_label.text())

    def test_review_shortcut_and_busy_state_preserve_editing_and_block_review(self):
        self.window.activateWindow()
        self.window.editor.setFocus()
        self.app.processEvents()
        QTest.keyClick(self.window.editor, Qt.Key_Return, Qt.ControlModifier)
        self.assertEqual([True], self.reviews)
        self.window.set_busy(True)
        self.assertFalse(self.window.review_button.isEnabled())
        self.assertTrue(self.window.editor.isEnabled())
        self.assertFalse(self.window.editor.isReadOnly())
        self.window.review_button.click()
        QTest.keyClick(self.window.editor, Qt.Key_Enter, Qt.ControlModifier)
        self.assertEqual([True], self.reviews)
        self.window.set_busy(False)
        QTest.keyClick(self.window.editor, Qt.Key_Enter, Qt.ControlModifier)
        self.assertEqual([True, True], self.reviews)


if __name__ == "__main__":
    unittest.main()
