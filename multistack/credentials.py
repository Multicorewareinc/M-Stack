"""Supplying credentials without writing them down.

Every other value a spec carries is safe in a file people commit. A
password is not, and the SDK had nowhere to put one except the spec
itself -- so `DatabaseConfig(password=...)` meant either a literal in a
script, an `os.environ[...]` read that pushed the problem into a
`.env`, or a `secrets.token_urlsafe()` call whose result nobody saw
again.

`prompt_secret()` is the fourth option: ask the person running the
script, at the moment the script runs, and never persist it.

The precedence is deliberate and, read top to bottom, is the order of
decreasing trust in where the value came from:

    explicit argument  ->  caller already had it, nothing to ask
    environment        ->  CI and automation, which have no terminal
    interactive prompt ->  a person is here; ask them
    generate           ->  only when the caller said a random one is fine
    raise              ->  otherwise, refuse rather than invent

The last line is the one that matters. A helper that silently generates
a password when it cannot ask produces a cluster nobody can log into,
discovered days later. Failing at the point of the missing value costs
one re-run; the alternative costs a re-deploy.

Nothing here logs, echoes, caches, or writes the value anywhere. It is
returned to the caller and forgotten.
"""

from __future__ import annotations

import os
import secrets as _stdlib_secrets
import sys
from getpass import getpass

__all__ = ["MissingSecretError", "prompt_secret", "generate_secret"]


# 18 bytes of urandom, url-safe encoded -- ~24 characters, ~143 bits.
# Well past anything that needs a length policy, and still short enough
# to paste out of a terminal without wrapping.
_GENERATED_BYTES = 18


class MissingSecretError(RuntimeError):
    """Raised when a credential is needed and there is no way to obtain it.

    Carries the environment variable name so the message tells a CI
    operator -- who has no terminal to be prompted at -- exactly which
    variable to set.
    """


def generate_secret() -> str:
    """A cryptographically random credential."""
    return _stdlib_secrets.token_urlsafe(_GENERATED_BYTES)


def _is_interactive() -> bool:
    """Whether there is a person at a terminal to ask.

    Both streams are checked. stdin carries the typing, but getpass
    writes its prompt to stderr, and a script whose stderr is
    redirected to a file would otherwise block on a prompt nobody can
    see. `isatty` can also raise on a closed or replaced stream, which
    is itself an answer.
    """
    try:
        return sys.stdin.isatty() and sys.stderr.isatty()
    except (AttributeError, ValueError):
        return False


def prompt_secret(
    label: str,
    *,
    env_var: str | None = None,
    value: str | None = None,
    generate: bool = False,
    confirm: bool = False,
    min_length: int = 8,
) -> str:
    """Obtain a credential without storing it in the calling script.

    Args:
        label: What is being asked for, in the prompt. Name the thing
            the credential belongs to -- "PostgreSQL owner password for
            'platform'" rather than "password" -- because a script that
            asks three times gives no other clue which is which.
        env_var: Environment variable consulted before prompting. Give
            one for anything that has to run unattended.
        value: An already-known value. Present so a caller can pass an
            optional argument straight through without branching on it.
        generate: Allow a random value when none is supplied. With a
            terminal the person is still asked, and an empty answer
            generates; without one it generates silently.
        confirm: Ask twice and require a match. For a credential being
            *set*, where a typo becomes a lockout rather than an error.
        min_length: Rejected below this. Only applies to typed input --
            a caller's explicit value and the environment are taken as
            given, since re-validating a decision already made just
            blocks a deploy at an unhelpful moment.

    Returns:
        The credential. Not logged, not cached, not written anywhere.

    Raises:
        MissingSecretError: Nothing supplied it and nothing may invent it.
    """
    if value:
        return value

    if env_var:
        from_env = os.environ.get(env_var)
        if from_env:
            return from_env

    if _is_interactive():
        return _ask(
            label,
            generate=generate,
            confirm=confirm,
            min_length=min_length,
        )

    if generate:
        return generate_secret()

    raise MissingSecretError(
        f"{label} is required, and there is no terminal to ask at.\n"
        + (
            f"Set {env_var} in the environment"
            if env_var
            else "Pass the value explicitly"
        )
        + ", or run this interactively."
    )


def _ask(
    label: str,
    *,
    generate: bool,
    confirm: bool,
    min_length: int,
) -> str:
    """The interactive half. Loops until the answer is usable.

    Separated from `prompt_secret` so the precedence above reads as the
    short list of rules it is, rather than as one function with a loop
    in the middle of it.
    """
    suffix = " (Enter to generate): " if generate else ": "

    while True:
        entered = getpass(f"{label}{suffix}")

        if not entered:
            if generate:
                print(f"  {label}: generated.", file=sys.stderr)
                return generate_secret()
            print("  Required -- nothing was entered.", file=sys.stderr)
            continue

        if len(entered) < min_length:
            print(
                f"  Too short -- {min_length} characters minimum.",
                file=sys.stderr,
            )
            continue

        if confirm and getpass(f"{label} (again): ") != entered:
            print("  Did not match. Starting over.", file=sys.stderr)
            continue

        return entered
