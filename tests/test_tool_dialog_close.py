"""Closing tools must preserve drafts on cancellation and clean up captures once."""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import onnxruntime

onnxruntime.disable_telemetry_events()

from PyQt5 import sip
from PyQt5.QtCore import QEvent, QPoint, QPointF, QRect, Qt
from PyQt5.QtGui import QCloseEvent, QColor, QMouseEvent, QPixmap
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QDialog, QDialogButtonBox, QMessageBox

from alimi.app import CaptureOverlay, EventDialog
from alimi.core import Event
from alimi.review import ImportReviewDialog


class FakeScreen:
    def __init__(self):
        self.pixmap = QPixmap(600, 360)
        self.pixmap.fill(QColor("#e4eae5"))

    def grabWindow(self, _window_id):
        return self.pixmap

    def geometry(self):
        return QRect(0, 0, 600, 360)


class ToolDialogCloseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.app.setProperty("shutting_down", False)
        self.windows = []
        self.event = Event(title="교직원 회의", start_date="2026-10-14", notes="원래 내용")

    def tearDown(self):
        for window in self.windows:
            if sip.isdeleted(window):
                continue
            if isinstance(window, CaptureOverlay):
                window._cancel_capture()
            else:
                window.reject()
                window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()
        self.app.setProperty("shutting_down", False)

    def shown(self, window):
        self.windows.append(window)
        window.show()
        self.app.processEvents()
        return window

    def test_event_close_cancellation_preserves_the_visible_unsaved_draft(self):
        window = self.shown(EventDialog(self.event))
        window.title_field.setText("수정 중인 회의")
        finished = []
        window.finished.connect(finished.append)
        with patch("alimi.app.QMessageBox.warning", return_value=QMessageBox.Cancel) as warning:
            self.assertFalse(window.close())
        self.assertTrue(window.isVisible())
        self.assertEqual("수정 중인 회의", window.title_field.text())
        self.assertEqual("교직원 회의", self.event.title)
        self.assertEqual([], finished)
        self.assertEqual(QMessageBox.Cancel, warning.call_args.args[-1])

    def test_confirmed_event_close_rejects_without_committing_fields(self):
        window = self.shown(EventDialog(self.event))
        window.title_field.setText("저장하지 않은 회의")
        finished = []
        window.finished.connect(finished.append)
        with patch("alimi.app.QMessageBox.warning", return_value=QMessageBox.Ok):
            self.assertTrue(window.close())
        self.assertFalse(window.isVisible())
        self.assertEqual([QDialog.Rejected], finished)
        self.assertEqual("교직원 회의", window.event.title)

    def test_event_save_cancel_and_escape_keep_existing_semantics(self):
        with patch("alimi.app.QMessageBox.warning") as warning:
            window = self.shown(EventDialog(self.event))
            window.title_field.setText("확정한 회의")
            buttons = window.findChild(QDialogButtonBox)
            buttons.button(QDialogButtonBox.Save).click()
            self.assertEqual(QDialog.Accepted, window.result())
            self.assertEqual("확정한 회의", window.event.title)
            window = self.shown(EventDialog(self.event))
            window.findChild(QDialogButtonBox).button(QDialogButtonBox.Cancel).click()
            self.assertFalse(window.isVisible())
            self.assertEqual(QDialog.Rejected, window.result())
            window = self.shown(EventDialog(self.event))
            QTest.keyClick(window, Qt.Key_Escape)
            self.assertFalse(window.isVisible())
            self.assertEqual(QDialog.Rejected, window.result())
        warning.assert_not_called()

    def test_import_close_cancellation_preserves_edits_then_confirmation_discards(self):
        window = self.shown(ImportReviewDialog([self.event]))
        window.table.item(0, window.TITLE).setText("수정 중인 가져오기")
        registered, finished = [], []
        window.confirmed.connect(registered.append)
        window.finished.connect(finished.append)
        with patch("alimi.review.QMessageBox.warning", return_value=QMessageBox.Cancel) as warning:
            self.assertFalse(window.close())
        self.assertTrue(window.isVisible())
        self.assertEqual("수정 중인 가져오기", window.table.item(0, window.TITLE).text())
        self.assertEqual(QMessageBox.Cancel, warning.call_args.args[-1])
        self.assertEqual([], registered)
        self.assertEqual([], finished)
        with patch("alimi.review.QMessageBox.warning", return_value=QMessageBox.Ok):
            self.assertTrue(window.close())
        self.assertFalse(window.isVisible())
        self.assertEqual([QDialog.Rejected], finished)
        self.assertEqual([], registered)
        self.assertEqual([], window.confirmed_events)
        self.assertEqual("교직원 회의", self.event.title)

    def test_import_cancel_escape_and_owner_accept_do_not_request_close_confirmation(self):
        with patch("alimi.review.QMessageBox.warning") as warning:
            window = self.shown(ImportReviewDialog([self.event]))
            window.cancel_button.click()
            self.assertEqual(QDialog.Rejected, window.result())
            window = self.shown(ImportReviewDialog([self.event]))
            QTest.keyClick(window, Qt.Key_Escape)
            self.assertEqual(QDialog.Rejected, window.result())
            window = self.shown(ImportReviewDialog([self.event]))
            window.accept()
            self.assertEqual(QDialog.Accepted, window.result())
        warning.assert_not_called()

    def test_capture_button_cancellation_keeps_selection_and_can_close_once(self):
        window = self.shown(CaptureOverlay(FakeScreen()))
        window.origin = QPoint(20, 70)
        window.selection = QRect(20, 70, 170, 110)
        captured, canceled = [], []
        window.captured.connect(captured.append)
        window.canceled.connect(lambda: canceled.append(True))
        self.assertTrue(window.close_button.isVisible())
        self.assertTrue(window.rect().contains(window.close_button.geometry()))
        with patch("alimi.app.QMessageBox.warning", return_value=QMessageBox.Cancel) as warning:
            window.close_button.click()
        self.assertTrue(window.isVisible())
        self.assertEqual(QRect(20, 70, 170, 110), window.selection)
        self.assertEqual([], captured)
        self.assertEqual([], canceled)
        self.assertEqual(QMessageBox.Cancel, warning.call_args.args[-1])
        with patch("alimi.app.QMessageBox.warning", return_value=QMessageBox.Ok):
            window.close_button.click()
        self.assertFalse(window.isVisible())
        self.assertEqual([True], canceled)
        self.assertEqual([], captured)
        window._cancel_capture()
        self.assertEqual([True], canceled)

    def test_capture_escape_keeps_existing_immediate_cancel_behavior(self):
        window = self.shown(CaptureOverlay(FakeScreen()))
        captured, canceled = [], []
        window.captured.connect(captured.append)
        window.canceled.connect(lambda: canceled.append(True))
        with patch("alimi.app.QMessageBox.warning") as warning:
            QTest.keyClick(window, Qt.Key_Escape)
        self.assertFalse(window.isVisible())
        self.assertEqual([True], canceled)
        self.assertEqual([], captured)
        warning.assert_not_called()

    def test_capture_selection_success_does_not_prompt_or_emit_cancel(self):
        window = self.shown(CaptureOverlay(FakeScreen()))
        captured, canceled = [], []
        window.captured.connect(captured.append)
        window.canceled.connect(lambda: canceled.append(True))
        with patch("alimi.app.QMessageBox.warning") as warning:
            QTest.mousePress(window, Qt.LeftButton, pos=QPoint(20, 70))
            # The offscreen platform has no OS cursor to deliver a held-button drag.
            self.app.sendEvent(window, QMouseEvent(
                QEvent.MouseMove, QPointF(180, 180), Qt.NoButton, Qt.LeftButton, Qt.NoModifier,
            ))
            QTest.mouseRelease(window, Qt.LeftButton, pos=QPoint(180, 180))
        self.assertFalse(window.isVisible())
        self.assertEqual(1, len(captured))
        self.assertEqual((161, 111), captured[0].size)
        self.assertEqual([], canceled)
        warning.assert_not_called()

    def test_capture_native_close_uses_same_cancel_signal_and_blocks_reentrant_prompt(self):
        window = self.shown(CaptureOverlay(FakeScreen()))
        canceled = []
        window.canceled.connect(lambda: canceled.append(True))
        def confirm(*_args):
            repeated_event = QCloseEvent()
            window.closeEvent(repeated_event)
            self.assertFalse(repeated_event.isAccepted())
            return QMessageBox.Ok
        with patch("alimi.app.QMessageBox.warning", side_effect=confirm) as warning:
            self.assertTrue(window.close())
        self.assertEqual([True], canceled)
        self.assertEqual(1, warning.call_count)

    def test_application_shutdown_closes_tools_without_prompt_and_cancels_capture_once(self):
        event_dialog = self.shown(EventDialog(self.event))
        event_dialog.title_field.setText("종료 때 저장하면 안 되는 수정")
        review_dialog = self.shown(ImportReviewDialog([self.event]))
        review_dialog.table.item(0, review_dialog.TITLE).setText("등록하면 안 되는 수정")
        overlay = self.shown(CaptureOverlay(FakeScreen()))
        canceled, captured, registered = [], [], []
        overlay.canceled.connect(lambda: canceled.append(True))
        overlay.captured.connect(captured.append)
        review_dialog.confirmed.connect(registered.append)
        self.app.setProperty("shutting_down", True)
        with patch("alimi.app.QMessageBox.warning") as warning:
            self.assertTrue(event_dialog.close())
            self.assertTrue(review_dialog.close())
            self.assertTrue(overlay.close())
            overlay._cancel_capture()
        self.assertFalse(event_dialog.isVisible())
        self.assertFalse(review_dialog.isVisible())
        self.assertFalse(overlay.isVisible())
        self.assertEqual(QDialog.Rejected, event_dialog.result())
        self.assertEqual(QDialog.Rejected, review_dialog.result())
        self.assertEqual("교직원 회의", self.event.title)
        self.assertEqual([True], canceled)
        self.assertEqual([], captured)
        self.assertEqual([], registered)
        warning.assert_not_called()


if __name__ == "__main__":
    unittest.main()
