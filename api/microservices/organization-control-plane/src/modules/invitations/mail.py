"""Emailing an invitation over SMTP.

Sending is best-effort BY DESIGN: an invitation that exists but whose email did not go out is still a good
invitation -- the admin copies the link from the page and sends it themselves. So nothing here ever raises
to the caller; it returns an EmailResult, and the route reports it next to the link.

Standard library only (smtplib, run in a worker thread so the event loop is never blocked), so there is no
extra dependency to ship into an air-gapped install.
"""

from __future__ import annotations

import asyncio
import html
import logging
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr
from typing import Literal

logger = logging.getLogger(__name__)

EmailStatus = Literal["sent", "not_configured", "failed", "rejected"]


@dataclass(frozen=True)
class EmailResult:
    status: EmailStatus
    detail: str | None = None


def _refusal_reason(recipients: dict) -> str:
    """The server's own words for why it refused the address, trimmed for display."""
    for _addr, (code, text) in recipients.items():
        text = text.decode("utf-8", "replace") if isinstance(text, bytes) else str(text)
        text = " ".join(text.split())[:200]
        return f"{code} {text}".strip()
    return "the mail server refused the address"


def not_configured_reason(settings, link: str | None) -> str | None:
    """Why an email cannot be sent at all, or None when it can."""
    if not settings.smtp_host or not settings.smtp_from_address:
        return "SMTP is not configured (SMTP_HOST and SMTP_FROM_ADDRESS)."
    if not link or not link.startswith(("http://", "https://")):
        return "ORG_PORTAL_BASE_URL is not set, so the email would have no usable link."
    return None


def _plain(
    org: str, role: str, invited_by: str | None, name: str | None, link: str, expires: str
) -> str:
    who = f" by {invited_by}" if invited_by else ""
    return (
        f"Hello{(' ' + name) if name else ''},\n\n"
        f"You've been invited{who} to join {org} as {role}.\n\n"
        f"Open this link to choose a password and sign in:\n{link}\n\n"
        f"The link can be used once and expires {expires}. If you weren't expecting this, you can ignore "
        f"this email.\n"
    )


def _html(
    org: str, role: str, invited_by: str | None, name: str | None, link: str, expires: str
) -> str:
    e = html.escape
    who = f" by {e(invited_by)}" if invited_by else ""
    return (
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:15px;color:#111827;line-height:1.5">'
        f"<p>Hello{(' ' + e(name)) if name else ''},</p>"
        f"<p>You've been invited{who} to join <strong>{e(org)}</strong> as <strong>{e(role)}</strong>.</p>"
        f'<p><a href="{e(link, quote=True)}" style="display:inline-block;padding:10px 18px;background:#2563eb;'
        'color:#ffffff;text-decoration:none;border-radius:6px">Accept invitation</a></p>'
        f'<p style="color:#6b7280;font-size:13px">Or paste this link into your browser:<br>{e(link)}</p>'
        f'<p style="color:#6b7280;font-size:13px">It can be used once and expires {e(expires)}. '
        "If you weren't expecting this, you can ignore this email.</p></div>"
    )


def build_message(
    settings,
    *,
    to: str,
    org_name: str,
    role_name: str,
    invited_by: str | None,
    name: str | None,
    link: str,
    expires_at: datetime,
) -> EmailMessage:
    expires = expires_at.strftime("%d %b %Y, %H:%M UTC")
    msg = EmailMessage()
    # EmailMessage refuses CR/LF in a header value (ValueError), which is what stops header injection
    # through a hostile name or address; send_invitation_email turns that into a "failed" result.
    msg["Subject"] = f"You've been invited to join {org_name}"
    msg["From"] = formataddr((settings.smtp_from_name, settings.smtp_from_address))
    msg["To"] = to
    msg.set_content(_plain(org_name, role_name, invited_by, name, link, expires))
    msg.add_alternative(_html(org_name, role_name, invited_by, name, link, expires), subtype="html")
    return msg


def _send_blocking(settings, msg: EmailMessage) -> None:
    host, port, timeout = settings.smtp_host, settings.smtp_port, settings.smtp_timeout_seconds
    context = ssl.create_default_context()  # verifies the server certificate and hostname
    if settings.smtp_security == "ssl":
        server: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=timeout, context=context)
    else:
        server = smtplib.SMTP(host, port, timeout=timeout)
    with server:
        server.ehlo()
        if settings.smtp_security == "starttls":
            server.starttls(context=context)
            server.ehlo()
        if settings.smtp_username:
            password = settings.smtp_password.get_secret_value() if settings.smtp_password else ""
            server.login(settings.smtp_username, password)
        server.send_message(msg)


async def send_invitation_email(
    settings,
    *,
    to: str,
    org_name: str,
    role_name: str,
    invited_by: str | None,
    name: str | None,
    link: str | None,
    expires_at: datetime,
) -> EmailResult:
    reason = not_configured_reason(settings, link)
    if reason:
        return EmailResult("not_configured", reason)
    try:
        msg = build_message(
            settings,
            to=to,
            org_name=org_name,
            role_name=role_name,
            invited_by=invited_by,
            name=name,
            link=link,  # type: ignore[arg-type]  # not None: not_configured_reason checked it
            expires_at=expires_at,
        )
        await asyncio.wait_for(
            asyncio.to_thread(_send_blocking, settings, msg),
            timeout=settings.smtp_send_deadline_seconds,
        )
    except smtplib.SMTPRecipientsRefused as exc:
        # The server looked at the recipient and said no (typically 550 "no such user" / "doesn't exist"): the
        # address is wrong, which is different from the mail system being broken.
        reason = _refusal_reason(exc.recipients)
        logger.warning("invitation email to %s was rejected: %s", to, reason)
        return EmailResult("rejected", reason)
    except TimeoutError:
        logger.warning(
            "invitation email to %s timed out after %ss", to, settings.smtp_send_deadline_seconds
        )
        return EmailResult(
            "failed",
            f"The mail server did not respond within {settings.smtp_send_deadline_seconds:g} seconds.",
        )
    except (smtplib.SMTPException, OSError, ssl.SSLError, ValueError) as exc:
        # Log the type and message, never the link (it is the credential) or the password.
        logger.warning("invitation email to %s failed: %s: %s", to, type(exc).__name__, exc)
        return EmailResult("failed", f"{type(exc).__name__}: {str(exc)[:160]}")
    return EmailResult("sent")
