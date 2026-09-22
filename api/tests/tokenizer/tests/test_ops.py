"""Covers specs/tokenizer/spec.md: health, metrics, startup validation of TOKENIZER_DEFAULT."""

from __future__ import annotations

import re

import pytest

from main import create_app
from settings import Settings


def test_health(client_factory):
    client = client_factory()
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_metrics(client_factory):
    client = client_factory()
    tok = client.post("/tokenize", json={"text": "hello"})
    assert tok.status_code == 200
    resp = client.get("/metrics")
    assert resp.status_code == 200
    # Council review fix: assert the actual counter VALUE, not just that its name appears
    # somewhere in the scrape (which would pass even if it were never incremented).
    m = re.search(r"^tokenizer_requests_total (\S+)$", resp.text, re.MULTILINE)
    assert m is not None, resp.text
    assert float(m.group(1)) >= 1.0


def test_unknown_default_fails_startup():
    with pytest.raises(RuntimeError):
        create_app(Settings(tokenizer_default="does-not-exist"), tokenizers={"cl100k_base": lambda t: 0})
