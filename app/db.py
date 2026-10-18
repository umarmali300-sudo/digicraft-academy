from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from app.content import PLAN_RANK, load_articles
from app.migrations import backup_before_migrating, run_migrations
from app.security import iso, new_token, utcnow


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    canonical_email TEXT NOT NULL UNIQUE,
    email TEXT NOT NULL,
    password_hash TEXT,
    display_name TEXT NOT NULL,
    email_verified INTEGER NOT NULL DEFAULT 0,
    google_sub TEXT UNIQUE,
    avatar_path TEXT,
    avatar_mime TEXT,
    plan TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    user_id INTEGER,
    csrf_token TEXT NOT NULL,
    flash TEXT,
    oauth_state TEXT,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS email_tokens (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    purpose TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL,
    used_at TEXT
);
CREATE TABLE IF NOT EXISTS courses (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    level TEXT NOT NULL,
    category TEXT NOT NULL,
    plan_required TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY,
    course_id INTEGER NOT NULL,
    slug TEXT NOT NULL,
    title TEXT NOT NULL,
    position INTEGER NOT NULL,
    summary TEXT NOT NULL,
    body TEXT NOT NULL,
    video_url TEXT,
    notes TEXT,
    UNIQUE(course_id, slug)
);
CREATE TABLE IF NOT EXISTS lesson_files (
    id INTEGER PRIMARY KEY,
    lesson_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    filename TEXT NOT NULL,
    original_name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS enrollments (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    course_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(user_id, course_id)
);
CREATE TABLE IF NOT EXISTS lesson_progress (
    user_id INTEGER NOT NULL,
    lesson_id INTEGER NOT NULL,
    completed INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, lesson_id)
);
CREATE TABLE IF NOT EXISTS lesson_comments (
    id INTEGER PRIMARY KEY,
    lesson_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    removed_at TEXT,
    removed_by INTEGER
);
CREATE TABLE IF NOT EXISTS assignments (
    id INTEGER PRIMARY KEY,
    course_id INTEGER NOT NULL,
    lesson_id INTEGER,
    title TEXT NOT NULL,
    instructions TEXT NOT NULL,
    deadline TEXT,
    formats TEXT NOT NULL,
    max_points INTEGER NOT NULL,
    published INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS submissions (
    id INTEGER PRIMARY KEY,
    assignment_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    stored_name TEXT NOT NULL,
    original_name TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    submitted_at TEXT NOT NULL,
    status TEXT NOT NULL,
    grade INTEGER,
    feedback TEXT,
    reviewed_at TEXT,
    late INTEGER NOT NULL DEFAULT 0,
    UNIQUE(assignment_id, user_id)
);
CREATE TABLE IF NOT EXISTS announcements (
    id INTEGER PRIMARY KEY,
    author_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS moderation_actions (
    id INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL,
    actor_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_reports (
    id INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL,
    reporter_id INTEGER NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(message_id, reporter_id)
);
CREATE TABLE IF NOT EXISTS consultations (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    team_member TEXT NOT NULL,
    contact_method TEXT NOT NULL,
    question TEXT NOT NULL,
    preferred_time TEXT,
    consent INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS resources (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    category TEXT NOT NULL,
    summary TEXT NOT NULL,
    body_html TEXT NOT NULL,
    published_on TEXT NOT NULL,
    updated_on TEXT,
    author TEXT NOT NULL,
    image TEXT,
    image_alt TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    plan TEXT NOT NULL,
    amount_pkr INTEGER NOT NULL,
    status TEXT NOT NULL,
    stripe_session_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self._needs_backup = self._has_pending_migrations(path)
        if self._needs_backup:
            self.backup_path = backup_before_migrating(path)
        else:
            self.backup_path = None
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.commit()
            run_migrations(self._conn)
        self.sync_resources()

    @staticmethod
    def _has_pending_migrations(path: Path) -> bool:
        """True when an existing database file has not yet recorded every migration."""
        from app.migrations import MIGRATIONS

        if not path.exists() or path.stat().st_size == 0:
            return False
        probe = sqlite3.connect(path)
        try:
            try:
                done = {row[0] for row in probe.execute("SELECT version FROM schema_migrations")}
            except sqlite3.OperationalError:
                done = set()
        finally:
            probe.close()
        return any(version not in done for version, _name, _step in MIGRATIONS)

    def _ensure_column(self, table: str, column: str, declaration: str) -> None:
        rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        names = {row[1] for row in rows}
        if column not in names:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def _one(self, sql: str, params: tuple = ()):
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    def _all(self, sql: str, params: tuple = ()):
        with self._lock:
            return [dict(row) for row in self._conn.execute(sql, params).fetchall()]

    def _run(self, sql: str, params: tuple = ()) -> int:
        with self._lock:
            cursor = self._conn.execute(sql, params)
            self._conn.commit()
            return cursor.lastrowid or 0

    def sync_resources(self) -> None:
        for article in load_articles():
            existing = self._one("SELECT id FROM resources WHERE slug = ?", (article["slug"],))
            if existing:
                self._run(
                    """UPDATE resources SET title=?, category=?, summary=?, body_html=?, published_on=?,
                       updated_on=?, author=?, image=?, image_alt=? WHERE slug=?""",
                    (
                        article["title"],
                        article["category"],
                        article["summary"],
                        article["body_html"],
                        article["published_on"],
                        article["updated_on"],
                        article["author"],
                        article["image"],
                        article["image_alt"],
                        article["slug"],
                    ),
                )
            else:
                self._run(
                    """INSERT INTO resources
                       (slug, title, category, summary, body_html, published_on, updated_on, author, image, image_alt)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        article["slug"],
                        article["title"],
                        article["category"],
                        article["summary"],
                        article["body_html"],
                        article["published_on"],
                        article["updated_on"],
                        article["author"],
                        article["image"],
                        article["image_alt"],
                    ),
                )

    def purge_expired(self) -> None:
        now = iso()
        self._run("DELETE FROM sessions WHERE expires_at < ?", (now,))

    def create_session(self, user_id: int | None = None) -> dict:
        session_id = new_token()
        csrf = new_token()
        now = iso()
        from app.security import expires_in

        self._run(
            """INSERT INTO sessions (id, user_id, csrf_token, expires_at, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (session_id, user_id, csrf, expires_in(days=14), now),
        )
        return self.get_session(session_id)

    def get_session(self, session_id: str | None) -> dict | None:
        if not session_id:
            return None
        row = self._one("SELECT * FROM sessions WHERE id = ?", (session_id,))
        if not row:
            return None
        from app.security import parse_iso

        if parse_iso(row["expires_at"]) < utcnow():
            self._run("DELETE FROM sessions WHERE id = ?", (session_id,))
            return None
        return row

    def rotate_session(self, old_id: str, user_id: int) -> dict:
        self._run("DELETE FROM sessions WHERE id = ?", (old_id,))
        return self.create_session(user_id)

    def destroy_session(self, session_id: str) -> None:
        self._run("DELETE FROM sessions WHERE id = ?", (session_id,))

    def set_session_fields(self, session_id: str, **fields) -> None:
        allowed = {"flash", "oauth_state", "user_id", "expires_at"}
        parts = []
        values = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            parts.append(f"{key} = ?")
            values.append(value)
        if not parts:
            return
        values.append(session_id)
        self._run(f"UPDATE sessions SET {', '.join(parts)} WHERE id = ?", tuple(values))

    def pop_flash(self, session_id: str) -> str | None:
        row = self.get_session(session_id)
        if not row or not row.get("flash"):
            return None
        self.set_session_fields(session_id, flash=None)
        return row["flash"]

    def get_user(self, user_id: int | None) -> dict | None:
        if not user_id:
            return None
        return self._one(
            """SELECT id, canonical_email, email, display_name, email_verified, google_sub,
                      avatar_path, avatar_mime, plan, created_at,
                      CASE WHEN password_hash IS NULL THEN 0 ELSE 1 END AS has_password
               FROM users WHERE id = ?""",
            (user_id,),
        )

    def get_user_by_canonical(self, canonical: str) -> dict | None:
        return self._one("SELECT * FROM users WHERE canonical_email = ?", (canonical,))

    def get_user_by_google(self, sub: str) -> dict | None:
        return self._one("SELECT * FROM users WHERE google_sub = ?", (sub,))

    def create_user(self, canonical: str, email: str, display_name: str, password_hash: str | None, verified: bool, google_sub: str | None) -> dict:
        now = iso()
        user_id = self._run(
            """INSERT INTO users
               (canonical_email, email, password_hash, display_name, email_verified, google_sub, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (canonical, email, password_hash, display_name, int(verified), google_sub, now, now),
        )
        return self.get_user(user_id)

    def update_user(self, user_id: int, **fields) -> None:
        allowed = {
            "email",
            "password_hash",
            "display_name",
            "email_verified",
            "google_sub",
            "avatar_path",
            "avatar_mime",
            "plan",
            "canonical_email",
        }
        parts = ["updated_at = ?"]
        values = [iso()]
        for key, value in fields.items():
            if key in allowed:
                parts.append(f"{key} = ?")
                values.append(value)
        values.append(user_id)
        self._run(f"UPDATE users SET {', '.join(parts)} WHERE id = ?", tuple(values))

    def store_token(self, user_id: int, purpose: str, token_hash: str, expires_at: str) -> None:
        self._run(
            "DELETE FROM email_tokens WHERE user_id = ? AND purpose = ? AND used_at IS NULL",
            (user_id, purpose),
        )
        self._run(
            """INSERT INTO email_tokens (user_id, purpose, token_hash, expires_at)
               VALUES (?, ?, ?, ?)""",
            (user_id, purpose, token_hash, expires_at),
        )

    def find_token(self, token_hash: str, purpose: str) -> dict | None:
        row = self._one(
            "SELECT * FROM email_tokens WHERE token_hash = ? AND purpose = ? AND used_at IS NULL",
            (token_hash, purpose),
        )
        if not row:
            return None
        from app.security import parse_iso

        if parse_iso(row["expires_at"]) < utcnow():
            return None
        return row

    def consume_token(self, token_hash: str, purpose: str) -> dict | None:
        row = self.find_token(token_hash, purpose)
        if not row:
            return None
        self._run("UPDATE email_tokens SET used_at = ? WHERE id = ?", (iso(), row["id"]))
        return row

    def list_resources(self, category: str = "") -> list[dict]:
        if category:
            return self._all(
                "SELECT * FROM resources WHERE lower(category) = lower(?) ORDER BY published_on DESC",
                (category,),
            )
        return self._all("SELECT * FROM resources ORDER BY published_on DESC")

    def latest_resources(self, limit: int = 3) -> list[dict]:
        return self._all("SELECT * FROM resources ORDER BY published_on DESC LIMIT ?", (limit,))

    def get_resource(self, slug: str) -> dict | None:
        return self._one("SELECT * FROM resources WHERE slug = ?", (slug,))

    def published_courses(self, query: str = "", category: str = "", level: str = "") -> list[dict]:
        sql = "SELECT * FROM courses WHERE status = 'published'"
        params: list = []
        if query:
            sql += " AND (title LIKE ? ESCAPE '\\' OR summary LIKE ? ESCAPE '\\')"
            like = f"%{query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')}%"
            params.extend([like, like])
        if category:
            sql += " AND lower(category) = lower(?)"
            params.append(category)
        if level:
            sql += " AND lower(level) = lower(?)"
            params.append(level)
        sql += " ORDER BY created_at DESC"
        return self._all(sql, tuple(params))

    def course_categories(self) -> list[str]:
        rows = self._all(
            "SELECT DISTINCT category FROM courses WHERE status = 'published' ORDER BY category"
        )
        return [row["category"] for row in rows]

    def course_levels(self) -> list[str]:
        rows = self._all(
            "SELECT DISTINCT level FROM courses WHERE status = 'published' ORDER BY level"
        )
        return [row["level"] for row in rows]

    def lesson_count(self, course_id: int) -> int:
        row = self._one("SELECT COUNT(*) AS count FROM lessons WHERE course_id = ?", (course_id,))
        return int(row["count"]) if row else 0

    def published_course_count(self) -> int:
        row = self._one("SELECT COUNT(*) AS count FROM courses WHERE status = 'published'")
        return int(row["count"]) if row else 0

    def get_course(self, slug: str) -> dict | None:
        return self._one("SELECT * FROM courses WHERE slug = ? AND status = 'published'", (slug,))

    def get_course_by_id(self, course_id: int) -> dict | None:
        return self._one("SELECT * FROM courses WHERE id = ?", (course_id,))

    def lessons_for(self, course_id: int) -> list[dict]:
        return self._all(
            "SELECT * FROM lessons WHERE course_id = ? ORDER BY position ASC, id ASC",
            (course_id,),
        )

    def get_lesson(self, course_id: int, slug: str) -> dict | None:
        return self._one(
            "SELECT * FROM lessons WHERE course_id = ? AND slug = ?",
            (course_id, slug),
        )

    def get_lesson_by_id(self, lesson_id: int) -> dict | None:
        return self._one("SELECT * FROM lessons WHERE id = ?", (lesson_id,))

    _PUBLIC_FILE_COLUMNS = "id, lesson_id, kind, filename, original_name, storage, size_bytes, content_type, created_at"

    def lesson_files(self, lesson_id: int) -> list[dict]:
        """File rows safe to hand to templates: the private MEGA node id is deliberately not selected."""
        return self._all(
            f"SELECT {self._PUBLIC_FILE_COLUMNS} FROM lesson_files WHERE lesson_id = ? ORDER BY id",
            (lesson_id,),
        )

    def get_lesson_file(self, lesson_id: int, file_id: int) -> dict | None:
        """One file row including the private node id. Server-side use only."""
        return self._one("SELECT * FROM lesson_files WHERE lesson_id = ? AND id = ?", (lesson_id, file_id))

    def lesson_files_by_lesson(self) -> dict[int, list[dict]]:
        grouped: dict[int, list[dict]] = {}
        for row in self._all(f"SELECT {self._PUBLIC_FILE_COLUMNS} FROM lesson_files ORDER BY id"):
            grouped.setdefault(row["lesson_id"], []).append(row)
        return grouped

    def add_lesson_file(
        self,
        *,
        lesson_id: int,
        kind: str,
        filename: str,
        original_name: str,
        storage: str,
        mega_node_id: str | None,
        size_bytes: int,
        content_type: str,
        uploaded_by: int,
    ) -> int:
        return self._run(
            """INSERT INTO lesson_files
               (lesson_id, kind, filename, original_name, storage, mega_node_id, size_bytes,
                content_type, uploaded_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (lesson_id, kind, filename, original_name, storage, mega_node_id, size_bytes, content_type, uploaded_by, iso()),
        )

    def enrollment(self, user_id: int, course_id: int) -> dict | None:
        return self._one(
            "SELECT * FROM enrollments WHERE user_id = ? AND course_id = ?",
            (user_id, course_id),
        )

    def enroll(self, user_id: int, course_id: int, status: str = "active") -> None:
        existing = self.enrollment(user_id, course_id)
        if existing:
            self._run(
                "UPDATE enrollments SET status = ? WHERE id = ?",
                (status, existing["id"]),
            )
            return
        self._run(
            "INSERT INTO enrollments (user_id, course_id, status, created_at) VALUES (?, ?, ?, ?)",
            (user_id, course_id, status, iso()),
        )

    def progress_map(self, user_id: int, lesson_ids: list[int]) -> dict[int, bool]:
        if not lesson_ids:
            return {}
        marks = ",".join("?" for _ in lesson_ids)
        rows = self._all(
            f"SELECT lesson_id, completed FROM lesson_progress WHERE user_id = ? AND lesson_id IN ({marks})",
            (user_id, *lesson_ids),
        )
        return {row["lesson_id"]: bool(row["completed"]) for row in rows}

    def set_progress(self, user_id: int, lesson_id: int, completed: bool) -> None:
        self._run(
            """INSERT INTO lesson_progress (user_id, lesson_id, completed, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id, lesson_id) DO UPDATE SET completed = excluded.completed, updated_at = excluded.updated_at""",
            (user_id, lesson_id, int(completed), iso()),
        )

    def continue_lesson(self, user_id: int) -> dict | None:
        row = self._one(
            """SELECT l.slug AS lesson_slug, c.slug AS course_slug, c.title AS course_title,
                      l.title AS lesson_title, l.position
               FROM enrollments e
               JOIN courses c ON c.id = e.course_id
               JOIN lessons l ON l.course_id = c.id
               LEFT JOIN lesson_progress p ON p.lesson_id = l.id AND p.user_id = e.user_id AND p.completed = 1
               WHERE e.user_id = ? AND e.status = 'active' AND c.status = 'published' AND p.lesson_id IS NULL
               ORDER BY c.id, l.position
               LIMIT 1""",
            (user_id,),
        )
        return row

    def comments(self, lesson_id: int) -> list[dict]:
        return self._all(
            """SELECT c.id, c.body, c.created_at, u.display_name
               FROM lesson_comments c JOIN users u ON u.id = c.user_id
               WHERE c.lesson_id = ? ORDER BY c.id ASC""",
            (lesson_id,),
        )

    def add_comment(self, lesson_id: int, user_id: int, body: str) -> None:
        self._run(
            "INSERT INTO lesson_comments (lesson_id, user_id, body, created_at) VALUES (?, ?, ?, ?)",
            (lesson_id, user_id, body, iso()),
        )

    def chat_messages(self, since: int = 0, limit: int = 100) -> list[dict]:
        return self._all(
            """SELECT m.id,
                      CASE WHEN m.removed_at IS NULL THEN m.body
                           ELSE 'This message was removed by a moderator.' END AS body,
                      m.created_at, u.display_name, u.id AS author_id,
                      CASE WHEN u.avatar_path IS NULL THEN 0 ELSE 1 END AS has_avatar
               FROM chat_messages m JOIN users u ON u.id = m.user_id
               WHERE m.id > ? ORDER BY m.id ASC LIMIT ?""",
            (since, limit),
        )

    def add_chat_message(self, user_id: int, body: str) -> dict:
        message_id = self._run(
            "INSERT INTO chat_messages (user_id, body, created_at) VALUES (?, ?, ?)",
            (user_id, body, iso()),
        )
        return self._one(
            """SELECT m.id, m.body, m.created_at, u.display_name, u.id AS author_id,
                      CASE WHEN u.avatar_path IS NULL THEN 0 ELSE 1 END AS has_avatar
               FROM chat_messages m JOIN users u ON u.id = m.user_id WHERE m.id = ?""",
            (message_id,),
        )

    def report_message(self, message_id: int, reporter_id: int, reason: str) -> bool:
        message = self._one("SELECT id, user_id FROM chat_messages WHERE id = ?", (message_id,))
        if not message or message["user_id"] == reporter_id:
            return False
        try:
            self._run(
                """INSERT INTO chat_reports (message_id, reporter_id, reason, created_at)
                   VALUES (?, ?, ?, ?)""",
                (message_id, reporter_id, reason, iso()),
            )
        except sqlite3.IntegrityError:
            return False
        return True

    def reports_for_admin(self) -> list[dict]:
        return self._all(
            """SELECT r.id, r.reason, r.created_at, m.body,
                      reporter.display_name AS reporter, author.display_name AS author
               FROM chat_reports r
               JOIN chat_messages m ON m.id = r.message_id
               JOIN users reporter ON reporter.id = r.reporter_id
               JOIN users author ON author.id = m.user_id
               ORDER BY r.id DESC LIMIT 100"""
        )

    def learning_snapshot(self, user_id: int) -> dict | None:
        course = self._one(
            """SELECT c.id, c.title, c.slug
               FROM enrollments e JOIN courses c ON c.id = e.course_id
               WHERE e.user_id = ? AND e.status = 'active' AND c.status = 'published'
               ORDER BY e.id DESC LIMIT 1""",
            (user_id,),
        )
        if not course:
            return None
        lessons = self.lessons_for(course["id"])
        done = self.progress_map(user_id, [lesson["id"] for lesson in lessons])
        completed = sum(1 for lesson in lessons if done.get(lesson["id"]))
        total = len(lessons)
        upcoming = next((lesson for lesson in lessons if not done.get(lesson["id"])), None)
        return {
            "course_title": course["title"],
            "course_slug": course["slug"],
            "completed": completed,
            "total": total,
            "percent": int(round(100 * completed / total)) if total else 0,
            "next_title": upcoming["title"] if upcoming else "Course complete",
            "next_slug": upcoming["slug"] if upcoming else "",
            "position": upcoming["position"] if upcoming else total,
        }

    def create_consultation(self, **fields) -> int:
        return self._run(
            """INSERT INTO consultations
               (name, email, team_member, contact_method, question, preferred_time, consent, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                fields["name"],
                fields["email"],
                fields["team_member"],
                fields["contact_method"],
                fields["question"],
                fields.get("preferred_time") or "",
                1,
                iso(),
            ),
        )

    def create_order(self, user_id: int, plan: str, amount: int) -> dict:
        order_id = self._run(
            """INSERT INTO orders (user_id, plan, amount_pkr, status, created_at, updated_at)
               VALUES (?, ?, ?, 'pending_payment', ?, ?)""",
            (user_id, plan, amount, iso(), iso()),
        )
        return self.get_order(order_id)

    def submit_easypaisa(self, order_id: int, user_id: int, transaction_id: str, proof_filename: str, proof_mime: str) -> dict:
        now = iso()
        with self._lock:
            order = self._conn.execute("SELECT * FROM orders WHERE id=? AND user_id=?", (order_id, user_id)).fetchone()
            if not order or order["status"] != "pending_payment" or order["stripe_session_id"]:
                raise ValueError("This order is not eligible for EasyPaisa submission.")
            self._conn.execute("INSERT INTO easypaisa_submissions(order_id,user_id,transaction_id,proof_filename,proof_mime,status,submitted_at) VALUES(?,?,?,?,?,'pending',?)", (order_id,user_id,transaction_id,proof_filename,proof_mime,now))
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM easypaisa_submissions WHERE order_id=?", (order_id,)).fetchone()
            return dict(row)

    def easypaisa_for_user_order(self, order_id: int, user_id: int) -> dict | None:
        return self._one("SELECT * FROM easypaisa_submissions WHERE order_id=? AND user_id=?", (order_id,user_id))

    def easypaisa_submissions(self) -> list[dict]:
        return self._all("SELECT p.*, o.plan, o.amount_pkr, o.status AS order_status, u.email, u.display_name FROM easypaisa_submissions p JOIN orders o ON o.id=p.order_id JOIN users u ON u.id=p.user_id ORDER BY CASE p.status WHEN 'pending' THEN 0 ELSE 1 END, p.submitted_at DESC")

    def review_easypaisa(self, submission_id: int, reviewer_id: int, action: str, note: str = "") -> bool:
        if action not in {"approve", "reject"}:
            return False
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                sub = self._conn.execute("SELECT * FROM easypaisa_submissions WHERE id=?", (submission_id,)).fetchone()
                if not sub or sub["status"] != "pending":
                    self._conn.rollback(); return False
                now = iso()
                if action == "reject":
                    self._conn.execute("UPDATE easypaisa_submissions SET status='rejected', reviewed_by=?, reviewed_at=?, review_note=? WHERE id=? AND status='pending'", (reviewer_id,now,note[:1000],submission_id))
                    self._conn.commit(); return True
                order = self._conn.execute("SELECT * FROM orders WHERE id=?", (sub["order_id"],)).fetchone()
                if not order or order["user_id"] != sub["user_id"] or order["status"] != "pending_payment" or order["stripe_session_id"]:
                    self._conn.rollback(); return False
                from app.content import PLAN_RANK, plan_by_slug
                selected_plan = plan_by_slug(order["plan"])
                if not selected_plan or int(order["amount_pkr"]) != int(selected_plan["price"]):
                    self._conn.rollback(); return False
                user = self._conn.execute("SELECT plan FROM users WHERE id=?", (order["user_id"],)).fetchone()
                current_plan = user["plan"] if user else None
                best_plan = order["plan"] if PLAN_RANK.get(order["plan"], 0) > PLAN_RANK.get(current_plan, 0) else current_plan
                self._conn.execute("UPDATE orders SET status='paid', updated_at=? WHERE id=? AND status='pending_payment' AND (stripe_session_id IS NULL OR stripe_session_id='')", (now,order["id"]))
                if self._conn.execute("SELECT changes()").fetchone()[0] != 1:
                    self._conn.rollback(); return False
                self._conn.execute("UPDATE users SET plan=?, updated_at=? WHERE id=?", (best_plan,now,order["user_id"]))
                courses = self._conn.execute("SELECT id FROM courses WHERE status='published' AND plan_required IN ('starter','pro','premium')").fetchall()
                for course in courses:
                    from app.content import PLAN_RANK
                    if PLAN_RANK.get(self._conn.execute("SELECT plan_required FROM courses WHERE id=?", (course["id"],)).fetchone()[0], 99) <= PLAN_RANK.get(best_plan, 0):
                        self._conn.execute("INSERT OR IGNORE INTO enrollments(user_id,course_id,status,created_at) VALUES(?,?, 'active',?)", (order["user_id"],course["id"],now))
                self._conn.execute("UPDATE easypaisa_submissions SET status='approved', reviewed_by=?, reviewed_at=?, review_note=? WHERE id=? AND status='pending'", (reviewer_id,now,note[:1000],submission_id))
                self._conn.commit(); return True
            except Exception:
                self._conn.rollback()
                raise

    def get_order(self, order_id: int) -> dict | None:
        return self._one("SELECT * FROM orders WHERE id = ?", (order_id,))

    def set_order_session(self, order_id: int, session_id: str) -> None:
        self._run(
            "UPDATE orders SET stripe_session_id = ?, updated_at = ? WHERE id = ?",
            (session_id, iso(), order_id),
        )

    def settle_paid_session(
        self,
        *,
        event_id: str,
        order_id: int,
        session_id: str,
        amount_total: int | None,
        currency: str | None,
        payment_intent: str | None = None,
    ) -> str:
        """Mark an order paid only if Stripe's verified data matches what we recorded.

        Called from the signature-verified webhook only. Returns an outcome string:
        ``paid``, ``duplicate``, ``already_paid``, ``order_not_found``, ``session_unrecorded``
        (retryable: the checkout session id is not saved yet), ``session_mismatch``,
        ``amount_mismatch`` or ``currency_mismatch``. Everything except ``session_unrecorded`` is
        written to ``payment_events`` so each Stripe event is handled once. A mismatch never
        activates a plan. A payment never downgrades a higher plan the user already holds.
        """
        now = iso()
        with self._lock:
            conn = self._conn
            if event_id and conn.execute(
                "SELECT 1 FROM payment_events WHERE event_id = ?", (event_id,)
            ).fetchone():
                return "duplicate"
            row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            order = dict(row) if row else None

            def record(outcome: str) -> str:
                conn.execute(
                    """INSERT OR IGNORE INTO payment_events
                       (event_id, event_type, order_id, session_id, outcome, received_at)
                       VALUES (?, 'checkout.session.completed', ?, ?, ?, ?)""",
                    (event_id or f"noid-{order_id}-{now}", order_id or None, session_id or None, outcome, now),
                )
                conn.commit()
                return outcome

            if not order:
                return record("order_not_found")
            if order["status"] == "paid":
                return record("already_paid")
            if not order.get("stripe_session_id"):
                return "session_unrecorded"
            if not session_id or order["stripe_session_id"] != session_id:
                return record("session_mismatch")
            if (currency or "").lower() != "pkr":
                return record("currency_mismatch")
            if amount_total is None or int(amount_total) != int(order["amount_pkr"]) * 100:
                return record("amount_mismatch")
            conn.execute(
                """UPDATE orders SET status = 'paid', currency = 'pkr', paid_at = ?,
                          stripe_payment_intent = ?, updated_at = ? WHERE id = ?""",
                (now, payment_intent, now, order_id),
            )
            user = conn.execute("SELECT plan FROM users WHERE id = ?", (order["user_id"],)).fetchone()
            current_rank = PLAN_RANK.get((user["plan"] if user else None) or "", 0)
            if PLAN_RANK.get(order["plan"], 0) > current_rank:
                conn.execute(
                    "UPDATE users SET plan = ?, updated_at = ? WHERE id = ?",
                    (order["plan"], now, order["user_id"]),
                )
                current_rank = PLAN_RANK[order["plan"]]
            # Enroll in published courses the plan covers. Existing enrollments (including ones an
            # admin set to another status) are left exactly as they are.
            covered = [name for name, rank in PLAN_RANK.items() if rank <= current_rank]
            if user and covered:
                marks = ",".join("?" for _ in covered)
                conn.execute(
                    f"""INSERT OR IGNORE INTO enrollments (user_id, course_id, status, created_at)
                        SELECT ?, id, 'active', ? FROM courses
                        WHERE status = 'published' AND plan_required IN ({marks})""",
                    (order["user_id"], now, *covered),
                )
            return record("paid")

    def admin_orders(self, status: str = "", query: str = "", limit: int = 300) -> list[dict]:
        sql = """SELECT o.id, o.plan, o.amount_pkr, o.status, o.stripe_session_id, o.created_at,
                        o.paid_at, o.anonymized_at,
                        CASE WHEN o.anonymized_at IS NOT NULL THEN NULL ELSE u.display_name END AS display_name,
                        CASE WHEN o.anonymized_at IS NOT NULL THEN NULL ELSE u.email END AS email
                 FROM orders o LEFT JOIN users u ON u.id = o.user_id"""
        where, params = [], []
        if status:
            where.append("o.status = ?")
            params.append(status)
        if query:
            like = f"%{query.strip().lower()}%"
            where.append("(lower(u.display_name) LIKE ? OR lower(u.email) LIKE ? OR CAST(o.id AS TEXT) = ?)")
            params.extend([like, like, query.strip()])
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY o.id DESC LIMIT ?"
        params.append(int(limit))
        return self._all(sql, tuple(params))

    def payment_summary(self) -> dict:
        paid = self._one(
            "SELECT COUNT(*) AS n, COALESCE(SUM(amount_pkr), 0) AS total FROM orders WHERE status = 'paid'"
        )
        pending = self._one("SELECT COUNT(*) AS n FROM orders WHERE status = 'pending_payment'")["n"]
        rejected = self._one(
            "SELECT COUNT(*) AS n FROM payment_events WHERE outcome NOT IN ('paid', 'duplicate', 'already_paid')"
        )["n"]
        return {"paid_orders": paid["n"], "paid_total_pkr": paid["total"], "pending_orders": pending, "rejected_events": rejected}

    def payment_events(self, limit: int = 100) -> list[dict]:
        return self._all("SELECT * FROM payment_events ORDER BY id DESC LIMIT ?", (limit,))

    def user_learning(self, user_id: int, include_unpublished: bool = False) -> list[dict]:
        rows = self._all(
            """SELECT c.id, c.slug, c.title, c.summary, e.status
               FROM enrollments e JOIN courses c ON c.id = e.course_id
               WHERE e.user_id = ? AND (? = 1 OR c.status = 'published')
               ORDER BY e.id DESC""",
            (user_id, int(include_unpublished)),
        )
        for row in rows:
            lessons = self.lessons_for(row["id"])
            done = self.progress_map(user_id, [lesson["id"] for lesson in lessons])
            completed = sum(1 for lesson in lessons if done.get(lesson["id"]))
            total = len(lessons)
            upcoming = next((lesson for lesson in lessons if not done.get(lesson["id"])), None)
            row["completed"] = completed
            row["total"] = total
            row["percent"] = int(round(100 * completed / total)) if total else 0
            row["resume_slug"] = upcoming["slug"] if upcoming else ""
            row["status_label"] = "Enrolled" if row["status"] == "active" else row["status"]
        return rows

    def set_user_status(self, user_id: int, status: str) -> None:
        if status not in {"active", "suspended"}:
            return
        self._run(
            "UPDATE users SET status = ?, updated_at = ? WHERE id = ?",
            (status, iso(), user_id),
        )

    def admin_students(self, query: str, sort: str) -> list[dict]:
        order = {
            "name": "u.display_name COLLATE NOCASE ASC",
            "newest": "u.created_at DESC",
            "activity": "activity DESC",
        }.get(sort, "u.display_name COLLATE NOCASE ASC")
        needle = f"%{query.strip()}%"
        exact = query.strip()
        return self._all(
            f"""SELECT u.id, u.display_name, u.email, u.created_at, u.email_verified, u.status,
                       COALESCE(
                         (SELECT MAX(updated_at) FROM lesson_progress WHERE user_id = u.id),
                         (SELECT MAX(created_at) FROM chat_messages WHERE user_id = u.id),
                         u.updated_at
                       ) AS activity
                FROM users u
                WHERE ? = '' OR u.display_name LIKE ? OR u.email LIKE ? OR CAST(u.id AS TEXT) = ?
                ORDER BY {order}""",
            (query.strip(), needle, needle, exact),
        )

    def student_profile(self, user_id: int) -> dict | None:
        user = self._one(
            """SELECT id, display_name, email, created_at, updated_at, email_verified, status, plan
               FROM users WHERE id = ?""",
            (user_id,),
        )
        if not user:
            return None
        user["learning"] = self.user_learning(user_id, include_unpublished=True)
        user["submissions"] = self.submissions_for_user(user_id)
        user["activity"] = self._all(
            """SELECT 'lesson' AS kind, l.title AS label, p.updated_at AS at
               FROM lesson_progress p JOIN lessons l ON l.id = p.lesson_id
               WHERE p.user_id = ? ORDER BY p.updated_at DESC LIMIT 8""",
            (user_id,),
        )
        return user

    def admin_overview(self) -> dict:
        students = self._one("SELECT COUNT(*) AS n FROM users")["n"]
        active = self._one("SELECT COUNT(*) AS n FROM users WHERE email_verified = 1 AND status = 'active'")["n"]
        courses = self._one("SELECT COUNT(*) AS n FROM courses")["n"]
        lessons = self._one("SELECT COUNT(*) AS n FROM lessons")["n"]
        waiting = self._one("SELECT COUNT(*) AS n FROM submissions WHERE status = 'pending'")["n"]
        reviewed = self._one("SELECT COUNT(*) AS n FROM submissions WHERE status = 'reviewed'")["n"]
        return {
            "students": students,
            "active_students": active,
            "courses": courses,
            "lessons": lessons,
            "awaiting_review": waiting,
            "reviewed_submissions": reviewed,
            "recent": self.recent_activity(),
            "needs_help": self.students_needing_help(),
        }

    def recent_activity(self) -> list[dict]:
        progress = self._all(
            """SELECT u.display_name, l.title AS label, p.updated_at AS at, 'lesson' AS kind
               FROM lesson_progress p
               JOIN users u ON u.id = p.user_id
               JOIN lessons l ON l.id = p.lesson_id
               ORDER BY p.updated_at DESC LIMIT 8"""
        )
        messages = self._all(
            """SELECT u.display_name, 'Chat message' AS label, m.created_at AS at, 'chat' AS kind
               FROM chat_messages m JOIN users u ON u.id = m.user_id
               WHERE m.removed_at IS NULL
               ORDER BY m.created_at DESC LIMIT 8"""
        )
        rows = progress + messages
        rows.sort(key=lambda row: row["at"], reverse=True)
        return rows[:8]

    def students_needing_help(self) -> list[dict]:
        found = []
        for user in self._all(
            "SELECT id, display_name, email FROM users WHERE status = 'active' AND email_verified = 1"
        ):
            learning = self.user_learning(user["id"])
            low = [course for course in learning if course["total"] and course["percent"] < 50]
            overdue = self.missing_work(user["id"])
            if low or overdue:
                found.append({
                    "id": user["id"],
                    "display_name": user["display_name"],
                    "email": user["email"],
                    "low_courses": len(low),
                    "overdue": len(overdue),
                })
        return found[:20]

    def missing_work(self, user_id: int) -> list[dict]:
        now = iso()
        return self._all(
            """SELECT a.id, a.title, a.deadline, c.title AS course_title
               FROM assignments a
               JOIN courses c ON c.id = a.course_id
               JOIN enrollments e ON e.course_id = a.course_id AND e.user_id = ? AND e.status = 'active'
               LEFT JOIN submissions s ON s.assignment_id = a.id AND s.user_id = ?
               WHERE a.published = 1 AND a.deadline IS NOT NULL AND a.deadline < ?
                 AND s.id IS NULL""",
            (user_id, user_id, now),
        )

    def create_assignment(self, course_id: int, lesson_id: int | None, title: str, instructions: str, deadline: str, formats: str, max_points: int) -> int:
        return self._run(
            """INSERT INTO assignments
               (course_id, lesson_id, title, instructions, deadline, formats, max_points, published, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)""",
            (course_id, lesson_id, title, instructions, deadline or None, formats, max_points, iso()),
        )

    def assignments(self, course_id: int | None = None) -> list[dict]:
        if course_id:
            return self._all(
                """SELECT a.*, c.title AS course_title,
                          (SELECT COUNT(*) FROM submissions s WHERE s.assignment_id = a.id) AS submissions
                   FROM assignments a JOIN courses c ON c.id = a.course_id
                   WHERE a.course_id = ? ORDER BY a.id DESC""",
                (course_id,),
            )
        return self._all(
            """SELECT a.*, c.title AS course_title,
                      (SELECT COUNT(*) FROM submissions s WHERE s.assignment_id = a.id) AS submissions
               FROM assignments a JOIN courses c ON c.id = a.course_id
               ORDER BY a.id DESC"""
        )

    def assignment(self, assignment_id: int) -> dict | None:
        return self._one(
            """SELECT a.*, c.title AS course_title FROM assignments a
               JOIN courses c ON c.id = a.course_id WHERE a.id = ?""",
            (assignment_id,),
        )

    def save_submission(self, assignment_id: int, user_id: int, stored_name: str, original_name: str, size_bytes: int, late: bool) -> int:
        existing = self._one(
            "SELECT id FROM submissions WHERE assignment_id = ? AND user_id = ?",
            (assignment_id, user_id),
        )
        now = iso()
        if existing:
            self._run(
                """UPDATE submissions SET stored_name=?, original_name=?, size_bytes=?, submitted_at=?,
                   status='pending', grade=NULL, feedback=NULL, reviewed_at=NULL, late=? WHERE id=?""",
                (stored_name, original_name, size_bytes, now, int(late), existing["id"]),
            )
            return existing["id"]
        return self._run(
            """INSERT INTO submissions
               (assignment_id, user_id, stored_name, original_name, size_bytes, submitted_at, status, late)
               VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)""",
            (assignment_id, user_id, stored_name, original_name, size_bytes, now, int(late)),
        )

    def submissions(self, assignment_id: int | None = None) -> list[dict]:
        sql = """SELECT s.*, a.title AS assignment_title, a.max_points, u.display_name, u.email
                 FROM submissions s
                 JOIN assignments a ON a.id = s.assignment_id
                 JOIN users u ON u.id = s.user_id"""
        if assignment_id:
            return self._all(sql + " WHERE s.assignment_id = ? ORDER BY s.submitted_at DESC", (assignment_id,))
        return self._all(sql + " ORDER BY s.submitted_at DESC")

    def submission(self, submission_id: int) -> dict | None:
        return self._one(
            """SELECT s.*, a.title AS assignment_title, a.max_points, a.course_id, u.display_name
               FROM submissions s
               JOIN assignments a ON a.id = s.assignment_id
               JOIN users u ON u.id = s.user_id
               WHERE s.id = ?""",
            (submission_id,),
        )

    def review_submission(self, submission_id: int, status: str, grade: int | None, feedback: str) -> None:
        self._run(
            """UPDATE submissions SET status=?, grade=?, feedback=?, reviewed_at=? WHERE id=?""",
            (status, grade, feedback, iso(), submission_id),
        )

    def submissions_for_user(self, user_id: int) -> list[dict]:
        return self._all(
            """SELECT s.id, s.status, s.grade, s.feedback, s.submitted_at, s.late, s.original_name,
                      a.title, a.max_points, c.title AS course_title
               FROM submissions s
               JOIN assignments a ON a.id = s.assignment_id
               JOIN courses c ON c.id = a.course_id
               WHERE s.user_id = ? ORDER BY s.submitted_at DESC""",
            (user_id,),
        )

    def published_assignments_for_user(self, user_id: int) -> list[dict]:
        return self._all(
            """SELECT a.*, c.title AS course_title, s.id AS submission_id, s.status AS submission_status,
                      s.grade, s.feedback, s.late
               FROM assignments a
               JOIN courses c ON c.id = a.course_id
               JOIN enrollments e ON e.course_id = a.course_id AND e.user_id = ? AND e.status = 'active'
               LEFT JOIN submissions s ON s.assignment_id = a.id AND s.user_id = ?
               WHERE a.published = 1 ORDER BY a.deadline IS NULL, a.deadline""",
            (user_id, user_id),
        )

    def create_announcement(self, author_id: int, title: str, body: str) -> int:
        return self._run(
            "INSERT INTO announcements (author_id, title, body, created_at) VALUES (?, ?, ?, ?)",
            (author_id, title, body, iso()),
        )

    def announcements(self) -> list[dict]:
        return self._all(
            """SELECT a.id, a.title, a.body, a.created_at, u.display_name
               FROM announcements a JOIN users u ON u.id = a.author_id
               ORDER BY a.id DESC"""
        )

    def admin_chat(self) -> list[dict]:
        return self._all(
            """SELECT m.id, m.body, m.created_at, m.removed_at, u.display_name,
                      (SELECT COUNT(*) FROM chat_reports r WHERE r.message_id = m.id) AS reports
               FROM chat_messages m JOIN users u ON u.id = m.user_id
               ORDER BY m.id DESC LIMIT 100"""
        )

    def remove_chat_message(self, message_id: int, actor_id: int) -> bool:
        message = self._one("SELECT id, removed_at FROM chat_messages WHERE id = ?", (message_id,))
        if not message or message["removed_at"]:
            return False
        now = iso()
        self._run(
            "UPDATE chat_messages SET removed_at = ?, removed_by = ? WHERE id = ?",
            (now, actor_id, message_id),
        )
        self._run(
            "INSERT INTO moderation_actions (message_id, actor_id, action, created_at) VALUES (?, ?, 'remove', ?)",
            (message_id, actor_id, now),
        )
        return True

    def course_progress_report(self) -> list[dict]:
        return self._all(
            """SELECT c.title AS course, u.display_name AS student,
                      (SELECT COUNT(*) FROM lessons l WHERE l.course_id = c.id) AS lessons,
                      (SELECT COUNT(*) FROM lesson_progress p
                         JOIN lessons l ON l.id = p.lesson_id
                         WHERE l.course_id = c.id AND p.user_id = u.id AND p.completed = 1) AS completed
               FROM enrollments e
               JOIN users u ON u.id = e.user_id
               JOIN courses c ON c.id = e.course_id
               WHERE e.status = 'active'
               ORDER BY c.title, u.display_name"""
        )

    def enrollment_report(self) -> list[dict]:
        return self._all(
            """SELECT c.title, c.status,
                      (SELECT COUNT(*) FROM enrollments e WHERE e.course_id = c.id AND e.status = 'active') AS enrolled
               FROM courses c ORDER BY c.title"""
        )

    def activity_by_day(self) -> list[dict]:
        progress = self._all(
            "SELECT substr(updated_at, 1, 10) AS day, COUNT(*) AS n FROM lesson_progress GROUP BY day"
        )
        messages = self._all(
            "SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS n FROM chat_messages WHERE removed_at IS NULL GROUP BY day"
        )
        counts: dict[str, int] = {}
        for row in progress + messages:
            counts[row["day"]] = counts.get(row["day"], 0) + row["n"]
        return [{"day": day, "events": counts[day]} for day in sorted(counts)]

    def save_course(self, course_id: int | None, slug: str, title: str, summary: str, level: str, category: str, plan_required: str, status: str) -> int:
        if course_id:
            self._run(
                """UPDATE courses SET slug=?, title=?, summary=?, level=?, category=?, plan_required=?, status=?
                   WHERE id=?""",
                (slug, title, summary, level, category, plan_required, status, course_id),
            )
            return course_id
        return self._run(
            """INSERT INTO courses (slug, title, summary, level, category, plan_required, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (slug, title, summary, level, category, plan_required, status, iso()),
        )

    def save_lesson(self, course_id: int, title: str, slug: str, summary: str, body: str) -> int:
        position = self._one("SELECT COALESCE(MAX(position), 0) + 1 AS n FROM lessons WHERE course_id = ?", (course_id,))["n"]
        return self._run(
            """INSERT INTO lessons (course_id, slug, title, position, summary, body, video_url, notes)
               VALUES (?, ?, ?, ?, ?, ?, '', '')""",
            (course_id, slug, title, position, summary, body),
        )

    def set_enrollment(self, user_id: int, course_id: int, status: str) -> None:
        existing = self._one(
            "SELECT id FROM enrollments WHERE user_id = ? AND course_id = ?",
            (user_id, course_id),
        )
        if existing:
            self._run("UPDATE enrollments SET status = ? WHERE id = ?", (status, existing["id"]))
            return
        self._run(
            "INSERT INTO enrollments (user_id, course_id, status, created_at) VALUES (?, ?, ?, ?)",
            (user_id, course_id, status, iso()),
        )

    def consultations_recent(self) -> list[dict]:
        return self._all(
            "SELECT id, name, email, question, created_at FROM consultations ORDER BY id DESC LIMIT 20"
        )

    def user_orders(self, user_id: int) -> list[dict]:
        return self._all(
            "SELECT * FROM orders WHERE user_id = ? ORDER BY id DESC",
            (user_id,),
        )

    def delete_user(self, user_id: int, email: str) -> None:
        self._run(
            """DELETE FROM chat_reports
               WHERE reporter_id = ? OR message_id IN (SELECT id FROM chat_messages WHERE user_id = ?)""",
            (user_id, user_id),
        )
        self._run("DELETE FROM chat_messages WHERE user_id = ?", (user_id,))
        self._run("DELETE FROM lesson_comments WHERE user_id = ?", (user_id,))
        self._run("DELETE FROM lesson_progress WHERE user_id = ?", (user_id,))
        self._run("DELETE FROM enrollments WHERE user_id = ?", (user_id,))
        self._run("DELETE FROM email_tokens WHERE user_id = ?", (user_id,))
        # Unpaid requests hold no financial record. Paid orders are kept without the account link.
        self._run("DELETE FROM orders WHERE user_id = ? AND status != 'paid'", (user_id,))
        self._run(
            "UPDATE orders SET user_id = 0, anonymized_at = ? WHERE user_id = ? AND status = 'paid'",
            (iso(), user_id),
        )
        self._run("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        self._run("DELETE FROM consultations WHERE lower(email) = lower(?)", (email,))
        self._run("DELETE FROM users WHERE id = ?", (user_id,))
