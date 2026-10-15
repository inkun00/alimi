"""Editable import confirmation. This module never writes schedule storage."""

from __future__ import annotations

from calendar import monthrange
from dataclasses import replace
from datetime import date
import re
from typing import Iterable

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QAbstractItemView, QApplication, QDialog, QHBoxLayout, QHeaderView, QLabel,
    QMessageBox, QPlainTextEdit, QPushButton, QSplitter, QTableWidget, QTableWidgetItem,
    QTabWidget, QVBoxLayout, QWidget,
)

from .core import Event, Store


class ImportReviewDialog(QDialog):
    """Review a batch before the owner commits the confirmed signal to storage.

    Every row owns a copy of its original event. Table edits and notes therefore
    cannot mutate parser output or a previous database object when cancelled.
    """

    confirmed = pyqtSignal(object)
    recognized_text_changed = pyqtSignal(str)
    INCLUDE, TITLE, DATE, END_DATE, TIME, PLACE = range(6)

    def __init__(self, candidates: Iterable[Event], errors: list[str] | None = None, parent=None,
                 *, recognized_text: str | None = None):
        super().__init__(parent)
        candidates = list(candidates)
        self.setWindowTitle("일정 확인 후 등록")
        self.setWindowFlags(Qt.Dialog | Qt.WindowTitleHint | Qt.WindowSystemMenuHint | Qt.WindowCloseButtonHint)
        self.setWindowModality(Qt.NonModal)
        self.resize(990, 670)
        self.setMinimumSize(720, 510)
        self._events: list[Event] = []
        self._original_events: list[Event] = []
        self._detail_row = -1
        self._loading_details = False
        self.confirmed_events: list[Event] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)
        self.summary_label = QLabel()
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)
        self.import_errors = QLabel()
        self.import_errors.setWordWrap(True)
        self.import_errors.setStyleSheet("color: #8b4513;")
        self._errors: list[str] = []
        layout.addWidget(self.import_errors)

        splitter = QSplitter(Qt.Vertical)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["포함", "제목", "날짜 / 월", "종료일", "시간", "장소"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setSectionResizeMode(self.TITLE, QHeaderView.Stretch)
        self.table.setColumnWidth(self.INCLUDE, 46)
        self.table.setColumnWidth(self.DATE, 124)
        self.table.setColumnWidth(self.END_DATE, 112)
        self.table.setColumnWidth(self.TIME, 76)
        self.table.setColumnWidth(self.PLACE, 130)
        self.table.currentCellChanged.connect(self._show_details)
        self.table.itemChanged.connect(self._item_changed)
        splitter.addWidget(self.table)

        details = QWidget()
        details_layout = QVBoxLayout(details)
        details_layout.setContentsMargins(0, 0, 0, 0)
        self.event_status_label = QLabel()
        self.event_status_label.setWordWrap(True)
        details_layout.addWidget(self.event_status_label)
        tabs = QTabWidget()
        recognized_page = QWidget()
        recognized_layout = QVBoxLayout(recognized_page)
        recognized_layout.setContentsMargins(0, 8, 0, 0)
        recognized_help = QLabel(
            "인식한 내용을 자유롭게 수정할 수 있습니다. 위 일정 표의 내용은 바뀌지 않습니다.\n"
            "수정한 텍스트는 관리 창 입력칸에 반영되고 PC에 자동 저장됩니다."
        )
        recognized_help.setWordWrap(True)
        recognized_layout.addWidget(recognized_help)
        self.recognized_editor = QPlainTextEdit()
        self.recognized_editor.setAccessibleName("인식한 텍스트 수정")
        self.recognized_editor.setPlaceholderText("일정 외 안내 내용과 인식 오류도 수정할 수 있습니다.")
        if recognized_text is None:
            recognized_text = "\n\n".join(dict.fromkeys(event.source_text for event in candidates if event.source_text))
        self.recognized_editor.setPlainText(recognized_text)
        self.recognized_editor.textChanged.connect(
            lambda: self.recognized_text_changed.emit(self.recognized_editor.toPlainText()))
        recognized_layout.addWidget(self.recognized_editor, 1)
        tabs.addTab(recognized_page, "인식한 텍스트")
        self.notes_editor = QPlainTextEdit()
        self.notes_editor.setPlaceholderText("선택한 일정의 세부 내용을 수정할 수 있습니다.")
        self.notes_editor.textChanged.connect(self._notes_changed)
        tabs.addTab(self.notes_editor, "세부 내용")
        self.source_text = QPlainTextEdit()
        self.source_text.setReadOnly(True)
        tabs.addTab(self.source_text, "일정 원문 (참고)")
        details_layout.addWidget(tabs)
        splitter.addWidget(details)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)

        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #b3261e;")
        layout.addWidget(self.error_label)
        buttons = QHBoxLayout()
        self.add_button = QPushButton("일정 추가")
        self.add_button.clicked.connect(lambda: self.add_event())
        self.delete_button = QPushButton("선택 행 삭제")
        self.delete_button.clicked.connect(self.delete_current_event)
        buttons.addWidget(self.add_button)
        buttons.addWidget(self.delete_button)
        buttons.addStretch(1)
        self.cancel_button = QPushButton("취소")
        self.cancel_button.clicked.connect(self.reject)
        self.register_button = QPushButton("등록")
        self.register_button.setDefault(True)
        self.register_button.setStyleSheet("background: #285f4c; color: white; padding: 8px 26px;")
        self.register_button.clicked.connect(self.confirm_registration)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.register_button)
        layout.addLayout(buttons)
        self.append_candidates(candidates, errors)

    def closeEvent(self, event) -> None:
        application = QApplication.instance()
        if application and application.property("shutting_down"):
            super().closeEvent(event)
            return
        answer = QMessageBox.warning(
            self, "일정 확인 창 닫기",
            "확인 창을 닫을까요?\n등록하지 않은 일정 수정 내용은 저장되지 않습니다.\n"
            "수정한 인식 텍스트는 PC에 초안으로 보관됩니다.",
            QMessageBox.Ok | QMessageBox.Cancel, QMessageBox.Cancel,
        )
        if answer == QMessageBox.Ok:
            super().closeEvent(event)
        else:
            event.ignore()

    def _update_summary(self) -> None:
        count = self.table.rowCount()
        if count:
            self.summary_label.setText(f"일정 {count}건을 찾았습니다. 내용을 수정한 뒤 등록하세요. 선택한 일정만 저장됩니다.")
            if any(event.id is not None for event in self._events):
                self.summary_label.setText(self.summary_label.text() +
                    " 이미 등록된 일정은 수정한 내용만 반영하며, 완료 상태는 유지합니다.")
        else:
            self.summary_label.setText("인식된 일정이 없습니다. '일정 추가'로 제목과 날짜를 입력해 등록할 수 있습니다.")

    def append_candidates(self, candidates: Iterable[Event], errors: list[str] | None = None) -> None:
        """Append worker results to one existing popup without losing edits."""
        for event in candidates:
            self.add_event(event, select=False)
        if errors:
            self._errors.extend(errors)
        self.import_errors.setText("\n".join(self._errors))
        self.import_errors.setVisible(bool(self._errors))
        self._update_summary()
        if self.table.rowCount() and self.table.currentRow() < 0:
            self.table.setCurrentCell(0, self.TITLE)

    def add_event(self, event: Event | None = None, select: bool = True) -> int:
        self._save_details()
        event = replace(event) if event is not None else Event(source="직접 입력")
        row = self.table.rowCount()
        self._events.append(event)
        self._original_events.append(replace(event))
        self.table.blockSignals(True)
        self.table.insertRow(row)
        include = QTableWidgetItem()
        include.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable)
        include.setCheckState(Qt.Checked)
        self.table.setItem(row, self.INCLUDE, include)
        displayed_date = event.start_date[:7] if event.date_precision == "month" else event.start_date
        end_date = "" if event.date_precision == "month" else event.end_date or ""
        values = (event.title, displayed_date, end_date, event.time or "", event.place)
        for column, value in enumerate(values, self.TITLE):
            item = QTableWidgetItem(value)
            if column in (self.DATE, self.END_DATE):
                item.setToolTip("월 일정: YYYY-MM / 날짜 일정: YYYY-MM-DD" if column == self.DATE else "기간이 있는 날짜 일정의 종료일: YYYY-MM-DD")
            self.table.setItem(row, column, item)
        self.table.blockSignals(False)
        if select:
            self.table.setCurrentCell(row, self.TITLE)
            self.table.scrollToItem(self.table.item(row, self.TITLE))
        self._update_summary()
        return row

    def delete_current_event(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        self._save_details()
        self._detail_row = -1
        self.table.blockSignals(True)
        self.table.removeRow(row)
        self._events.pop(row)
        self._original_events.pop(row)
        self.table.blockSignals(False)
        next_row = min(row, self.table.rowCount() - 1)
        if next_row >= 0:
            self.table.setCurrentCell(next_row, self.TITLE)
            self._show_details(next_row, self.TITLE, -1, -1)
        else:
            self._show_details(-1, -1, -1, -1)
        self._update_summary()

    def _save_details(self) -> None:
        if 0 <= self._detail_row < len(self._events) and not self._loading_details:
            self._events[self._detail_row].notes = self.notes_editor.toPlainText()

    def _notes_changed(self) -> None:
        self._save_details()

    def _show_details(self, row: int, _column: int, _previous_row: int, _previous_column: int) -> None:
        self._save_details()
        self._detail_row = row
        self._loading_details = True
        event = self._events[row] if 0 <= row < len(self._events) else None
        self.notes_editor.setEnabled(event is not None)
        self.notes_editor.setPlainText(event.notes if event else "")
        self.source_text.setPlainText(event.source_text if event else "")
        status = ""
        if event is None:
            status = "행을 선택하면 세부 내용이 표시됩니다."
        elif event.id is not None:
            status = ("기존 완료 일정 (수정해도 완료 상태 유지)" if event.status == "done" else "기존 일정 수정")
        self.event_status_label.setText(status)
        self.event_status_label.setVisible(bool(status))
        self._loading_details = False

    def _item_changed(self, _item: QTableWidgetItem) -> None:
        self.error_label.clear()

    @staticmethod
    def _parse_date(value: str, row_number: int) -> tuple[str, str]:
        if re.fullmatch(r"\d{4}-\d{2}", value):
            try:
                date.fromisoformat(value + "-01")
            except ValueError:
                raise ValueError(f"{row_number}행: 올바른 월을 입력해 주세요. (YYYY-MM)") from None
            return value + "-01", "month"
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            try:
                date.fromisoformat(value)
            except ValueError:
                raise ValueError(f"{row_number}행: 올바른 날짜를 입력해 주세요. (YYYY-MM-DD)") from None
            return value, "day"
        raise ValueError(f"{row_number}행: 날짜 또는 월을 입력해 주세요. (YYYY-MM-DD / YYYY-MM)")

    def selected_events(self) -> list[Event]:
        """Return all selected edits only when the entire selection is valid."""
        self._save_details()
        selected = []
        for row, original in enumerate(self._events):
            if self.table.item(row, self.INCLUDE).checkState() != Qt.Checked:
                continue
            value = lambda column: self.table.item(row, column).text().strip()
            start_date, precision = self._parse_date(value(self.DATE), row + 1)
            if precision == "month":
                when = date.fromisoformat(start_date)
                if value(self.END_DATE):
                    raise ValueError(f"{row + 1}행: 월 일정의 종료일은 비워 주세요. 해당 월 전체에 표시됩니다.")
                end_date = date(when.year, when.month, monthrange(when.year, when.month)[1]).isoformat()
            else:
                end_date = value(self.END_DATE) or None
                if end_date:
                    parsed_end, end_precision = self._parse_date(end_date, row + 1)
                    if end_precision != "day":
                        raise ValueError(f"{row + 1}행: 종료일은 YYYY-MM-DD로 입력해 주세요.")
                    end_date = parsed_end
            event = replace(
                original, id=None, title=value(self.TITLE), start_date=start_date,
                date_precision=precision, end_date=end_date, time=value(self.TIME) or None,
                place=value(self.PLACE), status="open", completed_at=None,
                created_at="", x=None, y=None,
            )
            try:
                Store._validate(event)
            except ValueError as exc:
                raise ValueError(f"{row + 1}행: {exc}") from None
            baseline = self._original_events[row]
            if baseline.id is not None and (baseline.status == "review" or
                    self._editable_values(event) != self._editable_values(baseline)):
                event.id = baseline.id
            selected.append(event)
        if not selected:
            raise ValueError("등록할 일정을 하나 이상 선택해 주세요.")
        return selected

    @staticmethod
    def _editable_values(event: Event) -> tuple:
        return (event.title, event.start_date, event.end_date or "", event.date_precision,
                event.time or "", event.place, event.notes)

    def confirm_registration(self) -> None:
        try:
            selected = self.selected_events()
        except ValueError as exc:
            self.error_label.setText(str(exc))
            return
        self.confirmed_events = selected
        self.error_label.clear()
        self.confirmed.emit(selected)
