"""Invitations: emailing the invitation over SMTP.

smtplib is replaced by a recording fake, so the exact SMTP conversation (STARTTLS, login, message) is asserted
without a mail server."""

from __future__ import annotations

import smtplib
import uuid

import pytest

from conftest import asgi_client, auth_headers, init_schema, make_auth_app, seed_org, seed_user

from modules.invitations import mail
from modules.roles.models import Role, RolePermission, UserRole

PASSWORD = "Chosen-By-Invitee-42!"
INVITEE = "new.person@example.com"
BASE_URL = "https://org.test"


# ------------------------------------------------------------------ helpers


async def _setup(**settings):
    app, engine = make_auth_app(**settings)
    await init_schema(engine)
    return app, app.state.sessionmaker


async def _seed_roles(sm, org_id):
    async with sm() as s:
        admin = Role(organization_id=org_id, name="org_admin", is_system_role=True)
        member = Role(organization_id=org_id, name="org_user", is_system_role=True)
        s.add_all([admin, member])
        await s.commit()
        return admin.id, member.id


async def _invite(c, org_id, email=INVITEE, **extra):
    r = await c.post(
        f"/internal/v1/organizations/{org_id}/invitations",
        json={"email": email, "name": "New Person", **extra},
        headers=auth_headers(),
    )
    assert r.status_code == 201, r.text
    return r.json()


def _tok(body):
    return body["invite_path"].split("token=", 1)[1]


# ================================================================== SMTP


class FakeSMTP:
    """Records the conversation. `instances` collects every connection the code opens."""

    instances: list["FakeSMTP"] = []
    fail_on: str | None = None
    kind = "SMTP"

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.timeout, self.context = host, port, timeout, context
        self.calls: list[str] = []
        self.sent = []
        type(self).instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.calls.append("quit")

    def _maybe_fail(self, step):
        if type(self).fail_on == step:
            raise {
                "login": smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted"),
                "starttls": smtplib.SMTPNotSupportedError("STARTTLS extension not supported by server."),
                "send": smtplib.SMTPRecipientsRefused({"x": (550, b"5.1.1 The email account that you tried to reach does not exist.")}),
                "connect": ConnectionRefusedError(111, "Connection refused"),
            }[step]

    def ehlo(self):
        self.calls.append("ehlo")

    def starttls(self, context=None):
        self._maybe_fail("starttls")
        self.calls.append("starttls")
        self.tls_context = context

    def login(self, user, password):
        self._maybe_fail("login")
        self.calls.append(f"login:{user}:{password}")

    def send_message(self, msg):
        self._maybe_fail("send")
        self.calls.append("send")
        self.sent.append(msg)


class FakeSMTPSSL(FakeSMTP):
    kind = "SMTP_SSL"


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.instances = []
    FakeSMTP.fail_on = None
    FakeSMTPSSL.instances = FakeSMTP.instances

    def make(cls):
        def factory(*a, **k):
            if FakeSMTP.fail_on == "connect":
                raise ConnectionRefusedError(111, "Connection refused")
            return cls(*a, **k)

        return factory

    monkeypatch.setattr(mail.smtplib, "SMTP", make(FakeSMTP))
    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", make(FakeSMTPSSL))
    return FakeSMTP


SMTP_ON = dict(
    smtp_host="smtp.example.test",
    smtp_port=587,
    smtp_username="mailer",
    smtp_password="s3cret-smtp-password",
    smtp_from_address="noreply@example.test",
    smtp_from_name="Acme Platform",
    org_portal_base_url=BASE_URL,
)


def _link_in(text: str) -> str:
    import re

    return re.search(r"https?://\S+/accept-invite\?token=[\w-]+", text).group(0)


async def _invited(app, sm, **extra):
    org = await seed_org(sm, name="Globex")
    await _seed_roles(sm, org)
    async with asgi_client(app) as c:
        return await _invite(c, org, **extra)


