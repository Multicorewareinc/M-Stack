"""Request DTO for super-admin invitations. The invitee is attached either to an EXISTING organization
(`organization_id`) or to a NEW one created on the spot (`new_organization`) -- exactly one of the two.
"""

from __future__ import annotations

import re
import uuid
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# The same practical, strict check the Org CP applies (modules/invitations/schemas.py there). It is repeated
# here on purpose: inviting someone into a NEW organization creates the organization first, so an address the
# Org CP would later refuse must be refused BEFORE that, or the request would fail and leave an empty
# organization behind. Well-formed only -- whether the mailbox exists is knowable only by sending to it.
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


class NewOrganization(BaseModel):
    name: str = Field(min_length=1)
    plan_id: uuid.UUID


class InvitationRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    name: str | None = Field(default=None, max_length=200)
    # The two system roles every org is provisioned with. Names (not ids) because for a new
    # organization the roles do not exist yet when this form is filled in.
    role: Literal["org_admin", "org_user"] = "org_user"
    organization_id: uuid.UUID | None = None
    new_organization: NewOrganization | None = None

    @field_validator("email")
    @classmethod
    def _valid_email(cls, value: str) -> str:
        email = value.strip().lower()
        if not _is_valid_email(email):
            raise ValueError("must be a valid email address")
        return email

    @model_validator(mode="after")
    def _exactly_one_target(self) -> InvitationRequest:
        if (self.organization_id is None) == (self.new_organization is None):
            raise ValueError(
                "Provide either organization_id or new_organization, not both or neither"
            )
        return self
