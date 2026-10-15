"""Offline Korean OCR backed by bundled PP-OCRv5 ONNX models.

No HTTP client, subprocess or external OCR service is used. All images and
inference stay in the current Windows process. Sessions are initialized lazily
so that starting the reminder application does not load OCR models into memory.

The preprocessing / CTC model contract is based on PaddleOCR and RapidOCR
(Apache-2.0). See assets/ocr/NOTICE.txt for model provenance and licenses.
"""
from __future__ import annotations

import hashlib
from calendar import monthrange
from datetime import date
import importlib.util
import math
from pathlib import Path
import re
import statistics
import sys
import threading
import unicodedata
from dataclasses import dataclass


MODELS = {
    "ch_PP-OCRv5_det_mobile.onnx": "4d97c44a20d30a81aad087d6a396b08f786c4635742afc391f6621f5c6ae78ae",
    "korean_PP-OCRv5_rec_mobile.onnx": "cd6e2ea50f6943ca7271eb8c56a877a5a90720b7047fe9c41a2e541a25773c9b",
}


@dataclass(frozen=True)
class _TextFragment:
    left: float
    top: float
    right: float
    bottom: float
    text: str

    @property
    def cy(self):
        return (self.top + self.bottom) / 2

    @property
    def height(self):
        return self.bottom - self.top


def _text_rows(fragments):
    """Keep ordinary document/table rows intact without mixing panel columns."""
    rows = []
    for fragment in sorted(fragments, key=lambda item: (item.top, item.left)):
        target = next((row for row in rows if abs(row[0].cy - fragment.cy)
                       <= max(8.0, min(row[0].height, fragment.height) * 0.45)), None)
        if target is None:
            rows.append([fragment])
        else:
            target.append(fragment)
    return [sorted(row, key=lambda item: item.left)
            for row in sorted(rows, key=lambda row: row[0].cy)]


def _rows_text(fragments):
    return "\n".join("  ".join(item.text for item in row) for row in _text_rows(fragments))


_ENGLISH_MONTH = (r"January|February|March|April|May|June|July|August|September|"
                  r"October|November|December")
_MONTH_HEADING = re.compile(r"(?:(\d{4})\s*(?:년|[.])\s*)?(1[0-2]|[1-9])\s*월[.]?"
                            rf"\s*(?:{_ENGLISH_MONTH})?", re.IGNORECASE)
_ENGLISH_MONTH_ONLY = re.compile(rf"(?:{_ENGLISH_MONTH})", re.IGNORECASE)
_SCHOOL_DAY_TOTAL = re.compile(r"수업\s*일수\s*[:：]?\s*\d+\s*일?\s*")
_CALENDAR_DATE_START = re.compile(r"^[^\w가-힣]*\d{1,2}\s*(?:일(?=\s|$)|[（(]|[./]\s*\d{1,2})")
_CALENDAR_EXPLICIT_MONTH = re.compile(r"^[^\w가-힣]*(\d{1,2})\s*[./]\s*\d{1,2}")


def _grid_numbers(text):
    tokens = text.split()
    return bool(tokens) and all(token.isdecimal() and 1 <= int(token) <= 31 for token in tokens)


def _calendar_body(fragments, *, drop_totals=True):
    return [item for item in fragments if any(ch.isalnum() for ch in item.text)
            and not _grid_numbers(item.text)
            and not (drop_totals and _SCHOOL_DAY_TOTAL.fullmatch(item.text.strip()))]


def _calendar_date_rows(fragments):
    """Count observable leading dates, without guessing or rewriting dates."""
    return sum(bool(_CALENDAR_DATE_START.match(" ".join(item.text for item in row)))
               for row in _text_rows(fragments))


def _unexpected_calendar_months(fragments, month):
    months = [_CALENDAR_EXPLICIT_MONTH.match(" ".join(item.text for item in row))
              for row in _text_rows(fragments)]
    return sum(int(match.group(1)) != month for match in months if match)


@dataclass(frozen=True)
class _CalendarPanel:
    heading: _TextFragment
    left: float
    top: float
    right: float
    bottom: float
    fragments: tuple[_TextFragment, ...]
    side_list: bool = False


