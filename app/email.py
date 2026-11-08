"""
Minimal, send-only email delivery for the one *kind* of message this system
sends: a newly-created user's randomly generated password. Phase 3 only
ever created Admins (via the Operator); Phase 3.5's POST /admin/users
creates Developer/Trainee/Admin through the identical pattern (decision
log: "same pattern as Phase 3"), so the message body is now parameterized
by role name instead of hardcoding "Admin account" -- same mechanism, one
new parameter, not a new kind of email.

Deliberately NOT the full password-reset flow (deferred to Phase 6) -- this
only ever sends a single, fixed-shape message at account-creation time. No
templates, no queue, no retry logic, no bounce handling.

Two modes, chosen automatically by whether SMTP_HOST is set:
  - Configured (smtp_host set): sends for real via smtplib over STARTTLS.
    This is the path Railway/Render use in production.
  - Unconfigured (smtp_host empty, the default): logs the message and
    appends it to `dev_outbox` instead of touching the network. This is
    what local dev and the test suite run against -- most sandboxed/CI
    environments have no SMTP egress at all, and tests shouldn't depend
    on a real mail server to pass.

Either way the caller's contract is identical: call
send_new_user_welcome_email(), get nothing back on success, get an
EmailSendError on failure. Callers (POST /operator/tenants/{id}/admin,
POST /admin/users) do not need to know which mode is active.
"""
from __future__ import annotations

import logging
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage

from app.config import settings

logger = logging.getLogger("guardrail.email")


@dataclass
class SentEmail:
    to: str
    subject: str
    body: str


# Dev-mode only: every "sent" email lands here instead of a real inbox, so
# tests can assert on exactly what would have been sent. Never populated
# when SMTP is actually configured. Not thread-safe -- fine for a single
# dev server process and pytest, not meant for production concurrency.
dev_outbox: list[SentEmail] = []


class EmailSendError(Exception):
    """Raised when a real SMTP send fails. The caller should treat Admin
    creation as failed and roll back the transaction -- an Admin account
    that exists but whose owner has no way to learn the password is a
    support burden, not a partial success."""


def _smtp_configured() -> bool:
    return bool(settings.smtp_host)


def _deliver(to_email: str, subject: str, body: str) -> None:
    """Send one plain subject+body email, or in dev mode (no SMTP_HOST) append it to dev_outbox."""
    if not _smtp_configured():
        logger.info(
            "DEV MODE (no SMTP_HOST configured) — email not actually sent: to=%s subject=%r",
            to_email, subject,
        )
        dev_outbox.append(SentEmail(to=to_email, subject=subject, body=body))
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.smtp_from_email
    msg["To"] = to_email
    msg.set_content(body)

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as server:
            server.starttls()
            if settings.smtp_username:
                server.login(settings.smtp_username, settings.smtp_password)
            server.send_message(msg)
    except Exception as e:
        logger.error("email failed to send to %s: %s", to_email, e)
        raise EmailSendError("failed to send email") from e


def send_new_user_welcome_email(to_email: str, temp_password: str, role_name: str) -> None:
    """
    A new user's randomly generated password, whatever their role. Subject + body only, per decision
    log item #4 -- no HTML, no branding. `role_name` is a display label ("Admin", "Developer",
    "Trainee"), not validated here -- callers pass a known-good value from app.models.Role.
    """
    subject = f"Your GuardRail {role_name} account"
    body = (
        f"A {role_name} account has been created for you on GuardRail.\n\n"
        f"Email: {to_email}\n"
        f"Temporary password: {temp_password}\n\n"
        f"You must change this password the first time you log in, and it stops working "
        f"after {settings.temp_password_ttl_hours} hours if it is not used. This is a "
        "one-time message -- GuardRail does not store or resend it."
    )
    _deliver(to_email, subject, body)


def send_password_reissue_email(to_email: str, temp_password: str) -> None:
    """An Admin reissued this person's password (ADR-0026). Same shape as the welcome email."""
    subject = "Your GuardRail password was reset"
    body = (
        "An Admin has reset your GuardRail password.\n\n"
        f"Email: {to_email}\n"
        f"Temporary password: {temp_password}\n\n"
        f"You must change this password the first time you log in, and it stops working "
        f"after {settings.temp_password_ttl_hours} hours if it is not used. All of your earlier "
        "sessions have been signed out. This is a one-time message -- GuardRail does not store "
        "or resend it."
    )
    _deliver(to_email, subject, body)
