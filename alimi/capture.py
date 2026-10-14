"""An editable desktop notepad that sends pasted or dropped input for review."""

from __future__ import annotations

from pathlib import Path

from PyQt5.QtCore import QEvent, QPoint, QSize, QSignalBlocker, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QIcon, QImage, QKeySequence, QPainter, QPen, QPixmap, QPolygon
from PyQt5.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QPushButton, QShortcut, QTextEdit,
    QVBoxLayout, QWidget,
)

from .desktop import (
    _BottomWindow, _native_is_topmost, _native_set_z_order,
    _native_windows_surface,
)


CAPTURE_INSTRUCTION = "텍스트를 입력하거나 텍스트·파일·이미지를 붙여넣고 끌어 놓으세요."


def _notepad_icon():
    """Draw a local paper icon without an external image dependency."""
    pixmap = QPixmap(56, 60)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(QPen(QColor("#ae9251"), 1.5))
    painter.setBrush(QColor("#fffbee"))
    painter.drawPolygon(QPolygon([QPoint(9, 4), QPoint(36, 4), QPoint(47, 15),
                                 QPoint(47, 55), QPoint(9, 55)]))
    painter.setBrush(QColor("#e8d39a"))
    painter.drawPolygon(QPolygon([QPoint(36, 4), QPoint(36, 15), QPoint(47, 15)]))
    painter.setPen(QPen(QColor("#9b895a"), 2))
    for y in (24, 32, 40, 48):
        painter.drawLine(17, y, 39 if y != 48 else 31, y)
    painter.end()
    return QIcon(pixmap)


