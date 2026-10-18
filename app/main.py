from __future__ import annotations

import hmac
import re
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.admin_panel import register_admin
from app.config import ROOT, Settings
from app.content import (
    FAQS,
    METHOD,
    PLAN_RANK,
    PLANS,
    TEAM,
    contact_methods,
    format_date,
    plan_by_slug,
    team_by_slug,
)
from app.db import Database
from app.google_oauth import GoogleOAuthError, authorization_url, exchange_code
from app.mailer import MailError, send_email
from app.payments import PaymentError, create_checkout, verify_webhook
from app.security import (
    RateLimiter,
    clean_display_name,
    clean_text,
    client_ip,
    expires_in,
    form_time_error,
    hash_password,
    new_token,
    parse_gmail,
    password_problems,
    safe_next,
    sign_form_time,
    token_hash,
    valid_display_name,
    is_configured_admin,
    verify_password,
)

SESSION_COOKIE = "nexora_session"
VIDEO_HOSTS = re.compile(
    r"^https://(?:www\.youtube\.com/watch\?v=|youtu\.be/|www\.youtube-nocookie\.com/embed/)([\w-]{6,})"
)
VIMEO = re.compile(r"^https://(?:www\.)?vimeo\.com/(?:video/)?(\d+)")


def decorate_course(db: Database, course: dict) -> dict:
    plan = plan_by_slug(course["plan_required"])
    item = dict(course)
    item["lesson_count"] = db.lesson_count(course["id"])
    item["plan_name"] = plan["name"] if plan else course["plan_required"]
    item["price"] = plan["price"] if plan else None
    return item


