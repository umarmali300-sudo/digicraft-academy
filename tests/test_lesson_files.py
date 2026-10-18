"""Admin uploads and protected downloads, with MEGA mocked. No real MEGA account or course content is used."""
from pathlib import Path

from fastapi.testclient import TestClient

from app import lesson_uploads
from app.mega_service import MegaError
from tests.test_app import client_for, csrf, register_and_verify

PDF = b"%PDF-1.4\n" + b"lesson content " * 50


class FakeDownload:
    def __init__(self, data: bytes):
        self.data, self.closed = data, False

    def chunks(self):
        try:
            for i in range(0, len(self.data), 100):
                yield self.data[i : i + 100]
        finally:
            self.closed = True

    def close(self):
        self.closed = True


class FakeMega:
    configured = True

    def __init__(self):
        self.store, self.uploads, self.temp_paths = {}, [], []
        self.fail_upload = self.fail_download = None
        self.downloads = []

    def upload(self, path, folders, name, size):
        self.temp_paths.append(Path(path))
        if self.fail_upload:
            raise MegaError(self.fail_upload)
        node = f"NODE{len(self.store):04d}"
        self.store[node] = Path(path).read_bytes()
        self.uploads.append((list(folders), name, size))
        return node

    def open_download(self, node_id):
        if self.fail_download:
            raise MegaError(self.fail_download)
        if node_id not in self.store:
            raise MegaError("not_found")
        self.downloads.append(FakeDownload(self.store[node_id]))
        return self.downloads[-1]


def setup(tmp_path: Path):
    admin = client_for(tmp_path)
    register_and_verify(admin, tmp_path, email="admin.test@gmail.com", name="Ada Admin")
    mega = FakeMega()
    admin.app.state.mega = mega
    db = admin.app.state.db
    course = db.save_course(None, "intro", "Intro", "Summary text", "Beginner", "Skills", "starter", "published")
    lesson = db.save_lesson(course, "Lesson One", "lesson-1", "Summary text", "Body text for the lesson")
    return admin, mega, db, course, lesson


def upload(client, lesson, name="Week 1.pdf", content=PDF, token=None, ctype="application/pdf"):
    page = client.get("/admin/courses")
    data = {"csrf_token": csrf(page.text) if token is None else token}
    return client.post(
        f"/admin/lessons/{lesson}/files", data=data, files={"file": (name, content, ctype)}, follow_redirects=False
    )


def student_client(admin, tmp_path, email="learner.two@gmail.com"):
    student = TestClient(admin.app)
    register_and_verify(student, tmp_path, email=email, name="Learner Two")
    return student


def test_admin_upload_stores_file_in_mega_and_metadata_in_sqlite(tmp_path: Path):
    admin, mega, db, _course, lesson = setup(tmp_path)
    response = upload(admin, lesson)
    assert response.status_code == 303
    row = db.lesson_files(lesson)[0]
    full = db.get_lesson_file(lesson, row["id"])
    assert full["storage"] == "mega" and full["mega_node_id"] == "NODE0000" and full["size_bytes"] == len(PDF)
    assert full["original_name"] == "Week 1.pdf" and full["content_type"] == "application/pdf" and full["kind"] == "pdf"
    assert full["filename"].startswith("mega-") and full["filename"].endswith(".pdf")
    folders, stored, size = mega.uploads[0]
    assert folders[0] == "DigicraftAcademy" and folders[1].startswith("course-") and folders[2].startswith("lesson-")
    assert mega.store["NODE0000"] == PDF
    assert not mega.temp_paths[0].exists(), "the temporary file must be removed"
    page = admin.get("/admin/courses")
    assert "Week 1.pdf" in page.text and "NODE0000" not in page.text and "mega.nz" not in page.text


def test_upload_requires_an_admin_account(tmp_path: Path):
    admin, mega, db, _course, lesson = setup(tmp_path)
    student = student_client(admin, tmp_path)
    page = student.get("/account")
    denied = student.post(
        f"/admin/lessons/{lesson}/files", data={"csrf_token": csrf(page.text)},
        files={"file": ("a.pdf", PDF, "application/pdf")}, follow_redirects=False,
    )
    assert denied.status_code == 303 and denied.headers["location"] == "/account"
    anonymous = TestClient(admin.app).post(
        f"/admin/lessons/{lesson}/files", data={"csrf_token": "x"},
        files={"file": ("a.pdf", PDF, "application/pdf")}, follow_redirects=False,
    )
    assert anonymous.status_code == 303 and anonymous.headers["location"].startswith("/login")
    assert db.lesson_files(lesson) == [] and mega.uploads == []


def test_upload_requires_a_valid_csrf_token(tmp_path: Path):
    admin, mega, db, _course, lesson = setup(tmp_path)
    for token in ("", "not-the-token"):
        response = upload(admin, lesson, token=token)
        assert response.status_code == 303
    assert db.lesson_files(lesson) == [] and mega.uploads == []


def test_upload_validation_rejects_bad_files(tmp_path: Path):
    admin, mega, db, _course, lesson = setup(tmp_path)
    assert upload(admin, lesson, name="run.exe", content=b"MZ" + b"0" * 50).status_code == 303
    assert upload(admin, lesson, name="fake.pdf", content=b"MZ not a pdf at all").status_code == 303
    assert upload(admin, lesson, name="empty.pdf", content=b"").status_code == 303
    original = lesson_uploads.MAX_PDF_BYTES
    lesson_uploads.MAX_PDF_BYTES = 100
    try:
        assert upload(admin, lesson, name="big.pdf", content=PDF).status_code == 303
    finally:
        lesson_uploads.MAX_PDF_BYTES = original
    assert upload(admin, 9999).status_code == 404
    assert db.lesson_files(lesson) == [] and mega.uploads == []


