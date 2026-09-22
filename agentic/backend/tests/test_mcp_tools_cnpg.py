"""Tests for the CNPG MCP tool (agentic/backend/src/mcp_tools/cnpg.py). Calls
the tool function directly as plain Python -- no MCP transport
involved, no real cluster touched (DatabaseBackend is never called).

The tool keeps the implementation's name; the spec it builds is
`Database(type="cnpg")`, which is what the rendered scripts below
say."""

import pytest
from pydantic import ValidationError

from mcp_tools.cnpg import build_cnpg_plan


def test_build_valid_operator_only_plan():
    result = build_cnpg_plan(cnpg={"kubeconfig_path": "/tmp/kc.yaml"})
    assert result["valid"] is True
    assert "database_url" not in result
    assert "database = Database(" in result["script"]
    assert "backend.create(database)" in result["script"]
    assert "create_cluster" not in result["script"]
    assert "DatabaseConfig" not in result["script"]
    compile(result["script"], "<rendered>", "exec")


def test_build_valid_plan_with_database():
    result = build_cnpg_plan(cnpg={
        "kubeconfig_path": "/tmp/kc.yaml",
        "name": "app-db",
        "database": {"name": "appdb", "owner": "appuser", "password": "hunter2"},
    })
    assert result["valid"] is True
    assert result["database_url"] == "postgresql://appuser@app-db-rw.postgres.svc.cluster.local:5432/appdb"
    assert "DatabaseConfig(name='appdb', owner='appuser', password='hunter2')" in result["script"]
    assert "backend.create(database)" in result["script"]
    assert "backend.create_cluster(database)" in result["script"]
    compile(result["script"], "<rendered>", "exec")


def test_build_rejects_database_without_a_cluster_name():
    # Regression test: constructing a Database does NOT catch this --
    # it validates the operator half, and the cluster half is optional
    # so that one spec can also drive an operator-only install. Only
    # validate_cluster() catches it, and nothing calls that
    # automatically. Confirms build_cnpg_plan calls it explicitly rather
    # than silently rendering a script that would fail once actually
    # run.
    result = build_cnpg_plan(cnpg={
        "kubeconfig_path": "/tmp/kc.yaml",
        "database": {"name": "appdb", "owner": "appuser", "password": "hunter2"},
    })
    assert result["valid"] is False
    assert "name is required" in result["error"]


def test_build_rejects_missing_kubeconfig_path():
    with pytest.raises(ValidationError):
        build_cnpg_plan(cnpg={"name": "app-db"})


def test_build_never_appears_to_validate_the_password_itself():
    # This tool must never reject or "improve" a password -- whatever
    # real value the user gave travels through unchanged, since this
    # tool has no business judging or generating credentials.
    result = build_cnpg_plan(cnpg={
        "kubeconfig_path": "/tmp/kc.yaml",
        "name": "app-db",
        "database": {"name": "appdb", "owner": "appuser", "password": "correct-horse-battery-staple!"},
    })
    assert result["valid"] is True
    assert "correct-horse-battery-staple!" in result["script"]


def test_build_script_includes_every_field_the_spec_actually_has():
    """Including the ones that now live in the implementation's options
    -- they render as a CNPGOptions(...) call, not a raw dict, so the
    script constructs the same object the tool validated."""
    result = build_cnpg_plan(cnpg={
        "kubeconfig_path": "/tmp/kc.yaml",
        "options": {"operator_chart_version": "0.30.0"},
        "instances": 5,
        "storage_size": "50Gi",
    })
    assert result["valid"] is True
    assert "options=CNPGOptions(" in result["script"]
    assert "operator_chart_version='0.30.0'" in result["script"]
    assert "instances=5" in result["script"]
    assert "storage_size='50Gi'" in result["script"]
    compile(result["script"], "<rendered>", "exec")


def test_the_operator_defaults_survive_the_round_trip():
    """`options` unset is filled in by the capability layer, so a plan
    that names nothing still renders the real defaults rather than
    `options=None` -- which would be a script that quietly deploys
    whatever a future default happens to be."""
    result = build_cnpg_plan(cnpg={"kubeconfig_path": "/tmp/kc.yaml"})
    assert result["operator_release_name"] == "cnpg"
    assert "operator_chart='cloudnative-pg'" in result["script"]
    assert "operator_chart_version='0.29.0'" in result["script"]
