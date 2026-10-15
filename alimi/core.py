"""Local storage and conservative Korean schedule extraction for School Alimi.

This module is deliberately independent of the UI and performs no network I/O.
"""

from __future__ import annotations

from calendar import monthrange
from dataclasses import asdict, dataclass, fields, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import re
import sqlite3
import threading
import unicodedata
from typing import Iterable


@dataclass
class Event:
    id: int | None = None
    title: str = ""
    start_date: str = ""
    end_date: str | None = None
    time: str | None = None
    place: str = ""
    notes: str = ""
    source: str = ""
    source_text: str = ""
    status: str = "open"
    completed_at: str | None = None
    created_at: str = ""
    x: int | None = None
    y: int | None = None
    date_precision: str = "day"


_EVENT_FIELDS = tuple(field.name for field in fields(Event))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normal(value: str | None) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value or "")).casefold()


def _fingerprint(event: Event) -> str:
    return "\x1f".join((_normal(event.title), event.start_date, event.time or "", _normal(event.place)))


def format_event_date(event: Event, include_year: bool = False) -> str:
    """Format the date actually supplied by the source, not its alert date."""
    if not event.start_date:
        return "날짜 미정"
    when = date.fromisoformat(event.start_date)
    if event.date_precision == "month":
        return f"{when.year}년 {when.month}월" if include_year else f"{when.month}월"
    return event.start_date


def _legacy_month_event(event: Event) -> bool:
    """Recognize old generated month reminders without reclassifying real days.

    Earlier versions stored month-only dates as complete first-to-last-day
    spans. The boundaries alone are not sufficient evidence: a real dated
    activity can span an entire month. Reparse its saved source and require the
    same title, start date, time and place from an explicitly month-only token.
    """
    if not event.start_date or not event.end_date or not event.source_text:
        return False
    try:
        when = date.fromisoformat(event.start_date)
    except ValueError:
        return False
    if when.day != 1 or event.end_date != date(when.year, when.month, monthrange(when.year, when.month)[1]).isoformat():
        return False
    source_text = event.source_text
    columns = re.split(r"[|\t]", source_text) if "\n" not in source_text else []
    if len(columns) == 6:
        # PDF extraction stores the row as source_text, omitting its header.
        # Restore that header to avoid treating unit numbers or prose as dates.
        source_text = "교과 | 영역 | 단원명 | 평가내용 | 평가방법 | 평가시기\n" + source_text
    elif len(columns) == 4:
        source_text = "교과 | 단원 | 평가내용 | 시기\n" + source_text
    candidates = parse_schedule_text(source_text, event.source, reference_date=when)
    candidates += parse_schedule_text(source_text, event.source, reference_date=when, _preserve_date_words=False)
    return any(candidate.date_precision == "month" and _fingerprint(candidate) == _fingerprint(event)
               for candidate in candidates)


