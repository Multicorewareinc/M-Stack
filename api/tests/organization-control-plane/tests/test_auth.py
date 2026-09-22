"""Service-bearer auth and the tenant-context dependency seam."""

from __future__ import annotations

import uuid

import pytest
from starlette.requests import Request

from conftest import auth_headers
from dependencies import require_org_context
from errors import UnprocessableError


def _fake_request(headers: dict[str, str]) -> Request:
    scope = {
        "type": "http",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    return Request(scope)


def test_missing_bearer_401(client):
    r = client.get(f"/internal/v1/organizations/{uuid.uuid4()}")
    assert r.status_code == 401 and r.json()["error"]["type"] == "unauthorized"


def test_wrong_key_401(client):
    r = client.get(f"/internal/v1/organizations/{uuid.uuid4()}", headers=auth_headers("wrong"))
    assert r.status_code == 401 and r.json()["error"]["type"] == "unauthorized"


def test_org_context_present_and_missing():
    oid = uuid.uuid4()
    assert require_org_context(_fake_request({"X-Organization-Id": str(oid)})) == oid

    with pytest.raises(UnprocessableError) as ei:
        require_org_context(_fake_request({}))
    assert ei.value.status_code == 422

    with pytest.raises(UnprocessableError):
        require_org_context(_fake_request({"X-Organization-Id": "not-a-uuid"}))
