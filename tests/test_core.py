import tempfile
import sqlite3
import unittest
from dataclasses import asdict, replace
from datetime import date
from pathlib import Path
from unittest.mock import patch

from alimi.core import Event, Store, format_event_date, parse_schedule_text, restored_source_title


def write_v1_database(path: Path, events: list[Event], settings: dict | None = None):
    """Make an actual old database, including the original unique identifier."""
    import re
    import unicodedata
    connection = sqlite3.connect(str(path))
    connection.executescript("""
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
            start_date TEXT NOT NULL DEFAULT '', end_date TEXT, time TEXT,
            place TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '', source_text TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL CHECK(status IN ('open', 'review', 'done')),
            completed_at TEXT, created_at TEXT NOT NULL, x INTEGER, y INTEGER,
            fingerprint TEXT NOT NULL UNIQUE
        );
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        PRAGMA user_version=1;
    """)
    def normal(text):
        return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or "")).casefold()
    names = ("id", "title", "start_date", "end_date", "time", "place", "notes", "source", "source_text", "status", "completed_at", "created_at", "x", "y")
    for event in events:
        fingerprint = "\x1f".join((normal(event.title), event.start_date, event.time or "", normal(event.place)))
        connection.execute(f"INSERT INTO events ({','.join(names)},fingerprint) VALUES ({','.join('?' for _ in range(len(names) + 1))})",
                           tuple(getattr(event, name) for name in names) + (fingerprint,))
    connection.executemany("INSERT INTO settings(key,value) VALUES (?,?)", (settings or {}).items())
    connection.commit()
    connection.close()


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.path = Path(self.directory.name) / "alimi.sqlite3"
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_overdue_completion_and_restore_survive_restart(self):
        event = self.store.upsert_event(Event(title="지난 일정", start_date="2026-10-01"))
        self.assertEqual([event.id], [item.id for item in self.store.due_events(date(2026, 10, 13))])
        self.store.update_position(event.id, -20, 420)
        self.store.complete_event(event.id)
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual([], self.store.due_events(date(2026, 10, 13)))
        saved = self.store.get_event(event.id)
        self.assertEqual((-20, 420), (saved.x, saved.y))
        self.assertIsNotNone(saved.completed_at)
        self.store.reopen_event(event.id)
        self.assertEqual("open", self.store.get_event(event.id).status)
        self.assertIsNone(self.store.get_event(event.id).completed_at)
        self.assertEqual(1, len(self.store.due_events(date(2026, 10, 13))))

    def test_reset_all_previews_every_status_and_preserves_local_settings(self):
        opened = self.store.upsert_event(Event(title="등록 일정", start_date="2026-10-15", notes="일정 메모"))
        completed = self.store.upsert_event(Event(title="완료 일정", start_date="2026-10-16"))
        self.store.complete_event(completed.id)
        review = self.store.upsert_event(Event(title="날짜 확인 필요", status="review"))
        settings = {"autostart": "True", "memo_mode": "icon", "memo_draft": "로컬에 남길 초안"}
        for key, value in settings.items():
            self.store.set_setting(key, value)
        before = self.store.list_events()
        self.assertEqual([opened.id, completed.id, review.id], self.store.schedule_reset_ids())
        count, backup_path = self.store.reset_events(self.store.schedule_reset_ids())
        self.assertEqual(3, count)
        self.assertTrue(backup_path.is_file())
        self.assertEqual(self.path.parent / "backups", backup_path.parent)
        self.assertEqual([], self.store.list_events())
        for key, value in settings.items():
            self.assertEqual(value, self.store.get_setting(key))
        backup = Store(backup_path)
        try:
            self.assertEqual(before, backup.list_events())
            for key, value in settings.items():
                self.assertEqual(value, backup.get_setting(key))
        finally:
            backup.close()
        self.store.restore(backup_path)
        self.assertEqual(before, self.store.list_events())

    def test_reset_range_uses_inclusive_overlap_for_all_dated_statuses(self):
        candidates = [
            Event(title="시작 경계", start_date="2026-10-10"),
            Event(title="종료 경계", start_date="2026-10-20"),
            Event(title="앞에서 겹침", start_date="2026-09-30", end_date="2026-10-10"),
            Event(title="뒤에서 겹침", start_date="2026-10-20", end_date="2026-11-01"),
            Event(title="기간 포함", start_date="2026-09-01", end_date="2026-12-01"),
            Event(title="완료", start_date="2026-10-15", status="done"),
            Event(title="확인", start_date="2026-10-15", status="review"),
            Event(title="월 일정", start_date="2026-10-01", date_precision="month"),
            Event(title="이전", start_date="2026-10-09"),
            Event(title="이후", start_date="2026-10-21"),
            Event(title="이전 월", start_date="2026-09-01", date_precision="month"),
            Event(title="날짜 미정", status="review"),
        ]
        self.store.import_candidates(candidates)
        before = self.store.list_events()
        changes = self.store._db.total_changes
        selected = self.store.schedule_reset_ids("2026-10-10", "2026-10-20")
        self.assertEqual([event.id for event in candidates[:8]], selected)
        self.assertEqual(before, self.store.list_events())
        self.assertEqual(changes, self.store._db.total_changes)
        count, _ = self.store.reset_events(selected)
        self.assertEqual(8, count)
        self.assertEqual({event.id for event in candidates[8:]}, {event.id for event in self.store.list_events()})

    def test_reset_month_overlap_includes_leap_day_and_year_boundaries(self):
        february = self.store.upsert_event(Event(title="윤년 월 일정", start_date="2028-02-01", date_precision="month"))
        december = self.store.upsert_event(Event(title="연말 월 일정", start_date="2027-12-01", date_precision="month"))
        year_span = self.store.upsert_event(Event(title="연말연시", start_date="2027-12-31", end_date="2028-01-02"))
        self.assertEqual([february.id], self.store.schedule_reset_ids("2028-02-29", "2028-02-29"))
        self.assertEqual([], self.store.schedule_reset_ids("2028-03-01", "2028-03-01"))
        self.assertEqual([december.id, year_span.id], self.store.schedule_reset_ids("2027-12-31", "2028-01-01"))
        self.assertEqual([year_span.id], self.store.schedule_reset_ids("2028-01-02", "2028-01-02"))

    def test_reset_rejects_incomplete_noncanonical_and_reversed_periods(self):
        event = self.store.upsert_event(Event(title="보존할 일정", start_date="2026-10-15"))
        invalid = [(None, "2026-10-15"), ("2026-10-15", None), ("", ""),
                   ("20261015", "2026-10-15"), ("2026-W42-4", "2026-10-15"),
                   ("2026-02-30", "2026-10-15"), ("2026-10-16", "2026-10-15"),
                   (date(2026, 10, 15), "2026-10-15"), (False, "2026-10-15")]
        for start, end in invalid:
            with self.subTest(start=start, end=end):
                with self.assertRaises(ValueError):
                    self.store.schedule_reset_ids(start, end)
                self.assertEqual([event], self.store.list_events())
        self.assertFalse((self.path.parent / "backups").exists())

    def test_reset_snapshot_keeps_later_imports_and_never_reuses_deleted_ids(self):
        first = self.store.upsert_event(Event(title="확인한 일정", start_date="2026-10-15"))
        selected = self.store.schedule_reset_ids()
        later = self.store.upsert_event(Event(title="확인 중 새로 가져온 일정", start_date="2026-10-15"))
        count, backup_path = self.store.reset_events(selected + selected + [999999])
        self.assertEqual(1, count)
        self.assertEqual([later], self.store.list_events())
        backup = Store(backup_path)
        try:
            self.assertEqual({first.id, later.id}, {event.id for event in backup.list_events()})
        finally:
            backup.close()
        self.store.reset_events([later.id])
        next_event = self.store.upsert_event(Event(title="초기화 후 새 일정", start_date="2026-10-15"))
        self.assertGreater(next_event.id, later.id)
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual([next_event], self.store.list_events())

    def test_reset_empty_or_already_absent_selection_does_not_create_backup(self):
        with patch.object(self.store, "backup") as backup:
            self.assertEqual((0, None), self.store.reset_events([]))
            self.assertEqual((0, None), self.store.reset_events([999999]))
            backup.assert_not_called()
        self.assertFalse((self.path.parent / "backups").exists())

    def test_reset_validates_every_id_before_any_backup_or_deletion(self):
        event = self.store.upsert_event(Event(title="보존할 일정", start_date="2026-10-15"))
        for invalid in (True, False, 0, -1, "1", 1.0, None):
            with self.subTest(invalid=invalid), patch.object(self.store, "backup") as backup:
                with self.assertRaises(ValueError):
                    self.store.reset_events([event.id, invalid])
                self.assertEqual([event], self.store.list_events())
                backup.assert_not_called()

    def test_reset_backup_failure_preserves_every_schedule(self):
        event = self.store.upsert_event(Event(title="백업 실패 시 보존", start_date="2026-10-15"))
        self.store.set_setting("memo_draft", "초안")
        with patch.object(self.store, "backup", side_effect=OSError("디스크 기록 실패")):
            with self.assertRaises(OSError):
                self.store.reset_events([event.id])
        self.assertEqual([event], self.store.list_events())
        self.assertEqual("초안", self.store.get_setting("memo_draft"))

    def test_reset_delete_failure_rolls_back_earlier_deletions(self):
        first = self.store.upsert_event(Event(title="첫 일정", start_date="2026-10-15"))
        second = self.store.upsert_event(Event(title="둘째 일정", start_date="2026-10-16"))
        before = self.store.list_events()
        self.store._db.executescript(f"""
            CREATE TRIGGER reset_failure BEFORE DELETE ON events WHEN OLD.id={second.id}
            BEGIN SELECT RAISE(ABORT, 'reset failed'); END;
        """)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.reset_events([first.id, second.id])
        self.assertEqual(before, self.store.list_events())
        self.assertEqual(1, len(list((self.path.parent / "backups").glob("*.sqlite3"))))
        self.store._db.execute("DROP TRIGGER reset_failure")
        self.store._db.commit()
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(before, self.store.list_events())

    def test_reset_large_batch_does_not_depend_on_sqlite_parameter_limit(self):
        events = [Event(title=f"초기화 일정 {number}", start_date="2026-10-15") for number in range(1200)]
        self.store.import_candidates(events)
        count, backup_path = self.store.reset_events(event.id for event in events)
        self.assertEqual(1200, count)
        self.assertEqual([], self.store.list_events())
        self.assertTrue(backup_path.is_file())

    def test_duplicate_does_not_reopen_completed_event(self):
        candidate = Event(title="교직원 협의회", start_date="2026-10-13", time="15:30", place="2층 회의실")
        self.assertEqual((1, 0), self.store.import_candidates([candidate]))
        self.store.complete_event(candidate.id)
        self.assertEqual((0, 1), self.store.import_candidates([Event(title="교직원  협의회", start_date="2026-10-13", time="15:30", place="2층  회의실")]))
        self.assertEqual("done", self.store.get_event(candidate.id).status)

    def test_find_duplicate_preserves_status_completion_and_identity(self):
        saved = self.store.upsert_event(Event(title="교직원 협의회", start_date="2026-10-13", time="15:30", place="2층 회의실"))
        self.store.complete_event(saved.id)
        before = self.store.get_event(saved.id)
        changes = self.store._db.total_changes
        existing = self.store.find_duplicate(Event(title="교직원  협의회", start_date="2026-10-13", time="15:30", place="2층  회의실"))
        self.assertEqual(before, existing)
        self.assertEqual("done", existing.status)
        self.assertIsNotNone(existing.completed_at)
        self.assertEqual(changes, self.store._db.total_changes)
        existing.status = "open"
        self.assertEqual(before, self.store.get_event(saved.id))
        self.assertIsNone(self.store.find_duplicate(Event(title="다른 일정", start_date="2026-10-13")))

    def test_invalid_import_rolls_back_whole_batch(self):
        with self.assertRaises(ValueError):
            self.store.import_candidates([Event(title="정상", start_date="2026-10-13"), Event(title="잘못된 날짜", start_date="2026-02-30")])
        self.assertEqual([], self.store.list_events())

    def test_reviewed_notes_and_end_date_edits_update_existing_identity(self):
        event = self.store.upsert_event(Event(
            title="상담 주간", start_date="2026-10-14", end_date="2026-10-16", notes="기존 메모"))
        self.assertEqual((0, 1, 0), self.store.save_reviewed_events([replace(event, notes="수정한 메모")]))
        self.assertEqual((0, 1, 0), self.store.save_reviewed_events([
            replace(self.store.get_event(event.id), end_date="2026-10-20")]))
        self.store.close()
        self.store = Store(self.path)
        saved = self.store.get_event(event.id)
        self.assertEqual((event.id, "수정한 메모", "2026-10-20"), (saved.id, saved.notes, saved.end_date))
        self.assertEqual(1, len(self.store.list_events()))
        # Ordinary imports remain add-only, including changed duplicate notes.
        self.assertEqual((0, 1), self.store.import_candidates([
            Event(title=event.title, start_date=event.start_date, notes="다시 가져온 메모")]))
        self.assertEqual(saved, self.store.get_event(event.id))

    def test_reviewed_field_edits_preserve_latest_completed_state_and_provenance(self):
        event = self.store.upsert_event(Event(
            title="기존 일정", start_date="2026-10-14", time="14:00", place="교실",
            notes="기존 메모", source="원본.pdf", source_text="원본 자료", created_at="2026-01-01T00:00:00+00:00"))
        draft = replace(event, title="변경한 일정", start_date="2028-02-01", end_date="2028-02-29",
                        date_precision="month", time="15:30", place="강당", notes="변경한 메모",
                        source="새로 가져온.pdf", source_text="새로 가져온 자료", x=1, y=2,
                        created_at="2099-01-01T00:00:00+00:00")
        self.store.complete_event(event.id)
        self.store.update_position(event.id, -30, 420)
        current = self.store.get_event(event.id)
        self.assertEqual((0, 1, 0), self.store.save_reviewed_events([draft]))
        saved = self.store.get_event(event.id)
        expected = replace(current, title=draft.title, start_date=draft.start_date, end_date=draft.end_date,
                           date_precision=draft.date_precision, time=draft.time, place=draft.place, notes=draft.notes)
        self.assertEqual(expected, saved)
        self.assertEqual(1, len(self.store.list_events()))
        self.assertEqual(event.id, self.store.find_duplicate(replace(saved, id=None)).id)
        self.assertIsNone(self.store.find_duplicate(replace(event, id=None)))

    def test_reviewed_edit_uses_current_reopened_state_and_confirms_review_state(self):
        event = self.store.upsert_event(Event(title="완료 후 되돌릴 일정", start_date="2026-10-14"))
        self.store.complete_event(event.id)
        stale_done = replace(self.store.get_event(event.id), notes="완료 이후 메모 수정")
        self.store.reopen_event(event.id)
        self.assertEqual((0, 1, 0), self.store.save_reviewed_events([stale_done]))
        saved = self.store.get_event(event.id)
        self.assertEqual(("open", None, stale_done.notes), (saved.status, saved.completed_at, saved.notes))
        review = self.store.upsert_event(Event(title="확인할 일정", status="review"))
        self.assertEqual((0, 1, 0), self.store.save_reviewed_events([
            replace(review, start_date="2026-10-15", status="done")]))
        self.assertEqual("open", self.store.get_event(review.id).status)

    def test_reviewed_additions_and_unchanged_duplicates_have_separate_counts(self):
        event = self.store.upsert_event(Event(title="기존 일정", start_date="2026-10-14"))
        self.store.complete_event(event.id)
        before = self.store.get_event(event.id)
        addition = Event(title="새 일정", start_date="2026-10-15")
        duplicate = replace(event, id=None, notes="중복 자료의 메모")
        drafts = [addition, duplicate]
        originals = [asdict(item) for item in drafts]
        self.assertEqual((1, 0, 1), self.store.save_reviewed_events(drafts))
        self.assertEqual(before, self.store.get_event(event.id))
        self.assertEqual(originals, [asdict(item) for item in drafts])
        self.assertEqual(2, len(self.store.list_events()))

    def test_reviewed_invalid_mixed_batch_rolls_back_without_mutating_drafts(self):
        event = self.store.upsert_event(Event(title="유지할 일정", start_date="2026-10-14", notes="기존 메모"))
        drafts = [Event(title="새 일정", start_date="2026-10-15"), replace(event, notes="수정한 메모"),
                  Event(title="잘못된 날짜", start_date="2026-02-30")]
        originals = [asdict(item) for item in drafts]
        with self.assertRaises(ValueError):
            self.store.save_reviewed_events(drafts)
        self.assertEqual([event], self.store.list_events())
        self.assertEqual(originals, [asdict(item) for item in drafts])
        drafts[2].start_date = "2026-10-16"
        self.assertEqual((2, 1, 0), self.store.save_reviewed_events(drafts))
        self.assertEqual(3, len(self.store.list_events()))
        self.assertEqual("수정한 메모", self.store.get_event(event.id).notes)

    def test_reviewed_collision_rolls_back_additions_and_earlier_edits(self):
        first = self.store.upsert_event(Event(title="첫 일정", start_date="2026-10-14"))
        second = self.store.upsert_event(Event(title="둘째 일정", start_date="2026-10-15"))
        before = self.store.list_events()
        addition = Event(title="새 일정", start_date="2026-10-16")
        with self.assertRaisesRegex(ValueError, "일정이 이미 있습니다"):
            self.store.save_reviewed_events([
                addition, replace(first, notes="수정한 메모"),
                replace(second, title=first.title, start_date=first.start_date)])
        self.assertEqual(before, self.store.list_events())
        self.assertIsNone(addition.id)

    def test_reviewed_missing_id_rolls_back_without_inserting_replacement(self):
        event = self.store.upsert_event(Event(title="유지할 일정", start_date="2026-10-14"))
        addition = Event(title="새 일정", start_date="2026-10-15")
        with self.assertRaisesRegex(ValueError, "수정할 일정이 없습니다"):
            self.store.save_reviewed_events([
                addition, replace(event, notes="수정한 메모"),
                Event(id=999, title="삭제된 일정", start_date="2026-10-16")])
        self.assertEqual([event], self.store.list_events())
        self.assertIsNone(addition.id)

    def test_reviewed_edit_validates_date_precision_and_range(self):
        event = self.store.upsert_event(Event(title="유지할 일정", start_date="2026-10-14"))
        for changes in ({"date_precision": "unknown"}, {"date_precision": "month"},
                        {"end_date": "2026-02-30"}, {"end_date": "2026-10-13"}, {"start_date": ""}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    self.store.save_reviewed_events([replace(event, **changes)])
                self.assertEqual(event, self.store.get_event(event.id))

    def test_legacy_date_titles_restore_without_changing_saved_schedule_fields(self):
        examples = ("10월 30일까지 수행평가 제출하세요", "11월 1일까지 수행평가 내세요", "10월 22일까지 인사기록 제출",
                    "2026년 10월 20일~2026년 10월 22일 상담 주간", "10월 국어 평가", "내일 학급 안내 제출")
        saved = []
        for index, text in enumerate(examples):
            event = parse_schedule_text(text, reference_date="2026-10-14", _preserve_date_words=False)[0]
            if event.start_date:
                event.status = "open"
            event.notes = "사용자가 수정한 세부 내용"
            event.x, event.y = 25 + index, 300 + index
            self.store.upsert_event(event)
            if index == 0:
                self.store.complete_event(event.id)
            saved.append(self.store.get_event(event.id))
        before = {event.id: asdict(event) for event in saved}
        # Mimic a real schema-2 database written before this data repair.
        self.store._db.execute("DELETE FROM settings WHERE key='source_date_titles_v1'")
        self.store._db.commit()
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(2, self.store._db.execute("PRAGMA user_version").fetchone()[0])
        for text, event in zip(examples, saved):
            repaired = self.store.get_event(event.id)
            self.assertEqual(text, repaired.title)
            expected = dict(before[event.id], title=text)
            self.assertEqual(expected, asdict(repaired))
        completed = self.store.get_event(saved[0].id)
        self.assertEqual("done", completed.status)
        self.assertEqual((0, 1), self.store.import_candidates(parse_schedule_text(examples[0], reference_date="2026-10-14")))
        self.assertEqual(completed, self.store.get_event(completed.id))

    def test_source_title_repair_skips_manual_edits_changed_dates_and_unclear_source(self):
        original = "10월 30일까지 수행평가 제출하세요"
        event = parse_schedule_text(original, reference_date="2026-10-14", _preserve_date_words=False)[0]
        self.assertEqual(original, restored_source_title(event))
        event.title = "제가 바꾼 제목"
        self.assertIsNone(restored_source_title(event))
        event.title = "까지 수행평가 제출하세요"
        event.start_date = "2026-10-31"
        self.assertIsNone(restored_source_title(event))
        event.start_date = "2026-10-30"
        event.source_text = original + "\n2026년 11월 2일 다른 일정"
        self.assertIsNone(restored_source_title(event))
        event.source_text = ""
        self.assertIsNone(restored_source_title(event))

    def test_title_repair_is_once_only_and_collision_keeps_both_existing_records(self):
        text = "10월 30일까지 수행평가 제출하세요"
        old = parse_schedule_text(text, reference_date="2026-10-14", _preserve_date_words=False)[0]
        old.status = "open"
        self.store.upsert_event(old)
        current = Event(title=text, start_date=old.start_date, status="done", completed_at="2026-10-14T00:00:00+00:00")
        self.store.upsert_event(current)
        before = self.store.list_events()
        self.store._db.execute("DELETE FROM settings WHERE key='source_date_titles_v1'")
        self.store._db.commit()
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(before, self.store.list_events())
        self.assertEqual(current.id, self.store.find_duplicate(parse_schedule_text(text, reference_date="2026-10-14")[0]).id)
        # Removing the collision later must not reinterpret an intentional edit.
        self.store._db.execute("DELETE FROM events WHERE id=?", (current.id,))
        self.store._db.commit()
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(old.title, self.store.get_event(old.id).title)

    def test_duplicate_alias_handles_old_titles_and_different_source_date_formats(self):
        original = "2026년 10월 30일까지 수행평가 제출하세요"
        old = parse_schedule_text(original, _preserve_date_words=False)[0]
        self.store.upsert_event(old)
        self.store.complete_event(old.id)
        candidate = parse_schedule_text(original)[0]
        changes = self.store._db.total_changes
        self.assertEqual(old.id, self.store.find_duplicate(candidate).id)
        self.assertEqual(changes, self.store._db.total_changes)
        self.assertEqual((0, 1), self.store.import_candidates([candidate]))
        # After restoration, a differently written date remains the same notice.
        self.store._db.execute("DELETE FROM settings WHERE key='source_date_titles_v1'")
        self.store._db.commit()
        self.store.close()
        self.store = Store(self.path)
        alternative = parse_schedule_text("2026-10-30까지 수행평가 제출하세요")[0]
        self.assertEqual((0, 1), self.store.import_candidates([alternative]))
        self.assertEqual("done", self.store.find_duplicate(alternative).status)
        candidate.title = "사용자가 직접 바꾼 새 일정 제목"
        self.assertIsNone(self.store.find_duplicate(candidate))

    def test_schema_two_backup_restore_repairs_only_generated_date_titles(self):
        text = "10월 22일까지 인사기록 제출"
        old = parse_schedule_text(text, reference_date="2026-10-14", _preserve_date_words=False)[0]
        old.status = "open"
        self.store.upsert_event(old)
        self.store.complete_event(old.id)
        before = self.store.get_event(old.id)
        self.store._db.execute("DELETE FROM settings WHERE key='source_date_titles_v1'")
        self.store._db.commit()
        backup = self.store.backup()
        backup_bytes = backup.read_bytes()
        self.store.restore(backup)
        restored = self.store.get_event(old.id)
        self.assertEqual(dict(asdict(before), title=text), asdict(restored))
        self.assertEqual(backup_bytes, backup.read_bytes())

    def test_date_title_alias_never_merges_different_range_ends_or_month_and_day(self):
        first_range = parse_schedule_text("2026년 10월 20일~2026년 10월 22일 상담 주간")[0]
        later_range = parse_schedule_text("2026년 10월 20일~2026년 10월 30일 상담 주간")[0]
        month = parse_schedule_text("10월 국어 평가", reference_date="2026-10-14")[0]
        actual_first = parse_schedule_text("2026년 10월 1일 국어 평가")[0]
        self.assertEqual((4, 0), self.store.import_candidates([first_range, later_range, month, actual_first]))
        self.assertEqual(4, len(self.store.list_events()))
        self.assertNotEqual(first_range.id, later_range.id)
        self.assertNotEqual(month.id, actual_first.id)
        # Check the direct old-fingerprint lookup path as well as aliases of
        # already repaired titles. A date range is distinct from a single day.
        old = parse_schedule_text("2026년 11월 20일~2026년 11월 22일 상담 주간", _preserve_date_words=False)[0]
        self.store.upsert_event(old)
        later = parse_schedule_text("2026년 11월 20일~2026년 11월 30일 상담 주간")[0]
        single = parse_schedule_text("2026년 11월 20일 상담 주간")[0]
        self.assertIsNone(self.store.find_duplicate(later))
        self.assertIsNone(self.store.find_duplicate(single))
        self.assertEqual((2, 0), self.store.import_candidates([later, single]))

    def test_review_and_future_do_not_show(self):
        self.store.import_candidates([Event(title="검토", start_date="2026-10-01", status="review"), Event(title="예정", start_date="2026-10-14"), Event(title="날짜 미상", status="review")])
        self.assertEqual([], self.store.due_events(date(2026, 10, 13)))

    def test_monthly_reminder_appears_on_first_and_stays_until_completed(self):
        candidates = parse_schedule_text("9월, 10월 국어 평가", reference_date=date(2026, 8, 20))
        self.assertEqual((2, 0), self.store.import_candidates(candidates))
        self.assertEqual([], self.store.due_events("2026-08-31"))
        self.assertEqual(["2026-09-01"], [event.start_date for event in self.store.due_events("2026-09-01")])
        self.assertEqual(["2026-09-01"], [event.start_date for event in self.store.due_events("2026-09-30")])
        self.assertEqual(["2026-09-01", "2026-10-01"], [event.start_date for event in self.store.due_events("2026-10-01")])
        self.store.complete_event(candidates[0].id)
        self.assertEqual(["2026-10-01"], [event.start_date for event in self.store.due_events("2026-10-01")])

    def test_default_unlimited_shows_past_and_future_unfinished_events(self):
        past = self.store.upsert_event(Event(title="오래된 미완료", start_date="2021-01-01"))
        future = self.store.upsert_event(Event(title="먼 미래", start_date="2029-10-22"))
        month = self.store.upsert_event(Event(title="미래 월 일정", start_date="2027-01-01", date_precision="month"))
        self.assertEqual([past.id, month.id, future.id], [event.id for event in self.store.reminder_events("2026-10-14")])
        self.assertEqual([past.id], [event.id for event in self.store.due_events("2026-10-14")])
        self.store.complete_event(past.id)
        self.assertEqual([month.id, future.id], [event.id for event in self.store.reminder_events("2026-10-14")])

    def test_saved_limited_window_has_inclusive_past_and_future_boundaries(self):
        self.store.set_setting("reminder_unlimited", "False")
        self.store.set_setting("reminder_past_days", "2")
        self.store.set_setting("reminder_future_days", "4")
        events = [self.store.upsert_event(Event(title=f"마감 {day}일", start_date=f"2026-10-{day:02d}"))
                  for day in range(10, 19)]
        expected = [event.id for event in events[1:-1]]
        self.assertEqual(expected, [event.id for event in self.store.reminder_events(date(2026, 10, 13))])
        self.assertEqual(expected, [event.id for event in self.store.reminder_events("2026-10-13")])
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(expected, [event.id for event in self.store.reminder_events("2026-10-13")])

    def test_range_uses_real_end_deadline_in_limited_window(self):
        nearing = self.store.upsert_event(Event(title="곧 마감", start_date="2026-10-14", end_date="2026-10-16"))
        later = self.store.upsert_event(Event(title="기간 시작 전", start_date="2026-10-14", end_date="2026-10-20"))
        ongoing = self.store.upsert_event(Event(title="진행 중", start_date="2026-10-12", end_date="2026-10-25"))
        self.assertEqual([nearing.id], [event.id for event in self.store.reminder_events("2026-10-13", lead_days=3, past_days=0)])
        self.assertEqual([ongoing.id], [event.id for event in self.store.reminder_events("2026-10-22", lead_days=3, past_days=0)])
        self.assertEqual([ongoing.id, nearing.id, later.id], [event.id for event in self.store.reminder_events("2026-10-13", unlimited=True)])

    def test_month_only_window_matches_full_month_not_invented_first_day(self):
        month = self.store.upsert_event(Event(title="10월 평가", start_date="2026-10-01", date_precision="month"))
        first_day = self.store.upsert_event(Event(title="10월 1일 제출", start_date="2026-10-01"))
        self.assertEqual([month.id], [event.id for event in self.store.reminder_events("2026-10-13", lead_days=0, past_days=0)])
        self.assertEqual([month.id, first_day.id], [event.id for event in self.store.reminder_events("2026-09-28", lead_days=3, past_days=0)])
        self.assertEqual([], self.store.reminder_events("2026-09-27", lead_days=3, past_days=0))
        self.assertEqual([month.id], [event.id for event in self.store.reminder_events("2026-11-01", lead_days=0, past_days=1)])
        self.assertEqual([], self.store.reminder_events("2026-11-02", lead_days=0, past_days=1))
        self.assertEqual("10월", format_event_date(self.store.get_event(month.id)))

    def test_reminder_window_crosses_year_and_leap_february(self):
        for title, today, inside, outside in (
            ("연말", "2026-12-29", "2027-01-01", "2027-01-02"),
            ("윤년", "2028-02-27", "2028-03-01", "2028-03-02"),
            ("평년", "2026-02-26", "2026-03-01", "2026-03-02"),
        ):
            with self.subTest(title=title):
                nearer = self.store.upsert_event(Event(title=title + " 포함", start_date=inside))
                later = self.store.upsert_event(Event(title=title + " 제외", start_date=outside))
                selected = [event.id for event in self.store.reminder_events(today, lead_days=3, past_days=0)]
                self.assertIn(nearer.id, selected)
                self.assertNotIn(later.id, selected)

    def test_reminder_selection_is_read_only_and_filters_finished_or_review(self):
        pending = self.store.upsert_event(Event(title="예정", start_date="2026-10-16", x=-20, y=450))
        completed = self.store.upsert_event(Event(title="완료", start_date="2026-10-15"))
        self.store.complete_event(completed.id)
        self.store.upsert_event(Event(title="검토", start_date="2026-10-15", status="review"))
        self.store.upsert_event(Event(title="날짜 없음", status="review"))
        self.store.set_setting("desktop_layout", "bottom")
        before = self.store._db.iterdump()
        snapshot = "\n".join(before)
        changes = self.store._db.total_changes
        trace = []
        self.store._db.set_trace_callback(trace.append)
        try:
            selected = self.store.reminder_events("2026-10-13")
        finally:
            self.store._db.set_trace_callback(None)
        self.assertEqual([pending.id], [event.id for event in selected])
        self.assertTrue(trace and all(statement.lstrip().upper().startswith("SELECT") for statement in trace))
        self.assertEqual(changes, self.store._db.total_changes)
        selected[0].status = "done"
        selected[0].x = 100
        self.assertEqual(snapshot, "\n".join(self.store._db.iterdump()))

    def test_reminder_order_matches_existing_due_order(self):
        events = [
            self.store.upsert_event(Event(title="같은 시간 첫 등록", start_date="2026-10-14", time="10:00")),
            self.store.upsert_event(Event(title="같은 시간 다음 등록", start_date="2026-10-14", time="10:00")),
            self.store.upsert_event(Event(title="이른 시간", start_date="2026-10-14", time="09:00")),
            self.store.upsert_event(Event(title="시간 없음", start_date="2026-10-14")),
            self.store.upsert_event(Event(title="이전 날짜", start_date="2026-10-13", time="15:00")),
        ]
        self.assertEqual([events[i].id for i in (4, 3, 2, 0, 1)], [event.id for event in self.store.reminder_events("2026-10-13")])

    def test_explicit_zero_days_shows_only_today_and_rejects_invalid_values(self):
        self.store.upsert_event(Event(title="어제", start_date="2026-10-12"))
        today = self.store.upsert_event(Event(title="당일", start_date="2026-10-13"))
        tomorrow = self.store.upsert_event(Event(title="내일", start_date="2026-10-14"))
        self.assertEqual([today.id], [event.id for event in self.store.reminder_events("2026-10-13", lead_days=0, past_days=0)])
        self.assertEqual([today.id, tomorrow.id], [event.id for event in self.store.reminder_events("2026-10-13", lead_days=1, past_days=0)])
        for invalid in (-1, 1.5, "3", True, 36501):
            with self.subTest(lead_days=invalid), self.assertRaises(ValueError):
                self.store.reminder_events("2026-10-13", lead_days=invalid)
            with self.subTest(past_days=invalid), self.assertRaises(ValueError):
                self.store.reminder_events("2026-10-13", past_days=invalid)
        with self.assertRaises(ValueError):
            self.store.reminder_events("2026-10-13", unlimited="True")

    def test_reminders_default_to_local_today(self):
        from unittest.mock import patch
        self.store.set_setting("reminder_unlimited", "False")
        self.store.set_setting("reminder_past_days", "0")
        self.store.set_setting("reminder_future_days", "3")
        inside = self.store.upsert_event(Event(title="기본 날짜 포함", start_date="2026-10-16"))
        self.store.upsert_event(Event(title="기본 날짜 제외", start_date="2026-10-17"))
        with patch("alimi.core.date", wraps=date) as local_date:
            local_date.today.return_value = date(2026, 10, 13)
            self.assertEqual([inside.id], [event.id for event in self.store.reminder_events()])
            local_date.today.assert_called_once_with()

    def test_invalid_stored_days_fall_back_without_changing_settings(self):
        self.store.set_setting("reminder_unlimited", " fAlSe ")
        self.store.set_setting("reminder_past_days", "bad")
        self.store.set_setting("reminder_future_days", "1.5")
        self.assertEqual((False, 30, 14), self.store.get_reminder_window())
        events = [self.store.upsert_event(Event(title=day, start_date=day)) for day in
                  ("2026-09-12", "2026-09-13", "2026-10-27", "2026-10-28")]
        self.assertEqual([events[1].id, events[2].id], [event.id for event in self.store.reminder_events("2026-10-13")])
        self.assertEqual("bad", self.store.get_setting("reminder_past_days"))
        self.assertEqual("1.5", self.store.get_setting("reminder_future_days"))
        self.store.set_setting("reminder_unlimited", " tRuE ")
        self.assertEqual((True, 30, 14), self.store.get_reminder_window())
        self.assertEqual([event.id for event in events], [event.id for event in self.store.reminder_events("2026-10-13")])
        self.store.set_setting("reminder_unlimited", "invalid")
        self.assertEqual((True, 30, 14), self.store.get_reminder_window())

    def test_stored_days_are_clamped_and_date_bounds_do_not_overflow(self):
        self.store.set_setting("reminder_unlimited", "False")
        self.store.set_setting("reminder_past_days", "-20")
        self.store.set_setting("reminder_future_days", "9999999")
        self.assertEqual((False, 0, 36500), self.store.get_reminder_window())
        past = self.store.upsert_event(Event(title="과거", start_date="2026-10-12"))
        today = self.store.upsert_event(Event(title="오늘", start_date="2026-10-13"))
        future = self.store.upsert_event(Event(title="100년 내", start_date="2126-09-18"))
        self.assertEqual([today.id, future.id], [event.id for event in self.store.reminder_events("2026-10-13")])
        first = self.store.upsert_event(Event(title="최초 날짜", start_date="0001-01-01"))
        last = self.store.upsert_event(Event(title="마지막 날짜", start_date="9999-12-31"))
        self.assertEqual([first.id], [event.id for event in self.store.reminder_events("0001-01-01", lead_days=0, past_days=36500)])
        self.assertEqual([last.id], [event.id for event in self.store.reminder_events("9999-12-31", lead_days=36500, past_days=0)])

    def test_month_precision_survives_restart_and_completion(self):
        event = parse_schedule_text("10월 국어 평가", reference_date="2026-09-20")[0]
        self.store.import_candidates([event])
        self.store.complete_event(event.id)
        self.store.close()
        self.store = Store(self.path)
        saved = self.store.get_event(event.id)
        self.assertEqual(("month", "done", "10월"), (saved.date_precision, saved.status, format_event_date(saved)))
        self.assertEqual((0, 1), self.store.import_candidates(parse_schedule_text("10월 국어 평가", reference_date="2026-09-20")))
        self.assertEqual("done", self.store.get_event(event.id).status)
        self.store.reopen_event(event.id)
        self.assertEqual([], self.store.due_events("2026-09-30"))
        self.assertEqual([event.id], [item.id for item in self.store.due_events("2026-10-01")])

    def test_legacy_migration_distinguishes_month_only_from_real_first_day(self):
        monthly = parse_schedule_text("9월 과학 평가", reference_date="2026-01-01")[0]
        monthly.id = 7
        monthly.status = "done"
        monthly.completed_at = "2026-09-02T08:00:00+00:00"
        monthly.x, monthly.y = -15, 230
        actual_span = parse_schedule_text("2026년 9월 1일 ~ 2026년 9월 30일 상담 주간")[0]
        actual_span.id = 8
        actual_first = parse_schedule_text("2026년 9월 1일 개학")[0]
        actual_first.id = 9
        unsupported = Event(id=10, title="기간 일정", start_date="2026-09-01", end_date="2026-09-30")
        legacy = self.path.parent / "v1.sqlite3"
        write_v1_database(legacy, [monthly, actual_span, actual_first, unsupported], {"autostart": "1"})
        migrated = Store(legacy)
        try:
            self.assertEqual(["month", "day", "day", "day"], [migrated.get_event(i).date_precision for i in (7, 8, 9, 10)])
            saved = migrated.get_event(7)
            self.assertEqual(("done", monthly.completed_at, -15, 230), (saved.status, saved.completed_at, saved.x, saved.y))
            self.assertEqual("1", migrated.get_setting("autostart"))
            self.assertEqual((0, 1), migrated.import_candidates(parse_schedule_text("9월 과학 평가", reference_date="2026-01-01")))
            self.assertEqual("done", migrated.get_event(7).status)
        finally:
            migrated.close()
        migrated = Store(legacy)
        try:
            self.assertEqual("month", migrated.get_event(7).date_precision)
        finally:
            migrated.close()

    def test_legacy_pdf_row_migration_ignores_unit_and_prose_dates(self):
        text = "교과 | 영역 | 단원명 | 평가내용 | 평가방법 | 평가시기\n체육 | 표현 | 3-2. 리듬 표현 | 10월 9일 행사에서 표현하기 | 실기 평가 | 9월"
        event = parse_schedule_text(text, reference_date="2026-01-01")[0]
        legacy = self.path.parent / "v1-table.sqlite3"
        write_v1_database(legacy, [event])
        migrated = Store(legacy)
        try:
            saved = migrated.list_events()[0]
            self.assertEqual(("month", "9월"), (saved.date_precision, format_event_date(saved)))
            self.assertEqual(event.notes, saved.notes)
        finally:
            migrated.close()

    def test_legacy_backup_restore_upgrades_without_modifying_backup(self):
        event = parse_schedule_text("10월 학급 평가", reference_date="2026-01-01")[0]
        event.id = 12
        event.status = "done"
        event.completed_at = "2026-10-02T08:00:00+00:00"
        event.x, event.y = 100, 200
        legacy = self.path.parent / "v1-backup.sqlite3"
        write_v1_database(legacy, [event], {"display_mode": "list"})
        before = legacy.read_bytes()
        self.store.upsert_event(Event(title="복원 전 일정", start_date="2026-10-14"))
        prior = self.store.restore(legacy)
        restored = self.store.get_event(12)
        self.assertEqual(("month", "done", event.completed_at, 100, 200),
                         (restored.date_precision, restored.status, restored.completed_at, restored.x, restored.y))
        self.assertEqual("list", self.store.get_setting("display_mode"))
        self.assertEqual(before, legacy.read_bytes())
        previous = Store(prior)
        try:
            self.assertEqual("복원 전 일정", previous.list_events()[0].title)
        finally:
            previous.close()

    def test_new_backup_preserves_month_precision(self):
        event = parse_schedule_text("11월 과학 평가", reference_date="2026-01-01")[0]
        self.store.upsert_event(event)
        backup = self.store.backup()
        self.store.upsert_event(Event(title="추가 일정", start_date="2026-10-13"))
        self.store.restore(backup)
        self.assertEqual([("month", "11월")], [(item.date_precision, format_event_date(item)) for item in self.store.list_events()])

    def test_invalid_backup_precision_never_changes_current_database(self):
        self.store.upsert_event(Event(title="유지할 일정", start_date="2026-10-13"))
        backup = self.store.backup()
        connection = sqlite3.connect(str(backup))
        try:
            connection.execute("PRAGMA ignore_check_constraints=ON")
            connection.execute("UPDATE events SET date_precision='unknown'")
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(ValueError):
            self.store.restore(backup)
        self.assertEqual([("유지할 일정", "day")], [(item.title, item.date_precision) for item in self.store.list_events()])

    def test_backup_copies_committed_wal_records_and_settings(self):
        self.store.upsert_event(Event(title="백업", start_date="2026-10-13"))
        self.store.set_setting("autostart", "1")
        backup = Store(self.store.backup())
        try:
            self.assertEqual("백업", backup.list_events()[0].title)
            self.assertEqual("1", backup.get_setting("autostart"))
        finally:
            backup.close()

    def test_restore_and_automatic_pre_restore_backup(self):
        self.store.upsert_event(Event(title="백업의 일정", start_date="2026-10-13"))
        self.store.set_setting("autostart", "1")
        saved_backup = self.store.backup()
        self.store.upsert_event(Event(title="복원 전 일정", start_date="2026-10-14"))
        self.store.set_setting("autostart", "0")
        previous_backup = self.store.restore(saved_backup)
        self.assertEqual(["백업의 일정"], [item.title for item in self.store.list_events()])
        self.assertEqual("1", self.store.get_setting("autostart"))
        previous = Store(previous_backup)
        try:
            self.assertEqual(2, len(previous.list_events()))
            self.assertEqual("0", previous.get_setting("autostart"))
        finally:
            previous.close()
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(["백업의 일정"], [item.title for item in self.store.list_events()])

    def test_corrupt_backup_never_changes_current_database(self):
        self.store.upsert_event(Event(title="유지할 일정", start_date="2026-10-13"))
        self.store.set_setting("autostart", "1")
        invalid = self.path.parent / "corrupt.sqlite3"
        invalid.write_bytes(b"not a SQLite database")
        with self.assertRaises(ValueError):
            self.store.restore(invalid)
        self.assertEqual("유지할 일정", self.store.list_events()[0].title)
        self.assertEqual("1", self.store.get_setting("autostart"))

    def test_wrong_schema_or_version_never_changes_current_database(self):
        self.store.upsert_event(Event(title="유지할 일정", start_date="2026-10-13"))
        wrong_schema = self.path.parent / "wrong.sqlite3"
        connection = sqlite3.connect(str(wrong_schema))
        try:
            connection.execute("CREATE TABLE events(id INTEGER)")
            connection.execute("PRAGMA user_version=1")
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(ValueError):
            self.store.restore(wrong_schema)
        wrong_version = self.store.backup()
        connection = sqlite3.connect(str(wrong_version))
        try:
            connection.execute("PRAGMA user_version=999")
        finally:
            connection.close()
        with self.assertRaises(ValueError):
            self.store.restore(wrong_version)
        self.assertEqual("유지할 일정", self.store.list_events()[0].title)


class CalendarSectionParserTests(unittest.TestCase):
    def test_semester_calendar_keeps_all_31_entries_in_their_2021_months(self):
        text = """주요 학사일정
2021. 3월
일 월 화 수 목 금 토
1 2 3 4 5 6
7 8 9 10 11 12 13
1(월) 삼일절
2(화) 시업식·입학식
2(화)~5(금) 평화학급 세우기
8(월)~19(금) 진단활동 주간
9(화) 학급 임원선거(3, 5학년)
11(목) 학급 임원선거(4, 6학년)
15(월) 진로적성검사(3, 5학년)
18(목) 진로적성검사(4, 6학년)
17(수) 학부모총회
수업일수: 22일
4월
12(월)~16일(금) 과학축제주간
19(월)~23(금) 정보통신윤리교육주간
19(월)~23(금) 인성실천 주간
20(화) 장애인권행사
23(금) 책의 날 행사
26(월)~30(목) 학부모상담주간
26(월)~30(목) 5~6학년 PAPS 측정 주간
수업일수: 22일
5월
3(월)~4(화) 재량휴업일
5(수) 어린이 날
6(목) 개교기념일
19(수) 부처님 오신 날
26(수) 민방위훈련(예정)
수업일수: 17일
6월
6(일) 현충일
7(월)~11일(금) 환경교육주간
23(수) 합동소방훈련(예정)
7월
5(월)~9(금) 5학년 진로체험주간
7(수) 교육과정 협의회
20(화) 여름방학식
7.21~8.18. 여름방학(29일)
8월
15(일) 광복절
19(목) 2학기 개학식
24(화) 2학기 학급임원선거
수업일수: 9일"""
        events = parse_schedule_text(text, reference_date="2026-10-15")
        self.assertEqual(31, len(events))
        self.assertEqual([9, 7, 5, 3, 4, 3],
                         [sum(event.start_date.startswith(f"2021-{month:02}-") for event in events)
                          for month in range(3, 9)])
        self.assertTrue(all(event.date_precision == "day" for event in events))
        self.assertEqual(["2021-04-26", "2021-04-26"],
                         [event.start_date for event in events if event.status == "review"])
        holiday = next(event for event in events if "여름방학(29일)" in event.title)
        self.assertEqual(("2021-07-21", "2021-08-18", "open"),
                         (holiday.start_date, holiday.end_date, holiday.status))

    def test_month_headers_are_context_while_month_reminders_still_work(self):
        events = parse_schedule_text("2021년\n3월\n1(월) 삼일절\n4월\n20(화) 장애인권행사",
                                     reference_date="2026-10-15")
        self.assertEqual([("2021-03-01", "open"), ("2021-04-20", "open")],
                         [(event.start_date, event.status) for event in events])
        self.assertTrue(all(event.date_precision == "day" for event in events))
        plain = parse_schedule_text("9월 국어 평가", reference_date="2026-10-15")
        self.assertEqual(("2026-09-01", "month", "open"),
                         (plain[0].start_date, plain[0].date_precision, plain[0].status))

    def test_grids_weekdays_and_school_day_totals_do_not_become_events(self):
        for prefix in ("•", "#"):
            with self.subTest(prefix=prefix):
                text = f"2021. 3월\n일 월 화 수 목 금 토\n1 2 3 4 5 6\n7 | 8 | 9 | 10 | 11 | 12 | 13\n{prefix}1(월) 삼일절\n수업 일수: 22일\n•\n:"
                events = parse_schedule_text(text, reference_date="2026-10-15")
                self.assertEqual([("2021-03-01", f"{prefix}1(월) 삼일절")],
                                 [(event.start_date, event.title) for event in events])

    def test_source_year_and_academic_year_are_available_to_calendar_sections(self):
        for text, source in (("3월\n1(월) 삼일절", "2021년 학사일정.png"),
                             ("2021학년도 주요 학사일정\n3월\n1(월) 삼일절", "")):
            with self.subTest(text=text, source=source):
                event = parse_schedule_text(text, source=source, reference_date="2026-10-15")[0]
                self.assertEqual(("2021-03-01", "open"), (event.start_date, event.status))

    def test_yearless_calendar_is_kept_for_review(self):
        event = parse_schedule_text("3월\n1(일) 삼일절", reference_date="2026-10-15")[0]
        self.assertEqual(("2026-03-01", "review"), (event.start_date, event.status))
        self.assertIn("연도가 없어", event.notes)
        self.assertEqual("3월\n1(일) 삼일절", event.source_text)

    def test_same_period_distinct_titles_are_not_merged(self):
        events = parse_schedule_text("2021년 4월\n19(월)~23(금) 정보통신윤리교육주간\n19(월)~23(금) 인성실천 주간")
        self.assertEqual(2, len(events))
        self.assertTrue(all((event.start_date, event.end_date) == ("2021-04-19", "2021-04-23")
                            for event in events))
        self.assertNotEqual(events[0].title, events[1].title)

    def test_grade_numbers_and_dates_in_event_body_are_not_extra_schedules(self):
        lines = ["18(목) 진로적성검사(4.6학년)", "26(월)~30(금) 5-6학년 PAPS 측정 주간",
                 "19(월)~23(금) 4.20 안내문으로 교육하기"]
        events = parse_schedule_text("2021년 3월\n" + lines[0] + "\n4월\n" + "\n".join(lines[1:]))
        self.assertEqual(lines, [event.title for event in events])
        self.assertEqual(["2021-03-18", "2021-04-26", "2021-04-19"],
                         [event.start_date for event in events])

    def test_both_range_weekdays_are_checked_without_changing_source_numbers(self):
        event = parse_schedule_text("2021년 4월\n26(월)~30(목) 학부모상담주간")[0]
        self.assertEqual(("2021-04-26", "2021-04-30", "review"),
                         (event.start_date, event.end_date, event.status))
        self.assertIn("종료 날짜와 요일", event.notes)
        self.assertIn("30(목)", event.title)

    def test_malformed_ocr_weekday_retains_dates_and_requires_review(self):
        for line, start, end in (("19()~23(금) 인성실천 주간", "2021-04-19", "2021-04-23"),
                                 ("26(월)~30(옥 학부모상담주간", "2021-04-26", "2021-04-30"),
                                 ("26()~30(옥 5-6학년 PAPS 측정 주간", "2021-04-26", "2021-04-30")):
            with self.subTest(line=line):
                events = parse_schedule_text("2021년 4월\n" + line)
                self.assertEqual(1, len(events))
                self.assertEqual((start, end, "review"),
                                 (events[0].start_date, events[0].end_date, events[0].status))
                self.assertEqual(line, events[0].title)
                self.assertIn("명확히 읽지 못했습니다", events[0].notes)

    def test_ambiguous_day_digit_is_preserved_as_undated_review_without_guessing(self):
        event = parse_schedule_text("2021년 8월\n1S(일) 광복절")[0]
        self.assertEqual(("", "review", "1S(일) 광복절"), (event.start_date, event.status, event.title))
        self.assertIn("날짜 숫자를 명확히", event.notes)

    def test_calendar_invalid_dates_and_reversed_ranges_need_review(self):
        for line in ("30(화) 잘못된 날짜", "28(일)~31(수) 잘못된 종료일", "28(일)~2(화) 월이 불명확한 기간"):
            with self.subTest(line=line):
                events = parse_schedule_text("2021년 2월\n" + line)
                self.assertEqual(1, len(events))
                self.assertEqual("review", events[0].status)
                self.assertIn(line, events[0].source_text)

    def test_explicit_date_overrides_month_context_and_preserves_full_time_range(self):
        event = parse_schedule_text("2021년 3월\n2022년 10월 22일(토) 교직원 회의 15:00-19:00")[0]
        self.assertEqual(("2022-10-22", "15:00", "open"), (event.start_date, event.time, event.status))
        self.assertIn("15:00-19:00", event.title)

    def test_explicit_month_range_can_cross_month_and_year(self):
        for expression in ("12.30~1.3", "12월 30일~2022년 1월 3일"):
            with self.subTest(expression=expression):
                event = parse_schedule_text("2021년 12월\n" + expression + " 겨울방학")[0]
                self.assertEqual(("2021-12-30", "2022-01-03", "open"),
                                 (event.start_date, event.end_date, event.status))

    def test_bare_day_and_day_suffix_are_supported_but_pupil_grade_is_not_a_date(self):
        events = parse_schedule_text("2021년 3월\n1 삼일절\n2일 시업식·입학식\n5학년 진로체험 안내")
        self.assertEqual(["2021-03-01", "2021-03-02"], [event.start_date for event in events])

    def test_calendar_repeated_entries_deduplicate_without_merging_titles(self):
        events = parse_schedule_text("2021년 3월\n1(월) 삼일절\n1(월) 삼일절\n1(월) 학급 안내")
        self.assertEqual(2, len(events))
        self.assertNotEqual(events[0].title, events[1].title)

    def test_grade_audience_range_does_not_reclassify_a_month_reminder(self):
        for separator in ("~", "∼", "–", "—", "부터"):
            with self.subTest(separator=separator):
                events = parse_schedule_text(f"5월\n3{separator}5학년 체험활동", reference_date="2026-10-15")
                self.assertEqual(1, len(events))
                self.assertEqual(("2026-05-01", "month", "open"),
                                 (events[0].start_date, events[0].date_precision, events[0].status))
                self.assertIn("체험활동", events[0].notes)

    def test_grade_audience_ranges_inside_calendar_do_not_add_dates(self):
        text = "2021. 5월\n3(월)~5(수) 진로 주간\n3~5학년 체험활동\n•3-5 학년 참여 안내\n6(목) 개교기념일"
        events = parse_schedule_text(text)
        self.assertEqual([("2021-05-03", "2021-05-05"), ("2021-05-06", None)],
                         [(event.start_date, event.end_date) for event in events])
        self.assertTrue(all(event.status == "open" for event in events))

    def test_calendar_month_heading_accepts_trailing_period_from_ocr(self):
        events = parse_schedule_text("2021. 3월.\n1(월) 삼일절\n4월.\n20(화) 장애인권행사",
                                     reference_date="2026-10-15")
        self.assertEqual([("2021-03-01", "open"), ("2021-04-20", "open")],
                         [(event.start_date, event.status) for event in events])


class AnnualAcademicCalendarParserTests(unittest.TestCase):
    def test_university_calendar_preserves_all_55_entries_and_24_ranges(self):
        text = """동명대학교 2022학년도 학사일정
2021.10.08.
2022년 3월 March
3. 1. 삼일절
3. 2. 개강일
3. 9. 대통령선거
3. 29. 수업일수 4주
3. 31. 수업일수 30일
4월 April
4. 7. 수업일수 1/3
4. 11. 개교기념일
4. 20~26. 중간시험
4. 25. 수업일수 1/2
5월 May
5. 2. 수업일수 60일
5. 5. 어린이날
5. 17. 수업일수 11주
5. 30. 수업일수 90일
6월 June
6. 1. 전국동시 지방선거
6. 6. 현충일
6. 8.~14. 보강주간
6. 15.~21. 기말시험
6. 22. 하계방학시작일
6. 22.~7. 12. 하계 계절학기
6. 22.~8. 31. 하계 현장실습기간
6. 28.~7. 1. 성적공시 및 정정
7월 July
7. 1.~7. 더PLUS+다전공, 전과,
재입학 신청
7. 1.~8. 31. 복학 신청
7. 15.~17. 계절학기 성적공시
7. 26.~29. 수강 신청
8월 August
8. 15. 광복절
8. 17. 8월 학위수여식
8. 22.~24. 등록기간
8. 31. 복학, 미등록휴학 마감
9월 September
9. 1. 개강일
9. 9.~12. 추석 연휴
9. 28. 수업일수 4주
9. 30. 수업일수 30일
10월 October
10. 3. 개천절
10. 11. 수업일수 1/3
10. 10. 한글날 대체공휴일
10. 20.~26. 중간시험
10. 26. 수업일수 1/2
10. 31. 수업일수 60일
11월 November
11. 16. 수업일수 11주
11. 29. 수업일수 90일
12월 December
12. 8.~14. 보강주간
12. 15.~21. 기말시험
12. 22. 동계방학시작일
12. 22.~1. 11. 동계 계절학기
12. 22.~2. 28. 동계 현장실습기간
12. 27.~30. 성적공시 및 정정
2023년 1월 January
1. 2.~6. 더PLUS+다전공, 전과,
재입학 신청
1. 2.~2. 28. 복학 신청
1. 13.~15. 계절학기 성적공시
1. 21.~24. 설날 연휴
2023년 2월 February
2. 7.~10. 수강 신청
2. 15. 2월 학위수여식
2. 20.~22. 등록기간
2. 28. 복학, 미등록휴학 마감"""
        events = parse_schedule_text(text, reference_date="2026-10-15")
        self.assertEqual(55, len(events))
        expected_counts = {"2022-03": 5, "2022-04": 4, "2022-05": 4, "2022-06": 8,
                           "2022-07": 4, "2022-08": 4, "2022-09": 4, "2022-10": 6,
                           "2022-11": 2, "2022-12": 6, "2023-01": 4, "2023-02": 4}
        self.assertEqual(expected_counts, {month: sum(event.start_date.startswith(month) for event in events)
                                           for month in expected_counts})
        expected_ranges = [
            ("2022-04-20", "2022-04-26"), ("2022-06-08", "2022-06-14"),
            ("2022-06-15", "2022-06-21"), ("2022-06-22", "2022-07-12"),
            ("2022-06-22", "2022-08-31"), ("2022-06-28", "2022-07-01"),
            ("2022-07-01", "2022-07-07"), ("2022-07-01", "2022-08-31"),
            ("2022-07-15", "2022-07-17"), ("2022-07-26", "2022-07-29"),
            ("2022-08-22", "2022-08-24"), ("2022-09-09", "2022-09-12"),
            ("2022-10-20", "2022-10-26"), ("2022-12-08", "2022-12-14"),
            ("2022-12-15", "2022-12-21"), ("2022-12-22", "2023-01-11"),
            ("2022-12-22", "2023-02-28"), ("2022-12-27", "2022-12-30"),
            ("2023-01-02", "2023-01-06"), ("2023-01-02", "2023-02-28"),
            ("2023-01-13", "2023-01-15"), ("2023-01-21", "2023-01-24"),
            ("2023-02-07", "2023-02-10"), ("2023-02-20", "2023-02-22")]
        self.assertEqual(expected_ranges, [(event.start_date, event.end_date) for event in events if event.end_date])
        self.assertTrue(all(event.status == "open" and event.date_precision == "day" for event in events))
        self.assertEqual(2, sum("재입학 신청" in event.title for event in events))
        self.assertEqual(2, sum("1/3" in event.title for event in events))
        self.assertEqual(2, sum("1/2" in event.title for event in events))

    def test_academic_month_year_is_independent_of_side_by_side_reading_order(self):
        text = """2022학년도 학사일정
2022년3월March
3. 1. 삼일절
9월 September
9. 1. 개강일
7월 July
7. 26.~29. 수강 신청
2023년 1월 January
1. 21.~24. 설날 연휴
8월 August
8. 15. 광복절
2023년2월 February
2. 15. 2월 학위수여식"""
        self.assertEqual(["2022-03-01", "2022-09-01", "2022-07-26", "2023-01-21",
                          "2022-08-15", "2023-02-15"],
                         [event.start_date for event in parse_schedule_text(text)])

    def test_academic_january_and_february_roll_over_without_explicit_year_headers(self):
        events = parse_schedule_text("2022학년도\n12월 December\n12.22.~2.28. 동계실습\n1월 January\n1.2. 개강\n2월 February\n2.15. 졸업식")
        self.assertEqual([("2022-12-22", "2023-02-28"), ("2023-01-02", None), ("2023-02-15", None)],
                         [(event.start_date, event.end_date) for event in events])

    def test_explicit_section_and_event_years_take_priority_over_academic_year(self):
        events = parse_schedule_text("2022학년도\n2024년1월 January\n1.2. 설명회\n2025년 1월 3일 특별행사\n8월 August\n8.15. 광복절")
        self.assertEqual(["2024-01-02", "2025-01-03", "2022-08-15"], [event.start_date for event in events])

    def test_publication_stamp_is_excluded_only_before_a_proved_calendar(self):
        events = parse_schedule_text("2022학년도 학사일정\n2021.10.08.\n2022년3월March\n3.1. 삼일절")
        self.assertEqual(["2022-03-01"], [event.start_date for event in events])
        actual = parse_schedule_text("2021.10.08. 설명회\n2022년3월March\n3.1. 삼일절")
        self.assertEqual(["2021-10-08", "2022-03-01"], [event.start_date for event in actual])

    def test_real_heading_and_date_before_calendar_are_not_discarded_as_a_publication_stamp(self):
        events = parse_schedule_text("2022학년도\n졸업식\n2022.02.18\n2022년3월 March\n3.1. 삼일절")
        self.assertEqual([("2022-02-18", "졸업식"), ("2022-03-01", "3.1. 삼일절")],
                         [(event.start_date, event.title) for event in events])

    def test_fractional_milestones_do_not_supply_extra_dates_or_lose_the_fraction(self):
        events = parse_schedule_text("2022년4월April\n4.7. 수업일수 1/3\n4.25. 수업일수 1/2\n수업일수: 22일")
        self.assertEqual([("2022-04-07", "4.7. 수업일수 1/3"), ("2022-04-25", "4.25. 수업일수 1/2")],
                         [(event.start_date, event.title) for event in events])

    def test_wrapped_title_becomes_one_event_and_source_keeps_original_line_break(self):
        events = parse_schedule_text("2022년7월July\n7.1.~7. 더PLUS+다전공, 전과,\n재입학 신청\n7.1.~8.31. 복학 신청")
        self.assertEqual(2, len(events))
        self.assertEqual("7.1.~7. 더PLUS+다전공, 전과, 재입학 신청", events[0].title)
        self.assertEqual("2022년 7월\n7.1.~7. 더PLUS+다전공, 전과,\n재입학 신청", events[0].source_text)
        self.assertEqual(("2022-07-01", "2022-07-07"), (events[0].start_date, events[0].end_date))

    def test_calendar_minus_and_ocr_punctuation_support_short_range_ends(self):
        for expression in ("12.15,~21.", "12.15.−21.", "12.15.–21."):
            with self.subTest(expression=expression):
                event = parse_schedule_text("2022년12월December\n" + expression + " 기말시험")[0]
                self.assertEqual(("2022-12-15", "2022-12-21", "open"),
                                 (event.start_date, event.end_date, event.status))

    def test_ocr_comma_can_separate_a_range_end_month_without_creating_a_grade_date(self):
        events = parse_schedule_text("2022학년도\n12월 December\n12.22,~1,11. 동계 계절학기\n3, 5학년 참여 안내")
        self.assertEqual(1, len(events))
        self.assertEqual(("2022-12-22", "2023-01-11", "open"),
                         (events[0].start_date, events[0].end_date, events[0].status))
        self.assertIn("1,11.", events[0].source_text)

    def test_long_wrapped_calendar_title_does_not_fail_when_joined(self):
        wrapped = "재입학 신청 방법과 제출 자료를 확인하세요. " * 4
        events = parse_schedule_text("2022년7월July\n7.1.~7. 더PLUS+다전공, 전과,\n" + wrapped)
        self.assertEqual(1, len(events))
        self.assertIn(wrapped.strip(), events[0].title)
        self.assertEqual("2022-07-07", events[0].end_date)

    def test_unreadable_calendar_end_is_kept_for_review_without_guessing(self):
        event = parse_schedule_text("2022년12월December\n12.22.~1S. 동계실습")[0]
        self.assertEqual(("2022-12-22", None, "review"), (event.start_date, event.end_date, event.status))
        self.assertIn("종료 날짜를 명확히", event.notes)
        self.assertIn("1S.", event.title)

    def test_english_month_headers_still_need_real_dated_entries(self):
        event = parse_schedule_text("2022년9월September\n관련 일정 안내")[0]
        self.assertEqual(("2022-09-01", "month"), (event.start_date, event.date_precision))


class ParserTests(unittest.TestCase):
    def test_month_precision_formats_only_the_supplied_month(self):
        event = parse_schedule_text("9월 과학 평가", reference_date="2026-01-01")[0]
        self.assertEqual("month", event.date_precision)
        self.assertEqual("9월", format_event_date(event))
        self.assertEqual("2026년 9월", format_event_date(event, include_year=True))
        actual_first = parse_schedule_text("2026년 9월 1일 개학")[0]
        self.assertEqual("day", actual_first.date_precision)
        self.assertEqual("2026-09-01", format_event_date(actual_first))
        self.assertEqual("날짜 미정", format_event_date(Event(status="review")))

    def test_month_only_uses_reference_year_and_opens_without_review(self):
        event = parse_schedule_text("9월 국어 평가", reference_date=date(2026, 10, 13))[0]
        self.assertEqual(("9월 국어 평가", "2026-09-01", "open"), (event.title, event.start_date, event.status))
        self.assertNotIn("확인 필요", event.notes)

    def test_multiple_months_and_ranges_create_one_event_per_month(self):
        for expression in ("9월, 10월", "9월 10월", "9~10월", "9월~10월", "9월부터 10월"):
            with self.subTest(expression=expression):
                events = parse_schedule_text(f"{expression} 국어 평가", reference_date="2026-08-01")
                self.assertEqual(["2026-09-01", "2026-10-01"], [event.start_date for event in events])
                self.assertEqual(["open", "open"], [event.status for event in events])
                self.assertEqual([f"{expression} 국어 평가"] * 2, [event.title for event in events])

    def test_month_ranges_crossing_year(self):
        events = parse_schedule_text("2026년 12월~2027년 2월 방학 활동", reference_date="2025-01-01")
        self.assertEqual(["2026-12-01", "2027-01-01", "2027-02-01"], [event.start_date for event in events])

    def test_monthly_event_covers_whole_month_including_leap_february(self):
        events = parse_schedule_text("2028년 2~3월 국어 평가", reference_date="2026-01-01")
        self.assertEqual([("2028-02-01", "2028-02-29"), ("2028-03-01", "2028-03-31")],
                         [(event.start_date, event.end_date) for event in events])
        table = "2026학년도 평가 예고안\n교과 | 단원 | 평가내용 | 시기\n국어 | 1. 비교하며 읽어요 | 작품 감상하기 | 9월"
        event = parse_schedule_text(table, reference_date="2027-01-01")[0]
        self.assertEqual(("2026-09-01", "2026-09-30", "open"), (event.start_date, event.end_date, event.status))

    def test_explicit_academic_year_beats_import_year(self):
        text = "2026학년도 4학년 2학기 교수·학습 및 평가 예고안\n9월 국어 평가\n10월 수학 평가"
        events = parse_schedule_text(text, reference_date="2027-02-01")
        self.assertEqual(["2026-09-01", "2026-10-01"], [event.start_date for event in events])

    def test_explicit_source_year_and_month_only_title_fallback(self):
        event = parse_schedule_text("9월", source="2026년 평가 예고안.pdf", reference_date="2027-01-01")[0]
        self.assertEqual(("2026년 평가 예고안", "2026-09-01", "open"), (event.title, event.start_date, event.status))
        self.assertNotIn("확인 필요", event.title + event.notes)

    def test_generic_month_only_title_is_active(self):
        event = parse_schedule_text("시기: 9월", reference_date="2026-10-01")[0]
        self.assertEqual(("9월 일정", "2026-09-01", "open"), (event.title, event.start_date, event.status))

    def test_month_repetitions_deduplicate(self):
        events = parse_schedule_text("9월, 9월 국어 평가\n9월 국어 평가", reference_date="2026-10-13")
        self.assertEqual(1, len(events))

    def test_inline_year_with_space_separated_months_is_not_day_ten(self):
        events = parse_schedule_text("2026년 9월 10월 국어 평가", reference_date="2027-01-01")
        self.assertEqual(["2026-09-01", "2026-10-01"], [event.start_date for event in events])

    def test_assessment_unit_numbers_and_prose_dates_are_not_schedule_dates(self):
        text = """2026학년도 평가 예고안
교과 | 영역 | 단원명 | 평가내용 | 평가방법 | 평가시기
체육 | 표현 | 3-2. 리듬 표현 | 10월 9일의 행사에서 리듬에 맞추어 표현하기 | 실기 평가 | 9월
체육 | 운동 | 2-2. 움직임 기술 | 안전한 움직임 기술 적용하기 | 실기 평가 | 10월"""
        events = parse_schedule_text(text, "2027년 참고자료.pdf", reference_date="2027-01-01")
        self.assertEqual(["2026-09-01", "2026-10-01"], [event.start_date for event in events])
        self.assertEqual(["open", "open"], [event.status for event in events])
        self.assertIn("3-2.", events[0].title)
        self.assertIn("2-2.", events[1].title)
        self.assertIn("10월 9일", events[0].notes)
        self.assertEqual("2027년 참고자료.pdf", events[0].source)

    def test_month_tokens_inside_full_dates_do_not_create_extra_events(self):
        events = parse_schedule_text("2026년 10월 15일 교직원 회의\n10월 16일 학급 회의", reference_date="2026-10-13")
        self.assertEqual(["2026-10-15", "2026-10-16"], [event.start_date for event in events])
        self.assertEqual(["open", "review"], [event.status for event in events])

    def test_month_only_metadata_is_not_schedule(self):
        self.assertEqual([], parse_schedule_text("작성월: 9월\n발행월 10월\n게시일: 2026년 11월\n학교 안내 공문"))

    def test_assessment_table_keeps_content_with_distinct_same_unit_rows(self):
        text = """2026학년도 4학년 2학기 교수·학습 및 평가 예고안
[표]
교과 | 영역 | 단원명 | 평가내용 | 평가 방법 | 평가 시기
국어 | 듣기·말하기 | 3. 의견을 모아서 | 일상생활의 문제 해결을 위한 토의 하기 | 토의·토론 평가 | 10월
국어 | 쓰기 | 3. 의견을 모아서 | 의견과 이유가 드러나게 제안하는 글 쓰기 | 서술 평가 | 10월

[표]
과학 | 생명 | 2. 생물과 환경 | 환경의 특징에 따라 생물이 사는 모습 비교하기 | 서술형 평가 | 9월"""
        events = parse_schedule_text(text, "평가 예고안.pdf", reference_date="2027-01-01")
        self.assertEqual(3, len(events))
        self.assertEqual(["2026-10-01", "2026-10-01", "2026-09-01"], [event.start_date for event in events])
        self.assertNotEqual(events[0].title, events[1].title)
        for event in events:
            self.assertEqual("open", event.status)
            self.assertIn("평가내용:", event.notes)
            self.assertIn("평가방법:", event.notes)
            self.assertNotIn("확인 필요", event.title + event.notes)
        self.assertIn("3. 의견을 모아서", events[0].title)
        self.assertIn("일상생활의 문제 해결을 위한 토의 하기", events[0].notes)

    def test_simple_assessment_table_preserves_long_description(self):
        description = "작품에서 이야기의 세계와 현실 세계를 비교하며 등장인물의 생각과 사건을 분석하고 작품을 감상하기"
        events = parse_schedule_text(f"교과 | 단원 | 평가내용 | 시기\n국어 | 1. 비교하며 읽어요 | {description} | 9월", reference_date="2026-01-01")
        self.assertEqual(1, len(events))
        self.assertIn("국어", events[0].title)
        self.assertIn("1. 비교하며 읽어요", events[0].title)
        self.assertIn(description, events[0].notes)
        self.assertLessEqual(len(events[0].title), 100)

    def test_explicit_date_time_place_from_document(self):
        events = parse_schedule_text("교직원 협의회\n일시: 2026년 10월 13일(화) 오후 3시 30분\n장소: 2층 회의실", "공문.txt")
        self.assertEqual(1, len(events))
        event = events[0]
        self.assertEqual(("교직원 협의회", "2026-10-13", "15:30", "2층 회의실", "open"), (event.title, event.start_date, event.time, event.place, event.status))
        self.assertEqual("공문.txt", event.source)

    def test_year_missing_requires_review(self):
        event = parse_schedule_text("10월 13일 교직원 협의회", reference_date=date(2026, 10, 1))[0]
        self.assertEqual("2026-10-13", event.start_date)
        self.assertEqual("review", event.status)
        self.assertIn("연도", event.notes)

    def test_weekday_mismatch_requires_review(self):
        event = parse_schedule_text("2026.10.13(수) 15:30 교직원 협의회")[0]
        self.assertEqual("review", event.status)
        self.assertIn("요일", event.notes)

    def test_table_extracts_multiple_events(self):
        events = parse_schedule_text("일정명 | 일시 | 장소\n교직원 협의회 | 2026-10-13 15:30 | 2층 회의실\n학부모 상담 | 2026/10/15 오전 10시 | 상담실")
        self.assertEqual(2, len(events))
        self.assertEqual(("교직원 협의회", "2층 회의실", "15:30"), (events[0].title, events[0].place, events[0].time))
        self.assertEqual(("학부모 상담", "상담실", "10:00"), (events[1].title, events[1].place, events[1].time))

    def test_document_creation_date_is_not_schedule(self):
        self.assertEqual([], parse_schedule_text("작성일: 2026.10.13\n담당: 홍길동\n학교 안내 공문"))

    def test_range_is_one_candidate(self):
        events = parse_schedule_text("2026.10.13 ~ 10.15 학부모 상담 주간")
        self.assertEqual(1, len(events))
        self.assertEqual(("2026-10-13", "2026-10-15"), (events[0].start_date, events[0].end_date))

    def test_booth_setup_date_and_clock_range_are_one_complete_schedule(self):
        text = "-부스 사전설치:2026.10.22.(목),15:00-19:00"
        events = parse_schedule_text(text, reference_date="2026-10-15")
        self.assertEqual(1, len(events))
        event = events[0]
        self.assertEqual((text, "2026-10-22", None, "15:00", "open"),
                         (event.title, event.start_date, event.end_date, event.time, event.status))
        self.assertEqual(text, event.source_text)

    def test_clock_range_separators_and_ocr_spaces_do_not_create_dates(self):
        for clock in ("09:10-11:30", "09:10 - 11:30", "09 : 10 - 11 : 30",
                      "０９：１０－１１：３０", "09:10~11:30", "09:10–11:30"):
            with self.subTest(clock=clock):
                text = f"2026.10.22.(목) 교직원 회의 {clock}"
                events = parse_schedule_text(text, reference_date="2026-10-15")
                self.assertEqual(1, len(events))
                self.assertEqual(("2026-10-22", "09:10", "open"),
                                 (events[0].start_date, events[0].time, events[0].status))

    def test_clock_range_without_date_does_not_invent_a_schedule(self):
        for text in ("부스 사전설치 15:00-19:00", "시간: 09:10-11:30", "운영 시간: 09 : 10 - 11 : 30"):
            with self.subTest(text=text):
                self.assertEqual([], parse_schedule_text(text, reference_date="2026-10-15"))

    def test_clock_range_on_following_time_line_keeps_heading_and_place(self):
        text = "부스 사전설치\n일자:2026.10.22.(목)\n시간:15:00-19:00\n장소:운동장"
        events = parse_schedule_text(text, reference_date="2026-10-15")
        self.assertEqual(1, len(events))
        self.assertEqual(("부스 사전설치", "2026-10-22", "15:00", "운동장", "open"),
                         (events[0].title, events[0].start_date, events[0].time, events[0].place, events[0].status))
        self.assertEqual(text.split("\n", 1)[1], events[0].source_text)

    def test_clock_ranges_in_table_keep_distinct_schedules(self):
        text = ("일정명 | 일시 | 장소\n"
                "부스 설치 | 2026.10.22.(목),15:00-19:00 | 운동장\n"
                "부스 운영 | 2026.10.23.(금),09:10-11:30 | 강당")
        events = parse_schedule_text(text, reference_date="2026-10-15")
        self.assertEqual([("부스 설치", "2026-10-22", "15:00", "운동장"),
                          ("부스 운영", "2026-10-23", "09:10", "강당")],
                         [(event.title, event.start_date, event.time, event.place) for event in events])

    def test_separate_inline_schedules_with_clock_ranges_remain_separate(self):
        lines = ["부스 설치:2026.10.22.(목),15:00-19:00", "부스 운영:2026.10.23.(금),09:10-11:30"]
        events = parse_schedule_text("\n".join(lines), reference_date="2026-10-15")
        self.assertEqual(lines, [event.title for event in events])
        self.assertEqual([("2026-10-22", "15:00"), ("2026-10-23", "09:10")],
                         [(event.start_date, event.time) for event in events])

    def test_clock_range_preserves_yearless_relative_and_month_uncertainty(self):
        for text, expected_date, precision in (("부스 설치 일자:10.22.(목),15:00-19:00", "2026-10-22", "day"),
                                                ("다음 주 목요일 부스 설치 15:00-19:00", "", "day"),
                                                ("10월 부스 운영 09:10-11:30", "2026-10-01", "month")):
            with self.subTest(text=text):
                events = parse_schedule_text(text, reference_date="2026-10-15")
                self.assertEqual(1, len(events))
                self.assertEqual((text, expected_date, precision),
                                 (events[0].title, events[0].start_date, events[0].date_precision))
                self.assertEqual("open" if precision == "month" else "review", events[0].status)

    def test_dotted_weekday_date_range_with_clock_range_is_one_schedule(self):
        for connector in ("-", "~", "부터"):
            with self.subTest(connector=connector):
                text = f"부스 운영:2026.10.22.(목){connector}2026.10.23.(금),09:10-11:30"
                events = parse_schedule_text(text, reference_date="2026-10-15")
                self.assertEqual(1, len(events))
                self.assertEqual((text, "2026-10-22", "2026-10-23", "09:10", "open"),
                                 (events[0].title, events[0].start_date, events[0].end_date, events[0].time, events[0].status))

    def test_dotted_date_with_wrong_weekday_still_requires_review(self):
        events = parse_schedule_text("부스 설치:2026.10.22.(금),15:00-19:00", reference_date="2026-10-15")
        self.assertEqual(1, len(events))
        self.assertEqual(("2026-10-22", "15:00", "review"),
                         (events[0].start_date, events[0].time, events[0].status))
        self.assertIn("요일", events[0].notes)

    def test_relative_date_is_not_invented(self):
        event = parse_schedule_text("다음 주 금요일 학급 비상 연락망 제출", reference_date=date(2026, 10, 13))[0]
        self.assertEqual("", event.start_date)
        self.assertEqual("review", event.status)

    def test_invalid_date_is_review_candidate(self):
        event = parse_schedule_text("2026년 2월 30일 연수")[0]
        self.assertEqual("", event.start_date)
        self.assertEqual("review", event.status)

    def test_repeated_text_is_deduplicated(self):
        text = "2026-10-13 교직원 협의회\n\n2026-10-13 교직원 협의회"
        self.assertEqual(1, len(parse_schedule_text(text)))

    def test_application_deadline_and_event_date_share_heading(self):
        events = parse_schedule_text("과학 체험 신청\n신청 마감: 2026.10.13\n행사일: 2026.10.20\n장소: 강당")
        self.assertEqual(2, len(events))
        self.assertEqual(["open", "open"], [event.status for event in events])
        self.assertIn("신청 마감", events[0].title)
        self.assertIn("행사일", events[1].title)
        self.assertEqual(["2026-10-13", "2026-10-20"], [event.start_date for event in events])

    def test_title_word_does_not_strip_similar_date_label(self):
        event = parse_schedule_text("2026-10-13 기간제 교사 채용")[0]
        self.assertEqual("2026-10-13 기간제 교사 채용", event.title)

    def test_inline_deadline_keeps_original_date_and_sentence(self):
        text = "10월 30일까지 수행평가 제출하세요"
        event = parse_schedule_text(text, reference_date="2026-10-14")[0]
        self.assertEqual(text, event.title)
        self.assertEqual(text, event.source_text)
        self.assertEqual("2026-10-30", event.start_date)

    def test_inline_full_date_weekday_time_and_place_remain_in_title(self):
        text = "2026년 10월 30일(금) 오후 3시 30분 교직원 협의회, 장소: 2층 회의실"
        event = parse_schedule_text(text)[0]
        self.assertEqual(text, event.title)
        self.assertEqual(("2026-10-30", "15:30", "2층 회의실", "open"),
                         (event.start_date, event.time, event.place, event.status))

    def test_date_range_and_relative_expression_keep_original_words(self):
        text = "2026년 10월 20일~2026년 10월 22일까지 상담 기록 제출"
        event = parse_schedule_text(text)[0]
        self.assertEqual(text, event.title)
        self.assertEqual(("2026-10-20", "2026-10-22"), (event.start_date, event.end_date))
        relative = "다음 주 금요일까지 학급 비상 연락망 제출"
        candidate = parse_schedule_text(relative)[0]
        self.assertEqual(relative, candidate.title)
        self.assertEqual(("", "review"), (candidate.start_date, candidate.status))


if __name__ == "__main__":
    unittest.main()