async def test_the_invitation_is_emailed_over_starttls_with_login(smtp):
    app, sm = await _setup(**SMTP_ON)
    body = await _invited(app, sm, role_name="org_admin")
    assert body["email_status"] == "sent" and body["email_detail"] is None

    (conn,) = smtp.instances
    assert (conn.host, conn.port) == ("smtp.example.test", 587) and conn.timeout == 10.0
    assert conn.calls == ["ehlo", "starttls", "ehlo", "login:mailer:s3cret-smtp-password", "send", "quit"]
    assert conn.tls_context.check_hostname and conn.tls_context.verify_mode.name == "CERT_REQUIRED"

    (msg,) = conn.sent
    assert msg["To"] == INVITEE
    assert msg["From"] == "Acme Platform <noreply@example.test>"
    assert msg["Subject"] == "You've been invited to join Globex"
    text = msg.get_body(("plain",)).get_content()
    page = msg.get_body(("html",)).get_content()
    link = _link_in(text)
    assert link.startswith(f"{BASE_URL}/accept-invite?token=")
    for part in (text, page):
        assert link in part and "Globex" in part and "org_admin" in part
    assert "expires" in text
    # the API response does NOT carry the link: the invitee has it, so no one else needs it
    assert body["invite_path"] is None and body["invite_url"] is None
    assert link.split("token=")[1] not in str(body)


async def test_ssl_mode_uses_implicit_tls_and_no_starttls(smtp):
    app, sm = await _setup(**{**SMTP_ON, "smtp_security": "ssl", "smtp_port": 465})
    body = await _invited(app, sm)
    assert body["email_status"] == "sent"
    (conn,) = smtp.instances
    assert conn.kind == "SMTP_SSL" and conn.port == 465
    assert "starttls" not in conn.calls and conn.calls[-2:] == ["send", "quit"]


async def test_none_mode_and_no_username_skip_tls_and_login(smtp):
    app, sm = await _setup(**{**SMTP_ON, "smtp_security": "none", "smtp_username": None, "smtp_password": None})
    body = await _invited(app, sm)
    assert body["email_status"] == "sent"
    (conn,) = smtp.instances
    assert conn.calls == ["ehlo", "send", "quit"]


@pytest.mark.parametrize(
    "step,status", [("login", "failed"), ("starttls", "failed"), ("connect", "failed"), ("send", "rejected")]
)
async def test_a_mail_failure_never_fails_the_invitation(smtp, step, status):
    smtp.fail_on = step
    app, sm = await _setup(**SMTP_ON)
    org = await seed_org(sm)
    await _seed_roles(sm, org)
    async with asgi_client(app) as c:
        body = await _invite(c, org)
        # the invitation exists and its link works, whatever happened to the email
        assert body["email_status"] == status and body["email_detail"]
        assert "s3cret-smtp-password" not in str(body)  # the credential never reaches the response
        token = _tok(body)
        assert (await c.post("/api/invitations/inspect", json={"token": token})).status_code == 200


async def test_nothing_is_sent_without_smtp_settings_or_a_public_link(smtp):
    app, sm = await _setup()  # no SMTP at all
    body = await _invited(app, sm)
    assert body["email_status"] == "not_configured" and "SMTP" in body["email_detail"]
    assert body["invite_path"]  # the copyable link is still there

    app, sm = await _setup(**{**SMTP_ON, "org_portal_base_url": None})
    body = await _invited(app, sm)
    assert body["email_status"] == "not_configured" and "ORG_PORTAL_BASE_URL" in body["email_detail"]

    app, sm = await _setup(**{**SMTP_ON, "smtp_from_address": None})
    assert (await _invited(app, sm))["email_status"] == "not_configured"
    assert smtp.instances == []  # not even a connection attempt


async def test_user_supplied_text_cannot_inject_html_or_headers(smtp):
    app, sm = await _setup(**SMTP_ON)
    org = await seed_org(sm, name='<img src=x onerror=alert(1)>')
    await _seed_roles(sm, org)
    async with asgi_client(app) as c:
        r = await c.post(
            f"/internal/v1/organizations/{org}/invitations",
            json={"email": INVITEE, "name": "<script>alert(1)</script>"},
            headers=auth_headers(),
        )
        assert r.status_code == 201
        # an address carrying a line break (an attempt to add a Bcc: header) is refused outright
        for evil in ("a@b.co\nBcc:attacker@evil.example", "a@b.co\r\nBcc: x@y.co", "a b@c.co", "a@b.co,c@d.co"):
            bad = await c.post(
                f"/internal/v1/organizations/{org}/invitations", json={"email": evil}, headers=auth_headers()
            )
            assert bad.status_code == 422, evil
    (msg,) = smtp.instances[-1].sent
    page = msg.get_body(("html",)).get_content()
    assert "<script>" not in page and "&lt;script&gt;" in page
    assert "<img src=x" not in page and "&lt;img" in page
    assert msg["Bcc"] is None and len(msg.get_all("To")) == 1