def _below_calendar_panels(fragments, width, height):
    """Recognize a month-panel calendar only with weekday and numeric grids.

    A normal table with month headings must retain its original row order. The
    repeated 7-day calendars provide the extra evidence needed to read each
    month's list down its own column instead of joining neighboring schedules.
    Coordinates come from OCR boxes, never a particular document template.
    """
    headings = [item for item in fragments if _MONTH_HEADING.fullmatch(item.text.strip())]
    heading_rows = _text_rows(headings)
    if not heading_rows or any(len(row) < 2 for row in heading_rows):
        return []
    panels = []
    for index, headers in enumerate(heading_rows):
        centers = [(item.left + item.right) / 2 for item in headers]
        gaps = [right - left for left, right in zip(centers, centers[1:])]
        spacing = statistics.median(gaps)
        if min(gaps) < max(item.height for item in headers) * 3 or max(gaps) > min(gaps) * 1.5:
            return []
        months = [_MONTH_HEADING.fullmatch(item.text.strip()).group(2) for item in headers]
        if len(set(months)) != len(months):
            return []
        boundaries = [max(0, centers[0] - spacing / 2)]
        boundaries.extend((left + right) / 2 for left, right in zip(centers, centers[1:]))
        boundaries.append(min(width, centers[-1] + spacing / 2))
        bottom = min(item.top for item in heading_rows[index + 1]) if index + 1 < len(heading_rows) else height
        for column, heading in enumerate(headers):
            left, right = boundaries[column:column + 2]
            inside = [item for item in fragments
                      if heading.cy < item.cy < bottom and left <= (item.left + item.right) / 2 < right]
            rows = _text_rows(inside)
            weekdays = next((n for n, row in enumerate(rows)
                             if sum(sum(ch in "일월화수목금토" for ch in item.text)
                                    for item in row if all(ch in "일월화수목금토 " for ch in item.text)) >= 5), None)
            if weekdays is None or weekdays > 1:
                return []
            grid = []
            for row in rows[weekdays + 1:]:
                if not all(_grid_numbers(item.text) for item in row):
                    break
                grid.append(row)
            if len(grid) < 3 or sum(len(item.text.split()) for row in grid for item in row) < 14:
                return []
            last_grid = grid[-1]
            # Include a little blank space after the last number row. Keeping
            # the original fragment fallback prevents losing a faint first list
            # line if the refined detector does not rediscover it.
            top = max(item.cy for item in last_grid) + min(item.height for item in last_grid) * 0.25
            body = tuple(_calendar_body([item for item in inside if item.cy > top]))
            panels.append(_CalendarPanel(heading, left, top, right, bottom, body))
    return panels


def _weekday_count(text):
    compact = text.replace(" ", "")
    return len(compact) if compact and all(ch in "일월화수목금토" for ch in compact) else 0


def _month_headings(fragments):
    """Join an English month label only when it is adjacent on the same row."""
    headings = []
    for item in fragments:
        if not _MONTH_HEADING.fullmatch(item.text.strip()):
            continue
        suffix = next((other for other in fragments
                       if _ENGLISH_MONTH_ONLY.fullmatch(other.text.strip())
                       and abs(other.cy - item.cy) <= min(other.height, item.height) * 0.5
                       and -item.height * 0.25 <= other.left - item.right <= item.height), None)
        if suffix is not None:
            item = _TextFragment(item.left, min(item.top, suffix.top), suffix.right,
                                 max(item.bottom, suffix.bottom), item.text + " " + suffix.text)
        headings.append(item)
    return headings


