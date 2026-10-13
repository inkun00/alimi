"""Deterministic desktop arrangement within a screen's available work area."""

from __future__ import annotations

from PyQt5.QtCore import QPoint, QRect, QSize


ALIGNMENTS = ("bottom", "top", "right", "left")


def arrange_rectangles(
    area: QRect,
    sizes: list[QSize | tuple[int, int]],
    alignment: str,
    margin: int = 12,
    gap: int = 10,
) -> list[QPoint]:
    """Return positions for an ordered row or column grid.

    Top and bottom arrangements fill each row from left to right, then add
    rows inward from the selected edge. Left and right arrangements fill each
    column from top to bottom, then add columns inward. Differently sized
    items align against the chosen edge of their row or column.

    The caller supplies the available screen geometry, which already excludes
    the taskbar. Items remain separated when this ordered shelf grid fits the
    work area. If it is exhausted, subsequent positions clamp to its bounds;
    all reminders still receive a deterministic location, but may overlap.
    An item larger than the work area anchors at its upper-left inner corner.
    """
    if not sizes:
        return []
    if not area.isValid():
        return [QPoint(area.topLeft()) for _ in sizes]

    alignment = alignment if alignment in ALIGNMENTS else "bottom"
    margin = max(0, int(margin))
    gap = max(0, int(gap))
    # Tiny work areas still retain one usable pixel instead of an inverted box.
    margin_x = min(margin, (area.width() - 1) // 2)
    margin_y = min(margin, (area.height() - 1) // 2)
    inner = QRect(
        area.x() + margin_x, area.y() + margin_y,
        area.width() - margin_x * 2, area.height() - margin_y * 2,
    )
    dimensions = []
    for size in sizes:
        width, height = (size.width(), size.height()) if isinstance(size, QSize) else size
        dimensions.append((max(1, int(width)), max(1, int(height))))

    horizontal = alignment in ("top", "bottom")
    trailing = alignment in ("bottom", "right")
    primary_limit = inner.width() if horizontal else inner.height()
    secondary_limit = inner.height() if horizontal else inner.width()

    groups = []
    group = []
    used = 0
    span = 0
    for index, (width, height) in enumerate(dimensions):
        primary, secondary = (width, height) if horizontal else (height, width)
        required = primary + (gap if group else 0)
        if group and used + required > primary_limit:
            groups.append((group, span))
            group, used, span = [], 0, 0
            required = primary
        group.append((index, used + (gap if used else 0), secondary))
        used += required
        span = max(span, secondary)
    if group:
        groups.append((group, span))

    positions = [QPoint() for _ in dimensions]
    secondary_offset = 0
    for group, span in groups:
        for index, primary_offset, secondary in group:
            offset = (
                secondary_limit - secondary_offset - secondary
                if trailing else secondary_offset
            )
            width, height = dimensions[index]
            x, y = (
                (inner.x() + primary_offset, inner.y() + offset)
                if horizontal else (inner.x() + offset, inner.y() + primary_offset)
            )
            # QRect.right()/bottom() are inclusive, so use width/height bounds.
            x = max(inner.x(), min(x, inner.x() + max(0, inner.width() - width)))
            y = max(inner.y(), min(y, inner.y() + max(0, inner.height() - height)))
            positions[index] = QPoint(x, y)
        secondary_offset += span + gap
    return positions