async def test_the_org_admin_route_emails_the_invitation_too(smtp):
    from pydantic import SecretStr
    from conftest import JWT_SECRET
    from modules.auth.jwt import encode_access_token

    app, sm = await _setup(**SMTP_ON)
    uid, org, _ = await seed_user(sm, email="admin@acme.com", username="admin")
    admin_id, member_id = await _seed_roles(sm, org)
    async with sm() as s:
        s.add(RolePermission(role_id=admin_id, permission_id=uuid.uuid5(uuid.NAMESPACE_DNS, "users.create")))
        s.add(UserRole(user_id=uid, role_id=admin_id))
        await s.commit()
    jwt_token = encode_access_token(
        sub=str(uid), email="admin@acme.com", org=str(org), secret=SecretStr(JWT_SECRET), ttl_seconds=3600
    )
    async with asgi_client(app) as c:
        r = await c.post(
            "/api/invitations",
            json={"email": INVITEE, "name": "New Person", "role_id": str(member_id)},
            headers={"Authorization": f"Bearer {jwt_token}"},
        )
    assert r.status_code == 201 and r.json()["email_status"] == "sent"
    (msg,) = smtp.instances[-1].sent
    assert msg["To"] == INVITEE
    assert "admin@acme.com" in msg.get_body(("plain",)).get_content()  # who invited them


async def test_a_mail_server_that_hangs_is_cut_off_and_never_holds_the_request_open(smtp, monkeypatch):
    import time as _time

    def slow_send(settings, msg):
        _time.sleep(1.0)  # far longer than the 0.2 s deadline below

    monkeypatch.setattr(mail, "_send_blocking", slow_send)
    app, sm = await _setup(**{**SMTP_ON, "smtp_send_deadline_seconds": 0.2})
    org = await seed_org(sm)
    await _seed_roles(sm, org)
    async with asgi_client(app) as c:
        started = _time.monotonic()
        body = await _invite(c, org)
        elapsed = _time.monotonic() - started
        assert elapsed < 0.9, elapsed  # answered at the deadline, not after the slow server finished
        assert body["email_status"] == "failed" and "did not respond within 0.2 seconds" in body["email_detail"]
        # the invitation itself is fine and its link works
        assert (await c.post("/api/invitations/inspect", json={"token": _tok(body)})).status_code == 200


async def test_a_refused_address_is_reported_as_rejected_with_the_servers_own_reason(smtp):
    smtp.fail_on = "send"
    app, sm = await _setup(**SMTP_ON)
    org = await seed_org(sm)
    await _seed_roles(sm, org)
    async with asgi_client(app) as c:
        body = await _invite(c, org, email="no.such.person@example.com")
        assert body["email_status"] == "rejected"
        assert body["email_detail"] == "550 5.1.1 The email account that you tried to reach does not exist."
        # a typo is the inviter's to fix: the invitation is still there (and usable), not silently lost
        assert (await c.post("/api/invitations/inspect", json={"token": _tok(body)})).status_code == 200


async def test_the_link_is_withheld_from_the_response_only_when_the_email_went_out(smtp):
    # sent -> no link in the API response (it lives in the invitee's inbox)
    app, sm = await _setup(**SMTP_ON)
    sent = await _invited(app, sm)
    assert sent["email_status"] == "sent" and sent["invite_path"] is None and sent["invite_url"] is None

    # ...and the emailed link is the real one: it works
    token = _link_in(smtp.instances[-1].sent[0].get_body(("plain",)).get_content()).split("token=")[1]
    async with asgi_client(app) as c:
        assert (await c.post("/api/invitations/inspect", json={"token": token})).status_code == 200

    # not sent, in any way -> the admin must be able to deliver it, so the link IS returned
    for settings, status in (
        ({}, "not_configured"),
        ({**SMTP_ON}, "failed"),
        ({**SMTP_ON}, "rejected"),
    ):
        smtp.fail_on = {"failed": "login", "rejected": "send"}.get(status)
        app, sm = await _setup(**settings)
        body = await _invited(app, sm)
        assert body["email_status"] == status
        assert body["invite_path"].startswith("/accept-invite?token="), status
        assert (await asgi_client(app).post("/api/invitations/inspect", json={"token": _tok(body)})).status_code == 200


async def test_the_list_never_exposes_a_link_either(smtp):
    app, sm = await _setup(**SMTP_ON)
    org = await seed_org(sm)
    await _seed_roles(sm, org)
    async with asgi_client(app) as c:
        await _invite(c, org)
        listed = (await c.get("/internal/v1/invitations", headers=auth_headers())).text
    assert "token" not in listed and "invite_path" not in listed and "invite_url" not in listed
