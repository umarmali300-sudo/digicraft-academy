from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    secret_key: str
    database_path: Path
    base_url: str
    google_client_id: str
    google_client_secret: str
    google_redirect_uri: str
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    smtp_from: str
    smtp_tls: bool
    dev_log_emails: bool
    email_log_path: Path
    whatsapp_number: str
    telegram_username: str
    contact_email: str
    zoom_url: str
    meet_url: str
    stripe_secret_key: str
    stripe_webhook_secret: str
    cookie_secure: bool
    form_min_seconds: int
    admin_emails: frozenset[str]
    trust_proxy: bool

    @property
    def google_enabled(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    @property
    def smtp_enabled(self) -> bool:
        return bool(self.smtp_host and self.smtp_from)

    @property
    def payments_ready(self) -> bool:
        return bool(self.stripe_secret_key and self.stripe_webhook_secret)

    @classmethod
    def from_env(cls) -> Settings:
        data = ROOT / "data"
        data.mkdir(parents=True, exist_ok=True)
        secret = os.environ.get("NEXORA_SECRET_KEY", "").strip()
        secret_path = data / ".secret"
        if not secret:
            if secret_path.exists():
                secret = secret_path.read_text(encoding="utf-8").strip()
            if not secret:
                secret = secrets.token_hex(32)
                secret_path.write_text(secret, encoding="utf-8")
        base = os.environ.get("NEXORA_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
        local = "127.0.0.1" in base or "localhost" in base
        admins = frozenset(
            item.strip().lower()
            for item in os.environ.get("NEXORA_ADMIN_EMAILS", "").split(",")
            if item.strip()
        )
        return cls(
            secret_key=secret,
            database_path=Path(os.environ.get("NEXORA_DATABASE_PATH", str(data / "nexora.db"))),
            base_url=base,
            google_client_id=os.environ.get("GOOGLE_CLIENT_ID", "").strip(),
            google_client_secret=os.environ.get("GOOGLE_CLIENT_SECRET", "").strip(),
            google_redirect_uri=os.environ.get(
                "GOOGLE_REDIRECT_URI", f"{base}/auth/google/callback"
            ).strip(),
            smtp_host=os.environ.get("SMTP_HOST", "").strip(),
            smtp_port=int(os.environ.get("SMTP_PORT", "587") or "587"),
            smtp_username=os.environ.get("SMTP_USERNAME", "").strip(),
            smtp_password=os.environ.get("SMTP_PASSWORD", ""),
            smtp_from=os.environ.get("SMTP_FROM", "").strip(),
            smtp_tls=_flag("SMTP_TLS", True),
            dev_log_emails=_flag("NEXORA_DEV_LOG_EMAILS", local),
            email_log_path=Path(os.environ.get("NEXORA_EMAIL_LOG", str(data / "email.log"))),
            whatsapp_number=os.environ.get("NEXORA_WHATSAPP_NUMBER", "").strip(),
            telegram_username=os.environ.get("NEXORA_TELEGRAM_USERNAME", "").strip(),
            contact_email=os.environ.get("NEXORA_CONTACT_EMAIL", "").strip(),
            zoom_url=os.environ.get("NEXORA_ZOOM_URL", "").strip(),
            meet_url=os.environ.get("NEXORA_MEET_URL", "").strip(),
            stripe_secret_key=os.environ.get("STRIPE_SECRET_KEY", "").strip(),
            stripe_webhook_secret=os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip(),
            cookie_secure=_flag("NEXORA_COOKIE_SECURE", base.startswith("https://")),
            form_min_seconds=int(os.environ.get("NEXORA_FORM_MIN_SECONDS", "2") or "2"),
            admin_emails=admins,
            trust_proxy=_flag("NEXORA_TRUST_PROXY", False),
        )

    @classmethod
    def for_tests(cls, directory: Path) -> Settings:
        directory.mkdir(parents=True, exist_ok=True)
        return cls(
            secret_key="test-secret-key-not-for-production",
            database_path=directory / "test.db",
            base_url="http://testserver",
            google_client_id="",
            google_client_secret="",
            google_redirect_uri="http://testserver/auth/google/callback",
            smtp_host="",
            smtp_port=587,
            smtp_username="",
            smtp_password="",
            smtp_from="",
            smtp_tls=True,
            dev_log_emails=True,
            email_log_path=directory / "email.log",
            whatsapp_number="",
            telegram_username="",
            contact_email="",
            zoom_url="",
            meet_url="",
            stripe_secret_key="",
            stripe_webhook_secret="",
            cookie_secure=False,
            form_min_seconds=0,
            admin_emails=frozenset({"admin.test@gmail.com"}),
            trust_proxy=False,
        )