def _side_calendar_panels(fragments, width, height):
    """Find calendars whose dated lists sit to the right of each numeric grid.

    Header centers do not divide these panels: a short month header is centered
    over the narrow calendar, not over the calendar plus its schedule list. The
    repeated weekday grids supply both the list's left edge and the next
    panel's boundary, so long descriptions retain their complete width.
    """
    heading_rows = _text_rows(_month_headings(fragments))
    if not heading_rows or any(len(row) < 2 or len(row) != len(heading_rows[0]) for row in heading_rows):
        return []
    columns = [[] for _ in heading_rows[0]]
    for index, headers in enumerate(heading_rows):
        if len({_MONTH_HEADING.fullmatch(item.text.strip()).group(2) for item in headers}) != len(headers):
            return []
        bottom = min(item.top for item in heading_rows[index + 1]) if index + 1 < len(heading_rows) else height
        grids = []
        for column, heading in enumerate(headers):
            low = heading.left - heading.height
            high = headers[column + 1].left - heading.height if column + 1 < len(headers) else width
            candidates = [item for item in fragments if low <= (item.left + item.right) / 2 < high
                          and heading.cy < item.cy < min(bottom, heading.bottom + heading.height * 2)
                          and _weekday_count(item.text)]
            weekdays = next((row for row in _text_rows(candidates)
                             if sum(_weekday_count(item.text) for item in row) >= 5), None)
            if weekdays is None:
                return []
            grid_left = min(item.left for item in weekdays)
            grid_right = max(item.right for item in weekdays)
            grid_cy = statistics.median(item.cy for item in weekdays)
            tolerance = statistics.median(item.height for item in weekdays) * 0.35
            numbers = [item for item in fragments if grid_left - tolerance <= (item.left + item.right) / 2 <= grid_right + tolerance
                       and grid_cy + tolerance < item.cy < bottom and _grid_numbers(item.text)]
            number_rows = _text_rows(numbers)
            if len(number_rows) < 3 or sum(len(item.text.split()) for item in numbers) < 14:
                return []
            grids.append((heading, grid_left, grid_right, tolerance, max(item.cy for item in numbers)))
        for column, (heading, grid_left, grid_right, tolerance, grid_bottom) in enumerate(grids):
            right = grids[column + 1][1] if column + 1 < len(grids) else width
            body = tuple(_calendar_body([item for item in fragments
                                        if grid_right + tolerance * 0.1 < (item.left + item.right) / 2 < right
                                        and heading.cy < item.cy < bottom], drop_totals=False))
            # A dated row beside the numeric grid distinguishes this layout
            # from the existing calendar-with-list-below layout.
            if not body or not any(_CALENDAR_DATE_START.match(item.text.strip())
                                   and item.cy < grid_bottom
                                   for item in body):
                return []
            top = min(item.top for item in body) - tolerance * 0.25
            columns[column].append(_CalendarPanel(heading, max(0, grid_right - tolerance), max(0, top),
                                                  right, bottom, body, True))
    # A side-list document is usually laid out as two vertical semesters.
    # Keeping each column intact also preserves explicit January year labels.
    return [panel for column in columns for panel in column]


def _calendar_panels(fragments, width, height):
    return (_below_calendar_panels(fragments, width, height)
            or _side_calendar_panels(fragments, width, height))


def _calendar_rows_text(fragments, *, join_continuations=False):
    rows = ["  ".join(item.text for item in row) for row in _text_rows(fragments)]
    if not join_continuations:
        return "\n".join(rows)
    lines = []
    for row in rows:
        if lines and not _CALENDAR_DATE_START.match(row):
            lines[-1] += " " + row
        else:
            lines.append(row)
    return "\n".join(lines)


_CELL_MONTH_TITLE = re.compile(
    r"(?:(?P<year>19\d{2}|20\d{2})\s*년\s*)?(?P<month>1[0-2]|0?[1-9])\s*월"
    r"\s*(?:주간\s*일정|주간\s*계획|월간\s*일정|월간\s*계획)")
_CELL_DAY = re.compile(r"(\d{1,2})\s*일?")


def _cell_body_text(items):
    """Keep multiple lines in one proved day cell, including its clock line."""
    return " ".join(" ".join(item.text for item in row) for row in _text_rows(items))


