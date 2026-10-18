"""Upload validation helpers. These need no web framework."""
import io
import re
from pathlib import Path

from app import lesson_uploads as lu
from app.lesson_uploads import UploadRejected


def rejected(func, *args):
    try:
        func(*args)
    except UploadRejected as error:
        return str(error)
    raise AssertionError("expected UploadRejected")


def test_clean_filename_accepts_supported_types_and_strips_paths():
    assert lu.clean_filename("Week 1 Notes.PDF") == ("Week 1 Notes.pdf", "pdf")
    assert lu.clean_filename("C:\\Users\\me\\lesson (final).mp4") == ("lesson (final).mp4", "mp4")
    assert lu.clean_filename("../../etc/passwd.pdf") == ("passwd.pdf", "pdf")
    for ext in ("pdf", "mp4", "m4v", "mov", "webm", "mkv"):
        assert lu.clean_filename(f"a.{ext}")[1] == ext


def test_clean_filename_rejects_unsafe_or_unsupported():
    for bad in ("", None, "noextension", "run.exe", "page.html", "archive.zip", "x.pdf.exe", "a\x00.pdf", ".pdf", "...pdf"):
        rejected(lu.clean_filename, bad)


def test_clean_filename_neutralises_odd_characters_and_length():
    name, ext = lu.clean_filename("we<ir>d;|name$.pdf")
    assert re.fullmatch(r"[\w .()\-]+\.pdf", name) and ext == "pdf"
    long_name, _ = lu.clean_filename("a" * 400 + ".mp4")
    assert len(long_name) <= lu.MAX_NAME_LENGTH and long_name.endswith(".mp4")


def test_signature_checks_match_real_formats_only():
    assert lu.signature_matches("pdf", b"%PDF-1.7 rest")
    assert not lu.signature_matches("pdf", b"MZ\x90\x00 not a pdf")
    assert lu.signature_matches("mp4", b"\x00\x00\x00\x18ftypmp42")
    assert lu.signature_matches("mov", b"\x00\x00\x00\x14ftypqt  ")
    assert lu.signature_matches("webm", b"\x1a\x45\xdf\xa3\x01")
    assert not lu.signature_matches("mp4", b"%PDF-1.7")


def test_copy_to_temp_streams_and_reports_size(tmp_path: Path):
    data = b"%PDF-1.4\n" + b"x" * 3_000_000
    path, size = lu.copy_to_temp(io.BytesIO(data), "pdf", str(tmp_path))
    assert size == len(data) and path.read_bytes() == data and path.parent == tmp_path


def test_copy_to_temp_rejects_and_cleans_up(tmp_path: Path):
    assert "do not match" in rejected(lu.copy_to_temp, io.BytesIO(b"MZ" + b"0" * 100), "pdf", str(tmp_path))
    assert "empty" in rejected(lu.copy_to_temp, io.BytesIO(b""), "pdf", str(tmp_path))
    original = lu.MAX_PDF_BYTES
    lu.MAX_PDF_BYTES = 2 * lu.MB
    try:
        assert "larger than" in rejected(lu.copy_to_temp, io.BytesIO(b"%PDF-" + b"0" * (3 * lu.MB)), "pdf", str(tmp_path))
    finally:
        lu.MAX_PDF_BYTES = original
    assert list(tmp_path.iterdir()) == [], "rejected uploads must not leave temp files"


def test_storage_name_matches_download_route_pattern_and_is_unique():
    names = {lu.storage_name("pdf") for _ in range(50)}
    assert len(names) == 50
    assert all(re.fullmatch(r"[A-Za-z0-9._-]+", n) for n in names)


def test_folder_parts_organise_by_course_and_lesson():
    parts = lu.folder_parts({"id": 3, "slug": "Intro to AI!"}, {"id": 12, "slug": "week-1"})
    assert parts == ["DigicraftAcademy", "course-3-intro-to-ai", "lesson-12-week-1"]
    odd = lu.folder_parts({"id": 1, "slug": "../x/"}, {"id": 2, "slug": ""})
    assert all("/" not in p and ".." not in p for p in odd)


def test_human_size():
    assert lu.human_size(None) == "" and lu.human_size(512) == "512 B"
    assert lu.human_size(5 * lu.MB) == "5.0 MB"
