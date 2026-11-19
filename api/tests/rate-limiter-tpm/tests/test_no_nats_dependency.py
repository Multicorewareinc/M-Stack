"""ADR-033 guards: the service does all queue I/O through the multistack.queue SDK, never a
backend client of its own.

`nats-py` must stay OUT of this service's requirements.txt. That is not because nothing needs
it -- the SDK's NATS driver imports it -- but because it is the SDK's dependency, declared as
its `nats` extra and installed by the Dockerfile via `pip install -e ".[nats]"`. Keeping it
out of here is what makes a backend swap a change to the SDK plus env, and nothing in this
service. If you are adding nats-py to fix an ImportError, fix the image's SDK install instead.

Must fail if: a direct `import nats` / `from nats ...` is reintroduced under src/, or nats-py
is added to this service's requirements.txt.
"""
from __future__ import annotations

import re
from pathlib import Path

_SERVICE = Path(__file__).resolve().parents[4] / "api" / "microservices" / "rate-limiter-tpm"
_SRC = _SERVICE / "src"
_REQUIREMENTS = _SERVICE / "requirements.txt"

_NATS_IMPORT = re.compile(r"^\s*(?:import\s+nats|from\s+nats[\s.])", re.M)


def test_no_direct_nats_import():
    offenders = [
        p.relative_to(_SERVICE).as_posix()
        for p in _SRC.rglob("*.py")
        if _NATS_IMPORT.search(p.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"direct nats import(s) remain: {offenders}"


def test_nats_py_absent_from_requirements():
    assert "nats-py" not in _REQUIREMENTS.read_text(encoding="utf-8")
