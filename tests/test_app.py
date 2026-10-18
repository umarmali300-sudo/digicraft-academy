import hashlib
import hmac
import json
import re
import time
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, html[:500]
    return match.group(1)


def client_for(tmp_path: Path, **changes) -> TestClient:
    settings = replace(Settings.for_tests(tmp_path), **changes)
    return TestClient(create_app(settings))


def register_and_verify(client: TestClient, tmp_path: Path, email="learner.one@gmail.com", name="Learner One"):
    page = client.get("/signup")
    response = client.post(
        "/signup",
        data={
            "csrf_token": csrf(page.text),
            "display_name": name,
            "email": email,
            "password": "Correcthorse1",
            "confirm": "Correcthorse1",
            "next": "/account",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    log = (tmp_path / "email.log").read_text(encoding="utf-8")
    token = re.findall(r"verify-email\?token=([^\s]+)", log)[-1]
    verified = client.get(f"/verify-email?token={token}", follow_redirects=False)
    assert verified.status_code == 303
    return email


def test_home_navigation_and_empty_courses(tmp_path):
    client = client_for(tmp_path)
    home = client.get("/")
    assert home.status_code == 200
    assert "Build digital skills you can" in home.text
    assert "practice immediately" in home.text
    assert "New courses are being prepared. Check back soon." in home.text
    nav = re.search(r'<nav id="site-nav".*?>(.*?)</nav>', home.text, re.S).group(1)
    labels = re.findall(r">([^<]+)</a>", nav)
    assert labels == ["Home", "Courses", "Pricing", "About Us", "FAQ", "Chatroom"]
    assert 'href="#"' not in home.text
    courses = client.get("/courses")
    assert "New courses are being prepared. Check back soon." in courses.text
    faq = client.get("/faq")
    assert faq.text.count("<details") == 12
    assert "are not guaranteed" in faq.text
    assert "No refund or cancellation policy is published." in faq.text


def test_course_filters_match_real_rows(tmp_path):
    client = client_for(tmp_path)
    db = client.app.state.db
    db._run(
        """INSERT INTO courses (slug, title, summary, level, category, plan_required, status, created_at)
           VALUES ('signal-course', 'Signal Course', 'A published beginner course', 'Beginner', 'Freelancing', 'starter', 'published', '2026-10-15')"""
    )
    db._run(
        """INSERT INTO courses (slug, title, summary, level, category, plan_required, status, created_at)
           VALUES ('vector-course', 'Vector Course', 'A published advanced course', 'Advanced', 'Design', 'pro', 'published', '2026-10-16')"""
    )
    filtered = client.get("/courses?level=Beginner&category=Freelancing")
    assert "Signal Course" in filtered.text
    assert "Vector Course" not in filtered.text
    home = client.get("/?q=Vector")
    assert "Vector Course" in home.text
    assert "Signal Course" not in home.text
    assert "PKR 5,500" in home.text


def test_articles_open(tmp_path):
    client = client_for(tmp_path)
    page = client.get("/resources/fiverr-gig-seo-rank-first-page-2026")
    assert page.status_code == 200
    assert "How Fiverr search is generally understood" in page.text
    assert "not guaranteed" in page.text.lower() or "not guarantee" in page.text.lower()


def test_gmail_signup_is_rejected(tmp_path):
    client = client_for(tmp_path)
    page = client.get("/signup")
    response = client.post(
        "/signup",
        data={
            "csrf_token": csrf(page.text),
            "display_name": "Learner",
            "email": "person@yahoo.com",
            "password": "Correcthorse1",
            "confirm": "Correcthorse1",
        },
    )
    assert response.status_code == 400
    assert "@gmail.com" in response.text


def test_verification_chat_and_privacy(tmp_path):
    client = client_for(tmp_path)
    register_and_verify(client, tmp_path)
    denied = client_for(tmp_path / "other")
    secret = "private-note-should-stay-hidden"
    page = client.get("/chat")
    posted = client.post(
        "/chat/messages",
        data={"csrf_token": csrf(page.text), "body": secret},
        headers={"Accept": "application/json", "X-Requested-With": "fetch"},
    )
    assert posted.status_code == 200
    assert "email" not in posted.json()["message"]
    assert posted.json()["message"]["author"] == "Learner One"
    blocked = denied.get("/api/chat/messages")
    assert blocked.status_code == 401
    assert secret not in blocked.text
    hostile = "<script>alert(1)</script>"
    page = client.get("/chat")
    client.post("/chat/messages", data={"csrf_token": csrf(page.text), "body": hostile})
    room = client.get("/chat")
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in room.text


def test_unverified_user_cannot_read_chat(tmp_path):
    client = client_for(tmp_path)
    page = client.get("/signup")
    client.post(
        "/signup",
        data={
            "csrf_token": csrf(page.text),
            "display_name": "Pending User",
            "email": "pending.user@gmail.com",
            "password": "Correcthorse1",
            "confirm": "Correcthorse1",
        },
        follow_redirects=True,
    )
    response = client.get("/api/chat/messages")
    assert response.status_code == 403


def test_consultation_saves_and_rejects_bad_email(tmp_path):
    client = client_for(tmp_path)
    page = client.get("/consultation")
    started = re.search(r'name="form_started" value="([^"]+)"', page.text).group(1)
    signature = re.search(r'name="form_sig" value="([^"]+)"', page.text).group(1)
    bad = client.post(
        "/consultation",
        data={
            "csrf_token": csrf(page.text),
            "form_started": started,
            "form_sig": signature,
            "name": "A",
            "email": "nope@example.com",
            "team_member": "abdul-raof",
            "question": "short",
        },
    )
    assert bad.status_code == 400
    ok = client.post(
        "/consultation",
        data={
            "csrf_token": csrf(page.text),
            "form_started": started,
            "form_sig": signature,
            "name": "Mahammad",
            "email": "student.name@gmail.com",
            "team_member": "naila-m-ali",
            "contact_method": "unspecified",
            "question": "I want help choosing a first skill to study.",
            "consent": "yes",
        },
        follow_redirects=False,
    )
    assert ok.status_code == 303
    followed = client.get("/consultation")
    assert "request was saved" in followed.text
    assert "No payment" not in followed.text or "free" in page.text


def test_plan_request_does_not_mark_paid(tmp_path):
    client = client_for(tmp_path)
    register_and_verify(client, tmp_path, email="payer.one@gmail.com", name="Payer One")
    page = client.get("/enroll?plan=pro")
    response = client.post(
        "/enroll",
        data={"csrf_token": csrf(page.text), "plan": "pro"},
        follow_redirects=True,
    )
    assert "pending payment" in response.text
    assert "No payment has been confirmed" in response.text
    account = client.get("/account")
    assert "No paid plan is active" in account.text


def test_progress_is_private_and_persists(tmp_path):
    client = client_for(tmp_path)
    register_and_verify(client, tmp_path, email="student.progress@gmail.com", name="Progress User")
    db = client.app.state.db
    db._run(
        """INSERT INTO courses (slug, title, summary, level, category, plan_required, status, created_at)
           VALUES ('foundations', 'Foundations', 'A real test course', 'Beginner', 'Freelancing', 'starter', 'published', '2026-10-15')"""
    )
    course = db.get_course("foundations")
    db._run(
        """INSERT INTO lessons (course_id, slug, title, position, summary, body)
           VALUES (?, 'welcome', 'Welcome', 1, 'Start here', 'Lesson notes')""",
        (course["id"],),
    )
    db.update_user(client.app.state.db.get_user_by_canonical("studentprogress@gmail.com")["id"], plan="starter")
    user = db.get_user_by_canonical("studentprogress@gmail.com")
    page = client.get("/courses/foundations")
    enrolled = client.post("/courses/foundations/enroll", data={"csrf_token": csrf(page.text)}, follow_redirects=True)
    assert "You are enrolled" in enrolled.text
    lesson = client.get("/courses/foundations/lessons/welcome")
    saved = client.post(
        "/courses/foundations/lessons/welcome/progress",
        data={"csrf_token": csrf(lesson.text), "completed": "1"},
        follow_redirects=True,
    )
    assert "Progress saved" in saved.text
    other = TestClient(client.app)
    register_and_verify(other, tmp_path, email="other.person@gmail.com", name="Other Person")
    blocked = other.post(
        "/courses/foundations/lessons/welcome/progress",
        data={"csrf_token": csrf(other.get("/login").text), "completed": "1"},
        headers={"Accept": "application/json"},
    )
    assert blocked.status_code in {401, 403}
    assert db.progress_map(user["id"], [db.get_lesson(course["id"], "welcome")["id"]])[db.get_lesson(course["id"], "welcome")["id"]] is True


def test_password_reset_and_google_setup(tmp_path):
    client = client_for(tmp_path)
    register_and_verify(client, tmp_path, email="reset.me@gmail.com", name="Reset Me")
    client.post("/logout", data={"csrf_token": csrf(client.get("/").text)})
    forgot = client.get("/forgot-password")
    sent = client.post("/forgot-password", data={"csrf_token": csrf(forgot.text), "email": "reset.me@gmail.com"})
    assert sent.status_code == 200
    token = re.findall(r"reset-password\?token=([^\s]+)", (tmp_path / "email.log").read_text(encoding="utf-8"))[-1]
    form = client.get(f"/reset-password?token={token}")
    updated = client.post(
        "/reset-password",
        data={"csrf_token": csrf(form.text), "token": token, "password": "Newpassword9", "confirm": "Newpassword9"},
        follow_redirects=False,
    )
    assert updated.status_code == 303
    google = client.get("/auth/google")
    assert google.status_code == 503
    assert "GOOGLE_CLIENT_ID" in google.text
    assert "No account was created" in google.text


def test_stripe_webhook_marks_only_signed_payment(tmp_path):
    secret = "whsec_test_secret"
    client = client_for(tmp_path, stripe_secret_key="sk_test", stripe_webhook_secret=secret)
    register_and_verify(client, tmp_path, email="stripe.user@gmail.com", name="Stripe User")
    user = client.app.state.db.get_user_by_canonical("stripeuser@gmail.com")
    order = client.app.state.db.create_order(user["id"], "starter", 2700)
    unsigned = client.post("/payments/stripe/webhook", content=b"{}", headers={"stripe-signature": "t=1,v1=bad"})
    assert unsigned.status_code == 400
    payload = json.dumps(
        {"type": "checkout.session.completed", "data": {"object": {"payment_status": "paid", "metadata": {"order_id": str(order["id"])}}}}
    ).encode()
    stamp = str(int(time.time()))
    signature = hmac.new(secret.encode(), f"{stamp}.".encode() + payload, hashlib.sha256).hexdigest()
    paid = client.post("/payments/stripe/webhook", content=payload, headers={"stripe-signature": f"t={stamp},v1={signature}"})
    assert paid.status_code == 200
    assert client.app.state.db.get_order(order["id"])["status"] == "paid"
    assert client.app.state.db.get_user(user["id"])["plan"] == "starter"


def test_open_redirect_is_blocked(tmp_path):
    client = client_for(tmp_path)
    page = client.get("/login?next=https://evil.example")
    assert 'value="/"' in page.text or 'name="next" value="/"' in page.text
