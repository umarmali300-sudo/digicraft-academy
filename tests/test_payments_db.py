"""Database-level tests for payment settlement and migrations. They need no web framework."""
import sqlite3

from app.db import Database
from app.migrations import MIGRATIONS


def make_db(tmp_path):
    return Database(tmp_path / "t.db")


def make_order(db, plan="starter", amount=2700, session="cs_1"):
    user = db.create_user("u@gmail.com", "u@gmail.com", "User One", None, True, None)
    order = db.create_order(user["id"], plan, amount)
    if session:
        db.set_order_session(order["id"], session)
    return user, order


def settle(db, order, **kw):
    args = dict(event_id="evt_1", order_id=order["id"], session_id="cs_1", amount_total=order["amount_pkr"] * 100, currency="pkr")
    args.update(kw)
    return db.settle_paid_session(**args)


def test_valid_payment_marks_paid_and_sets_plan(tmp_path):
    db = make_db(tmp_path)
    user, order = make_order(db)
    assert settle(db, order) == "paid"
    assert db.get_order(order["id"])["status"] == "paid"
    assert db.get_order(order["id"])["paid_at"]
    assert db.get_user(user["id"])["plan"] == "starter"


def test_replayed_event_is_ignored(tmp_path):
    db = make_db(tmp_path)
    _user, order = make_order(db)
    assert settle(db, order) == "paid"
    assert settle(db, order) == "duplicate"


def test_mismatches_never_pay(tmp_path):
    db = make_db(tmp_path)
    user, order = make_order(db)
    assert settle(db, order, event_id="a", amount_total=1) == "amount_mismatch"
    assert settle(db, order, event_id="b", session_id="cs_x") == "session_mismatch"
    assert settle(db, order, event_id="c", currency="usd") == "currency_mismatch"
    assert settle(db, order, event_id="d", order_id=9999) == "order_not_found"
    assert db.get_order(order["id"])["status"] == "pending_payment"
    assert not db.get_user(user["id"])["plan"]


def test_unrecorded_session_is_retryable_and_not_logged(tmp_path):
    db = make_db(tmp_path)
    _user, order = make_order(db, session=None)
    assert settle(db, order, event_id="late") == "session_unrecorded"
    assert db.payment_events() == []
    db.set_order_session(order["id"], "cs_1")
    assert settle(db, order, event_id="late") == "paid"


def test_payment_never_downgrades_plan(tmp_path):
    db = make_db(tmp_path)
    user, premium = make_order(db, plan="premium", amount=8300, session="cs_p")
    assert settle(db, premium, event_id="e1", session_id="cs_p") == "paid"
    starter = db.create_order(user["id"], "starter", 2700)
    db.set_order_session(starter["id"], "cs_s")
    assert settle(db, starter, event_id="e2", session_id="cs_s") == "paid"
    assert db.get_user(user["id"])["plan"] == "premium"


def test_migrations_upgrade_old_database_without_losing_rows(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, plan TEXT NOT NULL,
               amount_pkr INTEGER NOT NULL, status TEXT NOT NULL, stripe_session_id TEXT,
               created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
           INSERT INTO orders VALUES (1, 7, 'pro', 5500, 'pending_payment', 'cs_old', 't', 't');"""
    )
    conn.commit()
    conn.close()
    db = Database(path)
    assert db.get_order(1)["stripe_session_id"] == "cs_old"
    versions = {r["version"] for r in db._all("SELECT version FROM schema_migrations")}
    assert versions == {version for version, _n, _s in MIGRATIONS}
    assert list(tmp_path.glob("old.db.pre-migration-*.bak")), "a backup should exist"
    Database(path)  # second start is a no-op
    assert len(list(tmp_path.glob("old.db.pre-migration-*.bak"))) == 1


def _courses(db):
    ids = {}
    for slug, plan, status in (
        ("c-starter", "starter", "published"),
        ("c-pro", "pro", "published"),
        ("c-draft", "starter", "draft"),
    ):
        ids[slug] = db.save_course(None, slug, slug.title(), "Summary", "Beginner", "Skills", plan, status)
    return ids


def test_payment_enrolls_only_published_courses_within_plan(tmp_path):
    db = make_db(tmp_path)
    ids = _courses(db)
    user, order = make_order(db)
    assert settle(db, order) == "paid"
    assert db.enrollment(user["id"], ids["c-starter"])["status"] == "active"
    assert db.enrollment(user["id"], ids["c-pro"]) is None
    assert db.enrollment(user["id"], ids["c-draft"]) is None


def test_payment_keeps_existing_enrollment_status(tmp_path):
    db = make_db(tmp_path)
    ids = _courses(db)
    user, order = make_order(db)
    db.enroll(user["id"], ids["c-starter"], "paused")
    assert settle(db, order) == "paid"
    assert db.enrollment(user["id"], ids["c-starter"])["status"] == "paused"


def test_account_deletion_keeps_paid_orders_anonymised(tmp_path):
    db = make_db(tmp_path)
    user, paid = make_order(db)
    assert settle(db, paid) == "paid"
    unpaid = db.create_order(user["id"], "pro", 5500)
    db.delete_user(user["id"], user["email"])
    kept = db.get_order(paid["id"])
    assert kept["status"] == "paid" and kept["user_id"] == 0 and kept["anonymized_at"]
    assert db.get_order(unpaid["id"]) is None
    assert db.user_orders(user["id"]) == []
    row = [o for o in db.admin_orders() if o["id"] == paid["id"]][0]
    assert row["display_name"] is None and row["email"] is None


def test_admin_orders_filter_and_summary(tmp_path):
    db = make_db(tmp_path)
    user, paid = make_order(db)
    settle(db, paid)
    db.create_order(user["id"], "pro", 5500)
    assert [o["status"] for o in db.admin_orders("paid")] == ["paid"]
    assert len(db.admin_orders("", "User One")) == 2
    assert db.admin_orders("", "nobody-matches") == []
    summary = db.payment_summary()
    assert summary["paid_orders"] == 1 and summary["paid_total_pkr"] == 2700 and summary["pending_orders"] == 1
    settle(db, paid, event_id="bad", order_id=9999)
    assert db.payment_summary()["rejected_events"] == 1