def _weekly_cell_text(fragments, width, height):
    """Read explicit consecutive day columns only below a weekly/monthly title.

    A row of numbers alone is not evidence of schedule dates. The month title,
    explicit ``N일`` headers and repeated, evenly spaced columns must agree.
    """
    titles = [(item, match) for item in fragments
              if (match := _CELL_MONTH_TITLE.fullmatch(item.text.strip()))]
    if len(titles) != 1:
        return None
    title, month = titles[0]
    candidates = [item for item in fragments if item.top > title.bottom
                  and re.fullmatch(r"\d{1,2}\s*일", item.text.strip())]
    rows = _text_rows(candidates)
    if len(rows) != 1 or not 3 <= len(rows[0]) <= 7:
        return None
    headers = rows[0]
    days = [int(_CELL_DAY.fullmatch(item.text.strip()).group(1)) for item in headers]
    if days != list(range(days[0], days[0] + len(days))) or not 1 <= days[0] <= days[-1] <= 31:
        return None
    if month["year"] and days[-1] > monthrange(int(month["year"]), int(month["month"]))[1]:
        return None
    centers = [(item.left + item.right) / 2 for item in headers]
    gaps = [b - a for a, b in zip(centers, centers[1:])]
    pitch = statistics.median(gaps)
    if min(gaps) <= max(item.height for item in headers) * 2 or max(gaps) > min(gaps) * 1.25:
        return None
    edges = [max(0, centers[0] - pitch / 2)]
    edges.extend((a + b) / 2 for a, b in zip(centers, centers[1:]))
    edges.append(min(width, centers[-1] + pitch / 2))
    body = [item for item in fragments if item.top >= max(header.bottom for header in headers)]
    lines = []
    assigned = set()
    for column, day in enumerate(days):
        inside = [item for item in body if edges[column] <= (item.left + item.right) / 2 < edges[column + 1]]
        if any(item.left < edges[column] - pitch * .1 or item.right > edges[column + 1] + pitch * .1
               for item in inside):
            return None
        text = _cell_body_text(inside)
        assigned.update(inside)
        if re.search(r"[가-힣A-Za-z]", text):
            lines.append(f"{day}일 {text}")
    if any(item not in assigned and re.search(r"[가-힣A-Za-z]", item.text) for item in body):
        return None
    preamble = _rows_text([item for item in fragments if item.cy < title.cy])
    heading = (month["year"] + "년 " if month["year"] else "") + month["month"] + "월"
    return "\n".join(part for part in (preamble, heading if lines else "", "\n".join(lines)) if part)


def _monthly_cell_text(fragments, width, height):
    """Assign text to calendar cells only with seven weekdays and a full grid.

    Missing, duplicated or next-month day numbers make the geometry ambiguous;
    those documents retain the existing reading path instead of guessed dates.
    """
    heading_rows = _text_rows(_month_headings(fragments))
    if not heading_rows:
        return None
    panels = []
    for index, headers in enumerate(heading_rows):
        centers = [(item.left + item.right) / 2 for item in headers]
        divisions = [0] + [(a + b) / 2 for a, b in zip(centers, centers[1:])] + [width]
        bottom = min(item.top for item in heading_rows[index + 1]) if index + 1 < len(heading_rows) else height
        for column, heading in enumerate(headers):
            below = [item for item in fragments if heading.bottom <= item.cy < bottom
                     and divisions[column] <= (item.left + item.right) / 2 < divisions[column + 1]]
            weekday_rows = _text_rows([item for item in below if _weekday_count(item.text)
                                      and item.cy < heading.bottom + heading.height * 4])
            weekday = next((row for row in weekday_rows
                            if "".join(item.text.replace(" ", "") for item in row)
                            in ("일월화수목금토", "월화수목금토일")), None)
            if weekday is None:
                return None
            anchors = []
            for item in weekday:
                count = _weekday_count(item.text)
                anchors.extend(item.left + (item.right - item.left) * (n + .5) / count for n in range(count))
            gaps = [b - a for a, b in zip(anchors, anchors[1:])]
            pitch = statistics.median(gaps)
            if len(anchors) != 7 or min(gaps) <= 0 or max(gaps) > min(gaps) * 1.5:
                return None
            edges = [anchors[0] - pitch / 2] + [(a + b) / 2 for a, b in zip(anchors, anchors[1:])] + [anchors[-1] + pitch / 2]
            numbers = [item for item in below if item.cy > max(value.cy for value in weekday)
                       and edges[0] <= (item.left + item.right) / 2 < edges[-1]
                       and _CELL_DAY.fullmatch(item.text.strip())]
            number_rows = _text_rows(numbers)
            ordered = [item for row in number_rows for item in row]
            days = [int(_CELL_DAY.fullmatch(item.text.strip()).group(1)) for item in ordered]
            if len(number_rows) < 4 or days not in (list(range(1, n + 1)) for n in (28, 29, 30, 31)):
                return None
            month = _MONTH_HEADING.fullmatch(heading.text.strip())
            if month.group(1) and len(days) != monthrange(int(month.group(1)), int(month.group(2)))[1]:
                return None
            positions = [(row_index, min(range(7), key=lambda n: abs(
                          anchors[n] - (item.left + item.right) / 2)))
                         for row_index, row in enumerate(number_rows) for item in row]
            first_column = positions[0][1]
            if any(row != (first_column + day - 1) // 7 or cell != (first_column + day - 1) % 7
                   for day, (row, cell) in zip(days, positions)):
                return None
            if month.group(1):
                weekday_text = "".join(item.text.replace(" ", "") for item in weekday)
                week_start = 6 if weekday_text.startswith("일") else 0
                if first_column != (date(int(month.group(1)), int(month.group(2)), 1).weekday() - week_start) % 7:
                    return None
            row_centers = [statistics.median(item.cy for item in row) for row in number_rows]
            row_pitch = statistics.median(b - a for a, b in zip(row_centers, row_centers[1:]))
            if row_pitch <= max(item.height for item in numbers) * 1.5:
                return None
            cell_lines = []
            assigned = set()
            for row_index, row in enumerate(number_rows):
                row_top = min(item.bottom for item in row)
                row_bottom = min(item.top for item in number_rows[row_index + 1]) if row_index + 1 < len(number_rows) else min(bottom, row_centers[-1] + row_pitch)
                for number in row:
                    cx = (number.left + number.right) / 2
                    cell = min(range(7), key=lambda n: abs(anchors[n] - cx))
                    # Weekdays are centered; day numbers are commonly aligned
                    # near the cell's left edge. Keep the nearest column only
                    # while its center remains clearly inside that column.
                    if abs(anchors[cell] - cx) > pitch * .45:
                        return None
                    inside = [item for item in below if item not in numbers and item not in weekday
                              and row_top <= item.cy < row_bottom
                              and edges[cell] <= (item.left + item.right) / 2 < edges[cell + 1]]
                    if any(item.left < edges[cell] - pitch * .1 or item.right > edges[cell + 1] + pitch * .1
                           for item in inside):
                        return None
                    text = _cell_body_text(inside)
                    assigned.update(inside)
                    if re.search(r"[가-힣A-Za-z]", text):
                        cell_lines.append(f"{number.text.strip().rstrip('일').strip()}일 {text}")
            # A list outside the date cells belongs to the existing below/side
            # layout reader; never silently discard it for this new path.
            if any(item not in assigned and item not in weekday and item not in numbers
                   and re.search(r"[가-힣A-Za-z]", item.text)
                   and not _SCHOOL_DAY_TOTAL.fullmatch(item.text.strip()) for item in below):
                return None
            panels.append((heading.text, cell_lines))
    if not any(lines for _, lines in panels):
        return ""
    first_top = min(item.top for row in heading_rows for item in row)
    preamble = _rows_text([item for item in fragments if item.cy < first_top])
    return "\n".join(part for part in [preamble] + [heading + "\n" + "\n".join(lines)
                                                   for heading, lines in panels if lines] if part)


