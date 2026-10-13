"""Independent desktop reminders with foreground details when expanded."""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from datetime import date

from PyQt5.QtCore import QEvent, QSignalBlocker, QTimer, Qt, pyqtSignal
from PyQt5.QtGui import QBrush, QColor, QFont, QFontMetrics, QPen
from PyQt5.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QScrollArea, QSizePolicy, QStyle, QStyledItemDelegate,
    QStyleOptionViewItem, QVBoxLayout, QWidget,
)

from .core import Event, format_event_date
from .urgency import URGENCY_PALETTES, event_urgency


DESKTOP_STYLE = """
QWidget#desktopNote, QWidget#desktopList {
  background: #fff0ad; border: 1px solid #ddc872;
}
QLabel { color: #493e1e; background: transparent; border: none; }
QPushButton { background: transparent; color: #493e1e; border: none;
  border-radius: 4px; padding: 6px; text-align: left; }
QPushButton:hover, QPushButton:focus { background: #f3df8d; }
QPushButton#completeButton { background: #4c4124; color: #fff4c8;
  padding: 8px; text-align: center; min-height: 24px; }
QPushButton#completeButton:hover { background: #655831; }
QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; border: none; }
QListWidget { background: #fff5c9; color: #493e1e; border: none; padding: 2px; }
QListWidget::item { padding: 10px 6px; }
QScrollBar:vertical { background: #faeaaa; width: 12px; }
QScrollBar::handle:vertical { background: #b8a568; min-height: 24px; border-radius: 4px; }
QFrame#detailsDivider { background: #493e1e; border: none; }
"""

# Property selectors color only reminders, leaving the editable capture memo
# and the list's surrounding frame unchanged. Repolishing happens on a stage
# transition, rather than every periodic schedule refresh.
DESKTOP_STYLE += "\n".join(
    f'QWidget#desktopNote[urgency="{palette.code}"] {{ '
    f'background: {palette.background}; border: 1px solid {palette.border}; }}\n'
    f'QWidget#desktopNote[urgency="{palette.code}"] QPushButton#noteTitle:hover, '
    f'QWidget#desktopNote[urgency="{palette.code}"] QPushButton#noteTitle:focus {{ '
    f'background: {palette.hover}; }}'
    for palette in URGENCY_PALETTES
)


def _above_desktop_insert_after(own_hwnd: int) -> int | None:
    """Find the lowest visible position above Explorer's desktop surfaces.

    HWND_BOTTOM can be behind a visible desktop wallpaper window. Enumerating
    the current z-order lets us stay underneath other visible windows while
    keeping a normal independent tool window visible over the wallpaper.
    """
    user32 = ctypes.windll.user32
    user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    user32.GetClassNameW.restype = ctypes.c_int
    user32.IsWindowVisible.argtypes = (wintypes.HWND,)
    user32.IsWindowVisible.restype = wintypes.BOOL
    get_style = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
    get_style.argtypes = (wintypes.HWND, ctypes.c_int)
    get_style.restype = ctypes.c_ssize_t
    entries = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def collect(hwnd, _):
        if user32.IsWindowVisible(hwnd) and int(hwnd) != own_hwnd:
            class_name = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, class_name, len(class_name))
            entries.append((int(hwnd), class_name.value, bool(get_style(hwnd, -20) & 0x0008)))
        return True

    user32.EnumWindows.argtypes = (callback_type, wintypes.LPARAM)
    user32.EnumWindows.restype = wintypes.BOOL
    if not user32.EnumWindows(collect, 0):
        return None
    previous = 0  # HWND_TOP when the desktop is the first visible window.
    for hwnd, class_name, topmost in entries:
        if class_name in ("Progman", "WorkerW"):
            return previous
        if not topmost:
            previous = hwnd
    return None


def _native_windows_surface() -> bool:
    application = QApplication.instance()
    return bool(
        sys.platform == "win32" and application
        and application.platformName() == "windows"
    )


def _native_is_topmost(hwnd: int) -> bool:
    user32 = ctypes.windll.user32
    get_style = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
    get_style.argtypes = (wintypes.HWND, ctypes.c_int)
    get_style.restype = ctypes.c_ssize_t
    return bool(get_style(wintypes.HWND(hwnd), -20) & 0x0008)


