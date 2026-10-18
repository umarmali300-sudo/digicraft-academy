from pathlib import Path

from fastapi.testclient import TestClient

from tests.test_app import client_for, csrf, register_and_verify


def test_student_cannot_open_admin_or_become_admin(tmp_path: Path):
    client = client_for(tmp_path)
    register_and_verify(client, tmp_path)
    page = client.get("/admin", follow_redirects=False)
    assert page.status_code == 303
    assert page.headers["location"] == "/account"
    api = client.get("/api/admin/students")
    assert api.status_code == 403
    assert api.json()["error"] == "Admin authorization required."
    account = client.get("/account")
    changed = client.post(
        "/account",
        data={
            "csrf_token": csrf(account.text),
            "display_name": "Learner One",
            "status": "suspended",
            "role": "admin",
            "canonical_email": "admin.test@gmail.com",
        },
        follow_redirects=False,
    )
    assert changed.status_code == 303
    still = client.get("/api/admin/students")
    assert still.status_code == 403
    assert "Admin" not in client.get("/").text.split("site-nav", 1)[-1]


def test_admin_dashboard_uses_real_records(tmp_path: Path):
    client = client_for(tmp_path)
    register_and_verify(client, tmp_path, email="learner.one@gmail.com", name="Learner One")
    register_and_verify(client, tmp_path, email="admin.test@gmail.com", name="Ada Admin")
    home = client.get("/admin")
    assert home.status_code == 200
    assert "Registered students" in home.text
    assert ">2<" in home.text
    found = client.get("/admin/students?q=Learner&sort=name")
    assert "Learner One" in found.text
    student_id = found.text.split("Student ID", 1)[0]
    assert 'href="/admin/students/' in found.text
    profile_id = found.text.split('href="/admin/students/')[1].split('"')[0]
    profile = client.get(f"/admin/students/{profile_id}")
    assert "Learner One" in profile.text
    assert "password" not in profile.text.lower()
    assert student_id


def test_assignment_review_is_private_to_the_student(tmp_path: Path):
    client = client_for(tmp_path)
    register_and_verify(client, tmp_path, email="admin.test@gmail.com", name="Ada Admin")
    register_and_verify(client, tmp_path, email="learner.one@gmail.com", name="Learner One")
    db = client.app.state.db
    course_id = db.save_course(None, "studio-course", "Studio Course", "A published course for practice.", "Beginner", "Design", "starter", "published")
    learner = db.get_user_by_canonical("learnerone@gmail.com")
    db.update_user(learner["id"], plan="starter")
    db.set_enrollment(learner["id"], course_id, "active")
    assignment_id = db.create_assignment(course_id, None, "Portfolio draft", "Upload one text file for review.", "", "txt", 10)
    grades = client.get("/account/grades")
    submitted = client.post(
        f"/assignments/{assignment_id}/submit",
        data={"csrf_token": csrf(grades.text)},
        files={"submission": ("notes.txt", b"hello academy", "text/plain")},
        follow_redirects=False,
    )
    assert submitted.status_code == 303
    client.post("/logout", data={"csrf_token": csrf(client.get("/account/grades").text)})
    register_and_verify(client, tmp_path, email="learner.two@gmail.com", name="Learner Two")
    other = db.get_user_by_canonical("learnertwo@gmail.com")
    submission = db.submissions(assignment_id)[0]
    denied = client.get(f"/admin/submissions/{submission['id']}/file")
    assert denied.status_code == 403
    client.post("/logout", data={"csrf_token": csrf(client.get("/account").text)})
    login = client.post(
        "/login",
        data={"csrf_token": csrf(client.get("/login").text), "email": "admin.test@gmail.com", "password": "Correcthorse1", "next": "/admin/submissions"},
        follow_redirects=False,
    )
    assert login.status_code == 303
    page = client.get("/admin/submissions")
    reviewed = client.post(
        f"/admin/submissions/{submission['id']}/review",
        data={"csrf_token": csrf(page.text), "status": "reviewed", "grade": "8", "feedback": "Clear notes."},
        follow_redirects=False,
    )
    assert reviewed.status_code == 303
    downloaded = client.get(f"/admin/submissions/{submission['id']}/file")
    assert downloaded.status_code == 200
    assert downloaded.content == b"hello academy"
    client.post("/logout", data={"csrf_token": csrf(client.get("/admin").text)})
    client.post(
        "/login",
        data={"csrf_token": csrf(client.get("/login").text), "email": "learner.one@gmail.com", "password": "Correcthorse1", "next": "/account/grades"},
        follow_redirects=False,
    )
    own = client.get("/account/grades")
    assert "8 / 10" in own.text
    assert "Clear notes." in own.text
    client.post("/logout", data={"csrf_token": csrf(own.text)})
    client.post(
        "/login",
        data={"csrf_token": csrf(client.get("/login").text), "email": "learner.two@gmail.com", "password": "Correcthorse1", "next": "/account/grades"},
        follow_redirects=False,
    )
    theirs = client.get("/account/grades")
    assert "Clear notes." not in theirs.text
    assert other["id"] != learner["id"]


