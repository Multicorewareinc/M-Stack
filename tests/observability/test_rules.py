"""The shipped alert rules, checked and unit-tested with promtool.

`promtool check rules` catches PromQL that does not parse; `promtool test
rules` runs tests/observability/rules_test.yaml against synthetic series.
Skipped when promtool is not on PATH, the way the chart tests skip
without helm.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("promtool") is None, reason="promtool is not on PATH")

ROOT = Path(__file__).resolve().parents[2]
RULES = ROOT / "multistack/observability/rules/multistack.yaml"
RULE_TESTS = Path(__file__).with_name("rules_test.yaml")


def _promtool(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["promtool", *args], capture_output=True, text=True, timeout=120)


def test_rules_parse():
    result = _promtool("check", "rules", str(RULES))
    assert result.returncode == 0, result.stdout + result.stderr


def test_rules_unit_tests_pass():
    result = _promtool("test", "rules", str(RULE_TESTS))
    assert result.returncode == 0, result.stdout + result.stderr
