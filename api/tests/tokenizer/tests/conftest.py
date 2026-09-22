"""Test helpers: build the app with an injected fake tokenizer registry, so tests never
require tiktoken's encoding data (mirrors the rate-limiter-rpm injected-client pattern)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from main import create_app
from settings import Settings

FAKE_TOKENIZERS = {
    "cl100k_base": lambda text: len(text.split()),
    "other-backend": lambda text: len(text.split()) * 2,
}


def make_client(*, tokenizers: dict | None = None, **settings_kwargs):
    """Build the app with an injected fake tokenizer registry. Returns TestClient."""
    registry = tokenizers if tokenizers is not None else FAKE_TOKENIZERS
    app = create_app(Settings(**settings_kwargs), tokenizers=registry)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def client_factory():
    return make_client
