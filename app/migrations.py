"""Versioned, additive schema migrations.

The base tables are still created by ``SCHEMA`` in ``app/db.py`` (``CREATE TABLE IF NOT EXISTS``).
Everything that changes an existing table or adds a table afterwards lives here, so a database
created by an older version of the site is upgraded in place without losing rows.

Rules for adding a migration:
- Append to ``MIGRATIONS`` with the next version number. Never edit or reorder old entries.
- Only additive, idempotent changes (new tables, new nullable columns, new indexes).
- Never drop or rewrite existing data here.
"""
from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _add_column(conn: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
    if column not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def _m001_legacy_columns(conn: sqlite3.Connection) -> None:
    """Columns that earlier releases added with ad-hoc ALTER TABLE calls."""
    _add_column(conn, "users", "status", "TEXT NOT NULL DEFAULT 'active'")
    _add_column(conn, "chat_messages", "removed_at", "TEXT")
    _add_column(conn, "chat_messages", "removed_by", "INTEGER")


def _m002_payment_hardening(conn: sqlite3.Connection) -> None:
    """Audit trail and verification fields for Stripe payments."""
    _add_column(conn, "orders", "currency", "TEXT")
    _add_column(conn, "orders", "paid_at", "TEXT")
    _add_column(conn, "orders", "stripe_payment_intent", "TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_session ON orders(stripe_session_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id)")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS payment_events (
               id INTEGER PRIMARY KEY,
               event_id TEXT NOT NULL UNIQUE,
               event_type TEXT NOT NULL,
               order_id INTEGER,
               session_id TEXT,
               outcome TEXT NOT NULL,
               received_at TEXT NOT NULL
           )"""
    )


def _m003_order_retention(conn: sqlite3.Connection) -> None:
    """Orders of deleted accounts are kept (anonymised), so payment records survive deletion."""
    _add_column(conn, "orders", "anonymized_at", "TEXT")


def _m004_mega_lesson_files(conn: sqlite3.Connection) -> None:
    """Store private MEGA node IDs for protected lesson downloads."""
    _add_column(conn, "lesson_files", "mega_node_id", "TEXT")


def _m005_lesson_file_metadata(conn: sqlite3.Connection) -> None:
    """Storage backend and upload metadata. Existing rows are legacy local files."""
    _add_column(conn, "lesson_files", "storage", "TEXT NOT NULL DEFAULT 'local'")
    _add_column(conn, "lesson_files", "size_bytes", "INTEGER")
    _add_column(conn, "lesson_files", "content_type", "TEXT")
    _add_column(conn, "lesson_files", "uploaded_by", "INTEGER")
    _add_column(conn, "lesson_files", "created_at", "TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_lesson_files_lesson ON lesson_files(lesson_id)")




def _m006_easypaisa_payment_review(conn: sqlite3.Connection) -> None:
    """Add review metadata for manually verified EasyPaisa payments."""
    _add_column(conn, "orders", "payment_method", "TEXT")
    _add_column(conn, "orders", "easypaisa_transaction_id", "TEXT")
    _add_column(conn, "orders", "payment_proof_path", "TEXT")
    _add_column(conn, "orders", "submitted_at", "TEXT")
    _add_column(conn, "orders", "reviewed_at", "TEXT")
    _add_column(conn, "orders", "reviewed_by", "INTEGER")
    _add_column(conn, "orders", "review_note", "TEXT")
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_easypaisa_transaction
           ON orders(easypaisa_transaction_id)
           WHERE easypaisa_transaction_id IS NOT NULL
             AND trim(easypaisa_transaction_id) <> ''"""
    )


def _m007_easypaisa_submissions(conn: sqlite3.Connection) -> None:
    """Create the EasyPaisa manual-payment review table."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS easypaisa_submissions (
            id INTEGER PRIMARY KEY,
            order_id INTEGER NOT NULL UNIQUE,
            user_id INTEGER NOT NULL,
            transaction_id TEXT NOT NULL UNIQUE,
            proof_filename TEXT NOT NULL,
            proof_mime TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending', 'approved', 'rejected')),
            submitted_at TEXT NOT NULL,
            reviewed_by INTEGER,
            reviewed_at TEXT,
            review_note TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_easypaisa_status
        ON easypaisa_submissions(status, submitted_at)
    """)


MIGRATIONS: list[tuple[int, str, Callable[[sqlite3.Connection], None]]] = [
    (1, "legacy_columns", _m001_legacy_columns),
    (2, "payment_hardening", _m002_payment_hardening),
    (3, "order_retention", _m003_order_retention),
    (4, "mega_lesson_files", _m004_mega_lesson_files),
    (5, "lesson_file_metadata", _m005_lesson_file_metadata),
    (6, "easypaisa_payment_review", _m006_easypaisa_payment_review),
    (7, "easypaisa_submissions", _m007_easypaisa_submissions),
]


def applied_versions(conn: sqlite3.Connection) -> set[int]:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
               version INTEGER PRIMARY KEY,
               name TEXT NOT NULL,
               applied_at TEXT NOT NULL
           )"""
    )
    return {row[0] for row in conn.execute("SELECT version FROM schema_migrations").fetchall()}


def backup_before_migrating(path: Path) -> Path | None:
    """Copy an existing, non-empty database file next to itself. Never overwrites a backup."""
    if not path.exists() or path.stat().st_size == 0:
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = path.with_name(f"{path.name}.pre-migration-{stamp}.bak")
    counter = 1
    while target.exists():
        target = path.with_name(f"{path.name}.pre-migration-{stamp}-{counter}.bak")
        counter += 1
    shutil.copy2(path, target)
    return target


def run_migrations(conn: sqlite3.Connection) -> list[int]:
    """Apply pending migrations in order. Each runs in its own transaction."""
    done = applied_versions(conn)
    conn.commit()
    ran: list[int] = []
    for version, name, step in MIGRATIONS:
        if version in done:
            continue
        try:
            conn.execute("BEGIN")
            step(conn)
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                (version, name, datetime.now(timezone.utc).replace(microsecond=0).isoformat()),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        ran.append(version)
    return ran
