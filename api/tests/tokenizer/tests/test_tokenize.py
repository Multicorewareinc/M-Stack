"""Covers specs/tokenizer/spec.md: POST /tokenize + unknown-tokenizer rejection."""

from __future__ import annotations


def test_default_backend_counts(client_factory):
    client = client_factory()
    resp = client.post("/tokenize", json={"text": "hello world"})
    assert resp.status_code == 200
    assert resp.json() == {"tokens": 2, "tokenizer": "cl100k_base"}


def test_explicit_backend_honored(client_factory):
    client = client_factory()
    resp = client.post("/tokenize", json={"text": "hello world", "tokenizer": "other-backend"})
    assert resp.status_code == 200
    assert resp.json() == {"tokens": 4, "tokenizer": "other-backend"}


def test_empty_text_returns_zero(client_factory):
    client = client_factory()
    resp = client.post("/tokenize", json={"text": ""})
    assert resp.status_code == 200
    assert resp.json() == {"tokens": 0, "tokenizer": "cl100k_base"}


def test_unknown_tokenizer_rejected(client_factory):
    client = client_factory()
    resp = client.post("/tokenize", json={"text": "hello", "tokenizer": "does-not-exist"})
    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == "unknown_tokenizer"


def test_no_auth_required(client_factory):
    client = client_factory()
    resp = client.post("/tokenize", json={"text": "hello"})
    assert resp.status_code == 200
