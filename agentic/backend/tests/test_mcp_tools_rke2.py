"""Tests for the RKE2 MCP tool (agentic/backend/src/mcp_tools/rke2.py). Calls the
tool function directly as plain Python -- no MCP transport involved.
This layer only ever builds and validates a script; it never calls
RKE2Backend, so there's nothing here to stub out (RKE2Backend's own
provisioning logic is covered separately by tests/backends/test_rke2_client.py).

build_rke2_cluster_plan takes one `cluster` argument (a real RKE2Cluster,
or a plain dict @validate_call coerces into one) -- an invalid cluster
fails at that point, raising pydantic.ValidationError, and never reaches
this function's own code at all (see its docstring for why). That's
still what a caller going through orchestration/graph.py's execute_tools()
sees as a normal {"valid": False, "error": ...} result -- graph.py is
what catches this, not mcp_tools/rke2.py -- so these tests check the
raise directly rather than a returned "valid": False."""

import pytest
from pydantic import ValidationError

from mcp_tools.rke2 import build_rke2_cluster_plan


def test_build_valid_plan_includes_importable_script():
    result = build_rke2_cluster_plan(
        cluster={
            "name": "ai-cluster",
            "nodes": [
                {"address": "10.0.0.11", "role": "server"},
                {"address": "10.0.0.12", "role": "agent"},
            ],
            "cni": "cilium",
            "disable_kube_proxy": True,
        }
    )
    assert result["valid"] is True
    assert result["cluster_name"] == "ai-cluster"
    assert "RKE2Cluster(" in result["script"]
    assert "backend.create(cluster)" in result["script"]
    compile(result["script"], "<rendered>", "exec")  # syntax check


def test_build_rejects_invalid_kube_proxy_cni_combo():
    with pytest.raises(ValidationError, match="cilium"):
        build_rke2_cluster_plan(
            cluster={
                "name": "bad-cluster",
                "nodes": [{"address": "10.0.0.11", "role": "server"}],
                "cni": "canal",
                "disable_kube_proxy": True,
            }
        )


def test_build_accepts_loopback_nodes_same_as_any_other():
    # No special-casing left for loopback at the build/validate stage --
    # it's just another address as far as build_rke2_cluster_plan cares.
    result = build_rke2_cluster_plan(
        cluster={"name": "local-cluster", "nodes": [{"address": "127.0.0.1", "role": "server"}]}
    )
    assert result["valid"] is True


def test_build_script_includes_every_field_the_cluster_actually_has():
    # Real bug this guards against: the script used to be built from a
    # hand-typed template that only named a handful of fields, silently
    # dropping any that weren't explicitly listed -- ssh_key/ssh_port on a
    # node, and token/kubeconfig_path on the cluster, were all set on the
    # real (validated) cluster object but never appeared in the script
    # actually handed to the user. Every RKE2Cluster/RKE2Node field used
    # here should show up somewhere in the rendered text.
    result = build_rke2_cluster_plan(
        cluster={
            "name": "full-fields",
            "nodes": [
                {
                    "address": "10.0.0.11",
                    "role": "server",
                    "ssh_key": "/home/me/.ssh/id_ed25519",
                    "ssh_port": 2222,
                }
            ],
            "kubeconfig_path": "/tmp/my-cluster.yaml",
        }
    )
    assert result["valid"] is True
    compile(result["script"], "<rendered>", "exec")  # must still be valid Python
    assert "id_ed25519" in result["script"]
    assert "2222" in result["script"]
    assert "/tmp/my-cluster.yaml" in result["script"]


def test_build_plan_escapes_a_quote_in_the_cluster_name():
    # Real bug this guards against: the script used to be built by
    # hand-wrapping values in quotes (f'name="{cluster.name}"'), so a
    # name containing a `"` produced broken -- or maliciously
    # reinterpretable -- generated Python. Using repr() instead fixes it;
    # this proves the fix, not just that a *normal* name works.
    result = build_rke2_cluster_plan(
        cluster={"name": 'demo"cluster', "nodes": [{"address": "10.0.0.11", "role": "server"}]}
    )
    assert result["valid"] is True
    compile(result["script"], "<rendered>", "exec")  # must still be valid Python
    assert 'demo"cluster' in result["script"]  # the quote wasn't stripped or mangled