def _native_set_z_order(hwnd: int, insert_after: int):
    set_window_pos = ctypes.windll.user32.SetWindowPos
    set_window_pos.argtypes = (
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, wintypes.UINT,
    )
    set_window_pos.restype = wintypes.BOOL
    flags = 0x0001 | 0x0002 | 0x0010 | 0x0200  # NOSIZE, NOMOVE, NOACTIVATE, NOOWNERZORDER
    return bool(set_window_pos(
        wintypes.HWND(hwnd), wintypes.HWND(insert_after), 0, 0, 0, 0, flags,
    ))


def event_details(event: Event) -> str:
    """Show the source's date precision and the complete, untruncated details."""
    timing = format_event_date(event, include_year=True)
    if event.date_precision != "month" and event.end_date and event.end_date != event.start_date:
        timing += " ~ " + event.end_date
    if event.time:
        timing += " · " + event.time
    return "\n\n".join(part for part in (
        event.title, timing,
        "장소: " + event.place if event.place else "",
        event.notes,
        "출처: " + event.source if event.source else "",
    ) if part)


def format_event_deadline(event: Event, include_year: bool = False) -> str:
    """Show the actual deadline without adding a day to month-only schedules."""
    if event.date_precision == "month":
        return format_event_date(event, include_year=include_year)
    for value in (event.end_date, event.start_date):
        if value:
            try:
                deadline = date.fromisoformat(value)
            except (TypeError, ValueError):
                continue
            weekday = "월화수목금토일"[deadline.weekday()]
            text = deadline.isoformat() if include_year else f"{deadline.month}/{deadline.day}"
            return f"{text}({weekday})"
    return "날짜 미정"


class _TitleButton(QPushButton):
    """A keyboard-accessible title with at most two readable lines."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.full_title = ""
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMinimumHeight(48)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_full_title(self, title: str):
        self.full_title = title
        self.setToolTip(title + "\n클릭하면 세부 내용을 펼칩니다.")
        self.setAccessibleName(title)
        self.setAccessibleDescription("Enter 또는 Space로 세부 내용 펼치기 / 접기")
        self._update_lines()

    def _update_lines(self):
        available = max(45, self.width() - 14)
        metrics = QFontMetrics(self.font())
        remaining = " ".join(self.full_title.split())
        lines = []
        for index in range(2):
            if not remaining:
                break
            if index == 1:
                lines.append(metrics.elidedText(remaining, Qt.ElideRight, available))
                break
            count = 0
            while count < len(remaining) and metrics.horizontalAdvance(remaining[:count + 1]) <= available:
                count += 1
            count = max(1, count)
            # Prefer splitting at a word boundary when it leaves a useful line.
            if count < len(remaining):
                boundary = remaining.rfind(" ", 0, count + 1)
                if boundary >= count // 2:
                    count = boundary
            lines.append(remaining[:count].rstrip())
            remaining = remaining[count:].lstrip()
        self.setText("\n".join(lines))
        self.setFixedHeight(max(44, metrics.lineSpacing() * max(1, len(lines)) + 16))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_lines()


class _UrgencyListDelegate(QStyledItemDelegate):
    """Keep an urgent row's color when selected, with a visible selection edge."""

    def paint(self, painter, option, index):
        styled = QStyleOptionViewItem(option)
        selected = bool(styled.state & QStyle.State_Selected)
        # Native selection gradients can alter even a custom highlight palette.
        # Paint the model's actual urgency brush, then indicate selection with
        # an outline so a selected deadline retains the same readable color.
        styled.state &= ~(QStyle.State_Selected | QStyle.State_HasFocus)
        super().paint(painter, styled, index)
        if selected:
            painter.save()
            painter.setPen(QPen(QColor("#493e1e"), 2))
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(styled.rect.adjusted(1, 1, -2, -2))
            painter.restore()


