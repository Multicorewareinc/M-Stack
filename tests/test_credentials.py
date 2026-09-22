"""Tests for multistack.credentials.

The interactive path is driven through a real pty rather than by
monkeypatching getpass: the point of this module is that it behaves
correctly at a terminal, and a stubbed getpass would prove only that
the stub was called.
"""

from __future__ import annotations

import os
import pty
import select
import signal
import sys
import textwrap
import time

import pytest

from multistack.credentials import (
    MissingSecretError,
    generate_secret,
    prompt_secret,
)


# -- precedence, without a terminal -------------------------------------

def test_explicit_value_wins_over_everything(monkeypatch):
    monkeypatch.setenv("T_PW", "from-env")
    assert prompt_secret("Test", env_var="T_PW", value="explicit") == "explicit"


def test_environment_is_used_when_no_explicit_value(monkeypatch):
    monkeypatch.setenv("T_PW", "from-env")
    assert prompt_secret("Test", env_var="T_PW") == "from-env"


def test_empty_environment_variable_is_not_a_value(monkeypatch):
    # An exported-but-empty variable is the classic CI misconfiguration.
    # Treating "" as supplied would bootstrap a passwordless role.
    monkeypatch.setenv("T_PW", "")
    with pytest.raises(MissingSecretError):
        prompt_secret("Test", env_var="T_PW")


def test_refuses_to_invent_a_secret_with_no_terminal(monkeypatch):
    monkeypatch.delenv("T_PW", raising=False)
    with pytest.raises(MissingSecretError, match="T_PW"):
        prompt_secret("PostgreSQL password", env_var="T_PW")


def test_generates_without_a_terminal_only_when_allowed(monkeypatch):
    monkeypatch.delenv("T_PW", raising=False)
    value = prompt_secret("Test", env_var="T_PW", generate=True)
    assert len(value) >= 20


def test_generated_secrets_differ():
    assert generate_secret() != generate_secret()


def test_error_names_the_variable_to_set(monkeypatch):
    monkeypatch.delenv("MULTISTACK_DB_PASSWORD", raising=False)
    with pytest.raises(MissingSecretError) as exc:
        prompt_secret("DB password", env_var="MULTISTACK_DB_PASSWORD")
    # A CI operator reading this in a log has no terminal to be helped
    # by "run it interactively" alone -- the variable name is the fix.
    assert "MULTISTACK_DB_PASSWORD" in str(exc.value)


# -- the interactive path, at a real terminal ---------------------------

def _run_at_a_tty(body: str, answers: list[str], timeout: float = 10.0) -> str:
    """Runs `body` under a pty, typing `answers`, and returns its output.

    Reads with a deadline and feeds each answer only once the child has
    actually asked for it. Writing the whole list up front races the
    child's own `termios` call -- getpass disables echo *after* it
    starts, so anything already sitting in the buffer gets echoed back
    and the no-echo assertion becomes a coin flip.
    """
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {os.getcwd()!r})
        from multistack.credentials import prompt_secret
        {body}
    """)

    pid, fd = pty.fork()
    if pid == 0:                                  # child
        try:
            os.execv(sys.executable, [sys.executable, "-c", script])
        finally:
            os._exit(127)                         # never fall back into pytest

    out = b""
    pending = list(answers)
    deadline = time.monotonic() + timeout
    prompts_seen = 0
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.25)
            if ready:
                try:
                    chunk = os.read(fd, 4096)
                except OSError:                   # EIO -- the child exited
                    break
                if not chunk:
                    break
                out += chunk

            # One answer per ": " prompt the child has printed so far.
            asked = out.count(b": ")
            while pending and prompts_seen < asked:
                os.write(fd, pending.pop(0).encode() + b"\n")
                prompts_seen += 1
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        os.waitpid(pid, 0)
        os.close(fd)

    return out.decode(errors="replace")


def test_prompts_and_returns_what_was_typed():
    out = _run_at_a_tty(
        'print("GOT:" + prompt_secret("Password", min_length=4))',
        ["typed-value"],
    )
    assert "GOT:typed-value" in out


def test_typed_secret_is_never_echoed():
    out = _run_at_a_tty(
        'prompt_secret("Password", min_length=4); print("DONE")',
        ["super-secret-value"],
    )
    assert "DONE" in out
    # getpass suppresses the echo; the value must appear nowhere in the
    # terminal transcript, which is what a screen-share would capture.
    assert "super-secret-value" not in out


def test_too_short_is_rejected_and_reasked():
    out = _run_at_a_tty(
        'print("GOT:" + prompt_secret("Password", min_length=8))',
        ["short", "long-enough-value"],
    )
    assert "Too short" in out
    assert "GOT:long-enough-value" in out


def test_mismatched_confirmation_starts_over():
    out = _run_at_a_tty(
        'print("GOT:" + prompt_secret("Password", confirm=True, min_length=4))',
        ["first-value", "typo-value", "final-value", "final-value"],
    )
    assert "Did not match" in out
    assert "GOT:final-value" in out


def test_empty_answer_generates_when_generation_is_allowed():
    out = _run_at_a_tty(
        'print("LEN:" + str(len(prompt_secret("API key", generate=True))))',
        [""],
    )
    assert "generated" in out
    assert "LEN:24" in out


def test_empty_answer_is_refused_when_generation_is_not_allowed():
    out = _run_at_a_tty(
        'print("GOT:" + prompt_secret("Password", min_length=4))',
        ["", "eventually-typed"],
    )
    assert "Required" in out
    assert "GOT:eventually-typed" in out
