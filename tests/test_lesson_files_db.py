"""Lesson-file metadata and migrations. These need no web framework."""
import sqlite3
from pathlib import Path

from app.db import Database
from app.migrations import MIGRATIONS


def test_migrations_are_contiguous_and_unique():
    versions = [v for v, _n, _s in MIGRATIONS]
    assert versions == list(range(1, len(versions) + 1))


def test_old_lesson_files_table_is_upgraded_without_losing_rows(tmp_path: Path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """CREATE TABLE lesson_files (id INTEGER PRIMARY KEY, lesson_id INTEGER NOT NULL, kind TEXT NOT NULL,
               filename TEXT NOT NULL, original_name TEXT NOT NULL);
           INSERT INTO lesson_files VALUES (1, 5, 'pdf', 'notes.pdf', 'Notes.pdf');"""
    )
    conn.commit()
    conn.close()
    db = Database(path)
    row = db.get_lesson_file(5, 1)
    assert row["original_name"] == "Notes.pdf" and row["storage"] == "local" and row["mega_node_id"] is None
    assert list(tmp_path.glob("old.db.pre-migration-*.bak"))


def make_lesson(db):
    course = db.save_course(None, "intro", "Intro", "Summary text", "Beginner", "Skills", "starter", "published")
    return course, db.save_lesson(course, "Lesson", "lesson-1", "Summary text", "Body text for the lesson")


def test_add_and_read_mega_file_metadata(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    _course, lesson = make_lesson(db)
    user = db.create_user("a@gmail.com", "a@gmail.com", "Admin", None, True, None)
    file_id = db.add_lesson_file(
        lesson_id=lesson, kind="pdf", filename="mega-abc.pdf", original_name="Week 1.pdf", storage="mega",
        mega_node_id="AbCd1234", size_bytes=2048, content_type="application/pdf", uploaded_by=user["id"],
    )
    full = db.get_lesson_file(lesson, file_id)
    assert full["mega_node_id"] == "AbCd1234" and full["size_bytes"] == 2048 and full["uploaded_by"] == user["id"]
    assert full["storage"] == "mega" and full["created_at"]
    assert db.get_lesson_file(lesson + 1, file_id) is None, "a file is only reachable through its own lesson"


def test_template_facing_queries_never_expose_node_ids(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    _course, lesson = make_lesson(db)
    db.add_lesson_file(lesson_id=lesson, kind="video", filename="mega-x.mp4", original_name="Clip.mp4", storage="mega",
                       mega_node_id="SECRETID", size_bytes=10, content_type="video/mp4", uploaded_by=1)
    assert all("mega_node_id" not in row for row in db.lesson_files(lesson))
    grouped = db.lesson_files_by_lesson()
    assert all("mega_node_id" not in row for rows in grouped.values() for row in rows)
    assert "SECRETID" not in repr(db.lesson_files(lesson)) + repr(grouped)
