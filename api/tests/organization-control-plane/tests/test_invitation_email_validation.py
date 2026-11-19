"""The email address on an invitation must be a well-formed address (it is used as an SMTP recipient, put in a
header, and compared with the Google account's email). Well-formed is all that can be checked without sending:
whether the mailbox exists is only knowable by delivering to it."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from conftest import asgi_client, auth_headers, init_schema, make_auth_app, seed_org
from modules.invitations.schemas import InvitationCreate
from modules.roles.models import Role

VALID = [
    "jane@example.com",
    "jane.doe@example.com",
    "jane+invites@example.com",
    "o'brien@example.co.uk",
    "first_last-1@sub.domain.example.org",
    "a@b.io",
    "x@xn--80ak6aa92e.com",  # punycode (internationalised) domain
    "1234567890@example.com",
    "JANE@EXAMPLE.COM",  # case is normalised, not rejected
    "  jane@example.com  ",  # surrounding spaces are trimmed
    "a" * 64 + "@example.com",  # longest allowed local part
]

INVALID = [
    "",
    "jane",
    "jane@",
    "@example.com",
    "jane@example",  # no TLD
    "jane@example.c",  # one-letter TLD
    "jane@example.123",  # numeric TLD
    "jane@exam ple.com",
    "jane doe@example.com",
    "jane@@example.com",
    "jane@a@example.com",
    "a..b@example.com",  # doubled dot
    ".jane@example.com",  # leading dot
    "jane.@example.com",  # trailing dot
    "jane@example..com",
    "jane@.example.com",
    "jane@example.com.",
    "jane@-example.com",  # label starting with a hyphen
    "jane@example-.com",  # label ending with a hyphen
    "jane@exa_mple.com",  # underscore in a domain
    "<script>@example.com",
    "jane@example.com<script>",
    '"jane"@example.com',  # quoted local parts are not supported
    "jane@[127.0.0.1]",  # address literals are not supported
    "jane,bob@example.com",
    "jane@example.com,bob@example.com",
    "jane@example.com;bob@example.com",
    "jane@例え.jp",  # use the punycode form of an internationalised domain
    "jänë@example.com",
    "jane@example.com\nBcc:attacker@evil.example",
    "jane@example.com\r\nBcc: x@y.co",
    "jane\x00@example.com",
    "a" * 65 + "@example.com",  # local part over 64
    "a@" + "b" * 64 + ".com",  # label over 63
    "a@" + ".".join(["b" * 60] * 5) + ".com",  # domain over 253
    "a" * 64 + "@" + "b" * 63 + "." + "c" * 63 + "." + "d" * 63 + "." + "e" * 63 + ".com",  # over 254 total
]


@pytest.mark.parametrize("address", VALID)
def test_well_formed_addresses_are_accepted_and_normalised(address):
    assert InvitationCreate(email=address).email == address.strip().lower()


@pytest.mark.parametrize("address", INVALID)
def test_malformed_addresses_are_rejected(address):
    with pytest.raises(ValidationError):
        InvitationCreate(email=address)


async def test_the_api_refuses_a_malformed_address_without_creating_anything_or_sending_mail():
    app, engine = make_auth_app()
    await init_schema(engine)
    sm = app.state.sessionmaker
    org = await seed_org(sm)
    async with sm() as s:
        s.add(Role(organization_id=org, name="org_user", is_system_role=True))
        await s.commit()
    async with asgi_client(app) as c:
        for bad in ("a..b@example.com", "x@y.1", "<script>@x.io", "jane@example", "a@-bad-.com"):
            r = await c.post(
                f"/internal/v1/organizations/{org}/invitations", json={"email": bad}, headers=auth_headers()
            )
            assert r.status_code == 422, bad
        listed = await c.get("/internal/v1/invitations", headers=auth_headers())
    assert listed.json()["total"] == 0  # none of the refused addresses left an invitation behind