class Hub:
    def __init__(self) -> None:
        self.sockets: set[WebSocket] = set()

    async def broadcast(self, payload: dict) -> None:
        dead = []
        for socket in list(self.sockets):
            try:
                await socket.send_json(payload)
            except Exception:
                dead.append(socket)
        for socket in dead:
            self.sockets.discard(socket)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    app = FastAPI(title="Digicraft", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.db = Database(settings.database_path)
    app.state.hub = Hub()
    app.state.limiter = RateLimiter()
    templates = Jinja2Templates(directory=str(ROOT / "templates"))
    app.state.templates = templates
    app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")

    @app.middleware("http")
    async def session_middleware(request: Request, call_next):
        db: Database = request.app.state.db
        sid = request.cookies.get(SESSION_COOKIE)
        session = db.get_session(sid)
        created = False
        if session is None:
            session = db.create_session()
            created = True
        request.state.session = session
        request.state.session_created = created
        request.state.new_session_id = None
        request.state.clear_cookie = False
        user = db.get_user(session.get("user_id"))
        if user and user.get("status") == "suspended":
            user = None
        request.state.user = user
        response = await call_next(request)
        if request.state.clear_cookie:
            response.delete_cookie(SESSION_COOKIE, path="/")
        elif request.state.new_session_id:
            _set_cookie(response, request.state.new_session_id)
        elif created:
            _set_cookie(response, request.state.session["id"])
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; script-src 'self'; "
            "connect-src 'self' ws: wss:; frame-src https://www.youtube-nocookie.com https://player.vimeo.com; "
            "base-uri 'self'; form-action 'self'"
        )
        return response

    def _set_cookie(response, session_id: str) -> None:
        response.set_cookie(
            SESSION_COOKIE,
            session_id,
            httponly=True,
            samesite="lax",
            secure=settings.cookie_secure,
            max_age=60 * 60 * 24 * 14,
            path="/",
        )

    def render(request: Request, name: str, status_code: int = 200, **context):
        db: Database = request.app.state.db
        flash = db.pop_flash(request.state.session["id"])
        payload = {
            "user": request.state.user,
            "csrf": request.state.session["csrf_token"],
            "path": request.url.path,
            "flash": flash,
            "google_enabled": settings.google_enabled,
            "payments_ready": settings.payments_ready,
            "channels": contact_methods(settings),
            "is_admin": bool(
                request.state.user
                and request.state.user.get("email_verified")
                and is_configured_admin(request.state.user["canonical_email"], settings.admin_emails)
            ),
        }
        payload.update(context)
        return templates.TemplateResponse(request, name, payload, status_code=status_code)

    def redirect(request: Request, url: str, flash: str | None = None, status_code: int = 303):
        if flash:
            request.app.state.db.set_session_fields(request.state.session["id"], flash=flash)
        return RedirectResponse(url, status_code=status_code)

    def bind_user(request: Request, user_id: int) -> None:
        fresh = request.app.state.db.rotate_session(request.state.session["id"], user_id)
        request.state.session = fresh
        request.state.user = request.app.state.db.get_user(user_id)
        request.state.new_session_id = fresh["id"]

    async def form_data(request: Request):
        return await request.form()

    def csrf_ok(request: Request, form) -> bool:
        supplied = request.headers.get("x-csrf-token") or form.get("csrf_token") or ""
        expected = request.state.session["csrf_token"]
        return bool(supplied) and hmac.compare_digest(str(supplied), expected)

    def wants_json(request: Request) -> bool:
        accept = request.headers.get("accept", "")
        return "application/json" in accept or request.headers.get("x-requested-with") == "fetch"

    def issue_email(user: dict, purpose: str) -> str:
        token = new_token()
        hours = 24 if purpose == "verify" else 0
        minutes = 0 if purpose == "verify" else 60
        request_path = "/verify-email?token=" if purpose == "verify" else "/reset-password?token="
        app.state.db.store_token(
            user["id"],
            purpose,
            token_hash(token),
            expires_in(hours=hours or VERIFY_FALLBACK_HOURS(purpose), minutes=minutes),
        )
        link = f"{settings.base_url}{request_path}{quote(token)}"
        if purpose == "verify":
            subject = "Verify your Digicraft email"
            body = (
                "Confirm this Gmail address for your Digicraft account.\n\n"
                f"{link}\n\nThis link expires in 24 hours. "
                "If you did not create an account, you can ignore this message."
            )
        else:
            subject = "Reset your Digicraft password"
            body = (
                "A password reset was requested for your Digicraft account.\n\n"
                f"{link}\n\nThis link expires in 60 minutes and can be used once. "
                "If you did not ask for this, you can ignore this message."
            )
        return send_email(settings, user["email"], subject, body), link

    def verified(request: Request) -> bool:
        user = request.state.user
        return bool(user and user["email_verified"])

    def can_access_course(user: dict | None, course: dict) -> bool:
        if not user or not user["email_verified"] or not user.get("plan"):
            return False
        return PLAN_RANK.get(user["plan"], 0) >= PLAN_RANK.get(course["plan_required"], 99)

    @app.get("/")
    def home(request: Request, q: str = "", category: str = "", level: str = ""):
        db: Database = app.state.db
        snapshot = db.learning_snapshot(request.state.user["id"]) if verified(request) else None
        published = db.published_course_count()
        found = db.published_courses(q.strip(), category.strip(), level.strip()) if published else []
        return render(
            request,
            "home.html",
            title="Digicraft — Digital skills academy",
            description="Digicraft is a digital-skills academy with published courses, one-time plans, and a private chatroom. Results are not guaranteed.",
            plans=PLANS,
            method=METHOD,
            team=TEAM,
            faqs=FAQS,
            courses=[decorate_course(db, course) for course in found],
            categories=db.course_categories(),
            levels=db.course_levels(),
            query=q.strip(),
            category=category.strip(),
            level=level.strip(),
            has_courses=bool(published),
            snapshot=snapshot,
            announcements=db.announcements() if verified(request) else [],
        )

    @app.get("/courses")
    def courses(request: Request, q: str = "", category: str = "", level: str = ""):
        db: Database = app.state.db
        published = db.published_course_count()
        found = db.published_courses(q.strip(), category.strip(), level.strip()) if published else []
        continue_row = db.continue_lesson(request.state.user["id"]) if verified(request) else None
        return render(
            request,
            "courses.html",
            title="Courses — Digicraft",
            description="Browse published Digicraft courses. New courses are listed only when they are actually available.",
            courses=[decorate_course(db, course) for course in found],
            categories=db.course_categories(),
            levels=db.course_levels(),
            query=q.strip(),
            category=category.strip(),
            level=level.strip(),
            has_courses=bool(published),
            continue_row=continue_row,
            filter_action="/courses",
        )

    @app.get("/courses/{slug}")
    def course_detail(request: Request, slug: str):
        db: Database = app.state.db
        course = db.get_course(slug)
        if not course:
            return render(request, "not_found.html", status_code=404, title="Page not found — Digicraft", description="")
        lessons = db.lessons_for(course["id"])
        user = request.state.user
        enrollment = db.enrollment(user["id"], course["id"]) if user else None
        active = bool(enrollment and enrollment["status"] == "active")
        done = db.progress_map(user["id"], [lesson["id"] for lesson in lessons]) if active else {}
        completed = sum(1 for lesson in lessons if done.get(lesson["id"]))
        total = len(lessons)
        nxt = next((lesson for lesson in lessons if not done.get(lesson["id"])), None)
        return render(
            request,
            "course.html",
            title=f"{course['title']} — Digicraft",
            description=course["summary"],
            course=course,
            lessons=lessons,
            active=active,
            allowed=can_access_course(user, course),
            completed=completed,
            total=total,
            percent=int(round(100 * completed / total)) if total else 0,
            next_lesson=nxt,
            plan=plan_by_slug(course["plan_required"]),
        )

    @app.post("/courses/{slug}/enroll")
    async def enroll_course(request: Request, slug: str):
        form = await form_data(request)
        if not csrf_ok(request, form):
            return render(request, "message.html", status_code=403, title="Form expired", heading="Please refresh", text="The form expired. Go back and try again.", description="")
        course = app.state.db.get_course(slug)
        if not course:
            return render(request, "not_found.html", status_code=404, title="Page not found — Digicraft", description="")
        user = request.state.user
        if not user:
            return redirect(request, f"/login?next={quote('/courses/' + slug)}")
        if not user["email_verified"]:
            return redirect(request, "/account", "Verify your email before enrolling in a course.")
        if not can_access_course(user, course):
            return redirect(request, f"/enroll?plan={course['plan_required']}")
        app.state.db.enroll(user["id"], course["id"], "active")
        return redirect(request, f"/courses/{slug}", "You are enrolled. Your progress will be saved to this account.")

    @app.get("/courses/{slug}/lessons/{lesson_slug}")
    def lesson_page(request: Request, slug: str, lesson_slug: str):
        db: Database = app.state.db
        course = db.get_course(slug)
        lesson = db.get_lesson(course["id"], lesson_slug) if course else None
        if not course or not lesson:
            return render(request, "not_found.html", status_code=404, title="Page not found — Digicraft", description="")
        lessons = db.lessons_for(course["id"])
        index = next(i for i, item in enumerate(lessons) if item["id"] == lesson["id"])
        user = request.state.user
        enrollment = db.enrollment(user["id"], course["id"]) if user else None
        active = bool(enrollment and enrollment["status"] == "active")
        comments = db.comments(lesson["id"]) if active else []
        progress = db.progress_map(user["id"], [lesson["id"]]) if active else {}
        return render(
            request,
            "lesson.html",
            title=f"{lesson['title']} — Digicraft",
            description=lesson["summary"],
            course=course,
            lesson=lesson,
            active=active,
            comments=comments,
            completed=bool(progress.get(lesson["id"])),
            previous=lessons[index - 1] if index else None,
            nxt=lessons[index + 1] if index + 1 < len(lessons) else None,
            files=db.lesson_files(lesson["id"]) if active else [],
            embed=video_embed(lesson.get("video_url")) if active else None,
        )

    @app.post("/courses/{slug}/lessons/{lesson_slug}/progress")
    async def save_progress(request: Request, slug: str, lesson_slug: str):
        form = await form_data(request)
        if not csrf_ok(request, form):
            return _denied(request, 403, "The form expired. Refresh and try again.")
        db: Database = app.state.db
        course = db.get_course(slug)
        lesson = db.get_lesson(course["id"], lesson_slug) if course else None
        user = request.state.user
        if not course or not lesson:
            return _denied(request, 404, "That lesson was not found.")
        if not user:
            return _denied(request, 401, "Sign in to save progress.")
        if not user["email_verified"]:
            return _denied(request, 403, "Verify your email before saving progress.")
        enrollment = db.enrollment(user["id"], course["id"])
        if not enrollment or enrollment["status"] != "active":
            return _denied(request, 403, "Enroll in this course before saving progress.")
        db.set_progress(user["id"], lesson["id"], form.get("completed") == "1")
        if wants_json(request):
            return {"ok": True}
        return redirect(request, f"/courses/{slug}/lessons/{lesson_slug}", "Progress saved to your account.")

    @app.post("/courses/{slug}/lessons/{lesson_slug}/comments")
    async def add_comment(request: Request, slug: str, lesson_slug: str):
        form = await form_data(request)
        if not csrf_ok(request, form):
            return redirect(request, f"/courses/{slug}/lessons/{lesson_slug}", "The form expired. Try again.")
        db: Database = app.state.db
        course = db.get_course(slug)
        lesson = db.get_lesson(course["id"], lesson_slug) if course else None
        user = request.state.user
        if not course or not lesson or not user or not user["email_verified"]:
            return redirect(request, "/login?next=" + quote(f"/courses/{slug}/lessons/{lesson_slug}"))
        enrollment = db.enrollment(user["id"], course["id"])
        if not enrollment or enrollment["status"] != "active":
            return redirect(request, f"/courses/{slug}", "Join this course before posting in the discussion.")
        if not app.state.limiter.allow(f"comment:{user['id']}", 10, 600):
            return redirect(request, f"/courses/{slug}/lessons/{lesson_slug}", "Please wait before posting another comment.")
        body = clean_text(str(form.get("body") or ""), 1, 1000)
        if not body:
            return redirect(request, f"/courses/{slug}/lessons/{lesson_slug}", "Write a comment between 1 and 1000 characters.")
        db.add_comment(lesson["id"], user["id"], body)
        return redirect(request, f"/courses/{slug}/lessons/{lesson_slug}", "Your question was posted.")

    @app.get("/courses/{slug}/lessons/{lesson_slug}/files/{file_id}")
    def lesson_file(request: Request, slug: str, lesson_slug: str, file_id: int):
        db: Database = app.state.db
        course = db.get_course(slug)
        lesson = db.get_lesson(course["id"], lesson_slug) if course else None
        user = request.state.user
        if not course or not lesson or not user:
            return _denied(request, 401, "Sign in to download lesson files.")
        enrollment = db.enrollment(user["id"], course["id"])
        if not user["email_verified"] or not enrollment or enrollment["status"] != "active":
            return _denied(request, 403, "This file is available to enrolled members.")
        match = next((item for item in db.lesson_files(lesson["id"]) if item["id"] == file_id), None)
        if not match or not re.fullmatch(r"[A-Za-z0-9._-]+", match["filename"]):
            return _denied(request, 404, "That file was not found.")
        root = (settings.database_path.parent / "lesson_files" / str(lesson["id"])).resolve()
        path = (root / match["filename"]).resolve()
        if path.parent != root or not path.is_file():
            return _denied(request, 404, "That file was not found.")
        return FileResponse(path, filename=match["original_name"])

    @app.get("/faq")
    def faq(request: Request):
        return render(
            request,
            "faq.html",
            title="FAQ — Digicraft",
            description="Answers about Digicraft courses, progress, chat, and earnings. Results are not guaranteed.",
            faqs=FAQS,
        )

    @app.get("/consultation")
    def consultation(request: Request, member: str = ""):
        chosen = member if team_by_slug(member) else ""
        started = int(time.time())
        return render(
            request,
            "consultation.html",
            title="Free Consultation — Digicraft",
            description="Request free guidance from the Digicraft team. There is no consultation fee.",
            team=TEAM,
            channels=contact_methods(settings),
            selected=chosen,
            form_started=started,
            form_sig=sign_form_time(settings.secret_key, started),
            values={},
            errors=[],
        )

    @app.post("/consultation")
    async def submit_consultation(request: Request):
        form = await form_data(request)
        started = int(time.time())
        values = {
            "name": str(form.get("name") or ""),
            "email": str(form.get("email") or ""),
            "team_member": str(form.get("team_member") or ""),
            "contact_method": str(form.get("contact_method") or ""),
            "question": str(form.get("question") or ""),
            "preferred_time": str(form.get("preferred_time") or ""),
        }
        errors = []
        if not csrf_ok(request, form):
            errors.append("The form expired. Refresh the page and try again.")
        if form.get("company_website"):
            errors.append("The form could not be submitted.")
        timing = form_time_error(
            settings.secret_key,
            str(form.get("form_started") or ""),
            str(form.get("form_sig") or ""),
            settings.form_min_seconds,
        )
        if timing:
            errors.append(timing)
        name = clean_text(values["name"], 2, 80)
        if not name:
            errors.append("Enter your name, using 2 to 80 characters.")
        parsed = parse_gmail(values["email"])
        if not parsed:
            errors.append("Only verified personal @gmail.com addresses are accepted.")
        member = team_by_slug(values["team_member"])
        if not member:
            errors.append("Choose Abdul Raof or Naila M Ali.")
        channels = contact_methods(settings)
        configured = [item["key"] for item in channels if item["configured"]]
        method = values["contact_method"]
        if configured and method not in configured:
            errors.append("Choose one of the contact methods that is currently set up.")
        if not configured:
            method = "unspecified"
        question = clean_text(values["question"], 20, 2000)
        if not question:
            errors.append("Describe your question in 20 to 2000 characters.")
        preferred = clean_text(values["preferred_time"], 0, 120) if values["preferred_time"].strip() else ""
        if values["preferred_time"].strip() and preferred is None:
            errors.append("Keep the preferred time under 120 characters.")
        if form.get("consent") != "yes":
            errors.append("Consent is required before we can store this request.")
        ip = client_ip(request, settings.trust_proxy)
        if not app.state.limiter.allow(f"consult:{ip}", 5, 3600):
            errors.append("Too many requests from this network. Please try again later.")
        if errors:
            return render(
                request,
                "consultation.html",
                status_code=400,
                title="Free Consultation — Digicraft",
                description="Request free guidance from the Digicraft team.",
                team=TEAM,
                channels=channels,
                selected=values["team_member"],
                form_started=started,
                form_sig=sign_form_time(settings.secret_key, started),
                values=values,
                errors=errors,
            )
        request_id = app.state.db.create_consultation(
            name=name,
            email=parsed[1],
            team_member=member["slug"],
            contact_method=method,
            question=question,
            preferred_time=preferred or "",
        )
        if configured:
            note = "Your free consultation request was saved. The team can use the contact method you selected."
        else:
            note = (
                "Your free consultation request was saved. WhatsApp, Telegram, email, Zoom, and Google Meet "
                "are not configured yet, so nothing was sent automatically."
            )
        return redirect(request, f"/consultation?sent={request_id}", f"{note} Reference #{request_id}.")

    @app.get("/chat")
    def chat(request: Request):
        user = request.state.user
        messages = app.state.db.chat_messages() if verified(request) else []
        return render(
            request,
            "chat.html",
            title="Chatroom — Digicraft",
            description="Private community chat for verified Digicraft members.",
            messages=messages,
            can_read=verified(request),
        )

    @app.post("/chat/messages")
    async def post_message(request: Request):
        form = await form_data(request)
        if not csrf_ok(request, form):
            return _denied(request, 403, "The form expired. Refresh and try again.")
        user = request.state.user
        if not user:
            return _denied(request, 401, "Sign in to post in the chat room.")
        if not user["email_verified"]:
            return _denied(request, 403, "Verify your email before posting.")
        if not app.state.limiter.allow(f"chat:{user['id']}", 8, 60):
            return _denied(request, 429, "You are sending messages too quickly.")
        body = clean_text(str(form.get("body") or ""), 1, 1000)
        if not body:
            return _denied(request, 400, "Write a message between 1 and 1000 characters.")
        message = app.state.db.add_chat_message(user["id"], body)
        public = public_message(message)
        await app.state.hub.broadcast({"type": "message", "message": public})
        if wants_json(request):
            return {"ok": True, "message": public}
        return redirect(request, "/chat")

    @app.get("/api/chat/messages")
    def list_messages(request: Request, since: int = 0):
        user = request.state.user
        if not user:
            return JSONResponse({"error": "Sign in to read the chat room."}, status_code=401)
        if not user["email_verified"]:
            return JSONResponse({"error": "Verify your email before reading the chat room."}, status_code=403)
        rows = app.state.db.chat_messages(since=max(0, since))
        return {"messages": [public_message(row) for row in rows]}

    @app.post("/chat/reports")
    async def report_message(request: Request):
        form = await form_data(request)
        if not csrf_ok(request, form):
            return _denied(request, 403, "The form expired. Refresh and try again.")
        user = request.state.user
        if not verified(request):
            return _denied(request, 403, "Verified members can report a message.")
        reason = clean_text(str(form.get("reason") or ""), 10, 500)
        if not reason:
            return _denied(request, 400, "Describe the problem in 10 to 500 characters.")
        try:
            message_id = int(form.get("message_id") or 0)
        except ValueError:
            message_id = 0
        saved = app.state.db.report_message(message_id, user["id"], reason)
        if wants_json(request):
            return JSONResponse({"ok": saved}, status_code=200 if saved else 400)
        flash = "Report saved for review." if saved else "That message could not be reported."
        return redirect(request, "/chat", flash)

    @app.websocket("/ws/chat")
    async def chat_socket(websocket: WebSocket):
        await websocket.accept()
        session = app.state.db.get_session(websocket.cookies.get(SESSION_COOKIE))
        user = app.state.db.get_user(session.get("user_id")) if session and session.get("user_id") else None
        if not user or not user["email_verified"]:
            await websocket.send_json({"type": "error", "error": "Sign in with a verified account to use chat."})
            await websocket.close(code=4401)
            return
        app.state.hub.sockets.add(websocket)
        try:
            while True:
                payload = await websocket.receive_json()
                if payload.get("type") != "message":
                    continue
                if not app.state.limiter.allow(f"chat:{user['id']}", 8, 60):
                    await websocket.send_json({"type": "error", "error": "You are sending messages too quickly."})
                    continue
                body = clean_text(str(payload.get("body") or ""), 1, 1000)
                if not body:
                    await websocket.send_json({"type": "error", "error": "Write a message between 1 and 1000 characters."})
                    continue
                message = app.state.db.add_chat_message(user["id"], body)
                await app.state.hub.broadcast({"type": "message", "message": public_message(message)})
        except WebSocketDisconnect:
            app.state.hub.sockets.discard(websocket)
        except Exception:
            app.state.hub.sockets.discard(websocket)

    @app.get("/login")
    @app.get("/signup")
    def auth_page(request: Request, next: str = "/", mode: str = ""):
        if request.url.path == "/signup":
            mode = "signup"
        return render(
            request,
            "auth.html",
            title="Login — Digicraft",
            description="Sign in to Digicraft with Google or your Gmail address and Digicraft password.",
            mode="signup" if mode == "signup" else "login",
            next_url=safe_next(next),
            errors=[],
            email_value="",
            name_value="",
            auth_page=True,
        )

    @app.post("/login")
    async def login(request: Request):
        form = await form_data(request)
        email_value = str(form.get("email") or "")
        nxt = safe_next(str(form.get("next") or "/"))
        errors = []
        if not csrf_ok(request, form):
            errors.append("The form expired. Refresh and try again.")
        parsed = parse_gmail(email_value)
        if not parsed:
            errors.append("Only verified personal @gmail.com addresses are accepted.")
        ip = client_ip(request, settings.trust_proxy)
        if not app.state.limiter.allow(f"login:{ip}", 8, 900):
            errors.append("Too many sign-in attempts. Please wait and try again.")
        user = app.state.db.get_user_by_canonical(parsed[0]) if parsed and not errors else None
        password = str(form.get("password") or "")
        if not errors and (not user or not user.get("password_hash") or not verify_password(password, user.get("password_hash"))):
            errors.append("Those details don't match a Digicraft account.")
        if not errors and user and user.get("status") == "suspended":
            errors.append("This account is suspended.")
        if errors:
            return render(
                request,
                "auth.html",
                status_code=400,
                title="Login — Digicraft",
                description="Sign in to Digicraft.",
                mode="login",
                next_url=nxt,
                errors=errors,
                email_value=email_value,
                name_value="",
                auth_page=True,
            )
        bind_user(request, user["id"])
        if not user["email_verified"]:
            return redirect(request, "/account", "Confirm your email before using member features.")
        return redirect(request, nxt)

    @app.post("/signup")
    async def signup(request: Request):
        form = await form_data(request)
        email_value = str(form.get("email") or "")
        name_value = str(form.get("display_name") or "")
        nxt = safe_next(str(form.get("next") or "/account"))
        errors = []
        if not csrf_ok(request, form):
            errors.append("The form expired. Refresh and try again.")
        parsed = parse_gmail(email_value)
        if not parsed:
            errors.append("Only verified personal @gmail.com addresses are accepted.")
        if not valid_display_name(name_value):
            errors.append("Enter a display name of 2 to 40 characters, without an email address.")
        password = str(form.get("password") or "")
        if password != str(form.get("confirm") or ""):
            errors.append("The passwords do not match.")
        if parsed:
            errors.extend(password_problems(password, parsed[1]))
        ip = client_ip(request, settings.trust_proxy)
        if not app.state.limiter.allow(f"signup:{ip}", 5, 3600):
            errors.append("Too many new accounts from this network. Please try later.")
        if parsed and app.state.db.get_user_by_canonical(parsed[0]):
            errors.append("An account with this Gmail address already exists. Log in or reset the password.")
        if errors:
            return render(
                request,
                "auth.html",
                status_code=400,
                title="Sign up — Digicraft",
                description="Create a Digicraft account.",
                mode="signup",
                next_url=nxt,
                errors=errors,
                email_value=email_value,
                name_value=name_value,
                auth_page=True,
            )
        user = app.state.db.create_user(parsed[0], parsed[1], clean_display_name(name_value), hash_password(password), False, None)
        bind_user(request, user["id"])
        try:
            mode, _link = issue_email(user, "verify")
        except MailError:
            return redirect(request, "/account", "Your account was created, but the verification email could not be sent. Configure SMTP and use Resend verification.")
        if mode == "log":
            note = "Your account was created. SMTP is not configured, so the verification link was written to the server email log. Member features stay locked until you open that link."
        else:
            note = "Your account was created. Open the verification link sent to your Gmail address before using member features."
        return redirect(request, "/account", note)

    @app.post("/logout")
    async def logout(request: Request):
        form = await form_data(request)
        if csrf_ok(request, form):
            app.state.db.destroy_session(request.state.session["id"])
            request.state.clear_cookie = True
            request.state.user = None
        return RedirectResponse("/", status_code=303)

    @app.get("/forgot-password")
    def forgot(request: Request):
        return render(
            request,
            "forgot.html",
            title="Forgot password — Digicraft",
            description="Reset the password for your Digicraft account.",
            errors=[],
            email_value="",
            sent=False,
            auth_page=True,
        )

    @app.post("/forgot-password")
    async def send_reset(request: Request):
        form = await form_data(request)
        email_value = str(form.get("email") or "")
        errors = []
        if not csrf_ok(request, form):
            errors.append("The form expired. Refresh and try again.")
        parsed = parse_gmail(email_value)
        if not parsed:
            errors.append("Only verified personal @gmail.com addresses are accepted.")
        ip = client_ip(request, settings.trust_proxy)
        if not app.state.limiter.allow(f"reset:{ip}", 5, 3600):
            errors.append("Too many reset requests. Please try later.")
        if errors:
            return render(request, "forgot.html", status_code=400, title="Forgot password — Digicraft", description="", errors=errors, email_value=email_value, sent=False, auth_page=True)
        user = app.state.db.get_user_by_canonical(parsed[0])
        delivery = None
        if user:
            try:
                delivery, _link = issue_email(user, "reset")
            except MailError:
                return render(
                    request,
                    "forgot.html",
                    status_code=503,
                    title="Forgot password — Digicraft",
                    description="",
                    errors=["Email delivery is not configured, so the reset link could not be sent."],
                    email_value=email_value,
                    sent=False,
                    auth_page=True,
                )
        message = "If that Gmail address has a Digicraft account, password reset instructions were created."
        if delivery == "log":
            message += " SMTP is not configured, so the link was written to the server email log."
        return render(request, "forgot.html", title="Forgot password — Digicraft", description="", errors=[], email_value="", sent=True, info=message, auth_page=True)

    @app.get("/reset-password")
    def reset_form(request: Request, token: str = ""):
        return render(request, "reset.html", title="Reset password — Digicraft", description="", token=token, errors=[], auth_page=True)

    @app.post("/reset-password")
    async def reset_password(request: Request):
        form = await form_data(request)
        token = str(form.get("token") or "")
        errors = []
        if not csrf_ok(request, form):
            errors.append("The form expired. Refresh and try again.")
        row = app.state.db.find_token(token_hash(token), "reset") if token and not errors else None
        if not errors and not row:
            errors.append("This reset link is invalid or has already been used.")
        password = str(form.get("password") or "")
        user = app.state.db.get_user(row["user_id"]) if row else None
        full = app.state.db.get_user_by_canonical(user["canonical_email"]) if user else None
        if user and password != str(form.get("confirm") or ""):
            errors.append("The passwords do not match.")
        if user:
            errors.extend(password_problems(password, user["email"]))
        if errors:
            return render(request, "reset.html", status_code=400, title="Reset password — Digicraft", description="", token=token, errors=errors, auth_page=True)
        app.state.db.consume_token(token_hash(token), "reset")
        app.state.db.update_user(full["id"], password_hash=hash_password(password), email_verified=1)
        bind_user(request, full["id"])
        return redirect(request, "/account", "Your password was updated and your email is verified.")

    @app.get("/verify-email")
    def verify_email(request: Request, token: str = ""):
        row = app.state.db.consume_token(token_hash(token), "verify") if token else None
        if not row:
            return render(request, "message.html", status_code=400, title="Verification — Digicraft", description="", heading="Verification link not valid", text="This link is invalid or has already been used. Sign in and resend verification from your account page.", auth_page=True)
        app.state.db.update_user(row["user_id"], email_verified=1)
        bind_user(request, row["user_id"])
        return redirect(request, "/account", "Your email is verified. Member features are now available.")

    @app.post("/resend-verification")
    async def resend(request: Request):
        form = await form_data(request)
        if not csrf_ok(request, form) or not request.state.user:
            return redirect(request, "/login")
        user = request.state.user
        if user["email_verified"]:
            return redirect(request, "/account", "Your email is already verified.")
        if not app.state.limiter.allow(f"resend:{user['id']}", 3, 3600):
            return redirect(request, "/account", "Please wait before requesting another verification email.")
        full = app.state.db.get_user_by_canonical(user["canonical_email"])
        try:
            mode, _link = issue_email(full, "verify")
        except MailError:
            return redirect(request, "/account", "The verification email could not be sent because email delivery is not configured.")
        note = "A new verification link was written to the server email log because SMTP is not configured." if mode == "log" else "A new verification link was sent to your Gmail address."
        return redirect(request, "/account", note)

    @app.get("/auth/google")
    def google_start(request: Request, next: str = "/"):
        if not settings.google_enabled:
            return render(
                request,
                "google_setup.html",
                status_code=503,
                title="Google sign-in — Digicraft",
                description="Google sign-in requires OAuth credentials.",
                redirect_uri=settings.google_redirect_uri,
                auth_page=True,
            )
        state = new_token()
        app.state.db.set_session_fields(request.state.session["id"], oauth_state=state + "|" + safe_next(next))
        return RedirectResponse(authorization_url(settings, state), status_code=302)

    @app.get("/auth/google/callback")
    def google_callback(request: Request, code: str = "", state: str = "", error: str = ""):
        if not settings.google_enabled:
            return render(request, "google_setup.html", status_code=503, title="Google sign-in — Digicraft", description="", redirect_uri=settings.google_redirect_uri, auth_page=True)
        stored = request.state.session.get("oauth_state") or ""
        saved_state, _, nxt = stored.partition("|")
        app.state.db.set_session_fields(request.state.session["id"], oauth_state=None)
        if error or not code or not state or not hmac.compare_digest(state, saved_state):
            return render(request, "message.html", status_code=400, title="Google sign-in — Digicraft", description="", heading="Google sign-in was not completed", text="No Digicraft session was created. You can try again or use your Gmail address with a Digicraft password.", auth_page=True)
        try:
            profile = exchange_code(settings, code)
        except GoogleOAuthError:
            return render(request, "message.html", status_code=502, title="Google sign-in — Digicraft", description="", heading="Google did not complete sign-in", text="The OAuth exchange failed. Check the client ID, secret, and redirect URI, then try again.", auth_page=True)
        if not profile.get("email_verified"):
            return render(request, "message.html", status_code=400, title="Google sign-in — Digicraft", description="", heading="Google email is not verified", text="Digicraft only accepts a verified Google account.", auth_page=True)
        parsed = parse_gmail(str(profile.get("email") or ""))
        if not parsed:
            return render(request, "message.html", status_code=400, title="Google sign-in — Digicraft", description="", heading="A personal Gmail address is required", text="Only verified personal @gmail.com addresses are accepted.", auth_page=True)
        sub = str(profile.get("sub") or "")
        existing = app.state.db.get_user_by_google(sub) or app.state.db.get_user_by_canonical(parsed[0])
        if existing:
            app.state.db.update_user(existing["id"], google_sub=sub, email_verified=1, email=parsed[1])
            user_id = existing["id"]
        else:
            proposed = str(profile.get("name") or parsed[1].split("@")[0])
            display = clean_display_name(proposed) if valid_display_name(proposed) else "Digicraft member"
            created = app.state.db.create_user(parsed[0], parsed[1], display, None, True, sub)
            user_id = created["id"]
        chosen = app.state.db.get_user(user_id)
        if chosen and chosen.get("status") == "suspended":
            return redirect(request, "/login", "This account is suspended.")
        bind_user(request, user_id)
        return redirect(request, safe_next(nxt))

    @app.get("/account")
    def account(request: Request):
        if not request.state.user:
            return redirect(request, "/login?next=/account")
        return render(
            request,
            "account.html",
            title="Your account — Digicraft",
            description="Manage your Digicraft display name, photo, and plan requests.",
            orders=app.state.db.user_orders(request.state.user["id"]),
            plans={plan["slug"]: plan for plan in PLANS},
            learning=app.state.db.user_learning(request.state.user["id"]),
            errors=[],
        )

    @app.post("/account")
    async def update_account(request: Request):
        if not request.state.user:
            return redirect(request, "/login?next=/account")
        form = await form_data(request)
        if not csrf_ok(request, form):
            return redirect(request, "/account", "The form expired. Try again.")
        name = str(form.get("display_name") or "")
        if not valid_display_name(name):
            return render(request, "account.html", status_code=400, title="Your account — Digicraft", description="", orders=app.state.db.user_orders(request.state.user["id"]), plans={plan["slug"]: plan for plan in PLANS}, learning=app.state.db.user_learning(request.state.user["id"]), errors=["Enter a display name of 2 to 40 characters, without an email address."])
        app.state.db.update_user(request.state.user["id"], display_name=clean_display_name(name))
        request.state.user = app.state.db.get_user(request.state.user["id"])
        return redirect(request, "/account", "Display name saved.")

    @app.post("/account/avatar")
    async def upload_avatar(request: Request):
        user = request.state.user
        if not user:
            return redirect(request, "/login?next=/account")
        form = await form_data(request)
        if not csrf_ok(request, form):
            return redirect(request, "/account", "The form expired. Try again.")
        upload = form.get("avatar")
        if not upload or not getattr(upload, "filename", ""):
            return redirect(request, "/account", "Choose a PNG, JPEG, or WebP image.")
        data = await upload.read()
        mime = image_mime(data)
        if not mime or len(data) > 1_500_000:
            return redirect(request, "/account", "Use a PNG, JPEG, or WebP image under 1.5 MB.")
        folder = settings.database_path.parent / "avatars"
        folder.mkdir(parents=True, exist_ok=True)
        filename = f"{user['id']}.img"
        (folder / filename).write_bytes(data)
        app.state.db.update_user(user["id"], avatar_path=filename, avatar_mime=mime)
        return redirect(request, "/account", "Profile photo saved.")

    @app.post("/account/avatar/remove")
    async def remove_avatar(request: Request):
        user = request.state.user
        if not user or not csrf_ok(request, await form_data(request)):
            return redirect(request, "/account")
        if user.get("avatar_path"):
            path = settings.database_path.parent / "avatars" / user["avatar_path"]
            if path.is_file():
                path.unlink()
        app.state.db.update_user(user["id"], avatar_path=None, avatar_mime=None)
        return redirect(request, "/account", "Profile photo removed.")

    @app.get("/media/avatar/{user_id}")
    def avatar(request: Request, user_id: int):
        if not verified(request):
            return _denied(request, 403, "Verified members can view profile photos.")
        owner = app.state.db.get_user(user_id)
        if not owner or not owner.get("avatar_path"):
            return _denied(request, 404, "No profile photo is available.")
        root = (settings.database_path.parent / "avatars").resolve()
        path = (root / owner["avatar_path"]).resolve()
        if path.parent != root or not path.is_file():
            return _denied(request, 404, "No profile photo is available.")
        return FileResponse(path, media_type=owner.get("avatar_mime") or "application/octet-stream")

    @app.post("/account/delete")
    async def delete_account(request: Request):
        user = request.state.user
        form = await form_data(request)
        if not user or not csrf_ok(request, form):
            return redirect(request, "/login")
        full = app.state.db.get_user_by_canonical(user["canonical_email"])
        confirmed = str(form.get("confirm_delete") or "") == "DELETE"
        password_ok = full.get("password_hash") and verify_password(str(form.get("password") or ""), full.get("password_hash"))
        if not confirmed or (full.get("password_hash") and not password_ok):
            return redirect(request, "/account", "Account deletion was not confirmed.")
        if user.get("avatar_path"):
            path = settings.database_path.parent / "avatars" / user["avatar_path"]
            if path.is_file():
                path.unlink()
        app.state.db.delete_user(user["id"], user["email"])
        request.state.clear_cookie = True
        request.state.user = None
        return RedirectResponse("/", status_code=303)

    @app.get("/enroll")
    def enroll(request: Request, plan: str = ""):
        chosen = plan_by_slug(plan)
        if not chosen:
            return redirect(request, "/#pricing")
        if not request.state.user:
            return redirect(request, "/login?next=" + quote(f"/enroll?plan={chosen['slug']}"))
        if not request.state.user["email_verified"]:
            return redirect(request, "/account", "Verify your email before choosing a plan.")
        return render(
            request,
            "enroll.html",
            title=f"{chosen['name']} plan — Digicraft",
            description="Review a Digicraft plan. No charge is made unless Stripe confirms payment.",
            plan=chosen,
            missing_payment=not settings.payments_ready,
        )

    @app.post("/enroll")
    async def create_enrollment_order(request: Request):
        form = await form_data(request)
        if not csrf_ok(request, form):
            return redirect(request, "/#pricing", "The form expired. Try again.")
        chosen = plan_by_slug(str(form.get("plan") or ""))
        user = request.state.user
        if not chosen or not user:
            return redirect(request, "/login?next=/")
        if not user["email_verified"]:
            return redirect(request, "/account", "Verify your email before choosing a plan.")
        order = app.state.db.create_order(user["id"], chosen["slug"], chosen["price"])
        if not settings.payments_ready:
            return redirect(
                request,
                f"/enroll/return?order_id={order['id']}",
                "Your plan request was saved. No payment was taken because Stripe is not fully configured.",
            )
        try:
            session_id, url = create_checkout(settings, order["id"], chosen["price"], chosen["name"], user["email"])
        except PaymentError:
            return redirect(request, f"/enroll/return?order_id={order['id']}", "Stripe did not start checkout. No payment has been confirmed.")
        app.state.db.set_order_session(order["id"], session_id)
        return RedirectResponse(url, status_code=303)

    @app.get("/enroll/return")
    def enroll_return(request: Request, order_id: int = 0):
        user = request.state.user
        order = app.state.db.get_order(order_id) if user else None
        if not user or not order or order["user_id"] != user["id"]:
            return render(request, "not_found.html", status_code=404, title="Page not found — Digicraft", description="")
        return render(
            request,
            "enroll_return.html",
            title="Plan request — Digicraft",
            description="The status of your Digicraft plan request.",
            order=order,
            plan=plan_by_slug(order["plan"]),
        )

    @app.post("/payments/stripe/webhook")
    async def stripe_webhook(request: Request):
        if not settings.stripe_webhook_secret:
            return JSONResponse({"error": "Stripe webhook secret is not configured."}, status_code=503)
        payload = await request.body()
        try:
            event = verify_webhook(payload, request.headers.get("stripe-signature", ""), settings.stripe_webhook_secret)
        except PaymentError:
            return JSONResponse({"error": "Invalid Stripe signature."}, status_code=400)
        if event.get("type") == "checkout.session.completed":
            obj = event.get("data", {}).get("object", {})
            if obj.get("payment_status") == "paid":
                raw_id = (obj.get("metadata") or {}).get("order_id") or obj.get("client_reference_id") or 0
                try:
                    app.state.db.mark_order_paid(int(raw_id))
                except (TypeError, ValueError):
                    pass
        return {"received": True}

    @app.get("/resources")
    def resources(request: Request, category: str = ""):
        rows = app.state.db.list_resources(category.strip())
        for row in rows:
            row["published_label"] = format_date(row["published_on"])
        return render(request, "resources.html", title="Learning resources — Digicraft", description="Free step-by-step guides. No account is required.", articles=rows, category=category.strip())

    @app.get("/resources/{slug}")
    def resource(request: Request, slug: str):
        article = app.state.db.get_resource(slug)
        if not article:
            return render(request, "not_found.html", status_code=404, title="Page not found — Digicraft", description="")
        article["published_label"] = format_date(article["published_on"])
        article["updated_label"] = format_date(article["updated_on"]) if article.get("updated_on") else ""
        related = [item for item in app.state.db.latest_resources(4) if item["slug"] != slug][:3]
        for item in related:
            item["published_label"] = format_date(item["published_on"])
        return render(request, "article.html", title=f"{article['title']} — Digicraft", description=article["summary"], article=article, related=related)

    @app.get("/contact")
    def contact(request: Request):
        return render(request, "contact.html", title="Contact Us — Digicraft", description="Contact the Digicraft team through the free consultation form.", team=TEAM, channels=contact_methods(settings))

    @app.get("/privacy")
    def privacy(request: Request):
        return render(request, "privacy.html", title="Privacy Policy — Digicraft", description="How Digicraft handles account, learning, and consultation information.")

    @app.get("/terms")
    def terms(request: Request):
        return render(request, "terms.html", title="Terms — Digicraft", description="Terms for using Digicraft.")

    @app.get("/disclaimer")
    def disclaimer(request: Request):
        return render(request, "disclaimer.html", title="Disclaimer — Digicraft", description="Digicraft provides educational information. Income is not guaranteed.")

    @app.get("/editorial-policy")
    def editorial(request: Request):
        return render(request, "editorial.html", title="Editorial Policy — Digicraft", description="How Digicraft learning resources are written and corrected.")

    @app.get("/moderation")
    def moderation(request: Request):
        user = request.state.user
        if not user or not user.get("email_verified") or not is_configured_admin(user["canonical_email"], settings.admin_emails):
            return render(request, "not_found.html", status_code=404, title="Page not found — Digicraft", description="")
        return render(request, "moderation.html", title="Reports — Digicraft", description="", reports=app.state.db.reports_for_admin())

    @app.get("/health")
    def health():
        return {"ok": True}

    def _denied(request: Request, status: int, message: str):
        if wants_json(request) or request.url.path.startswith("/api/"):
            return JSONResponse({"error": message}, status_code=status)
        return render(request, "message.html", status_code=status, title="Digicraft", description="", heading="Unable to continue", text=message)

    app.state.render = render
    app.state.redirect = redirect
    app.state.csrf_ok = csrf_ok
    app.state.form_data = form_data
    app.state.denied = _denied
    register_admin(app)
    return app


def VERIFY_FALLBACK_HOURS(purpose: str) -> int:
    return 24 if purpose == "verify" else 0


def public_message(row: dict) -> dict:
    return {
        "id": row["id"],
        "body": row["body"],
        "created_at": row["created_at"],
        "author": row["display_name"],
        "author_id": row["author_id"],
        "has_avatar": bool(row["has_avatar"]),
    }


def video_embed(url: str | None) -> str | None:
    if not url:
        return None
    youtube = VIDEO_HOSTS.match(url)
    if youtube:
        return f"https://www.youtube-nocookie.com/embed/{youtube.group(1)}"
    vimeo = VIMEO.match(url)
    if vimeo:
        return f"https://player.vimeo.com/video/{vimeo.group(1)}"
    return None


def image_mime(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) > 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


app = create_app()
