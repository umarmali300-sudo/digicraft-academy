"""Validation helpers for lesson file uploads. No web framework needed, so they are easy to test."""
from __future__ import annotations

import os
import re
import secrets
import tempfile
import unicodedata
from pathlib import Path
from typing import BinaryIO

MB = 1024 * 1024
MAX_PDF_BYTES = 50 * MB
MAX_VIDEO_BYTES = 1024 * MB
MAX_NAME_LENGTH = 100
CONTENT_TYPES = {
    "pdf": ("pdf", "application/pdf"),
    "mp4": ("video", "video/mp4"),
    "m4v": ("video", "video/x-m4v"),
    "mov": ("video", "video/quicktime"),
    "webm": ("video", "video/webm"),
    "mkv": ("video", "video/x-matroska"),
}
ACCEPT_ATTRIBUTE = ",".join(f".{ext}" for ext in CONTENT_TYPES)
MEGA_ROOT_FOLDER = "DigicraftAcademy"


class UploadRejected(ValueError):
    """The message is safe to show to an admin."""


def max_bytes(kind: str) -> int:
    return MAX_PDF_BYTES if kind == "pdf" else MAX_VIDEO_BYTES


def clean_filename(raw: str | None) -> tuple[str, str]:
    """Return (display name, lowercase extension). Raises UploadRejected for anything unsafe."""
    if not raw or "\x00" in raw:
        raise UploadRejected("Choose a file to upload.")
    name = re.split(r"[\\/]", raw)[-1]
    name = unicodedata.normalize("NFKC", name)
    name = "".join(ch for ch in name if ch.isprintable())
    stem, dot, ext = name.rpartition(".")
    ext = ext.lower()
    if not dot or ext not in CONTENT_TYPES:
        raise UploadRejected("Only PDF, MP4, M4V, MOV, WEBM and MKV files can be uploaded.")
    stem = re.sub(r"[^\w .()\-]", "_", stem, flags=re.UNICODE)
    stem = re.sub(r"\s+", " ", stem).strip(" ._")
    if not stem:
        raise UploadRejected("The file name is not usable. Rename the file and try again.")
    stem = stem[: MAX_NAME_LENGTH - len(ext) - 1].rstrip(" ._") or "lesson-file"
    return f"{stem}.{ext}", ext


def kind_and_type(ext: str) -> tuple[str, str]:
    return CONTENT_TYPES[ext]


def signature_matches(ext: str, head: bytes) -> bool:
    """Check the first bytes really look like the claimed format (the browser's type is not trusted)."""
    if ext == "pdf":
        return head.startswith(b"%PDF-")
    if ext in {"mp4", "m4v", "mov"}:
        return head[4:8] == b"ftyp"
    if ext in {"webm", "mkv"}:
        return head.startswith(b"\x1a\x45\xdf\xa3")
    return False


def storage_name(ext: str) -> str:
    """Server-chosen name. It matches the pattern the download route already enforces."""
    return f"mega-{secrets.token_hex(12)}.{ext}"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:40] or "item"


def folder_parts(course: dict, lesson: dict) -> list[str]:
    """MEGA folders by course and lesson. Ids keep them stable if a slug is edited later."""
    return [
        MEGA_ROOT_FOLDER,
        f"course-{int(course['id'])}-{_slug(course.get('slug', ''))}",
        f"lesson-{int(lesson['id'])}-{_slug(lesson.get('slug', ''))}",
    ]


def copy_to_temp(source: BinaryIO, ext: str, directory: str | None = None) -> tuple[Path, int]:
    """Stream an upload to a private temp file (never into memory), enforcing type and size.

    The caller must delete the returned path. On any rejection the temp file is removed here.
    """
    kind, _ = kind_and_type(ext)
    limit = max_bytes(kind)
    handle, name = tempfile.mkstemp(prefix="lesson-upload-", suffix=".part", dir=directory)
    path = Path(name)
    size = 0
    try:
        with os.fdopen(handle, "wb") as out:
            head = b""
            while True:
                chunk = source.read(MB)
                if not chunk:
                    break
                if not head:
                    head = chunk[:16]
                    if not signature_matches(ext, head):
                        raise UploadRejected("That file's contents do not match its type.")
                size += len(chunk)
                if size > limit:
                    raise UploadRejected(f"That file is larger than the {limit // MB} MB limit for {kind} files.")
                out.write(chunk)
        if size == 0:
            raise UploadRejected("That file is empty.")
        return path, size
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def human_size(value: int | None) -> str:
    if not value:
        return ""
    units = ("B", "KB", "MB", "GB")
    number, index = float(value), 0
    while number >= 1024 and index < len(units) - 1:
        number /= 1024
        index += 1
    return f"{number:.0f} {units[index]}" if index == 0 else f"{number:.1f} {units[index]}"
