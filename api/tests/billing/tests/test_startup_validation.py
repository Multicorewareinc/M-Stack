"""Covers specs/billing-metering/spec.md "Startup validation of the enriched-subject
dependency" (ADR-030 AD-07): an allowlist check in create_app, before any engine/consumer
construction."""

from __future__ import annotations

import pytest

from conftest import make_engine
from main import create_app
from settings import Settings


def test_default_subject_is_enriched_and_valid():
    app = create_app(Settings(event_backbone_url="nats://x"), engine=make_engine())
    assert app is not None


def test_misconfigured_raw_subject_fails_fast():
    with pytest.raises(RuntimeError):
        create_app(Settings(event_backbone_url="nats://x", event_stream_subject="gateway.events"))


def test_arbitrary_or_mismatched_subject_fails_fast():
    with pytest.raises(RuntimeError):
        create_app(Settings(event_backbone_url="nats://x", event_stream_subject="something.else"))
    with pytest.raises(RuntimeError):
        create_app(
            Settings(
                event_backbone_url="nats://x",
                event_stream_subject="gateway.events.enriched",
                event_stream_name="GATEWAY_EVENTS",
            )
        )


def test_no_validation_when_backbone_empty_even_with_raw_subject():
    app = create_app(
        Settings(event_backbone_url="", event_stream_subject="gateway.events"),
        engine=make_engine(),
    )
    assert app is not None


def test_misconfigured_subject_never_constructs_a_database_engine(monkeypatch):
    import main as main_module

    calls = []
    monkeypatch.setattr(main_module, "build_engine", lambda url: calls.append(url) or make_engine())

    with pytest.raises(RuntimeError):
        create_app(Settings(event_backbone_url="nats://x", event_stream_subject="gateway.events"))

    assert calls == []