class Store:
    """SQLite repository; mutations commit before they return.

    ``path`` names a database file. Callers choose the local application-data
    directory. A single Store can be used by the UI and its import worker.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), timeout=15, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                start_date TEXT NOT NULL DEFAULT '',
                end_date TEXT,
                time TEXT,
                place TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                source_text TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL CHECK(status IN ('open', 'review', 'done')),
                completed_at TEXT,
                created_at TEXT NOT NULL,
                x INTEGER,
                y INTEGER,
                date_precision TEXT NOT NULL DEFAULT 'day' CHECK(date_precision IN ('day', 'month')),
                fingerprint TEXT NOT NULL UNIQUE
            );
            CREATE INDEX IF NOT EXISTS idx_events_due ON events(status, start_date);
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        self._migrate_schema(self._db)
        self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @staticmethod
    def _event(row: sqlite3.Row | None) -> Event | None:
        return Event(**{name: row[name] for name in _EVENT_FIELDS if name in row.keys()}) if row else None

    @classmethod
    def _migrate_schema(cls, connection: sqlite3.Connection) -> None:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(events)")}
        if "date_precision" not in columns:
            connection.execute("ALTER TABLE events ADD COLUMN date_precision TEXT NOT NULL DEFAULT 'day' CHECK(date_precision IN ('day', 'month'))")
            for row in connection.execute("SELECT * FROM events").fetchall():
                event = cls._event(row)
                if _legacy_month_event(event):
                    connection.execute("UPDATE events SET date_precision='month' WHERE id=?", (event.id,))
        connection.execute("PRAGMA user_version=2")
        cls._restore_source_date_titles(connection)

    @classmethod
    def _restore_source_date_titles(cls, connection: sqlite3.Connection) -> None:
        """Restore only titles proven to have been produced by the old parser.

        This is a one-time data repair, not a schema change. Keeping the marker
        prevents a later intentional title edit from being changed on restart.
        A unique-index collision leaves both existing records untouched.
        """
        marker = "source_date_titles_v1"
        if connection.execute("SELECT 1 FROM settings WHERE key=?", (marker,)).fetchone():
            return
        for row in connection.execute("SELECT * FROM events").fetchall():
            event = cls._event(row)
            title = restored_source_title(event)
            if title is None:
                continue
            repaired = Event(**asdict(event))
            repaired.title = title
            fingerprint = _fingerprint(repaired)
            collision = connection.execute(
                "SELECT 1 FROM events WHERE fingerprint=? AND id<>?", (fingerprint, event.id)
            ).fetchone()
            if not collision:
                connection.execute("UPDATE events SET title=?,fingerprint=? WHERE id=?",
                                   (title, fingerprint, event.id))
        connection.execute("INSERT INTO settings(key,value) VALUES (?,?)", (marker, "1"))

    def list_events(self, status: str | None = None) -> list[Event]:
        with self._lock:
            query = "SELECT * FROM events"
            values: tuple = ()
            if status is not None:
                query += " WHERE status=?"
                values = (status,)
            query += " ORDER BY CASE WHEN start_date='' THEN 1 ELSE 0 END, start_date, time, id"
            return [self._event(row) for row in self._db.execute(query, values).fetchall()]

    def get_event(self, event_id: int) -> Event | None:
        with self._lock:
            return self._event(self._db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone())

    def find_duplicate(self, event: Event) -> Event | None:
        """Read an existing matching schedule, including its completion state."""
        with self._lock:
            return self._event(self._duplicate_row(event))

    def _duplicate_row(self, event: Event) -> sqlite3.Row | None:
        row = self._db.execute("SELECT * FROM events WHERE fingerprint=?", (_fingerprint(event),)).fetchone()
        if row is not None:
            return row
        # An old database can contain a stripped title that could not be
        # repaired safely, or it may have been imported after the data repair.
        # Alias only a candidate whose own saved source proves its current title.
        legacy_title = _legacy_source_title(event)
        if legacy_title is None:
            return None
        legacy = Event(**asdict(event))
        legacy.title = legacy_title
        row = self._db.execute("SELECT * FROM events WHERE fingerprint=?", (_fingerprint(legacy),)).fetchone()
        if row is not None and self._same_alias_dates(self._event(row), event):
            return row
        # Dates may have been written in a different format in a second copy
        # of the same notice. Match only source-proven legacy titles, rather
        # than removing dates from arbitrary user-edited titles.
        rows = self._db.execute("SELECT * FROM events WHERE start_date=? AND COALESCE(time,'')=? ORDER BY id",
                                (event.start_date, event.time or "")).fetchall()
        for candidate in rows:
            stored = self._event(candidate)
            if not self._same_alias_dates(stored, event) or _normal(stored.place) != _normal(event.place):
                continue
            stored_legacy = _legacy_source_title(stored)
            if stored_legacy is not None and _normal(stored_legacy) == _normal(legacy_title):
                return candidate
        return None

    @staticmethod
    def _same_alias_dates(stored: Event, incoming: Event) -> bool:
        # The legacy fingerprint did not include a range end or month
        # precision. Its alias must not merge genuinely different schedules.
        return (stored.start_date, stored.end_date, stored.date_precision) == (
            incoming.start_date, incoming.end_date, incoming.date_precision)

    @staticmethod
    def _validate(event: Event) -> None:
        if not event.title.strip():
            raise ValueError("일정 제목을 입력해 주세요.")
        if event.status not in ("open", "review", "done"):
            raise ValueError("올바르지 않은 일정 상태입니다.")
        if event.date_precision not in ("day", "month"):
            raise ValueError("올바르지 않은 날짜 단위입니다.")
        if not event.start_date and event.status != "review":
            raise ValueError("일정 날짜를 확인해 주세요.")
        for value in (event.start_date, event.end_date):
            if value:
                parsed = date.fromisoformat(value)
                if parsed.isoformat() != value:
                    raise ValueError("날짜 형식은 YYYY-MM-DD입니다.")
        if event.end_date and event.start_date and event.end_date < event.start_date:
            raise ValueError("종료일이 시작일보다 빠릅니다.")
        if event.date_precision == "month" and (not event.start_date or date.fromisoformat(event.start_date).day != 1):
            raise ValueError("월 일정의 알림 날짜는 해당 월 1일이어야 합니다.")
        if event.time and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", event.time):
            raise ValueError("시간 형식은 HH:mm입니다.")

    def _upsert(self, event: Event) -> Event:
        self._validate(event)
        event.title = event.title.strip()
        if not event.created_at:
            event.created_at = _now()
        fingerprint = _fingerprint(event)
        duplicate = self._duplicate_row(event)
        if duplicate and duplicate["id"] != event.id:
            if event.id is None:
                return self._event(duplicate)
            raise ValueError("동일한 제목·날짜·시간·장소의 일정이 이미 있습니다.")
        values = asdict(event)
        names = [name for name in _EVENT_FIELDS if name != "id"]
        if event.id is None:
            names.append("fingerprint")
            values["fingerprint"] = fingerprint
            cursor = self._db.execute(
                f"INSERT INTO events ({','.join(names)}) VALUES ({','.join('?' for _ in names)})",
                tuple(values[name] for name in names),
            )
            event.id = cursor.lastrowid
        else:
            assignments = ",".join(f"{name}=?" for name in names) + ",fingerprint=?"
            cursor = self._db.execute(
                f"UPDATE events SET {assignments} WHERE id=?",
                tuple(values[name] for name in names) + (fingerprint, event.id),
            )
            if not cursor.rowcount:
                raise ValueError("수정할 일정이 없습니다.")
        return event

    def upsert_event(self, event: Event) -> Event:
        with self._lock, self._db:
            return self._upsert(event)

    def import_candidates(self, candidates: Iterable[Event]) -> tuple[int, int]:
        """Atomically add candidates, returning (added count, duplicate count)."""
        added, duplicates = 0, 0
        with self._lock, self._db:
            for event in candidates:
                if self._duplicate_row(event) is not None:
                    duplicates += 1
                    continue
                self._upsert(event)
                added += 1
        return added, duplicates

    def save_reviewed_events(self, candidates: Iterable[Event]) -> tuple[int, int, int]:
        """Atomically save confirmed additions and explicit existing-event edits.

        An existing id identifies the exact schedule chosen in the review. Its
        current completion state, provenance and desktop position stay intact,
        even if they changed while the review was open. Clone incoming events
        so a failed batch never leaves draft rows with rolled-back insert ids.
        """
        added, updated, duplicates = 0, 0, 0
        with self._lock, self._db:
            for candidate in candidates:
                event = replace(candidate)
                if event.id is None:
                    if self._duplicate_row(event) is not None:
                        duplicates += 1
                        continue
                    self._upsert(event)
                    added += 1
                    continue
                current = self.get_event(event.id)
                if current is None:
                    raise ValueError("수정할 일정이 없습니다. 일정을 다시 확인해 주세요.")
                event = replace(
                    current, title=event.title, start_date=event.start_date,
                    end_date=event.end_date, date_precision=event.date_precision,
                    time=event.time, place=event.place, notes=event.notes,
                    status="open" if current.status == "review" else current.status,
                )
                self._upsert(event)
                updated += 1
        return added, updated, duplicates

    def complete_event(self, event_id: int) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE events SET status='done', completed_at=? WHERE id=?", (_now(), event_id))

    def reopen_event(self, event_id: int) -> None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE events SET status=CASE WHEN start_date='' THEN 'review' ELSE 'open' END, completed_at=NULL WHERE id=?",
                (event_id,),
            )

    def update_position(self, event_id: int, x: int, y: int) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE events SET x=?, y=? WHERE id=?", (int(x), int(y), event_id))

    def schedule_reset_ids(self, start: str | None = None, end: str | None = None) -> list[int]:
        """Snapshot the schedules to reset, including completed/review records.

        With no bounds every schedule is selected. A bounded reset includes
        schedules whose dates overlap the inclusive period; month-only dates
        cover their entire month. Undated review records belong only to a full
        reset. Returning ids lets confirmation keep later imports untouched.
        """
        if start is None and end is None:
            with self._lock:
                return [row[0] for row in self._db.execute("SELECT id FROM events ORDER BY id")]
        try:
            if not isinstance(start, str) or not isinstance(end, str):
                raise ValueError
            first, last = date.fromisoformat(start), date.fromisoformat(end)
            if first.isoformat() != start or last.isoformat() != end:
                raise ValueError
        except ValueError:
            raise ValueError("초기화할 시작일과 종료일을 YYYY-MM-DD 형식으로 지정해 주세요.") from None
        if first > last:
            raise ValueError("초기화할 종료일이 시작일보다 빠릅니다.")
        with self._lock:
            selected = []
            for row in self._db.execute(
                "SELECT id,start_date,end_date,date_precision FROM events WHERE start_date<>'' ORDER BY id"
            ):
                event_start = date.fromisoformat(row["start_date"])
                if row["date_precision"] == "month":
                    event_start = event_start.replace(day=1)
                    event_end = event_start.replace(day=monthrange(event_start.year, event_start.month)[1])
                else:
                    event_end = date.fromisoformat(row["end_date"] or row["start_date"])
                if event_start <= last and event_end >= first:
                    selected.append(row["id"])
            return selected

    def reset_events(self, event_ids: Iterable[int]) -> tuple[int, Path | None]:
        """Back up local state, then atomically delete only confirmed ids.

        Settings, drafts and the id sequence are retained. A failed backup or
        delete leaves all schedules intact, and an empty selection writes no
        backup. Keep the backup and deletion within the same repository lock
        so this Store's import worker cannot slip writes between them.
        """
        selected = []
        for event_id in event_ids:
            if isinstance(event_id, bool) or not isinstance(event_id, int) or event_id < 1:
                raise ValueError("초기화할 일정 번호는 양의 정수여야 합니다.")
            selected.append(event_id)
        if not selected:
            return 0, None
        with self._lock:
            existing = {row[0] for row in self._db.execute("SELECT id FROM events")}
            selected = list(dict.fromkeys(event_id for event_id in selected if event_id in existing))
            if not selected:
                return 0, None
            previous = self.backup()
            with self._db:
                cursor = self._db.executemany("DELETE FROM events WHERE id=?", ((event_id,) for event_id in selected))
            return cursor.rowcount, previous

    def due_events(self, today: date | str | None = None) -> list[Event]:
        today = today or date.today()
        today_string = today.isoformat() if isinstance(today, date) else date.fromisoformat(today).isoformat()
        with self._lock:
            return [self._event(row) for row in self._db.execute(
                "SELECT * FROM events WHERE status='open' AND start_date<>'' AND start_date<=? ORDER BY start_date,time,id",
                (today_string,),
            ).fetchall()]

    def get_reminder_window(self) -> tuple[bool, int, int]:
        """Read validated (unlimited, past days, future days) desktop settings."""
        def configured_days(key: str, default: int) -> int:
            try:
                value = int(self.get_setting(key, str(default)))
            except (TypeError, ValueError):
                return default
            return max(0, min(value, 36500))

        with self._lock:
            value = str(self.get_setting("reminder_unlimited", "True")).strip().casefold()
            unlimited = value not in ("false", "0", "no", "off")
            return unlimited, configured_days("reminder_past_days", 30), configured_days("reminder_future_days", 14)

    def reminder_events(
        self, today: date | str | None = None, lead_days: int | None = None, *,
        past_days: int | None = None, unlimited: bool | None = None,
    ) -> list[Event]:
        """Read unfinished reminders within the configured desktop date window.

        With no date limit (the default), every confirmed dated event is shown.
        Otherwise a day event is selected by its actual deadline, and a
        month-only event by overlap with its full month. Merely hiding an event
        outside the window never changes its status or saved position.

        Explicit day arguments provide a limited window for callers, unless
        ``unlimited`` is also explicitly set. The historical ``lead_days``
        argument names the number of future days.
        """
        for name, value in (("미리 알림", lead_days), ("지난 일정 표시", past_days)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 36500):
                raise ValueError(f"{name} 일수는 0부터 36500까지의 정수여야 합니다.")
        if unlimited is not None and not isinstance(unlimited, bool):
            raise ValueError("기간 제한 없음은 참 또는 거짓이어야 합니다.")
        saved_unlimited, saved_past, saved_future = self.get_reminder_window()
        if unlimited is None:
            if lead_days is not None or past_days is not None:
                unlimited = False
            else:
                unlimited = saved_unlimited

        when = date.today() if today is None else date.fromisoformat(today) if isinstance(today, str) else today

        with self._lock:
            events = [self._event(row) for row in self._db.execute(
                "SELECT * FROM events WHERE status='open' AND start_date<>'' ORDER BY start_date,time,id"
            ).fetchall()]
            if unlimited:
                return events
            before = saved_past if past_days is None else past_days
            after = saved_future if lead_days is None else lead_days

        lower = when - timedelta(days=min(before, (when - date(1, 1, 1)).days))
        upper = when + timedelta(days=min(after, (date(9999, 12, 31) - when).days))
        selected = []
        for event in events:
            start = date.fromisoformat(event.start_date)
            if event.date_precision == "month":
                first = start.replace(day=1)
                last = start.replace(day=monthrange(start.year, start.month)[1])
                inside = first <= upper and last >= lower
            else:
                deadline = date.fromisoformat(event.end_date or event.start_date)
                inside = lower <= deadline <= upper
            if inside:
                selected.append(event)
        return selected

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    def backup(self, destination: str | Path | None = None) -> Path:
        if destination is None:
            destination = self.path.parent / "backups" / f"alimi-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.sqlite3"
        destination = Path(destination)
        if destination.resolve() == self.path.resolve():
            raise ValueError("백업 파일은 현재 데이터 파일과 다른 경로여야 합니다.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            target = sqlite3.connect(str(destination))
            try:
                self._db.backup(target)
            finally:
                target.close()
        return destination

    def restore(self, source: str | Path) -> Path:
        """Restore a verified Alimi backup, returning the prior database backup.

        Verification occurs before any write to the current database. Holding a
        read transaction on the source keeps its verified snapshot unchanged
        through the SQLite backup operation, even when it is a live database.
        """
        source = Path(source).resolve()
        if source == self.path.resolve():
            raise ValueError("현재 사용 중인 데이터 파일은 복원 대상으로 선택할 수 없습니다.")
        if not source.is_file():
            raise ValueError("백업 파일을 찾을 수 없습니다.")
        incoming = None
        staged = None
        with self._lock:
            try:
                incoming = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=15)
                incoming.row_factory = sqlite3.Row
                incoming.execute("PRAGMA trusted_schema=OFF")
                incoming.execute("BEGIN")
                if [row[0] for row in incoming.execute("PRAGMA integrity_check")] != ["ok"]:
                    raise ValueError("백업 파일의 무결성 검사에 실패했습니다.")
                version = incoming.execute("PRAGMA user_version").fetchone()[0]
                if version not in (1, 2):
                    raise ValueError("지원하지 않는 알리미 백업 버전입니다.")
                expected = {
                    "events": {
                        "id": ("INTEGER", 0, 1), "title": ("TEXT", 1, 0),
                        "start_date": ("TEXT", 1, 0), "end_date": ("TEXT", 0, 0),
                        "time": ("TEXT", 0, 0), "place": ("TEXT", 1, 0),
                        "notes": ("TEXT", 1, 0), "source": ("TEXT", 1, 0),
                        "source_text": ("TEXT", 1, 0), "status": ("TEXT", 1, 0),
                        "completed_at": ("TEXT", 0, 0), "created_at": ("TEXT", 1, 0),
                        "x": ("INTEGER", 0, 0), "y": ("INTEGER", 0, 0),
                        "fingerprint": ("TEXT", 1, 0),
                    },
                    "settings": {"key": ("TEXT", 0, 1), "value": ("TEXT", 1, 0)},
                }
                if version == 2:
                    expected["events"]["date_precision"] = ("TEXT", 1, 0)
                for table, schema in expected.items():
                    actual = {row[1]: (row[2].upper(), row[3], row[5]) for row in incoming.execute(f"PRAGMA table_info({table})")}
                    if actual != schema:
                        raise ValueError("알리미 백업 파일의 데이터 구조가 올바르지 않습니다.")
                objects = incoming.execute(
                    "SELECT name,type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' AND type IN ('table','view','trigger')"
                ).fetchall()
                if {(row["name"], row["type"]) for row in objects} != {("events", "table"), ("settings", "table")}:
                    raise ValueError("알리미 백업에 예상하지 못한 데이터 구조가 있습니다.")
                fingerprints: set[str] = set()
                for row in incoming.execute("SELECT * FROM events"):
                    event = self._event(row)
                    self._validate(event)
                    if event.id is None or event.id < 1 or row["fingerprint"] != _fingerprint(event) or row["fingerprint"] in fingerprints:
                        raise ValueError("백업 일정 데이터가 올바르지 않습니다.")
                    fingerprints.add(row["fingerprint"])
                for row in incoming.execute("SELECT key,value FROM settings"):
                    if not isinstance(row["key"], str) or not isinstance(row["value"], str):
                        raise ValueError("백업 설정 데이터가 올바르지 않습니다.")
                # Upgrade a private copy before touching the active database.
                # This also permits read-only backups from older releases.
                staged = sqlite3.connect(":memory:")
                staged.row_factory = sqlite3.Row
                incoming.backup(staged)
                self._migrate_schema(staged)
                for row in staged.execute("SELECT * FROM events"):
                    self._validate(self._event(row))
                staged.commit()
                previous = self.backup()
                staged.backup(self._db)
                self._db.commit()
                return previous
            except sqlite3.Error as error:
                raise ValueError("백업 파일을 읽거나 복원할 수 없습니다. 현재 데이터는 유지됩니다.") from error
            finally:
                if incoming is not None:
                    incoming.close()
                if staged is not None:
                    staged.close()


_DATE = re.compile(
    r"(?<!\d)(?:(?P<year>20\d{2}|19\d{2})\s*(?:년\s*|[./-]\s*)"
    r"(?P<month>\d{1,2})\s*(?:월\s*|[./-]\s*)(?P<day>\d{1,2})(?:\s*일|(?!\s*월))"
    r"|(?P<kmonth>\d{1,2})\s*월\s*(?P<kday>\d{1,2})(?:\s*일|(?!\s*(?:월|주|차)))"
    r"|(?P<smonth>\d{1,2})\s*[./-]\s*(?P<sday>\d{1,2}))(?!\d)"
)
_TIME = re.compile(r"(?<!\d)(?:(?P<period>오전|오후)\s*)?(?P<hour>\d{1,2})(?:\s*:\s*(?P<minute>\d{2})|\s*시(?!간)(?:\s*(?P<kminute>\d{1,2})\s*분)?)(?!\d)")
_RELATIVE = re.compile(r"(?:다음\s*주|이번\s*주|금주|차주)\s*[월화수목금토일](?:요일)?|모레|내일|오늘")
_DATE_LABEL = re.compile(r"^(?:[•·*\-\d.)\s]*)(?:일시|일자|날짜|기간|기한|마감(?:일|기한)?|제출\s*기한|제출일|신청\s*기한|신청\s*마감(?:일)?|접수\s*마감(?:일)?|행사일|실시일)(?=\s|[:：]|$|\d)\s*[:：]?\s*")
_METADATA = re.compile(r"(?:작성\s*(?:일|일자)|문서\s*작성일|게시일|발행일|등록일|시행일자)\s*[:：]?")
_PLACE = re.compile(r"(?:제출\s*(?:장소|처)|장소|회의실|위치)\s*[:：]\s*([^\n|;]+)")
_WEEKDAY = "월화수목금토일"
_YEAR = re.compile(r"(?<!\d)(19\d{2}|20\d{2})\s*(?:학년도|년)(?!\d)")
_MONTH = re.compile(r"(?<!\d)(?:(?P<year>19\d{2}|20\d{2})\s*(?:학년도|년)\s*)?(?P<month>1[0-2]|[1-9])\s*월(?!\s*\d\s*일)")
_MONTH_RANGE = re.compile(
    r"(?<!\d)(?:(?P<year>19\d{2}|20\d{2})\s*(?:학년도|년)\s*)?"
    r"(?P<first>1[0-2]|[1-9])\s*(?:월\s*)?(?:~|∼|–|—|-|부터)\s*"
    r"(?:(?P<end_year>19\d{2}|20\d{2})\s*(?:학년도|년)\s*)?"
    r"(?P<last>1[0-2]|[1-9])\s*월"
)
_MONTH_LABEL = re.compile(r"^(?:시기|평가\s*시기|실시\s*시기|평가월|일정월|월|월별|일정|날짜|일자|기간)\s*[:：]?\s*")
_MONTH_METADATA = re.compile(r"^(?:작성|발행|게시|등록|수정|출력|다운로드)\s*(?:일자|일시|일|월)?(?=\s|[:：]|$)\s*[:：]?")
_CALENDAR_MONTH_HEADER = re.compile(
    r"^[\[({]?\s*(?:(?P<year>19\d{2}|20\d{2})\s*(?:학년도|년|[./-])\s*)?"
    r"(?P<month>1[0-2]|0?[1-9])\s*월\s*[.]?\s*"
    r"(?:(?:January|February|March|April|May|June|July|August|September|October|November|December)\s*)?"
    r"[\])}]?$", re.IGNORECASE
)
_CALENDAR_YEAR_HEADER = re.compile(r"^(19\d{2}|20\d{2})\s*(?:학년도|년|[.])?$")
_CALENDAR_DATE = re.compile(
    r"(?:(?P<year>19\d{2}|20\d{2})\s*(?:년|[./-])\s*)?"
    r"(?P<month>\d{1,2})\s*(?:월|[./-])\s*(?P<day>\d{1,2})(?:\s*일)?(?!\d)"
)
_CALENDAR_COMMA_END_DATE = re.compile(r"(?P<month>\d{1,2})\s*,\s*(?P<day>\d{1,2})(?:\s*일)?(?!\d)")
_CALENDAR_DAY = re.compile(r"(?P<day>\d{1,2})(?:\s*일)?(?!\d)")
_CALENDAR_WEEKDAY = re.compile(r"\s*\.?\s*[(\[]\s*([월화수목금토일])(?:요일)?\s*[)\]]")
_CALENDAR_UNCLEAR_WEEKDAY = re.compile(r"\s*\.?\s*[(\[]\s*[^)\]\s]{0,3}\s*(?:[)\]]|(?=\s))")
_CALENDAR_UNCLEAR_DAY = re.compile(r"\d[A-Za-z\d]{0,2}(?=\s*[(\[])")
_CALENDAR_RANGE = re.compile(r"\s*[.,]?\s*(?:~|∼|–|—|−|-|부터)\s*")
_CALENDAR_GRADE_RANGE = re.compile(r"^[\s•·*▪■●○▶#\-]*\d{1,2}\s*(?:~|∼|–|—|-|부터)\s*\d{1,2}\s*학년")


def _calendar_date_token(text: str, position: int, *, allow_comma: bool = False) -> tuple[int | None, int | None, int, str, int] | None:
    """Read one leading calendar date, including an optional weekday.

    A separate reader keeps pupil grades such as ``(4.6학년)`` and numbers in
    event descriptions from becoming additional schedule candidates.
    """
    unclear_day = _CALENDAR_UNCLEAR_DAY.match(text, position)
    if unclear_day and re.search(r"[A-Za-z]", unclear_day.group()):
        weekday = _CALENDAR_WEEKDAY.match(text, unclear_day.end())
        if weekday:
            return None, None, 0, "숫자?", weekday.end()
    match = _CALENDAR_DATE.match(text, position)
    if match is None and allow_comma:
        # OCR often reads the dot of a range's explicit end month as a comma.
        # Only permit this after a proved range connector; an audience such as
        # ``3, 5학년`` must not become a leading March date.
        match = _CALENDAR_COMMA_END_DATE.match(text, position)
    if match:
        own_year = match.groupdict().get("year")
        year = int(own_year) if own_year else None
        month = int(match["month"])
    else:
        match = _CALENDAR_DAY.match(text, position)
        year = month = None
    if not match:
        return None
    end = match.end()
    if month is None and end < len(text) and text[end] == ",":
        return None
    weekday = _CALENDAR_WEEKDAY.match(text, end)
    if weekday:
        return year, month, int(match["day"]), weekday.group(1), weekday.end()
    unclear_weekday = _CALENDAR_UNCLEAR_WEEKDAY.match(text, end)
    if unclear_weekday:
        return year, month, int(match["day"]), "?", unclear_weekday.end()
    # A digit immediately followed by prose is not a bare calendar day:
    # ``5학년 진로체험`` names an audience, rather than the fifth of the month.
    if end < len(text) and not re.match(r"[\s.,~∼–—−\-]", text[end]):
        return None
    return year, month, int(match["day"]), "", end


def _calendar_entry_parts(line: str) -> tuple[tuple, tuple | None, str] | None:
    if _CALENDAR_GRADE_RANGE.match(line):
        return None
    prefix = re.match(r"^[\s•·*▪■●○▶#\-]*", line).end()
    first = _calendar_date_token(line, prefix)
    if first is None:
        return None
    end = first[-1]
    connector = _CALENDAR_RANGE.match(line, end)
    second = _calendar_date_token(line, connector.end(), allow_comma=True) if connector else None
    if second is not None:
        end = second[-1]
    body = line[end:].strip(" \t.·•:：~∼–—-,，")
    # Calendar number grids, weekday rows and empty bullet placeholders are
    # layout. A date needs a textual event description to establish a section.
    if not re.search(r"[가-힣A-Za-z]", body) or not _looks_title(body):
        return None
    return first, second, body


def _calendar_sections(lines: list[str], document_year: int | None,
                       academic_year: int | None = None) -> tuple[set[int], dict[int, tuple[int | None, int]]]:
    """Identify month headings only when they introduce dated calendar entries.

    A lone ``9월`` retains the existing month-reminder behavior. Calendar
    headings instead provide context to their following day entries, without
    adding a spurious first-of-month event for the heading itself.
    """
    headings = [(index, match) for index, line in enumerate(lines)
                if (match := _CALENDAR_MONTH_HEADER.fullmatch(line))]
    skip: set[int] = set()
    contexts: dict[int, tuple[int | None, int]] = {}
    current_year = document_year
    separate_year: int | None = None
    previous_index = 0
    for position, (index, heading) in enumerate(headings):
        for line in lines[previous_index:index]:
            year_header = _CALENDAR_YEAR_HEADER.fullmatch(line)
            if year_header:
                current_year = int(year_header.group(1))
                if "학년도" not in line:
                    separate_year = current_year
        if heading["year"]:
            current_year = int(heading["year"])
        section_month = int(heading["month"])
        if academic_year is not None:
            # A school year starts in March. Side-by-side semester calendars
            # can be read row by row, so a January 2023 heading must not change
            # a later August panel from the same 2022 school year.
            section_year = (int(heading["year"]) if heading["year"] else separate_year
                            if separate_year is not None else academic_year + (section_month < 3))
        else:
            section_year = current_year
        end = headings[position + 1][0] if position + 1 < len(headings) else len(lines)
        previous_index = index + 1
        if not any(_calendar_entry_parts(line) for line in lines[index + 1:end]):
            continue
        skip.add(index)
        for body_index in range(index + 1, end):
            contexts[body_index] = section_year, section_month
    return skip, contexts


def _calendar_event(line: str, parts: tuple[tuple, tuple | None, str], section_year: int | None,
                    section_month: int, reference_year: int, source: str,
                    preserve_date_words: bool, *, source_line: str | None = None) -> tuple[Event, str]:
    first, second, body = parts
    own_year, own_month, own_day, weekday, _ = first
    year = own_year or section_year or reference_year
    month = own_month or section_month
    reasons: list[str] = []
    if own_year is None and section_year is None:
        reasons.append(f"연도가 없어 {year}년으로 표시했습니다. 원문의 연도를 확인해 주세요.")
    try:
        start = date(year, month, own_day)
    except ValueError:
        start = None
        reasons.append("유효하지 않은 날짜입니다.")
    if weekday == "숫자?":
        reasons.append("날짜 숫자를 명확히 읽지 못했습니다. 원문을 확인해 주세요.")
    elif weekday == "?":
        reasons.append("요일 문자를 명확히 읽지 못했습니다. 원문을 확인해 주세요.")
    elif weekday and start and _WEEKDAY[start.weekday()] != weekday:
        reasons.append("날짜와 요일이 일치하지 않습니다.")
    finish = None
    if second is None and _CALENDAR_RANGE.match(line, first[-1]):
        reasons.append("종료 날짜를 명확히 읽지 못했습니다. 원문을 확인해 주세요.")
    if second is not None:
        end_year, end_month, end_day, end_weekday, _ = second
        # An explicitly written next month can cross December into January.
        # A bare smaller day is ambiguous and is kept for review instead.
        last_month = end_month or month
        last_year = end_year or (year + (end_month is not None and last_month < month))
        try:
            finish = date(last_year, last_month, end_day)
            if end_weekday in ("?", "숫자?"):
                reasons.append("종료 날짜의 숫자나 요일을 명확히 읽지 못했습니다. 원문을 확인해 주세요.")
            elif end_weekday and _WEEKDAY[finish.weekday()] != end_weekday:
                reasons.append("종료 날짜와 요일이 일치하지 않습니다.")
            if start and finish < start:
                reasons.append("종료일이 시작일보다 빠릅니다.")
                finish = None
        except ValueError:
            reasons.append("종료 날짜가 올바르지 않습니다.")
    title_body = re.sub(r"\s+", " ", _PLACE.sub("", _TIME.sub("", body))).strip(" \t,，:：")
    if _invalid_time(body):
        reasons.append("시간이 올바르지 않습니다. 원문을 확인해 주세요.")
    title = line if preserve_date_words else title_body
    place_match = _PLACE.search(body)
    header = f"{section_year}년 {section_month}월" if section_year is not None else f"{section_month}월"
    event = Event(title=title, start_date=start.isoformat() if start else "",
                  end_date=finish.isoformat() if finish else None, time=_extract_time(body),
                  place=place_match.group(1).strip() if place_match else "",
                  notes="확인 필요: " + " ".join(dict.fromkeys(reasons)) if reasons else "",
                  source=source, source_text=header + "\n" + (source_line if source_line is not None else line),
                  status="review" if reasons else "open")
    return event, title_body


def _date_matches(text: str) -> list[re.Match]:
    """Keep clock ranges from supplying fragments that look like dates.

    For example, the short-date expression also matches ``00-19`` inside
    ``15:00-19:00``. A clock token is stronger evidence at that position;
    excluding overlapping matches also keeps the whole sentence together.
    """
    times = [match.span() for match in _TIME.finditer(text)]
    return [match for match in _DATE.finditer(text)
            if not any(start < match.end() and match.start() < end for start, end in times)
            and not (match["smonth"] and re.match(
                r"\s*(?:%|(?:kg|cm|mm)(?![A-Za-z])|(?:퍼센트|시간|학년|명|점|쪽|페이지|킬로|만원|원)(?![가-힣A-Za-z]))",
                text[match.end():], re.IGNORECASE))]


def _month_dates(line: str, date_matches: list[re.Match], year: int) -> tuple[list[str], str]:
    """Resolve explicit month-only tokens to the first of their months.

    Ranges produce independent reminders per month. Real day-level dates are
    masked before matching so importing a dated event never creates an extra
    first-of-month reminder.
    """
    masked = list(line)
    for match in date_matches:
        masked[match.start():match.end()] = " " * (match.end() - match.start())
    remaining = "".join(masked)
    dates: list[str] = []
    spans: list[tuple[int, int]] = []
    for match in _MONTH_RANGE.finditer(remaining):
        start_year = int(match["year"] or year)
        first, last = int(match["first"]), int(match["last"])
        end_year = int(match["end_year"] or (start_year + (last < first)))
        first_index, last_index = start_year * 12 + first - 1, end_year * 12 + last - 1
        # A month expression is not a request to create many years of events.
        if 0 <= last_index - first_index < 24:
            for value in range(first_index, last_index + 1):
                dates.append(date(value // 12, value % 12 + 1, 1).isoformat())
        spans.append(match.span())
    range_masked = list(remaining)
    for start, end in spans:
        range_masked[start:end] = " " * (end - start)
    for match in _MONTH.finditer("".join(range_masked)):
        dates.append(date(int(match["year"] or year), int(match["month"]), 1).isoformat())
        spans.append(match.span())
    clean = list(line)
    for start, end in spans:
        clean[start:end] = " " * (end - start)
    return list(dict.fromkeys(dates)), "".join(clean)


def _month_clean_title(text: str) -> str:
    value = _line_title(text)
    value = _YEAR.sub("", value)
    value = _MONTH_LABEL.sub("", value).strip(" \t•·*|-:：~∼–—.,，、()[]/&")
    return re.sub(r"\s+", " ", value)


def _month_title(lines: list[str], index: int, clean_line: str, source: str, months: list[str]) -> str:
    if "|" in clean_line or "\t" in clean_line:
        raw_columns = [re.sub(r"\s+", " ", part).strip() for part in re.split(r"[|\t]", clean_line)]
    else:
        raw_columns = [_month_clean_title(clean_line)]
    if len(raw_columns) == 6 and not raw_columns[-1]:
        subject, area, unit, content = raw_columns[:4]
        parts = [subject[:12], area[:15], unit[:35]]
        if content:
            parts.append(content[:35].rstrip() + ("…" if len(content) > 35 else ""))
        return " · ".join(part for part in parts if part)
    columns = raw_columns
    columns = [part for part in columns if part and part not in ("교과", "단원", "평가내용", "평가 내용", "시기", "평가 시기")]
    if len(columns) >= 2:
        # Subject + unit identify a table row. Keep detailed assessment criteria
        # in notes rather than squeezing a complete paragraph into a sticky.
        title = " · ".join(columns[:2])
        if len(columns) >= 3:
            summary = columns[2]
            title += " · " + (summary[:35].rstrip() + "…" if len(summary) > 35 else summary)
        return title[:100].rstrip()
    own = columns[0] if columns else ""
    if own and own not in ("교과", "단원", "평가내용", "평가 내용", "시기", "평가 시기") and not _MONTH_METADATA.search(own):
        return own[:100].rstrip() + ("…" if len(own) > 100 else "")
    for prior in reversed(lines[max(0, index - 6):index]):
        if not prior:
            break
        if _date_matches(prior) or _MONTH.search(prior) or _MONTH_RANGE.search(prior):
            continue
        title = _month_clean_title(prior)
        if title != "표" and _looks_title(title) and not re.match(r"^(?:교과|단원|평가내용|평가 내용|시기|평가 시기)(?:\s*[|:]|$)", title):
            return title
    source_title = Path(source).stem if source else ""
    return source_title or f"{int(months[0][5:7])}월 일정"


def _line_title(line: str) -> str:
    # Remove complete clock tokens before the date cleaner can consume the
    # middle of a clock range as a short numeric date.
    title = _DATE.sub("", _TIME.sub("", line))
    title = re.sub(r"\([월화수목금토일](?:요일)?\)|[월화수목금토일]요일", "", title)
    title = _PLACE.sub("", title)
    title = _DATE_LABEL.sub("", title)
    title = re.sub(r"(?:부터|까지|예정|오전|오후)\s*$", "", title)
    title = title.strip(" \t•·*|-:：~∼–—.,，()[]")
    return re.sub(r"\s+", " ", title)


def _looks_title(text: str) -> bool:
    if not text or len(text) > 120 or _METADATA.search(text):
        return False
    if re.match(r"^(?:장소|제출처|담당|문의|대상|준비물|참고|작성자|시간|일시|일자|기간|기한|날짜|내용|비고|일정명)\s*[:：|]", text):
        return False
    return text not in ("일시", "일자", "날짜", "장소", "기간", "기한", "시간", "일정", "일정명", "내용", "비고", "안내", "공문")


def _time_value(match: re.Match) -> str | None:
    hour = int(match["hour"])
    minute = int(match["minute"] or match["kminute"] or 0)
    period = match["period"]
    if period and not 1 <= hour <= 12:
        return None
    if period == "오후" and hour != 12:
        hour += 12
    elif period == "오전" and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute:02d}" if hour <= 23 and minute <= 59 else None


def _extract_time(text: str) -> str | None:
    match = _TIME.search(text)
    return _time_value(match) if match else None


def _invalid_time(text: str) -> bool:
    return any(_time_value(match) is None for match in _TIME.finditer(text))


def _month_heading_with_dates(lines: list[str], index: int, clean_line: str) -> bool:
    """A month schedule heading is layout only when dated rows follow it."""
    clean = re.sub(r"\s+", " ", _YEAR.sub("", clean_line)).strip(" \t•·*|-:：~∼–—.,，、()[]/&")
    if not clean or not re.fullmatch(r"(?:(?:업무|학사|학교|행사|주요|월별|월간|수업|학년|교육|학급|연간|운영)?\s*일정\s*)+", clean):
        return False
    for following in lines[index + 1:index + 7]:
        if not following:
            continue
        if _date_matches(following):
            return True
    return False


def _title_for_line(lines: list[str], index: int, segment: str, *,
                    preserve_date_words: bool = False, original_segment: str | None = None) -> str:
    if "|" in segment or "\t" in segment:
        columns = re.split(r"[|\t]", segment)
        for column in columns:
            if not _date_matches(column) and not _TIME.search(column):
                clean = _line_title(column)
                if _looks_title(clean):
                    return clean
    own = _line_title(segment)
    if _looks_title(own):
        # The cleaner is only for identifying a meaningful title. Once an
        # inline sentence is recognized, keep its date, weekday and wording.
        # Date-only metadata still uses the separate heading below.
        return (original_segment if original_segment is not None else segment).strip() if preserve_date_words else own
    label_match = _DATE_LABEL.match(segment)
    label = label_match.group().strip(" \t•·*|-:：~∼–—.()[]") if label_match else ""
    meaningful_label = label if re.search(r"신청|접수|제출|마감|행사|실시", label) else ""
    prior_title = ""
    for prior in reversed(lines[max(0, index - 6):index]):
        if not prior or _date_matches(prior):
            break
        clean = _line_title(prior)
        if _looks_title(clean):
            prior_title = clean
            break
    generic_banner = re.fullmatch(r"(?:(?:학교|학급|학년|행사|업무|월간|주간|학사)\s*)?일정(?:\s*(?:알림|안내|공지|목록|표))?|공지(?:\s*사항)?", prior_title)
    if not meaningful_label and (not prior_title or generic_banner):
        # Some notices print the date above its title. Prefer an immediately
        # following title to a generic document banner from earlier lines.
        for following in lines[index + 1:index + 3]:
            if not following or _date_matches(following) or _MONTH.search(following):
                break
            clean = _line_title(following)
            if _looks_title(clean):
                return clean
            if _TIME.search(following) or _PLACE.search(following):
                break
    for prior in reversed(lines[max(0, index - 6):index]):
        if not prior:
            break
        clean = _line_title(prior)
        if _date_matches(prior):
            if not clean:
                continue
            break
        if _looks_title(clean) and not re.match(r"^(?:장소|대상|담당|문의|제출처|내용|비고)\s*[:：]", clean):
            return f"{clean} · {meaningful_label}" if meaningful_label else clean
    # A date is still evidence of a candidate, but a generic title requires review.
    return "제목 확인 필요"


def _semester_table_header(columns: list[str]) -> tuple[int, int, int] | None:
    """Require explicit semester columns before interpreting a date matrix."""
    labels = [_normal(value) for value in columns]
    if len(labels) != 3:
        return None
    title_labels = {"구분", "행사", "행사명", "일정명"}
    first = [i for i, value in enumerate(labels) if value in {"1학기", "제1학기"}]
    second = [i for i, value in enumerate(labels) if value in {"2학기", "제2학기"}]
    title = [i for i, value in enumerate(labels) if value in title_labels]
    if len(first) == len(second) == len(title) == 1:
        return title[0], first[0], second[0]
    return None


def _semester_cell_event(cell: str, title: str, *, year: int | None,
                         academic_year: int | None, reference_year: int,
                         source: str, source_text: str) -> Event | None:
    """Read one proved semester cell, without parsing numbers in its title.

    Two-digit years are accepted only inside this explicitly labelled date
    matrix. This keeps ordinary numbers and unit identifiers out of the
    general date parser. Empty/slashed cells mean no event for that semester.
    """
    if not cell or re.fullmatch(r"[\s./\\~∼–—−\-]+", cell) or _normal(cell) in {"없음", "해당없음", "-"}:
        return None
    expanded = re.sub(
        r"(?<![\d.])['‘’ʼ]?\s*(\d{2})\s*\.\s*(\d{1,2})\s*\.\s*(\d{1,2})(?!\d)",
        lambda match: f"{2000 + int(match[1])}.{match[2]}.{match[3]}", cell)
    first = _calendar_date_token(expanded + " ", 0)
    if first is None or first[1] is None:
        return Event(title=title or "제목 확인 필요", source=source, source_text=source_text,
                     status="review", notes="확인 필요: 학기별 일정의 날짜를 명확히 읽지 못했습니다. 원문을 확인해 주세요.")
    connector = _CALENDAR_RANGE.match(expanded, first[-1])
    second = _calendar_date_token(expanded + " ", connector.end(), allow_comma=True) if connector else None
    end = second[-1] if second is not None else first[-1]
    context_year = (academic_year + (first[1] < 3) if academic_year is not None else year)
    event, _ = _calendar_event(expanded + " " + (title or "일정"), (first, second, title or "일정"),
                               context_year, first[1], reference_year, source, False)
    event.title = title or "제목 확인 필요"
    event.source_text = source_text
    if expanded[end:].strip(" \t."):
        event.status = "review"
        detail = "확인 필요: 학기별 날짜 셀에 명확히 읽지 못한 내용이 있습니다. 원문을 확인해 주세요."
        event.notes = "\n".join(value for value in (event.notes, detail) if value)
    if not title:
        event.status = "review"
        event.notes = "\n".join(value for value in (event.notes, "확인 필요: 일정 제목을 확인해 주세요.") if value)
    return event


def parse_schedule_text(text: str, source: str = "", reference_date: date | str | None = None, *,
                        _preserve_date_words: bool = True) -> list[Event]:
    """Extract dated candidates, never fabricate a date for invalid/relative text.

    Month-only dates intentionally become the first of each listed month and
    are immediately active, using a document year or the reference/current year.
    A day-level date missing a year is still marked ``review``. Relative
    expressions also remain review candidates because the
    source document's base date is not necessarily the import date. Review notes
    explain each uncertainty; due_events never returns review candidates.
    """
    text = unicodedata.normalize("NFKC", text).replace("\r\n", "\n").replace("\r", "\n")
    reference = reference_date or date.today()
    if isinstance(reference, str):
        reference = date.fromisoformat(reference)
    # In tabular text, boundary tabs encode empty first/last cells. Removing
    # them would move a finish date into the start column or lose an empty title.
    lines = [line.strip(" \v\f") if "\t" in line and line.strip() else line.strip()
             for line in text.splitlines()]
    academic_year = re.search(r"(?<!\d)(19\d{2}|20\d{2})\s*학년도", text)
    document_year = academic_year or _YEAR.search(text) or _YEAR.search(source)
    calendar_year = next((int(match["year"]) for line in lines
                          if (match := _CALENDAR_MONTH_HEADER.fullmatch(line)) and match["year"]), None)
    if calendar_year is None:
        calendar_year = next((int(match.group(1)) for line in lines
                              if (match := _CALENDAR_YEAR_HEADER.fullmatch(line))), None)
    explicit_year = int(document_year.group(1)) if document_year else calendar_year
    month_year = explicit_year or reference.year
    calendar_headers, calendar_contexts = _calendar_sections(
        lines, explicit_year, int(academic_year.group(1)) if academic_year else None)
    calendar_preamble_end = min(calendar_headers) if calendar_headers else 0
    events: list[Event] = []
    seen: set[str] = set()
    assessment_columns: list[str] | None = None
    assessment_time_column: int | None = None
    period_columns: list[str] | None = None
    period_indexes: tuple[int, int, int] | None = None
    semester_indexes: tuple[int, int, int] | None = None
    semester_header = ""
    calendar_continuations: set[int] = set()
    for index, line in enumerate(lines):
        if not line:
            semester_indexes = None
            continue
        if re.fullmatch(r"\[[^\[\]]+\]", line):
            period_columns = None
            period_indexes = None
            semester_indexes = None
        if index in calendar_headers or index in calendar_continuations:
            continue
        if index < calendar_preamble_end:
            # A date immediately below a calendar/document heading can be its
            # publication stamp. A real event heading such as ``졸업식`` must
            # still supply the title of its following date-only line.
            stamp = _DATE.fullmatch(line.rstrip(" ."))
            preceding = next((value for value in reversed(lines[:index]) if value), "")
            document_heading = (re.search(r"학사\s*일정|학사력|연간\s*일정|주요\s*일정", preceding)
                                or (_CALENDAR_YEAR_HEADER.fullmatch(preceding) and "학년도" in preceding))
            if stamp and stamp["year"] and (document_heading or _METADATA.search(preceding)):
                continue
        if index in calendar_contexts:
            if _CALENDAR_GRADE_RANGE.match(line):
                continue
            parts = _calendar_entry_parts(line)
            if parts is not None:
                section_year, section_month = calendar_contexts[index]
                wrapped = [line]
                for following_index in range(index + 1, len(lines)):
                    following = lines[following_index]
                    if (not following or following_index in calendar_headers
                            or calendar_contexts.get(following_index) != (section_year, section_month)
                            or _calendar_entry_parts(following) is not None
                            or _CALENDAR_YEAR_HEADER.fullmatch(following)
                            or _date_matches(following) or _MONTH.search(following)
                            or re.match(r"^수업\s*일수\s*[:：]?", following)
                            or re.fullmatch(r"[\s|일월화수목금토\d.,/\-]+", following)
                            or not re.search(r"[가-힣A-Za-z]", following)
                            or not _looks_title(following)):
                        break
                    wrapped.append(following)
                    calendar_continuations.add(following_index)
                if len(wrapped) > 1:
                    line = " ".join(wrapped)
                    parts = (parts[0], parts[1], " ".join([parts[2]] + wrapped[1:]))
                event, identity_title = _calendar_event(
                    line, parts, section_year, section_month, reference.year, source,
                    _preserve_date_words, source_line="\n".join(wrapped))
                identity_event = Event(**asdict(event))
                identity_event.title = identity_title
                fingerprint = _fingerprint(identity_event)
                if fingerprint not in seen:
                    events.append(event)
                    seen.add(fingerprint)
                continue
            if re.match(r"^수업\s*일수\s*[:：]?", line) or not re.search(r"[가-힣A-Za-z]", line):
                continue
            if re.fullmatch(r"[\s|일월화수목금토\d.,/\-]+", line):
                continue
        original_table_line = line
        if "|" not in line and "\t" not in line:
            spaced_columns = re.split(r"\s{2,}", line)
            spaced_labels = {_normal(value) for value in spaced_columns}
            if ((period_columns is not None and len(spaced_columns) == len(period_columns))
                    or (semester_indexes is not None and len(spaced_columns) == 3)
                    or _semester_table_header(spaced_columns) is not None
                    or ({"시작일", "종료일"} <= spaced_labels and len(spaced_columns) >= 3)):
                line = "\t".join(spaced_columns)
            else:
                semester_indexes = None
        if "|" in line or "\t" in line:
            table_columns = [column.strip() for column in re.split(r"[|\t]", line)]
            semester_cells = list(table_columns)
            if line.startswith("|") and not semester_cells[0]:
                semester_cells.pop(0)
            if line.endswith("|") and not semester_cells[-1]:
                semester_cells.pop()
            new_semester_indexes = _semester_table_header(semester_cells)
            if new_semester_indexes is not None:
                semester_indexes = new_semester_indexes
                semester_header = "\t".join(semester_cells)
                period_columns = period_indexes = None
                assessment_columns = assessment_time_column = None
                continue
            if semester_indexes is not None:
                # A trailing blank cell may be omitted by document readers.
                # Only the established matrix header supplies that position.
                if 1 < len(semester_cells) < 3:
                    semester_cells.extend([""] * (3 - len(semester_cells)))
                if len(semester_cells) == 3:
                    if all(re.fullmatch(r"[:\-\s]+", value) for value in semester_cells):
                        continue
                    title_index, first_index, second_index = semester_indexes
                    # A different table's explicit headers end this matrix.
                    labels = [_normal(value) for value in semester_cells]
                    general_headers = {"기간", "내용", "장소", "시간", "제목", "일정명", "행사명", "비고"}
                    if (any(value in {"날짜", "일자", "일시", "시작일", "종료일", "평가시기", "평가내용"}
                            for value in labels)
                            or sum(value in general_headers for value in labels) >= 2):
                        semester_indexes = None
                    else:
                        context = f"{explicit_year}학년도\n" if academic_year else f"{explicit_year}년\n" if explicit_year else ""
                        context += semester_header + "\n" + original_table_line
                        for cell_index in (first_index, second_index):
                            candidate = _semester_cell_event(
                                semester_cells[cell_index], semester_cells[title_index], year=explicit_year,
                                academic_year=int(academic_year.group(1)) if academic_year else None,
                                reference_year=reference.year, source=source, source_text=context)
                            if candidate is not None and _fingerprint(candidate) not in seen:
                                events.append(candidate)
                                seen.add(_fingerprint(candidate))
                        continue
                else:
                    semester_indexes = None
            normalized_columns = [_normal(column) for column in table_columns]
            start_headers = {"시작일", "시작일자", "시작날짜", "개시일"}
            end_headers = {"종료일", "종료일자", "종료날짜", "마지막날짜"}
            title_headers = {"제목", "일정명", "일정", "행사", "행사명", "내용", "업무", "일정내용", "활동명"}
            if (any(column in start_headers for column in normalized_columns)
                    and any(column in end_headers for column in normalized_columns)
                    and any(column in title_headers for column in normalized_columns)):
                period_columns = normalized_columns
                period_indexes = (next(i for i, value in enumerate(normalized_columns) if value in start_headers),
                                  next(i for i, value in enumerate(normalized_columns) if value in end_headers),
                                  next(i for i, value in enumerate(normalized_columns) if value in title_headers))
                continue
            if (period_columns is not None
                    and ((any(value in {"날짜", "일자", "일시"} for value in normalized_columns)
                          and any(value in title_headers for value in normalized_columns))
                         or ("평가내용" in normalized_columns
                             and any(value in {"평가시기", "시기", "평가월", "평가일", "평가일시"} for value in normalized_columns)))):
                period_columns = None
                period_indexes = None
            if (period_columns is not None and 0 < len(table_columns) < len(period_columns)
                    and any(_date_matches(value) for value in table_columns)):
                # CSV cleanup and spreadsheet readers can omit trailing empty
                # cells. A proved period header supplies their column positions.
                table_columns.extend([""] * (len(period_columns) - len(table_columns)))
            if period_columns is not None and len(table_columns) == len(period_columns):
                start_index, end_index, title_index = period_indexes
                start_cell, end_cell, title_cell = (table_columns[i] for i in period_indexes)
                if start_cell:
                    date_cell = start_cell + ("~" + end_cell if end_cell else "")
                    candidates = parse_schedule_text(date_cell + " " + (title_cell or "일정"), source,
                                                     reference_date=reference, _preserve_date_words=_preserve_date_words)
                else:
                    candidates = [Event(status="review", notes="확인 필요: 시작일이 없습니다.")]
                for candidate in candidates:
                    candidate.title = title_cell or "제목 확인 필요"
                    if not title_cell:
                        candidate.status = "review"
                        candidate.notes = "\n".join(value for value in (candidate.notes, "확인 필요: 일정 제목을 확인해 주세요.") if value)
                    details = []
                    for column_index, (label, value) in enumerate(zip(period_columns, table_columns)):
                        if column_index in period_indexes or not value:
                            continue
                        if label in {"장소", "위치", "제출처"}:
                            candidate.place = value
                        elif label in {"시간", "시각", "시작시간"}:
                            candidate.time = _extract_time(value)
                            if _invalid_time(value):
                                candidate.status = "review"
                                candidate.notes = "\n".join(part for part in (candidate.notes, "확인 필요: 시간이 올바르지 않습니다.") if part)
                        else:
                            details.append(f"{label}: {value}")
                    candidate.notes = "\n".join(part for part in [candidate.notes] + details if part)
                    candidate.source = source
                    candidate.source_text = " | ".join(period_columns) + "\n" + original_table_line
                    fingerprint = _fingerprint(candidate)
                    if fingerprint not in seen:
                        events.append(candidate)
                        seen.add(fingerprint)
                continue
            time_headers = {"평가시기", "시기", "평가월", "평가일", "평가일시"}
            if "평가내용" in normalized_columns and any(column in time_headers for column in normalized_columns):
                assessment_columns = table_columns
                assessment_time_column = next(i for i, column in enumerate(normalized_columns) if column in time_headers)
                continue
            if assessment_columns is not None and len(table_columns) == len(assessment_columns):
                # Dates in unit numbers (e.g. '3-2.') or assessment prose are
                # not the assessment schedule. Only the timing column is read.
                timing = table_columns[assessment_time_column]
                candidates = parse_schedule_text(timing + " 평가", reference_date=date(month_year, 1, 1),
                                                 _preserve_date_words=_preserve_date_words)
                body_columns = list(table_columns)
                body_columns[assessment_time_column] = ""
                title = _month_title(lines, index, " | ".join(body_columns), source,
                                     [candidate.start_date or reference.isoformat() for candidate in candidates] or [reference.isoformat()])
                details = "\n".join(f"{_normal(label)}: {value}" for i, (label, value) in enumerate(zip(assessment_columns, table_columns))
                                    if value and i != assessment_time_column)
                for candidate in candidates:
                    candidate.title = title
                    candidate.source = source
                    candidate.notes = "\n".join(value for value in (details, candidate.notes if candidate.status == "review" else "") if value)
                    candidate.source_text = line
                    fingerprint = _fingerprint(candidate)
                    if fingerprint not in seen:
                        events.append(candidate)
                        seen.add(fingerprint)
                continue
        matches = _date_matches(line)
        if _METADATA.search(line) and not re.search(r"행사|회의|연수|제출|마감|신청|접수|교육|일정", line):
            continue
        months, month_clean = _month_dates(line, matches, month_year)
        if months and not matches and _month_heading_with_dates(lines, index, month_clean):
            continue
        if months and not _MONTH_METADATA.search(line):
            month_context: list[str] = []
            if "|" not in line and "\t" not in line:
                for following in lines[index + 1:index + 5]:
                    if not following or _date_matches(following) or _MONTH.search(following) or _MONTH_RANGE.search(following):
                        break
                    month_context.append(following)
            context = "\n".join([line] + month_context)
            title = _month_title(lines, index, month_clean, source, months)
            identity_title = title
            if _preserve_date_words and "|" not in line and "\t" not in line:
                own = _month_clean_title(month_clean)
                if own and _looks_title(own):
                    title = line
            place_match = _PLACE.search(context)
            place = place_match.group(1).strip() if place_match else ""
            details = _month_clean_title(month_clean)
            if "|" in line or "\t" in line:
                columns = [_month_clean_title(part) for part in re.split(r"[|\t]", month_clean)]
                if len(columns) == 6 and not columns[-1]:
                    labels = ("교과", "영역", "단원", "평가내용", "평가방법")
                    details = "\n".join(f"{label}: {value}" for label, value in zip(labels, columns) if value)
            notes = "\n".join(part for part in [details] + month_context if part)
            for first_day in months:
                month_date = date.fromisoformat(first_day)
                last_day = date(month_date.year, month_date.month, monthrange(month_date.year, month_date.month)[1]).isoformat()
                invalid_time = _invalid_time(line)
                if invalid_time:
                    notes = "\n".join(value for value in (notes, "확인 필요: 시간이 올바르지 않습니다. 원문을 확인해 주세요.") if value)
                event = Event(title=title, start_date=first_day, end_date=last_day, time=_extract_time(line),
                              place=place, notes=notes, source=source, source_text=context, status="open", date_precision="month")
                if invalid_time:
                    event.status = "review"
                identity_event = Event(**asdict(event))
                identity_event.title = identity_title
                fingerprint = _fingerprint(identity_event)
                if fingerprint not in seen:
                    events.append(event)
                    seen.add(fingerprint)
        if not matches:
            if months:
                continue
            relative = _RELATIVE.search(line)
            if relative:
                title = _title_for_line(lines, index, line.replace(relative.group(), ""),
                                        preserve_date_words=_preserve_date_words, original_segment=line)
                event = Event(title=title, notes="확인 필요: 상대 날짜의 기준일을 확인해 주세요.", source=source,
                              source_text=line, status="review", time=_extract_time(line))
                if _fingerprint(event) not in seen:
                    events.append(event)
                    seen.add(_fingerprint(event))
            continue
        context_after: list[str] = []
        for following in lines[index + 1:index + 5]:
            if not following or _date_matches(following):
                break
            context_after.append(following)
        context = "\n".join([line] + context_after)
        cursor = 0
        while cursor < len(matches):
            first_cursor = cursor
            match = matches[cursor]
            reasons: list[str] = []
            year = int(match["year"] or reference.year)
            month = int(match["month"] or match["kmonth"] or match["smonth"])
            day = int(match["day"] or match["kday"] or match["sday"])
            start = ""
            end = None
            try:
                parsed = date(year, month, day)
                start = parsed.isoformat()
            except ValueError:
                parsed = None
                reasons.append("유효하지 않은 날짜입니다.")
            if not match["year"]:
                reasons.append(f"연도가 없어 {year}년으로 표시했습니다. 원문의 연도를 확인해 주세요.")
            # Match a date range only when its joining text actually denotes a range.
            range_end = match.end()
            if cursor + 1 < len(matches):
                next_match = matches[cursor + 1]
                connector = line[match.end():next_match.start()].strip()
                if re.fullmatch(r"\.?\s*(?:\([월화수목금토일](?:요일)?\))?\s*(?:~|∼|–|—|−|-|부터)\s*", connector):
                    end_month = int(next_match["month"] or next_match["kmonth"] or next_match["smonth"])
                    end_year = int(next_match["year"] or (year + (end_month < month)))
                    end_day = int(next_match["day"] or next_match["kday"] or next_match["sday"])
                    try:
                        end = date(end_year, end_month, end_day).isoformat()
                        if start and end < start:
                            reasons.append("종료일이 시작일보다 빠릅니다.")
                            end = None
                    except ValueError:
                        reasons.append("종료 날짜가 올바르지 않습니다.")
                    range_end = next_match.end()
                    end_weekday = _CALENDAR_WEEKDAY.match(line, range_end)
                    if end_weekday and end and _WEEKDAY[date.fromisoformat(end).weekday()] != end_weekday.group(1):
                        reasons.append("종료 날짜와 요일이 일치하지 않습니다.")
                    cursor += 1
            if range_end == match.end():
                connector = re.match(r"\.?\s*(?:\([월화수목금토일](?:요일)?\))?\s*(?:~|∼|–|—|−|-|부터)\s*",
                                     line[match.end():])
                if connector:
                    end_position = match.end() + connector.end()
                    shortened = _calendar_date_token(line, end_position)
                    if shortened is None:
                        until = re.match(r"(?P<day>\d{1,2})(?:\s*일|\.)?\s*까지(?=\s|$)", line[end_position:])
                        if until:
                            shortened = (None, None, int(until["day"]), "", end_position + until.end())
                    if shortened is not None and shortened[0] is None and shortened[1] is None:
                        end_day, end_weekday, range_end = shortened[2:]
                        try:
                            end_date = date(year, month, end_day)
                            end = end_date.isoformat()
                            if start and end < start:
                                end = None
                                reasons.append("종료일이 시작일보다 빠릅니다.")
                            if end_weekday in ("?", "숫자?"):
                                reasons.append("종료 날짜의 숫자나 요일을 명확히 읽지 못했습니다. 원문을 확인해 주세요.")
                            elif end_weekday and _WEEKDAY[end_date.weekday()] != end_weekday:
                                reasons.append("종료 날짜와 요일이 일치하지 않습니다.")
                        except ValueError:
                            reasons.append("종료 날짜가 올바르지 않습니다.")
                    else:
                        reasons.append("종료 날짜를 명확히 읽지 못했습니다. 원문을 확인해 주세요.")
            tail = line[match.end():]
            weekday = re.match(r"\s*\.?\s*[(\[]?\s*([월화수목금토일])(?:요일)?\s*[)\]]", tail)
            if not weekday:
                weekday = re.match(r"\s*([월화수목금토일])요일", tail)
            if weekday and parsed and _WEEKDAY[parsed.weekday()] != weekday.group(1):
                reasons.append("날짜와 요일이 일치하지 않습니다.")
            segment_start = match.start() if first_cursor > 0 else 0
            if first_cursor > 0:
                separators = list(re.finditer(r"[|;；/]", line[matches[first_cursor - 1].end():match.start()]))
                if separators:
                    segment_start = matches[first_cursor - 1].end() + separators[-1].end()
            segment_end = matches[cursor + 1].start() if cursor + 1 < len(matches) else len(line)
            segment = line[segment_start:segment_end]
            title = _title_for_line(lines, index, segment, preserve_date_words=_preserve_date_words)
            if title == "제목 확인 필요":
                reasons.append("일정 제목을 확인해 주세요.")
            place_match = _PLACE.search(context)
            place = place_match.group(1).strip() if place_match else ""
            if not place and ("|" in line or "\t" in line):
                columns = [column.strip() for column in re.split(r"[|\t]", line) if column.strip()]
                if len(columns) >= 3 and not _date_matches(columns[-1]) and not _TIME.search(columns[-1]):
                    place = re.sub(r"^장소\s*[:：]?\s*", "", columns[-1])
            when = _extract_time(segment)
            invalid_time = _invalid_time(segment)
            if not when:
                for following in context_after:
                    if (re.match(r"^(?:시간|일시)\s*[:：]", following)
                            or (_TIME.search(following) and not _line_title(following).strip(" /|;，,"))):
                        when = _extract_time(following)
                        invalid_time = invalid_time or _invalid_time(following)
                        break
            if invalid_time:
                reasons.append("시간이 올바르지 않습니다. 원문을 확인해 주세요.")
            event = Event(title=title, start_date=start, end_date=end, time=when, place=place,
                          notes="확인 필요: " + " ".join(reasons) if reasons else "", source=source,
                          source_text=context, status="review" if reasons else "open")
            identity_event = Event(**asdict(event))
            identity_event.title = _title_for_line(lines, index, segment)
            fingerprint = _fingerprint(identity_event)
            if fingerprint not in seen:
                events.append(event)
                seen.add(fingerprint)
            cursor += 1
    return events


def _source_title_pairs(event: Event) -> list[tuple[str, str]]:
    """Return unique old/new titles proved by this record's saved source."""
    if not event.source_text:
        return []
    source_text = event.source_text
    saved_context = "\n".join(line.strip() for line in unicodedata.normalize("NFKC", source_text)
                              .replace("\r\n", "\n").replace("\r", "\n").splitlines())
    columns = re.split(r"[|\t]", source_text) if "\n" not in source_text else []
    if len(columns) == 6:
        source_text = "교과 | 영역 | 단원명 | 평가내용 | 평가방법 | 평가시기\n" + source_text
    elif len(columns) == 4:
        source_text = "교과 | 단원 | 평가내용 | 시기\n" + source_text
    try:
        reference = date.fromisoformat(event.start_date) if event.start_date else date.today()
    except ValueError:
        return []
    old = parse_schedule_text(source_text, event.source, reference_date=reference, _preserve_date_words=False)
    new = parse_schedule_text(source_text, event.source, reference_date=reference)

    def identity(candidate: Event) -> tuple:
        return (candidate.start_date, candidate.end_date, candidate.time or "", _normal(candidate.place),
                candidate.date_precision)

    expected = identity(event)
    pairs = set()
    for prior in old:
        if identity(prior) != expected or prior.source_text != saved_context:
            continue
        # Full source equality pairs the same candidate even when several
        # independent deadlines share a date. Ambiguous titles are not repaired.
        for current in new:
            if identity(current) == expected and current.source_text == prior.source_text:
                pairs.add((prior.title, current.title))
    return sorted(pairs)


def restored_source_title(event: Event) -> str | None:
    """Find a safe date-preserving replacement for a legacy generated title.

    Callers can use this pure helper when auditing an upgrade. It never changes
    a record, and returns None for manual edits, changed dates or ambiguous text.
    """
    titles = {new for old, new in _source_title_pairs(event)
              if old == event.title and new != event.title}
    return next(iter(titles)) if len(titles) == 1 else None


def _legacy_source_title(event: Event) -> str | None:
    titles = {old for old, new in _source_title_pairs(event)
              if _normal(new) == _normal(event.title) and _normal(old) != _normal(new)}
    return next(iter(titles)) if len(titles) == 1 else None
