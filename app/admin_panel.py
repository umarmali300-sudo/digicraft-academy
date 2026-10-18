from __future__ import annotations

import csv
import io
import re
import secrets
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from app.security import is_configured_admin, iso, parse_iso

ALLOWED_EXTENSIONS = {"pdf", "txt", "png", "jpg", "jpeg", "docx", "zip"}
MAX_SUBMISSION_BYTES = 5_000_000
LEVELS = {"Beginner", "Intermediate", "Advanced"}
PLANS = {"starter", "pro", "premium"}


def register_admin(app) -> None:
    def admin_user(request: Request):
        user = request.state.user
        settings = request.app.state.settings
        if not user or user.get("status") == "suspended" or not user.get("email_verified"):
            return None
        if not is_configured_admin(user["canonical_email"], settings.admin_emails):
            return None
        return user

    def page_guard(request: Request):
        if not request.state.user:
            return request.app.state.redirect(request, "/login?next=" + request.url.path)
        if not admin_user(request):
            return request.app.state.redirect(request, "/account", "That page is not available.")
        return None

    def api_guard(request: Request):
        if not request.state.user:
            return JSONResponse({"error": "Sign in required."}, status_code=401)
        if not admin_user(request):
            return JSONResponse({"error": "Admin authorization required."}, status_code=403)
        return None

    def render_admin(request: Request, section: str, **context):
        return request.app.state.render(
            request,
            "admin.html",
            title=f"{section.title()} — Admin — Digicraft",
            description="Digicraft academy administration.",
            section=section,
            **context,
        )

    @app.get("/admin")
    def overview(request: Request):
        blocked = page_guard(request)
        if blocked:
            return blocked
        return render_admin(request, "overview", stats=request.app.state.db.admin_overview())

    @app.get("/api/admin/students")
    def students_api(request: Request, q: str = "", sort: str = "name"):
        blocked = api_guard(request)
        if blocked:
            return blocked
        return {"students": request.app.state.db.admin_students(q, sort)}

    @app.get("/admin/students")
    def students(request: Request, q: str = "", sort: str = "name"):
        blocked = page_guard(request)
        if blocked:
            return blocked
        return render_admin(request, "students", students=request.app.state.db.admin_students(q, sort), query=q, sort=sort)

    @app.get("/admin/students/{user_id}")
    def student(request: Request, user_id: int):
        blocked = page_guard(request)
        if blocked:
            return blocked
        profile = request.app.state.db.student_profile(user_id)
        if not profile:
            return request.app.state.denied(request, 404, "That student was not found.")
        return render_admin(request, "student", profile=profile, courses=request.app.state.db._all("SELECT id, title FROM courses ORDER BY title"))

    @app.post("/admin/students/{user_id}/status")
    async def student_status(request: Request, user_id: int):
        blocked = page_guard(request)
        if blocked:
            return blocked
        form = await request.app.state.form_data(request)
        if not request.app.state.csrf_ok(request, form):
            return request.app.state.redirect(request, f"/admin/students/{user_id}", "The form expired. Try again.")
        status = str(form.get("status") or "")
        if status not in {"active", "suspended"} or str(form.get("confirm") or "") != "yes":
            return request.app.state.redirect(request, f"/admin/students/{user_id}", "Confirm the status change before it is saved.")
        if not request.app.state.db.student_profile(user_id):
            return request.app.state.denied(request, 404, "That student was not found.")
        request.app.state.db.set_user_status(user_id, status)
        return request.app.state.redirect(request, f"/admin/students/{user_id}", "Student status saved.")

    @app.get("/admin/courses")
    def courses(request: Request):
        blocked = page_guard(request)
        if blocked:
            return blocked
        db = request.app.state.db
        return render_admin(request, "courses", courses=db._all("SELECT * FROM courses ORDER BY title"), lessons=db._all("SELECT l.*, c.title AS course_title FROM lessons l JOIN courses c ON c.id = l.course_id ORDER BY c.title, l.position"))

    @app.post("/admin/courses")
    async def save_course(request: Request):
        blocked = page_guard(request)
        if blocked:
            return blocked
        form = await request.app.state.form_data(request)
        if not request.app.state.csrf_ok(request, form):
            return request.app.state.redirect(request, "/admin/courses", "The form expired. Try again.")
        title = " ".join(str(form.get("title") or "").split())
        summary = " ".join(str(form.get("summary") or "").split())
        level = str(form.get("level") or "")
        category = " ".join(str(form.get("category") or "").split())
        plan = str(form.get("plan_required") or "")
        status = str(form.get("status") or "draft")
        if not (2 <= len(title) <= 80 and 10 <= len(summary) <= 400 and level in LEVELS and 2 <= len(category) <= 40 and plan in PLANS and status in {"draft", "published"}):
            return request.app.state.redirect(request, "/admin/courses", "Check the course title, summary, level, category, plan, and status.")
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60] or "course"
        existing = request.app.state.db._one("SELECT id FROM courses WHERE slug = ?", (slug,))
        if existing:
            slug = f"{slug}-{existing['id'] + 1}"
        request.app.state.db.save_course(None, slug, title, summary, level, category, plan, status)
        return request.app.state.redirect(request, "/admin/courses", "Course saved.")

    @app.post("/admin/courses/{course_id}/status")
    async def course_status(request: Request, course_id: int):
        blocked = page_guard(request)
        if blocked:
            return blocked
        form = await request.app.state.form_data(request)
        if not request.app.state.csrf_ok(request, form):
            return request.app.state.redirect(request, "/admin/courses", "The form expired. Try again.")
        status = str(form.get("status") or "")
        course = request.app.state.db._one("SELECT * FROM courses WHERE id = ?", (course_id,))
        if not course or status not in {"draft", "published"}:
            return request.app.state.denied(request, 404, "That course was not found.")
        request.app.state.db.save_course(course_id, course["slug"], course["title"], course["summary"], course["level"], course["category"], course["plan_required"], status)
        return request.app.state.redirect(request, "/admin/courses", "Course visibility saved.")

    @app.post("/admin/courses/{course_id}/lessons")
    async def add_lesson(request: Request, course_id: int):
        blocked = page_guard(request)
        if blocked:
            return blocked
        form = await request.app.state.form_data(request)
        if not request.app.state.csrf_ok(request, form):
            return request.app.state.redirect(request, "/admin/courses", "The form expired. Try again.")
        title = " ".join(str(form.get("title") or "").split())
        summary = " ".join(str(form.get("summary") or "").split())
        body = str(form.get("body") or "").strip()
        if not request.app.state.db._one("SELECT id FROM courses WHERE id = ?", (course_id,)):
            return request.app.state.denied(request, 404, "That course was not found.")
        if not (2 <= len(title) <= 80 and 10 <= len(summary) <= 400 and 20 <= len(body) <= 8000):
            return request.app.state.redirect(request, "/admin/courses", "Check the lesson title, summary, and lesson text.")
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60] or "lesson"
        request.app.state.db.save_lesson(course_id, title, slug, summary, body)
        return request.app.state.redirect(request, "/admin/courses", "Lesson saved.")

    @app.post("/admin/courses/{course_id}/enroll")
    async def enroll(request: Request, course_id: int):
        blocked = page_guard(request)
        if blocked:
            return blocked
        form = await request.app.state.form_data(request)
        if not request.app.state.csrf_ok(request, form):
            return request.app.state.redirect(request, "/admin/courses", "The form expired. Try again.")
        try:
            user_id = int(form.get("user_id") or 0)
        except ValueError:
            user_id = 0
        action = str(form.get("enrollment") or "")
        if action not in {"active", "withdrawn"} or not request.app.state.db.student_profile(user_id):
            return request.app.state.redirect(request, "/admin/courses", "Choose a student and an enrollment action.")
        if not request.app.state.db._one("SELECT id FROM courses WHERE id = ?", (course_id,)):
            return request.app.state.denied(request, 404, "That course was not found.")
        request.app.state.db.set_enrollment(user_id, course_id, action)
        return request.app.state.redirect(request, f"/admin/students/{user_id}", "Enrollment saved.")

    @app.get("/admin/assignments")
    def assignments(request: Request):
        blocked = page_guard(request)
        if blocked:
            return blocked
        db = request.app.state.db
        return render_admin(request, "assignments", assignments=db.assignments(), courses=db._all("SELECT id, title FROM courses ORDER BY title"), lessons=db._all("SELECT id, course_id, title FROM lessons ORDER BY position"))

    @app.post("/admin/assignments")
    async def create_assignment(request: Request):
        blocked = page_guard(request)
        if blocked:
            return blocked
        form = await request.app.state.form_data(request)
        if not request.app.state.csrf_ok(request, form):
            return request.app.state.redirect(request, "/admin/assignments", "The form expired. Try again.")
        title = " ".join(str(form.get("title") or "").split())
        instructions = str(form.get("instructions") or "").strip()
        try:
            course_id = int(form.get("course_id") or 0)
            max_points = int(form.get("max_points") or 0)
            lesson_raw = str(form.get("lesson_id") or "").strip()
            lesson_id = int(lesson_raw) if lesson_raw else None
        except ValueError:
            return request.app.state.redirect(request, "/admin/assignments", "Choose a course and a whole-number grading scale.")
        formats = [item.strip().lower().lstrip(".") for item in str(form.get("formats") or "").split(",") if item.strip()]
        deadline_raw = str(form.get("deadline") or "").strip()
        deadline = ""
        if deadline_raw:
            try:
                deadline = iso(parse_iso(deadline_raw))
            except ValueError:
                return request.app.state.redirect(request, "/admin/assignments", "Enter a valid deadline or leave it empty.")
        if not (2 <= len(title) <= 80 and 10 <= len(instructions) <= 4000 and 1 <= max_points <= 100 and formats and set(formats) <= ALLOWED_EXTENSIONS):
            return request.app.state.redirect(request, "/admin/assignments", "Check the title, instructions, grading scale, and file types.")
        if not request.app.state.db._one("SELECT id FROM courses WHERE id = ?", (course_id,)):
            return request.app.state.redirect(request, "/admin/assignments", "Choose a course that exists.")
        request.app.state.db.create_assignment(course_id, lesson_id, title, instructions, deadline, ",".join(formats), max_points)
        return request.app.state.redirect(request, "/admin/assignments", "Assignment saved.")

    @app.get("/admin/submissions")
    def submissions(request: Request):
        blocked = page_guard(request)
        if blocked:
            return blocked
        return render_admin(request, "submissions", submissions=request.app.state.db.submissions())

    @app.post("/admin/submissions/{submission_id}/review")
    async def review(request: Request, submission_id: int):
        blocked = page_guard(request)
        if blocked:
            return blocked
        form = await request.app.state.form_data(request)
        if not request.app.state.csrf_ok(request, form):
            return request.app.state.redirect(request, "/admin/submissions", "The form expired. Try again.")
        row = request.app.state.db.submission(submission_id)
        if not row:
            return request.app.state.denied(request, 404, "That submission was not found.")
        status = str(form.get("status") or "")
        feedback = str(form.get("feedback") or "").strip()
        grade_raw = str(form.get("grade") or "").strip()
        if status not in {"pending", "reviewed", "resubmit"} or not (1 <= len(feedback) <= 2000):
            return request.app.state.redirect(request, "/admin/submissions", "Choose a review status and write feedback.")
        grade = None
        if status == "reviewed":
            try:
                grade = int(grade_raw)
            except ValueError:
                grade = -1
            if grade < 0 or grade > row["max_points"]:
                return request.app.state.redirect(request, "/admin/submissions", f"Enter a mark from 0 to {row['max_points']}.")
        request.app.state.db.review_submission(submission_id, status, grade, feedback)
        return request.app.state.redirect(request, "/admin/submissions", "Review saved.")

    @app.get("/admin/submissions/{submission_id}/file")
    def submission_file(request: Request, submission_id: int):
        user = admin_user(request)
        row = request.app.state.db.submission(submission_id)
        if not row:
            return request.app.state.denied(request, 404, "That submission was not found.")
        if not user and not (request.state.user and request.state.user["id"] == row["user_id"] and request.state.user.get("status") != "suspended"):
            if not request.state.user:
                return JSONResponse({"error": "Sign in required."}, status_code=401)
            return JSONResponse({"error": "You cannot open another student's submission."}, status_code=403)
        path = _submission_path(request, row["stored_name"])
        if path is None or not path.is_file():
            return request.app.state.denied(request, 404, "The submitted file is not available.")
        return Response(path.read_bytes(), media_type="application/octet-stream", headers={"Content-Disposition": f'attachment; filename="{_safe_filename(row["original_name"])}"'})

    @app.get("/admin/chat")
    def chat(request: Request):
        blocked = page_guard(request)
        if blocked:
            return blocked
        db = request.app.state.db
        return render_admin(request, "chat", messages=db.admin_chat(), requests=db.consultations_recent())

    @app.post("/admin/chat/{message_id}/remove")
    async def remove_message(request: Request, message_id: int):
        blocked = page_guard(request)
        if blocked:
            return blocked
        form = await request.app.state.form_data(request)
        if not request.app.state.csrf_ok(request, form) or str(form.get("confirm") or "") != "yes":
            return request.app.state.redirect(request, "/admin/chat", "Confirm before removing a message.")
        if not request.app.state.db.remove_chat_message(message_id, admin_user(request)["id"]):
            return request.app.state.redirect(request, "/admin/chat", "That message was not removed.")
        return request.app.state.redirect(request, "/admin/chat", "Message removed and the action was recorded.")

    @app.get("/admin/announcements")
    def announcements(request: Request):
        blocked = page_guard(request)
        if blocked:
            return blocked
        return render_admin(request, "announcements", announcements=request.app.state.db.announcements())

    @app.post("/admin/announcements")
    async def create_announcement(request: Request):
        blocked = page_guard(request)
        if blocked:
            return blocked
        form = await request.app.state.form_data(request)
        if not request.app.state.csrf_ok(request, form):
            return request.app.state.redirect(request, "/admin/announcements", "The form expired. Try again.")
        title = " ".join(str(form.get("title") or "").split())
        body = str(form.get("body") or "").strip()
        if not (2 <= len(title) <= 80 and 10 <= len(body) <= 2000):
            return request.app.state.redirect(request, "/admin/announcements", "Check the announcement title and message.")
        request.app.state.db.create_announcement(admin_user(request)["id"], title, body)
        return request.app.state.redirect(request, "/admin/announcements", "Announcement saved for signed-in students.")

    @app.get("/admin/reports")
    def reports(request: Request):
        blocked = page_guard(request)
        if blocked:
            return blocked
        db = request.app.state.db
        return render_admin(
            request,
            "reports",
            progress=db.course_progress_report(),
            grades=db.submissions(),
            enrollment=db.enrollment_report(),
            activity=db.activity_by_day(),
            overdue=db._all(
                """SELECT a.title, a.deadline, u.display_name
                   FROM assignments a
                   JOIN enrollments e ON e.course_id = a.course_id AND e.status = 'active'
                   JOIN users u ON u.id = e.user_id
                   LEFT JOIN submissions s ON s.assignment_id = a.id AND s.user_id = u.id
                   WHERE a.published = 1 AND a.deadline IS NOT NULL AND a.deadline < ? AND s.id IS NULL
                   ORDER BY a.deadline""",
                (iso(),),
            ),
        )

    @app.get("/admin/reports.csv")
    def reports_csv(request: Request):
        blocked = api_guard(request)
        if blocked:
            return blocked
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["course", "student", "completed_lessons", "total_lessons"])
        for row in request.app.state.db.course_progress_report():
            writer.writerow([row["course"], row["student"], row["completed"], row["lessons"]])
        return Response(buffer.getvalue(), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="course-progress.csv"'})

    @app.get("/admin/settings")
    def settings_page(request: Request):
        blocked = page_guard(request)
        if blocked:
            return blocked
        return render_admin(request, "settings", admin_count=len(request.app.state.settings.admin_emails))

    @app.get("/account/grades")
    def grades(request: Request):
        user = request.state.user
        if not user:
            return request.app.state.redirect(request, "/login?next=/account/grades")
        if not user["email_verified"]:
            return request.app.state.redirect(request, "/account", "Verify your email before viewing grades.")
        return request.app.state.render(
            request,
            "grades.html",
            title="Your grades — Digicraft",
            description="Grades and feedback for your own submissions.",
            assignments=request.app.state.db.published_assignments_for_user(user["id"]),
        )

    @app.post("/assignments/{assignment_id}/submit")
    async def submit_assignment(request: Request, assignment_id: int):
        user = request.state.user
        if not user:
            return JSONResponse({"error": "Sign in required."}, status_code=401)
        if not user["email_verified"] or user.get("status") == "suspended":
            return JSONResponse({"error": "A verified active student account is required."}, status_code=403)
        form = await request.app.state.form_data(request)
        if not request.app.state.csrf_ok(request, form):
            return JSONResponse({"error": "The form expired."}, status_code=403)
        assignment = request.app.state.db.assignment(assignment_id)
        enrolled = request.app.state.db._one(
            "SELECT id FROM enrollments WHERE user_id = ? AND course_id = ? AND status = 'active'",
            (user["id"], assignment["course_id"] if assignment else 0),
        )
        if not assignment or not assignment["published"] or not enrolled:
            return JSONResponse({"error": "That assignment is not open for you."}, status_code=404)
        upload = form.get("submission")
        if not upload or not getattr(upload, "filename", ""):
            return JSONResponse({"error": "Choose a file to submit."}, status_code=400)
        original = _safe_filename(upload.filename)
        extension = original.rsplit(".", 1)[-1].lower() if "." in original else ""
        allowed = {item.strip().lower() for item in assignment["formats"].split(",")}
        data = await upload.read()
        if extension not in allowed or extension not in ALLOWED_EXTENSIONS or not data or len(data) > MAX_SUBMISSION_BYTES:
            return JSONResponse({"error": "Use an allowed file type under 5 MB."}, status_code=400)
        stored = f"{secrets.token_hex(16)}.{extension}"
        folder = request.app.state.settings.database_path.parent / "submissions"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / stored).write_bytes(data)
        late = False
        if assignment["deadline"]:
            late = parse_iso(iso()) > parse_iso(assignment["deadline"])
        request.app.state.db.save_submission(assignment_id, user["id"], stored, original, len(data), late)
        return request.app.state.redirect(request, "/account/grades", "Submission saved.")


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(name).name).strip(".-")
    return cleaned[:80] or "submission"


def _submission_path(request: Request, stored_name: str) -> Path | None:
    if not re.fullmatch(r"[A-Za-z0-9]+[.][A-Za-z0-9]+", stored_name or ""):
        return None
    root = (request.app.state.settings.database_path.parent / "submissions").resolve()
    path = (root / stored_name).resolve()
    if root not in path.parents:
        return None
    return path