class _BottomWindow(QWidget):
    """No owner or close button; the default layer is below ordinary windows."""

    def __init__(self):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnBottomHint)
        self.allow_close = False
        self.drag_offset = None
        self._lowering = False
        self._lower_pending = False
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setStyleSheet(DESKTOP_STYLE)

    def keep_below_windows(self):
        if self._lowering or not self.isVisible():
            return
        self._lowering = True
        try:
            self.lower()
            # Offscreen test handles are not native HWNDs. Use native APIs only
            # for a Windows Qt surface, without changing size or stealing focus.
            if _native_windows_surface():
                hwnd = int(self.winId())
                _native_set_z_order(hwnd, -2)  # HWND_NOTOPMOST
                _native_set_z_order(hwnd, 1)   # HWND_BOTTOM
                insert_after = _above_desktop_insert_after(int(self.winId()))
                if insert_after is not None:
                    _native_set_z_order(hwnd, insert_after)
        finally:
            self._lowering = False

    def showEvent(self, event):
        super().showEvent(event)
        self.keep_below_windows()

    def event(self, event):
        result = super().event(event)
        if event.type() == QEvent.WindowActivate and not self._lowering and not self._lower_pending:
            self._lower_pending = True
            QTimer.singleShot(0, self._after_activation)
        return result

    def _after_activation(self):
        self._lower_pending = False
        self.keep_below_windows()

    def _make_handle(self):
        handle = QLabel("⠿")
        handle.setTextFormat(Qt.PlainText)
        handle.setCursor(Qt.SizeAllCursor)
        handle.setToolTip("드래그해서 이동")
        handle.setAccessibleName("바탕화면 알림 이동 영역")
        handle.setTextInteractionFlags(Qt.NoTextInteraction)
        handle.installEventFilter(self)
        return handle

    def eventFilter(self, obj, event):
        if obj is getattr(self, "handle", None):
            if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                self.drag_offset = event.globalPos() - self.frameGeometry().topLeft()
                return True
            if event.type() == QEvent.MouseMove and self.drag_offset is not None:
                self.move(event.globalPos() - self.drag_offset)
                return True
            if event.type() == QEvent.MouseButtonRelease and self.drag_offset is not None:
                self.drag_offset = None
                self._emit_moved()
                self.keep_below_windows()
                return True
        return super().eventFilter(obj, event)

    def closeEvent(self, event):
        application = QApplication.instance()
        if self.allow_close or (application and application.property("shutting_down")):
            event.accept()
        else:
            event.ignore()

    def finish(self):
        self.allow_close = True
        self.close()
        self.deleteLater()


