"""The agent model's sampling temperature: a setting, unset by default."""

from __future__ import annotations

import llm


def test_no_temperature_is_sent_unless_set(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_BASE_URL", "http://model.test/v1")
    monkeypatch.delenv("LLM_TEMPERATURE", raising=False)
    assert llm.get_chat_model().temperature is None


def test_a_set_temperature_is_used(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_BASE_URL", "http://model.test/v1")
    monkeypatch.setenv("LLM_TEMPERATURE", "0.2")
    assert llm.get_chat_model().temperature == 0.2


def test_an_empty_judgement_is_asked_for_once_more(monkeypatch):
    import orchestration.graph as graph

    class Reply:
        def __init__(self, text):
            self.text = text

    class Judge:
        def __init__(self, texts):
            self.texts, self.calls = iter(texts), 0

        def invoke(self, _messages):
            self.calls += 1
            return Reply(next(self.texts))

    judge = Judge(["", '["rpm"]'])
    monkeypatch.setattr(graph, "_judge_model", lambda: judge)
    assert graph._ask_unbound([]) == '["rpm"]' and judge.calls == 2

    silent = Judge(["", "  ", "never reached"])
    monkeypatch.setattr(graph, "_judge_model", lambda: silent)
    assert graph._ask_unbound([]) == "" and silent.calls == 2

    first_time = Judge(["no"])
    monkeypatch.setattr(graph, "_judge_model", lambda: first_time)
    assert graph._ask_unbound([]) == "no" and first_time.calls == 1