class OcrEngine:
    def __init__(self, resources_path=None):
        roots = []
        if resources_path is not None:
            root = Path(resources_path)
            roots.extend((root / "assets" / "ocr", root / "ocr", root))
        bundle = getattr(sys, "_MEIPASS", None)
        if bundle:
            roots.append(Path(bundle) / "assets" / "ocr")
        roots.append(Path(__file__).resolve().parents[1] / "assets" / "ocr")
        self.resources_path = next(
            (root for root in roots if all((root / name).is_file() for name in MODELS)),
            roots[0],
        )
        self._detector = None
        self._recognizer = None
        self._characters = None
        self._lock = threading.Lock()
        self._error = ""

    def available(self) -> bool:
        if self._error:
            return False
        if not all((self.resources_path / name).is_file() for name in MODELS):
            return False
        return all(importlib.util.find_spec(name) is not None for name in ("onnxruntime", "numpy", "cv2"))

    def diagnostics(self) -> str:
        if self._error:
            return "한국어 문자 인식 엔진 오류: " + self._error
        if not self.available():
            return "한국어 문자 인식 모델 또는 실행 모듈을 찾을 수 없습니다. 배포 파일을 다시 확인해 주세요."
        return "오프라인 한국어 문자 인식 준비됨 · PP-OCRv5 · 모든 분석은 이 PC에서 처리됩니다."

    def _load(self):
        if self._detector is not None:
            return
        if not self.available():
            raise RuntimeError(self.diagnostics())
        import onnxruntime as ort

        for name, expected in MODELS.items():
            path = self.resources_path / name
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise RuntimeError("한국어 문자 인식 모델이 손상되었습니다: " + name)
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        options.log_severity_level = 3
        detector = ort.InferenceSession(
            str(self.resources_path / "ch_PP-OCRv5_det_mobile.onnx"),
            sess_options=options, providers=["CPUExecutionProvider"],
        )
        recognizer = ort.InferenceSession(
            str(self.resources_path / "korean_PP-OCRv5_rec_mobile.onnx"),
            sess_options=options, providers=["CPUExecutionProvider"],
        )
        dictionary = recognizer.get_modelmeta().custom_metadata_map.get("character", "")
        if not dictionary:
            raise RuntimeError("한국어 문자 인식 모델에 글자 사전이 없습니다.")
        self._characters = [""] + dictionary.splitlines() + [" "]
        self._detector = detector
        self._recognizer = recognizer

    @staticmethod
    def _ordered_quad(points):
        import numpy as np
        points = np.asarray(points, dtype=np.float32)
        by_x = points[np.argsort(points[:, 0])]
        left = by_x[:2][np.argsort(by_x[:2, 1])]
        right = by_x[2:][np.argsort(by_x[2:, 1])]
        return np.asarray([left[0], right[0], right[1], left[1]], dtype=np.float32)

    def _detect(self, image):
        import cv2
        import numpy as np
        height, width = image.shape[:2]
        ratio = min(1.0, 1600.0 / max(height, width))
        # Text detection models require multiples of 32.
        resized_w = max(32, round(width * ratio / 32) * 32)
        resized_h = max(32, round(height * ratio / 32) * 32)
        resized = cv2.resize(image, (resized_w, resized_h))
        pixels = resized.astype(np.float32) / 255.0
        pixels -= np.array([0.485, 0.456, 0.406], dtype=np.float32)
        pixels /= np.array([0.229, 0.224, 0.225], dtype=np.float32)
        tensor = np.ascontiguousarray(pixels.transpose(2, 0, 1)[None])
        prob = self._detector.run(None, {self._detector.get_inputs()[0].name: tensor})[0][0, 0]
        bitmap = (prob > 0.3).astype(np.uint8)
        bitmap = cv2.dilate(bitmap, np.ones((2, 2), dtype=np.uint8))
        contours, _ = cv2.findContours(bitmap, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        output = []
        scale = np.array([width / prob.shape[1], height / prob.shape[0]], dtype=np.float32)
        for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:1500]:
            rect = cv2.minAreaRect(contour)
            rw, rh = rect[1]
            if min(rw, rh) < 3 or rw * rh < 20:
                continue
            x, y, bw, bh = cv2.boundingRect(contour)
            mask = np.zeros((bh, bw), dtype=np.uint8)
            local = contour.copy()
            local[:, 0, 0] -= x
            local[:, 0, 1] -= y
            cv2.fillPoly(mask, [local], 1)
            score = cv2.mean(prob[y:y + bh, x:x + bw], mask)[0]
            if score < 0.5:
                continue
            # For a rectangular text contour, area * ratio / perimeter is the
            # DB polygon offset; expanding its min-area rectangle uses that
            # same distance without a polygon clipping runtime dependency.
            distance = rw * rh * 1.6 / (2.0 * (rw + rh))
            expanded = (rect[0], (rw + distance * 2, rh + distance * 2), rect[2])
            box = self._ordered_quad(cv2.boxPoints(expanded)) * scale
            box[:, 0] = np.clip(box[:, 0], 0, width - 1)
            box[:, 1] = np.clip(box[:, 1], 0, height - 1)
            output.append(box)
        return sorted(output, key=lambda box: (float(box[:, 1].min()), float(box[:, 0].min())))

    def _read_box(self, image, box):
        import cv2
        import numpy as np
        width = max(4, round(max(np.linalg.norm(box[0] - box[1]), np.linalg.norm(box[2] - box[3]))))
        height = max(4, round(max(np.linalg.norm(box[0] - box[3]), np.linalg.norm(box[1] - box[2]))))
        destination = np.float32([[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]])
        transform = cv2.getPerspectiveTransform(box.astype(np.float32), destination)
        crop = cv2.warpPerspective(image, transform, (width, height), borderMode=cv2.BORDER_REPLICATE)
        if height / width > 1.5:
            crop = np.rot90(crop)
        ch, cw = crop.shape[:2]
        resized_w = min(4096, max(1, math.ceil(48 * cw / ch)))
        tensor_w = max(320, resized_w)
        crop = cv2.resize(crop, (resized_w, 48)).astype(np.float32)
        tensor = np.zeros((1, 3, 48, tensor_w), dtype=np.float32)
        tensor[0, :, :, :resized_w] = (crop.transpose(2, 0, 1) / 255.0 - 0.5) / 0.5
        predictions = self._recognizer.run(None, {self._recognizer.get_inputs()[0].name: tensor})[0][0]
        indices = predictions.argmax(axis=1)
        confidences = predictions.max(axis=1)
        selected = indices != 0
        selected[1:] &= indices[1:] != indices[:-1]
        chars = []
        scores = []
        for index, confidence in zip(indices[selected], confidences[selected]):
            if int(index) < len(self._characters):
                chars.append(self._characters[int(index)])
                scores.append(float(confidence))
        if not scores or sum(scores) / len(scores) < 0.4:
            return ""
        return unicodedata.normalize("NFC", "".join(chars)).strip()

    def _read_fragments(self, pixels):
        fragments = []
        for box in self._detect(pixels):
            text = self._read_box(pixels, box)
            if text:
                fragments.append(_TextFragment(float(box[:, 0].min()), float(box[:, 1].min()),
                                               float(box[:, 0].max()), float(box[:, 1].max()), text))
        return fragments

    def _calendar_text(self, pixels, fragments):
        import cv2
        height, width = pixels.shape[:2]
        cell_text = _weekly_cell_text(fragments, width, height)
        if cell_text is None:
            cell_text = _monthly_cell_text(fragments, width, height)
        if cell_text is not None:
            return cell_text
        panels = _calendar_panels(fragments, width, height)
        if not panels:
            return None
        first_heading = min(panel.heading.top for panel in panels)
        preamble = [item for item in fragments if item.cy < first_heading]
        output = [_rows_text(preamble)] if preamble else []
        for panel in panels:
            body = list(panel.fragments)
            if body and statistics.median(item.height for item in body) < 26:
                # Small calendar print benefits from a second detection pass on
                # its own enlarged panel, without neighboring month columns.
                crop = pixels[max(0, int(panel.top)):min(height, math.ceil(panel.bottom)),
                              max(0, int(panel.left)):min(width, math.ceil(panel.right))]
                if min(crop.shape[:2]) >= 4:
                    try:
                        refined = _calendar_body(self._read_fragments(cv2.resize(crop, None, fx=2, fy=2,
                                                                                interpolation=cv2.INTER_CUBIC)),
                                                 drop_totals=not panel.side_list)
                        if panel.side_list:
                            refined = [item for item in refined if not _weekday_count(item.text)]
                        month = int(_MONTH_HEADING.fullmatch(panel.heading.text.strip()).group(2))
                        if (len(_text_rows(refined)) >= len(_text_rows(body))
                                and _calendar_date_rows(refined) >= _calendar_date_rows(body)
                                and (not panel.side_list or _unexpected_calendar_months(refined, month)
                                     <= _unexpected_calendar_months(body, month))):
                            body = refined
                    except Exception:
                        # Refinement is optional: the successfully recognized
                        # original list must survive a failed second OCR pass.
                        pass
            output.extend((panel.heading.text, _calendar_rows_text(body, join_continuations=panel.side_list)))
        return "\n".join(text for text in output if text)

    def recognize(self, image) -> str:
        """Return detected Korean/Latin text in reading order for a PIL image.

        Call from a worker thread for larger files; one engine serializes its
        sessions. Empty text is a valid result for an image without readable text.
        """
        import cv2
        import numpy as np
        if image is None or image.width < 2 or image.height < 2:
            return ""
        with self._lock:
            try:
                self._load()
            except Exception as exc:
                self._error = str(exc)
                raise RuntimeError(self.diagnostics()) from exc
            rgb = image.convert("RGB")
            # Bound oversized scans while keeping common A4 documents readable.
            if max(rgb.size) > 6000:
                rgb.thumbnail((6000, 6000))
            pixels = cv2.cvtColor(np.asarray(rgb), cv2.COLOR_RGB2BGR)
            fragments = self._read_fragments(pixels)
            calendar_text = self._calendar_text(pixels, fragments)
            if calendar_text is not None:
                return calendar_text
            # Preserve one source table row as one text line where possible,
            # which keeps its event title and date together during extraction.
            return _rows_text(fragments)