def test_supported_video_is_accepted(tmp_path: Path):
    admin, mega, db, _course, lesson = setup(tmp_path)
    video = b"\x00\x00\x00\x18ftypmp42" + b"0" * 5000
    assert upload(admin, lesson, name="Intro clip.mp4", content=video, ctype="video/mp4").status_code == 303
    row = db.lesson_files(lesson)[0]
    assert row["kind"] == "video" and row["content_type"] == "video/mp4"


def test_mega_failure_stores_nothing_and_cleans_up(tmp_path: Path):
    admin, mega, db, _course, lesson = setup(tmp_path)
    for code in ("network", "auth", "timeout", "failed"):
        mega.fail_upload = code
        response = upload(admin, lesson)
        assert response.status_code == 303
    assert db.lesson_files(lesson) == []
    assert mega.temp_paths and all(not p.exists() for p in mega.temp_paths)
    mega.configured = False
    mega.fail_upload = None
    assert upload(admin, lesson).status_code == 303
    assert db.lesson_files(lesson) == []


def test_enrolled_student_streams_private_file_through_the_server(tmp_path: Path):
    admin, mega, db, course, lesson = setup(tmp_path)
    upload(admin, lesson)
    file_id = db.lesson_files(lesson)[0]["id"]
    student = student_client(admin, tmp_path)
    user = db.get_user_by_canonical("learnertwo@gmail.com")
    url = f"/courses/intro/lessons/lesson-1/files/{file_id}"
    assert student.get(url).status_code == 403, "not enrolled yet"
    db.enroll(user["id"], course)
    lesson_page = student.get("/courses/intro/lessons/lesson-1")
    assert url in lesson_page.text and "NODE" not in lesson_page.text and "mega.nz" not in lesson_page.text
    ok = student.get(url)
    assert ok.status_code == 200 and ok.content == PDF
    assert ok.headers["content-type"] == "application/pdf"
    assert "attachment" in ok.headers["content-disposition"] and "Week 1.pdf" in ok.headers["content-disposition"]
    assert ok.headers["x-content-type-options"] == "nosniff" and "no-store" in ok.headers["cache-control"]
    assert "mega" not in " ".join(f"{k}:{v}" for k, v in ok.headers.items()).lower()
    assert mega.downloads[-1].closed
    db.enroll(user["id"], course, "withdrawn")
    assert student.get(url).status_code == 403


def test_download_checks_sign_in_and_existence(tmp_path: Path):
    admin, mega, db, course, lesson = setup(tmp_path)
    upload(admin, lesson)
    file_id = db.lesson_files(lesson)[0]["id"]
    url = f"/courses/intro/lessons/lesson-1/files/{file_id}"
    assert TestClient(admin.app).get(url).status_code == 401
    db.enroll(db.get_user_by_canonical("admintest@gmail.com")["id"], course)
    assert admin.get(f"/courses/intro/lessons/lesson-1/files/{file_id + 50}").status_code == 404
    other_lesson = db.save_lesson(course, "Two", "lesson-2", "Summary text", "Body text for lesson two")
    assert admin.get(f"/courses/intro/lessons/lesson-2/files/{file_id}").status_code == 404
    db._run("UPDATE lesson_files SET mega_node_id = NULL WHERE id = ?", (file_id,))
    assert admin.get(url).status_code == 404


def test_mega_download_failures_are_safe(tmp_path: Path):
    admin, mega, db, course, lesson = setup(tmp_path)
    upload(admin, lesson)
    file_id = db.lesson_files(lesson)[0]["id"]
    db.enroll(db.get_user_by_canonical("admintest@gmail.com")["id"], course)
    url = f"/courses/intro/lessons/lesson-1/files/{file_id}"
    mega.fail_download = "network"
    down = admin.get(url)
    assert down.status_code == 503 and "NODE" not in down.text
    mega.fail_download = "not_found"
    assert admin.get(url).status_code == 404
    mega.fail_download = None
    mega.configured = False
    assert admin.get(url).status_code == 503


def test_legacy_local_files_still_download(tmp_path: Path):
    admin, mega, db, course, lesson = setup(tmp_path)
    root = admin.app.state.settings.database_path.parent / "lesson_files" / str(lesson)
    root.mkdir(parents=True)
    (root / "notes.pdf").write_bytes(b"%PDF-legacy")
    file_id = db.add_lesson_file(
        lesson_id=lesson, kind="pdf", filename="notes.pdf", original_name="Old Notes.pdf", storage="local",
        mega_node_id=None, size_bytes=11, content_type="application/pdf", uploaded_by=1,
    )
    db.enroll(db.get_user_by_canonical("admintest@gmail.com")["id"], course)
    got = admin.get(f"/courses/intro/lessons/lesson-1/files/{file_id}")
    assert got.status_code == 200 and got.content == b"%PDF-legacy"
    (root / "notes.pdf").unlink()
    assert admin.get(f"/courses/intro/lessons/lesson-1/files/{file_id}").status_code == 404
    assert mega.downloads == []