class MimeTextEdit(QTextEdit):
    """Use the same MIME route for keyboard paste, context paste, and drops.

    Clipboard file copies commonly contain both URLs and their text form. File
    URLs take precedence so their contents can be read by the application.
    Neither this editor nor the notepad reads files, performs OCR, or saves events.
    """

    payload_received = pyqtSignal(object)
    status_message = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptRichText(False)
        self.setAcceptDrops(True)
        self.setReadOnly(False)

    @staticmethod
    def _payload(source):
        local_paths = []
        if source.hasUrls():
            for url in source.urls():
                if url.isLocalFile():
                    local_name = url.toLocalFile()
                    if not local_name:
                        continue
                    path = str(Path(local_name))
                    if path and path not in local_paths:
                        local_paths.append(path)
        if local_paths:
            return {"kind": "files", "paths": local_paths}
        if source.hasImage():
            data = source.imageData()
            if isinstance(data, QPixmap):
                data = data.toImage()
            if isinstance(data, QImage) and not data.isNull():
                return {"kind": "image", "image": data.copy()}
        if source.hasText() and source.text():
            return {"kind": "text", "text": source.text()}
        return None

    def canInsertFromMimeData(self, source):
        # A drag can query this many times. Copy the image only upon insertion.
        return (any(url.isLocalFile() and url.toLocalFile() for url in source.urls())
                or source.hasImage() or (source.hasText() and bool(source.text())))

    def insertFromMimeData(self, source):
        self.accept_mime(source)

    def accept_mime(self, source, position=None):
        """Insert plain text and emit a snapshot of exactly this input."""
        payload = self._payload(source)
        if payload is None:
            self.status_message.emit("텍스트, 이미지 또는 이 PC의 파일을 붙여넣어 주세요.")
            return False
        if payload["kind"] == "text":
            if position is not None:
                self.setTextCursor(self.cursorForPosition(position))
            self.insertPlainText(payload["text"])
        self.payload_received.emit(payload)
        return True

    def dragEnterEvent(self, event):
        if self.canInsertFromMimeData(event.mimeData()):
            event.setDropAction(Qt.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if self.canInsertFromMimeData(event.mimeData()):
            event.setDropAction(Qt.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dropEvent(self, event):
        # Never move or delete a source file, or move existing source text.
        if self.accept_mime(event.mimeData(), event.pos()):
            event.setDropAction(Qt.CopyAction)
            event.accept()
        else:
            event.ignore()


class CaptureNotepad(_BottomWindow):
    """An independently editable notepad above ordinary windows in both views."""

    payload_received = pyqtSignal(object)
    review_requested = pyqtSignal()
    draft_changed = pyqtSignal(str)
    moved = pyqtSignal(int, int)
    view_changed = pyqtSignal(bool)

    def __init__(self, draft="", font_size=12):
        super().__init__()
        self.setWindowFlags((self.windowFlags() & ~Qt.WindowStaysOnBottomHint)
                            | Qt.WindowStaysOnTopHint)
        # Windows needs a translucent frameless surface before the first HWND
        # exists. Keep that same surface while the opaque paper panel opens.
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setObjectName("captureNotepad")
        self.setWindowTitle("학교 알리미 · 일정 메모장")
        self.setAcceptDrops(True)
        self.collapsed = False
        self._expanded_size = QSize(390, 360)
        self._drag_start = None
        self._drag_origin = None
        self._dragging = False
        self.setStyleSheet("""
QWidget#captureNotepad, QWidget#notepadCompact { background: transparent; border: none; }
QWidget#notepadPaper { background: #fff2b6; border: 1px solid #dbc36b; }
QLabel { color: #4c4124; background: transparent; border: none; }
QLabel#notepadStatus { color: #70623c; }
QTextEdit { background: #fffbee; color: #493e1e; border: 1px solid #e6d89e;
  selection-background-color: #eed784; selection-color: #493e1e; padding: 8px; }
QPushButton { background: #fff6d0; color: #493e1e; border: 1px solid #dcca85;
  border-radius: 4px; padding: 8px 10px; }
QPushButton:hover, QPushButton:focus { background: #f4e39e; }
QPushButton#reviewDraft { background: #4c4124; color: #fff6d0; }
QPushButton#reviewDraft:hover { background: #655831; }
QPushButton:disabled { color: #9c937b; background: #e8dfa9; }
QPushButton#notepadIcon { background: transparent; border: none; padding: 0;
  min-width: 72px; max-width: 72px; min-height: 70px; max-height: 70px; }
QPushButton#notepadIcon:hover, QPushButton#notepadIcon:focus,
QPushButton#notepadIcon:pressed { background: transparent; border: none; }
QPushButton#minimizeNotepad, QPushButton#closeTool { padding: 1px 6px; font-size: 18px; }
QScrollBar:vertical { background: #faedbd; width: 12px; }
QScrollBar::handle:vertical { background: #b8a568; min-height: 24px; border-radius: 4px; }
""")
        # Switching the inherited window layer can retain the base tool's style
        # cache. Apply the paper styles before creating its child controls.
        self.style().unpolish(self)
        self.style().polish(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.compact_panel = QWidget(self)
        self.compact_panel.setObjectName("notepadCompact")
        self.compact_panel.setAcceptDrops(True)
        compact_layout = QVBoxLayout(self.compact_panel)
        compact_layout.setContentsMargins(0, 0, 0, 0)
        compact_layout.setSpacing(0)
        compact_header = QHBoxLayout()
        compact_header.setContentsMargins(0, 0, 0, 0)
        compact_header.setSpacing(0)
        compact_header.addStretch(1)
        self.icon_close_button = self._make_close_button(self.compact_panel)
        compact_header.addWidget(self.icon_close_button)
        compact_layout.addLayout(compact_header)
        self.icon_button = QPushButton(self.compact_panel)
        self.icon_button.setObjectName("notepadIcon")
        self.icon_button.setIcon(_notepad_icon())
        self.icon_button.setIconSize(QSize(56, 60))
        self.icon_button.setFixedSize(72, 70)
        self.icon_button.setAcceptDrops(True)
        self.icon_button.setAccessibleName("일정 메모장 열기")
        self.icon_button.setAccessibleDescription("더블클릭 또는 Enter, Space로 메모장을 펼칩니다. 드래그해서 이동합니다.")
        self.icon_button.setToolTip("더블클릭하면 일정 메모장을 엽니다.\n드래그해서 이동하거나 파일을 끌어 놓으세요.")
        self.icon_button.clicked.connect(self.expand_notepad)
        compact_layout.addWidget(self.icon_button, 0, Qt.AlignHCenter)
        layout.addWidget(self.compact_panel)

        self.expanded_panel = QWidget(self)
        self.expanded_panel.setObjectName("notepadPaper")
        self.expanded_panel.setAttribute(Qt.WA_StyledBackground)
        expanded_layout = QVBoxLayout(self.expanded_panel)
        expanded_layout.setContentsMargins(12, 9, 12, 12)
        expanded_layout.setSpacing(8)
        self.header = QWidget(self.expanded_panel)
        header = QHBoxLayout(self.header)
        header.setContentsMargins(0, 0, 0, 0)
        self.title_label = QLabel("일정 메모장", self.header)
        self.title_label.setFont(QFont("Malgun Gothic", max(11, font_size), QFont.Bold))
        self.title_label.setToolTip("제목을 더블클릭하면 바탕화면 아이콘으로 최소화합니다.")
        self.header.setToolTip(self.title_label.toolTip())
        header.addWidget(self.title_label, 1)
        self.minimize_button = QPushButton("−", self.header)
        self.minimize_button.setObjectName("minimizeNotepad")
        self.minimize_button.setFixedSize(28, 26)
        self.minimize_button.setAccessibleName("아이콘으로 최소화")
        self.minimize_button.setToolTip("바탕화면 아이콘으로 최소화")
        self.minimize_button.clicked.connect(lambda: self.set_collapsed(True))
        header.addWidget(self.minimize_button)
        self.close_button = self._make_close_button(self.header)
        header.addWidget(self.close_button)
        self.handle = self._make_handle()
        self.handle.setAccessibleName("일정 메모장 이동 영역")
        self.handle.setToolTip("드래그해서 이동 · 더블클릭하면 아이콘으로 최소화")
        header.addWidget(self.handle)
        expanded_layout.addWidget(self.header)
        self.editor = MimeTextEdit(self.expanded_panel)
        self.editor.setFont(QFont("Malgun Gothic", font_size))
        self.editor.setPlaceholderText("예: 10월 15일 오후 3시 교직원 회의\n\n직접 입력 후 ‘일정 확인’을 누르세요.\n붙여넣기나 파일 드래그는 바로 인식합니다.")
        self.editor.setAccessibleName("일정 초안 입력")
        self.editor.setAccessibleDescription("붙여넣기 또는 드래그 후 일정을 확인하고 등록합니다. Ctrl+Enter로 입력한 초안을 확인합니다.")
        expanded_layout.addWidget(self.editor, 1)
        self.status_label = QLabel(CAPTURE_INSTRUCTION)
        self.status_label.setObjectName("notepadStatus")
        self.status_label.setTextFormat(Qt.PlainText)
        self.status_label.setWordWrap(True)
        self.status_label.setFont(QFont("Malgun Gothic", max(9, font_size - 2)))
        expanded_layout.addWidget(self.status_label)
        actions = QHBoxLayout()
        self.paste_button = QPushButton("붙여넣기")
        self.paste_button.setToolTip("복사한 텍스트, 파일 또는 이미지를 가져옵니다.")
        self.paste_button.clicked.connect(self.paste_clipboard)
        actions.addWidget(self.paste_button)
        self.review_button = QPushButton("일정 확인")
        self.review_button.setObjectName("reviewDraft")
        self.review_button.setToolTip("입력한 초안을 인식하고 등록 전에 확인합니다. (Ctrl+Enter)")
        self.review_button.clicked.connect(self.review_requested)
        actions.addWidget(self.review_button, 1)
        expanded_layout.addLayout(actions)
        layout.addWidget(self.expanded_panel)
        self._drag_widgets = (self.compact_panel, self.icon_button,
                              self.header, self.title_label, self.handle, self.expanded_panel)
        for widget in self._drag_widgets:
            widget.installEventFilter(self)
        self.editor.payload_received.connect(self.payload_received)
        self.editor.status_message.connect(self.status_label.setText)
        self.editor.textChanged.connect(self._draft_changed)
        self._review_shortcuts = []
        for key in ("Ctrl+Return", "Ctrl+Enter"):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.WidgetWithChildrenShortcut)
            shortcut.activated.connect(self._review_if_enabled)
            self._review_shortcuts.append(shortcut)
        self._paste_shortcut = QShortcut(QKeySequence.Paste, self)
        self._paste_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self._paste_shortcut.activated.connect(self.paste_clipboard)
        self.set_draft(draft)
        self.resize(self._expanded_size)
        self.set_collapsed(True)

    def set_collapsed(self, collapsed):
        """Keep one desktop window and editor alive in both icon and memo views."""
        collapsed = bool(collapsed)
        if collapsed == self.collapsed:
            return
        if collapsed:
            # The cursor and undo stack belong to the retained editor, not to
            # either visible panel. Hiding the panel leaves both untouched.
            self._expanded_size = self.size() if self.width() >= 330 else self._expanded_size
            self.expanded_panel.hide()
            self.compact_panel.show()
            # The application's button stylesheet can repolish the child and
            # resize it before its first layout. Use the icon view's intended
            # bounds, rather than copying that temporary child geometry.
            self.setFixedSize(72, 96)
        else:
            self.compact_panel.hide()
            self.expanded_panel.show()
            self.setMaximumSize(16777215, 16777215)
            self.setMinimumSize(330, 285)
            self.resize(self._expanded_size)
        self.collapsed = collapsed
        self._drag_start = None
        self._drag_origin = None
        self._dragging = False
        self.drag_offset = None
        for shortcut in self._review_shortcuts:
            shortcut.setEnabled(not collapsed)
        if self.isVisible():
            (self.icon_button if collapsed else self.editor).setFocus(Qt.OtherFocusReason)
            self.keep_below_windows()
        self.view_changed.emit(collapsed)

    def keep_below_windows(self):
        """Retain the foreground layer without activating the editable tool."""
        if self._lowering or not self.isVisible():
            return
        self._lowering = True
        try:
            handle = self.windowHandle()
            if handle is not None:
                flags = handle.flags()
                desired = ((flags & ~Qt.WindowStaysOnBottomHint)
                           | Qt.WindowStaysOnTopHint)
                if flags != desired:
                    # QWindow flags keep the visible QWidget and its HWND alive.
                    handle.setFlags(desired)
            if _native_windows_surface():
                hwnd = int(self.winId())
                if not _native_is_topmost(hwnd):
                    _native_set_z_order(hwnd, -1)  # HWND_TOPMOST, no activation
            else:
                self.raise_()
        finally:
            self._lowering = False

    def toggle_collapsed(self):
        self.set_collapsed(not self.collapsed)

    def expand_notepad(self):
        self.set_collapsed(False)

    def eventFilter(self, obj, event):
        if obj in getattr(self, "_drag_widgets", ()):
            if (obj is self.icon_button and event.type() == QEvent.KeyPress
                    and event.modifiers() == Qt.NoModifier
                    and event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space)):
                self.expand_notepad()
                return True
            if event.type() == QEvent.MouseButtonDblClick and event.button() == Qt.LeftButton:
                self.toggle_collapsed()
                return True
            if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                self._drag_start = event.globalPos()
                self._drag_origin = self.pos()
                self._dragging = False
                if obj is self.icon_button:
                    self.icon_button.setFocus(Qt.MouseFocusReason)
                return True
            if event.type() == QEvent.MouseMove and self._drag_start is not None:
                difference = event.globalPos() - self._drag_start
                if difference.manhattanLength() >= QApplication.startDragDistance():
                    self._dragging = True
                if self._dragging:
                    self.move(self._drag_origin + difference)
                return True
            if event.type() == QEvent.MouseButtonRelease and self._drag_start is not None:
                moved = self._dragging
                self._drag_start = None
                self._drag_origin = None
                self._dragging = False
                if moved:
                    self._emit_moved()
                    self.keep_below_windows()
                return True
            if obj in (self.compact_panel, self.icon_button):
                if event.type() == QEvent.DragEnter:
                    self.dragEnterEvent(event)
                    return True
                if event.type() == QEvent.DragMove:
                    self.dragMoveEvent(event)
                    return True
                if event.type() == QEvent.Drop:
                    self.dropEvent(event)
                    return True
        return super().eventFilter(obj, event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.toggle_collapsed()
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)

    def _review_if_enabled(self):
        if not self.collapsed and self.review_button.isEnabled():
            self.review_requested.emit()

    def _draft_changed(self):
        self.draft_changed.emit(self.editor.toPlainText())

    def _emit_moved(self):
        self.moved.emit(self.x(), self.y())

    def set_draft(self, text):
        """Restore a draft without treating it as pasted input."""
        blocker = QSignalBlocker(self.editor)
        self.editor.setPlainText(text or "")
        del blocker

    def append_recognized_text(self, text):
        """Show extracted text without initiating another recognition pass."""
        text = str(text or "").strip()
        if not text:
            return
        cursor = self.editor.textCursor()
        cursor.movePosition(cursor.End)
        cursor.insertText(("\n\n" if self.editor.toPlainText().strip() else "") + text)
        self.editor.setTextCursor(cursor)

    def set_busy(self, busy):
        self.review_button.setEnabled(not busy)
        self.status_label.setText("일정 내용을 인식하고 있습니다…" if busy else CAPTURE_INSTRUCTION)

    def paste_clipboard(self):
        mime = QApplication.clipboard().mimeData()
        if self.editor.canInsertFromMimeData(mime):
            self.expand_notepad()
        self.editor.accept_mime(mime)

    def dragEnterEvent(self, event):
        self.editor.dragEnterEvent(event)

    def dragMoveEvent(self, event):
        self.editor.dragMoveEvent(event)

    def dropEvent(self, event):
        if self.editor.canInsertFromMimeData(event.mimeData()):
            self.expand_notepad()
        if self.editor.accept_mime(event.mimeData()):
            event.setDropAction(Qt.CopyAction)
            event.accept()
        else:
            event.ignore()