class NoteWindow(_BottomWindow):
    completed = pyqtSignal(int)
    moved = pyqtSignal(int, int, int)
    size_changed = pyqtSignal()

    def __init__(self, event: Event, font_size: int = 12):
        super().__init__()
        self.event = event
        self.expanded = False
        self.setObjectName("desktopNote")
        self.setFixedWidth(340)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 12)
        layout.setSpacing(8)
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        self.date_label = QLabel()
        self.date_label.setTextFormat(Qt.PlainText)
        self.date_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.date_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
        self.date_label.setObjectName("deadlineLabel")
        title_row.addWidget(self.date_label)
        self.title_label = _TitleButton()
        self.title_label.setObjectName("noteTitle")
        self.title_label.setFont(QFont("Malgun Gothic", font_size, QFont.Bold))
        self.title_label.clicked.connect(self.toggle_details)
        title_row.addWidget(self.title_label, 1)
        self.handle = self._make_handle()
        title_row.addWidget(self.handle)
        layout.addLayout(title_row)
        self.details_label = QLabel()
        self.details_label.setTextFormat(Qt.PlainText)
        self.details_label.setWordWrap(True)
        self.details_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.details_label.setFont(QFont("Malgun Gothic", max(10, font_size - 2)))
        self.details_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.details_scroll = QScrollArea()
        self.details_scroll.setWidgetResizable(True)
        self.details_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.details_scroll.setWidget(self.details_label)
        self.details_scroll.setFixedHeight(220)
        layout.addWidget(self.details_scroll)
        self.complete_button = QPushButton("완료")
        self.complete_button.setObjectName("completeButton")
        self.complete_button.clicked.connect(self._complete)
        layout.addWidget(self.complete_button)
        self.set_font_size(font_size)
        self.update_event(event)
        self.set_expanded(False)

    def set_font_size(self, font_size: int):
        self.date_label.setFont(QFont("Malgun Gothic", max(10, font_size - 1), QFont.Bold))
        metrics = QFontMetrics(self.date_label.font())
        self.date_label.setFixedWidth(max(76, metrics.horizontalAdvance("12/31(목)") + 8))
        self.title_label.setFont(QFont("Malgun Gothic", font_size, QFont.Bold))
        self.details_label.setFont(QFont("Malgun Gothic", max(10, font_size - 2)))
        self.title_label._update_lines()
        self.adjustSize()

    def update_event(self, event: Event, today: date | None = None):
        self.event = event
        self.setWindowTitle(event.title)
        self.date_label.setText(format_event_deadline(event))
        self.date_label.setToolTip(format_event_deadline(event, include_year=True))
        self.date_label.setAccessibleName("마감 날짜 " + self.date_label.toolTip())
        self.title_label.set_full_title(event.title)
        self.details_label.setText(event_details(event))
        self.urgency_state = event_urgency(event, today)
        style_code = self.urgency_state.code
        if style_code in ("month", "undated"):
            style_code = "normal"
        if self.property("urgency") != style_code:
            self.setProperty("urgency", style_code)
            for widget in (self, self.title_label):
                widget.style().unpolish(widget)
                widget.style().polish(widget)
                widget.update()
        self.adjustSize()

    def set_expanded(self, expanded: bool):
        self.expanded = bool(expanded)
        self._sync_window_layer()
        self.details_scroll.setVisible(self.expanded)
        self.complete_button.setVisible(self.expanded)
        self.title_label.setAccessibleDescription(
            "Enter 또는 Space로 세부 내용 접기" if self.expanded else "Enter 또는 Space로 세부 내용 펼치기"
        )
        self.adjustSize()
        self.size_changed.emit()
        if self.expanded:
            self._bring_details_forward()
        else:
            self.keep_below_windows()

    def _sync_window_layer(self):
        handle = self.windowHandle()
        if handle is None:
            return
        # Qt's Windows backend intercepts native window placement according to
        # its layer hints. Update that backend before using SetWindowPos; native
        # TOPMOST alone is ineffective while it still has the bottom hint.
        # QWindow.setFlags retains the visible QWidget, HWND, focus and geometry.
        # QWidget.setWindowFlags would hide the card during every toggle.
        flags = handle.flags()
        desired = flags & ~(Qt.WindowStaysOnTopHint | Qt.WindowStaysOnBottomHint)
        desired |= Qt.WindowStaysOnTopHint if self.expanded else Qt.WindowStaysOnBottomHint
        if flags != desired:
            handle.setFlags(desired)

    def keep_below_windows(self):
        # The owner calls this after periodic refreshes, alignment changes and
        # activation. An expanded card must survive all of those calls at the
        # foreground layer, while compact cards retain the usual desktop layer.
        if not getattr(self, "expanded", False):
            self._sync_window_layer()
            return super().keep_below_windows()
        if self._lowering or not self.isVisible():
            return
        self._lowering = True
        try:
            self._sync_window_layer()
            if _native_windows_surface():
                hwnd = int(self.winId())
                # Preserve the order of already expanded cards during refresh;
                # only a user's explicit expansion brings a card to the front.
                if not _native_is_topmost(hwnd):
                    _native_set_z_order(hwnd, -1)  # HWND_TOPMOST
            else:
                self.raise_()
        finally:
            self._lowering = False

    def _bring_details_forward(self):
        if self._lowering or not self.isVisible():
            return
        self._lowering = True
        try:
            self._sync_window_layer()
            self.raise_()
            if _native_windows_surface():
                # Promote the existing HWND after updating the backend hint.
                _native_set_z_order(int(self.winId()), -1)  # HWND_TOPMOST
        finally:
            self._lowering = False

    def toggle_details(self):
        self.set_expanded(not self.expanded)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Title wrapping can settle after the window is first shown. Keep the
        # selected screen edge anchored when that changes the compact height.
        self.size_changed.emit()

    def _complete(self):
        if self.event.id is not None:
            self.completed.emit(int(self.event.id))

    def _emit_moved(self):
        if self.event.id is not None:
            self.moved.emit(int(self.event.id), self.x(), self.y())


