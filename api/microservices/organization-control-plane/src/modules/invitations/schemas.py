"""DTOs for invitations. The raw token is returned exactly once, inside `invite_path` / `invite_url`
of the creation response; it is never readable again (only its hash is stored)."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# A practical, strict email check (it does not try to accept every oddity RFC 5322 allows):
#   local part  1-64 chars of letters, digits and !#$%&'*+/=?^_`{|}~- , dot-separated, never starting, ending
#               or doubling a dot
#   domain      at least two dot-separated labels, each 1-63 chars of letters/digits/hyphens that neither
#               start nor end with a hyphen; the last label (TLD) is letters only, 2+ long (or punycode)
#   whole thing at most 254 chars, ASCII only, exactly one "@"
# No whitespace, control characters or angle brackets can match, which matters now that the address goes into
# an email header (a line break would let a caller add headers such as Bcc:) and into an HTML body.
# It proves the address is well-formed -- not that the mailbox exists; only sending to it can show that.
_ATEXT = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]"
_LOCAL = re.compile(rf"{_ATEXT}+(?:\.{_ATEXT}+)*")
_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
_TLD = re.compile(r"(?:[A-Za-z]{2,63}|xn--[A-Za-z0-9-]{1,59})")


def _is_valid_email(email: str) -> bool:
    if len(email) > 254 or email.count("@") != 1 or not email.isascii():
        return False
    local, _, domain = email.partition("@")
    if not 1 <= len(local) <= 64 or not _LOCAL.fullmatch(local):
        return False
    labels = domain.split(".")
    return (
        len(domain) <= 253
        and len(labels) >= 2
        and all(_LABEL.fullmatch(label) for label in labels)
        and bool(_TLD.fullmatch(labels[-1]))
    )


def _clean_email(value: str) -> str:
    email = value.strip().lower()
    if not _is_valid_email(email):
        raise ValueError("must be a valid email address")
    return email


class InvitationCreate(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    name: str | None = Field(default=None, max_length=200)
    # The role the invitee will hold. Either an id (org portal: picked from the org's own roles) or
    # a system-role name such as "org_admin" / "org_user" (admin portal: the org may not exist yet
    # when the form is filled). Neither -> the org's default `org_user` role.
    role_id: uuid.UUID | None = None
    role_name: str | None = Field(default=None, max_length=100)

    _email = field_validator("email")(_clean_email)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        value = (value or "").strip()
        return value or None


class InvitationCreated(BaseModel):
    id: uuid.UUID
    email: str
    name: str | None
    organization_id: uuid.UUID
    organization_name: str
    role_id: uuid.UUID
    role_name: str
    expires_at: datetime
    # Path (with the one-time token) the invitee opens in the Organization Portal. It is returned ONLY when the
    # email did not go out (email_status != "sent"): once the invitee has the link in their inbox, nobody else
    # needs it, and leaving it out keeps a live credential out of API responses, logs and browser history.
    # `invite_url` is the
    # absolute form, present only when `org_portal_base_url` is configured.
    invite_path: str | None = None
    invite_url: str | None = None
    # Whether the invitation was also emailed. "not_configured" / "failed" / "rejected" never fail the request:
    # the link above is always usable, and `email_detail` says why nothing was sent. "rejected" means the mail
    # server refused the ADDRESS (e.g. no such mailbox) -- the inviter should check it for typos.
    email_status: Literal["sent", "not_configured", "failed", "rejected"] = "not_configured"
    email_detail: str | None = None


class InvitationOut(BaseModel):
    """A pending invitation as listed. Never carries the token (only its hash is stored)."""

    id: uuid.UUID
    email: str
    name: str | None
    organization_id: uuid.UUID
    role_id: uuid.UUID
    role_name: str
    invited_by: str | None
    created_at: datetime
    expires_at: datetime


class InvitationPage(BaseModel):
    items: list[InvitationOut]
    total: int
    limit: int
    offset: int


class InvitationInspectIn(BaseModel):
    token: str = Field(min_length=10, max_length=512)


class InvitationInspectOut(BaseModel):
    email: str
    name: str | None
    organization_name: str
    role_name: str
    expires_at: datetime


class InvitationAcceptIn(BaseModel):
    token: str = Field(min_length=10, max_length=512)
    # Capped so an over-large value can't be fed into Argon2id (same guard as the /api/auth schemas).
    password: str = Field(min_length=1, max_length=4096)