def test_announcement_report_and_suspension(tmp_path: Path):
    client = client_for(tmp_path)
    register_and_verify(client, tmp_path, email="admin.test@gmail.com", name="Ada Admin")
    empty = client.get("/admin/reports")
    assert "No active enrollments are stored." in empty.text
    notice = client.get("/admin/announcements")
    saved = client.post(
        "/admin/announcements",
        data={"csrf_token": csrf(notice.text), "title": "Studio hours", "body": "The studio is open on Friday."},
        follow_redirects=False,
    )
    assert saved.status_code == 303
    home = client.get("/")
    assert "Studio hours" in home.text
    assert "The studio is open on Friday." in home.text
    posted = client.post(
        "/chat/messages",
        data={"csrf_token": csrf(client.get("/chat").text), "body": "Please review this message."},
        follow_redirects=False,
    )
    assert posted.status_code in {200, 303}
    chat = client.get("/admin/chat")
    assert "Please review this message." in chat.text
    message_id = chat.text.split('action="/admin/chat/')[1].split("/")[0]
    removed = client.post(
        f"/admin/chat/{message_id}/remove",
        data={"csrf_token": csrf(chat.text), "confirm": "yes"},
        follow_redirects=False,
    )
    assert removed.status_code == 303
    public = client.get("/api/chat/messages")
    assert "Please review this message." not in public.text
    assert "removed by a moderator" in public.text
    students = client.get("/admin/students?q=Ada")
    admin_id = students.text.split('href="/admin/students/')[1].split('"')[0]
    profile = client.get(f"/admin/students/{admin_id}")
    client.post(
        f"/admin/students/{admin_id}/status",
        data={"csrf_token": csrf(profile.text), "status": "suspended", "confirm": "yes"},
        follow_redirects=False,
    )
    client.post("/logout", data={"csrf_token": csrf(client.get("/login").text)})
    blocked = client.post(
        "/login",
        data={"csrf_token": csrf(client.get("/login").text), "email": "admin.test@gmail.com", "password": "Correcthorse1"},
    )
    assert "This account is suspended." in blocked.text


def test_payments_routes_reject_students(tmp_path: Path):
    client = client_for(tmp_path)
    register_and_verify(client, tmp_path, email="learner.one@gmail.com", name="Learner One")
    assert client.get("/admin/payments", follow_redirects=False).status_code == 303
    assert client.get("/api/admin/payments").status_code == 403
    assert client.get("/admin/payments.csv").status_code == 403


def test_admin_payments_use_real_orders(tmp_path: Path):
    client = client_for(tmp_path)
    register_and_verify(client, tmp_path, email="admin.test@gmail.com", name="Ada Admin")
    db = client.app.state.db
    db.create_order(db.get_user_by_canonical("admintest@gmail.com")["id"], "pro", 5500)
    page = client.get("/admin/payments")
    assert page.status_code == 200
    assert "Ada Admin" in page.text and "Stripe is not fully configured" in page.text
    api = client.get("/api/admin/payments?status=pending_payment")
    assert api.status_code == 200 and api.json()["orders"][0]["plan"] == "pro"
    export = client.get("/admin/payments.csv")
    assert export.headers["content-type"].startswith("text/csv")
    assert "order_id,student,email,plan" in export.text
