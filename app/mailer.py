from __future__ import annotations

import smtplib
from email.message import EmailMessage

from app.config import Settings


class MailError(Exception):
    pass


def send_email(settings: Settings, recipient: str, subject: str, body: str) -> str:
    """Send mail when SMTP is configured. Returns 'smtp' or 'log'.

    Local development can record the message in the email log. That log is not
    a substitute for delivery in production.
    """
    if settings.smtp_enabled:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = settings.smtp_from
        message["To"] = recipient
        message.set_content(body)
        try:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as server:
                if settings.smtp_tls:
                    server.starttls()
                if settings.smtp_username:
                    server.login(settings.smtp_username, settings.smtp_password)
                server.send_message(message)
        except OSError as exc:
            raise MailError("The email could not be sent.") from exc
        return "smtp"
    if settings.dev_log_emails:
        settings.email_log_path.parent.mkdir(parents=True, exist_ok=True)
        with settings.email_log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"TO: {recipient}\nSUBJECT: {subject}\n{body}\n---\n")
        return "log"
    raise MailError("Email delivery is not configured.")