class DesktopListWindow(_BottomWindow):
    completed = pyqtSignal(int)
    moved = pyqtSignal(int, int)
    size_changed = pyqtSignal()

    def __init__(self, events: list[Event], font_size: int = 12):
        super().__init__()
        self.events: dict[int, Event] = {}
        self.selected_event_id: int | None = None
        self.expanded = False
        self.setObjectName("desktopList")
        self.setWindowTitle("학교 알리미 · 일정 목록")
        self.setFixedWidth(360)
        self.resize(360, 350)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(8)
        header = QHBoxLayout()
        heading = QLabel("일정 목록")
        heading.setFont(QFont("Malgun Gothic", font_size, QFont.Bold))
        header.addWidget(heading, 1)
        self.handle = self._make_handle()
        header.addWidget(self.handle)
        layout.addLayout(header)
        self.list_widget = QListWidget()
        self.list_widget.setItemDelegate(_UrgencyListDelegate(self.list_widget))
        self.list_widget.setFont(QFont("Malgun Gothic", font_size))
        self.list_widget.setMinimumHeight(200)
        self.list_widget.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list_widget.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.list_widget.setTextElideMode(Qt.ElideRight)
        self.list_widget.setAccessibleName("미완료 일정 제목 목록")
        self.list_widget.setToolTip("제목을 클릭하거나 Enter를 누르면 세부 내용을 표시합니다.")
        self.list_widget.itemClicked.connect(self._show_item)
        self.list_widget.itemActivated.connect(self._show_item)
        self.list_widget.installEventFilter(self)
        layout.addWidget(self.list_widget, 1)
        self.details_panel = QFrame()
        panel_layout = QVBoxLayout(self.details_panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        panel_layout.setSpacing(8)
        self.details_divider = QFrame()
        self.details_divider.setObjectName("detailsDivider")
        self.details_divider.setFixedHeight(3)
        self.details_divider.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.details_divider.setAccessibleName("일정 목록과 세부 내용 구분선")
        panel_layout.addWidget(self.details_divider)
        self.details_label = QLabel()
        self.details_label.setTextFormat(Qt.PlainText)
        self.details_label.setWordWrap(True)
        self.details_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.details_label.setFont(QFont("Malgun Gothic", max(10, font_size - 2)))
        self.details_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.details_scroll = QScrollArea()
        self.details_scroll.setWidgetResizable(True)
        self.details_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.details_scroll.setWidget(self.details_label)
        self.details_scroll.setFixedHeight(180)
        panel_layout.addWidget(self.details_scroll)
        self.complete_button = QPushButton("완료")
        self.complete_button.setObjectName("completeButton")
        self.complete_button.clicked.connect(self._complete)
        panel_layout.addWidget(self.complete_button)
        layout.addWidget(self.details_panel)
        self.details_panel.hide()
        self.update_events(events)

    def update_events(self, events: list[Event], today: date | None = None):
        scroll = self.list_widget.verticalScrollBar().value()
        current = self.list_widget.currentItem()
        current_id = current.data(Qt.UserRole) if current else None
        was_expanded = self.expanded
        blocker = QSignalBlocker(self.list_widget)
        self.events = {int(event.id): event for event in events if event.id is not None}
        self.list_widget.clear()
        for event_id, event in self.events.items():
            item = QListWidgetItem(event.title)
            item.setData(Qt.UserRole, event_id)
            item.setToolTip(event.title)
            urgency = event_urgency(event, today)
            item.setBackground(QBrush(QColor(urgency.background)))
            item.setForeground(QBrush(QColor(urgency.foreground)))
            item.setData(Qt.AccessibleDescriptionRole, urgency.label)
            self.list_widget.addItem(item)
            if event_id == current_id:
                self.list_widget.setCurrentItem(item)
        del blocker
        if self.selected_event_id in self.events and was_expanded:
            self._show_details(self.selected_event_id, resize=False)
        else:
            self.selected_event_id = None
            self.expanded = False
            self.details_label.clear()
            self.details_panel.hide()
            if was_expanded:
                self.resize(self.width(), 350)
                self.size_changed.emit()
        # Rebuilding the model invalidates its scrollbar range until layout.
        # Recompute it before restoring position so a stale range cannot clamp it.
        self.list_widget.doItemsLayout()
        self.list_widget.verticalScrollBar().setValue(scroll)
        self.keep_below_windows()

    def _show_item(self, item: QListWidgetItem):
        self.set_selected_event(int(item.data(Qt.UserRole)))

    def set_selected_event(self, event_id: int):
        if event_id in self.events:
            for row in range(self.list_widget.count()):
                item = self.list_widget.item(row)
                if item.data(Qt.UserRole) == event_id:
                    self.list_widget.setCurrentItem(item)
                    break
            self._show_details(event_id)

    def _show_details(self, event_id: int, resize: bool = True):
        changed = not self.expanded
        self.selected_event_id = event_id
        self.expanded = True
        self.details_label.setText(event_details(self.events[event_id]))
        self.details_panel.show()
        if changed and resize:
            self.adjustSize()
            self.size_changed.emit()
        self.keep_below_windows()

    def eventFilter(self, obj, event):
        if obj is getattr(self, "list_widget", None) and event.type() == QEvent.KeyPress:
            if event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
                item = self.list_widget.currentItem()
                if item:
                    self._show_item(item)
                    return True
        return super().eventFilter(obj, event)

    def _complete(self):
        if self.selected_event_id in self.events:
            self.completed.emit(self.selected_event_id)

    def _emit_moved(self):
        self.moved.emit(self.x(), self.y())
