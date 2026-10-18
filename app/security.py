from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

PBKDF2_ROUNDS = 600_000
SESSION_DAYS = 14
VERIFY_HOURS = 24
RESET_MINUTES = 60


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime | None = None) -> str:
    return (moment or utcnow()).replace(microsecond=0).isoformat()


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored or not password:
        return False
    try:
        scheme, rounds, salt_hex, digest_hex = stored.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, int(rounds)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, expected)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def password_problems(password: str, email: str) -> list[str]:
    problems = []
    if len(password) < 10:
        problems.append("Use at least 10 characters.")
    if len(password) > 128:
        problems.append("Use at most 128 characters.")
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        problems.append("Include at least one letter and one number.")
    if password.lower() == email.lower():
        problems.append("Do not use your email address as the password.")
    return problems


def is_configured_admin(canonical_email: str, configured: frozenset[str] | set[str]) -> bool:
    allowed = set()
    for item in configured:
        parsed = parse_gmail(item)
        allowed.add(parsed[0] if parsed else item.strip().lower())
    return canonical_email in allowed


def parse_gmail(email: str) -> tuple[str, str] | None:
    raw = (email or "").strip().lower()
    if raw.count("@") != 1:
        return None
    local, domain = raw.rsplit("@", 1)
    if domain != "gmail.com":
        return None
    base = local.split("+", 1)[0]
    if not re.fullmatch(r"[a-z0-9]+(?:\.[a-z0-9]+)*", base):
        return None
    canonical_local = base.replace(".", "")
    if not 1 <= len(canonical_local) <= 30:
        return None
    return f"{canonical_local}@gmail.com", raw


def valid_display_name(name: str) -> bool:
    cleaned = " ".join((name or "").split())
    if not 2 <= len(cleaned) <= 40:
        return False
    lowered = cleaned.lower()
    if "@" in cleaned or "://" in lowered or "<" in cleaned or ">" in cleaned:
        return False
    return bool(re.fullmatch(r"[\w .'-]+", cleaned, flags=re.UNICODE))


def clean_display_name(name: str) -> str:
    return " ".join(name.split())


def clean_text(value: str, min_len: int, max_len: int) -> str | None:
    text = (value or "").replace("\x00", "").replace("\r\n", "\n").strip()
    if not min_len <= len(text) <= max_len:
        return None
    return text


def safe_next(value: str | None, default: str = "/") -> str:
    if not value:
        return default
    if not value.startswith("/") or value.startswith("//") or "\\" in value:
        return default
    if any(ord(char) < 32 for char in value):
        return default
    return value


def client_ip(request, trust_proxy: bool) -> str:
    if trust_proxy:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()[:80]
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def sign_form_time(secret: str, started: int) -> str:
    return hmac.new(secret.encode("utf-8"), str(started).encode("utf-8"), hashlib.sha256).hexdigest()


def form_time_error(secret: str, started: str, signature: str, min_seconds: int) -> str | None:
    try:
        stamp = int(started)
    except (TypeError, ValueError):
        return "The form expired. Refresh the page and try again."
    expected = sign_form_time(secret, stamp)
    if not hmac.compare_digest(expected, signature or ""):
        return "The form expired. Refresh the page and try again."
    age = int(time.time()) - stamp
    if age < min_seconds:
        return "Please wait a moment and submit the form again."
    if age > 60 * 60 * 2:
        return "The form expired. Refresh the page and try again."
    return None


def expires_in(*, days: int = 0, hours: int = 0, minutes: int = 0) -> str:
    return iso(utcnow() + timedelta(days=days, hours=hours, minutes=minutes))


class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window_seconds: int) -> bool:
        now = time.time()
        with self._lock:
            recent = [stamp for stamp in self._hits.get(key, []) if now - stamp < window_seconds]
            if len(recent) >= limit:
                self._hits[key] = recent
                return False
            recent.append(now)
            self._hits[key] = recent
            return True
