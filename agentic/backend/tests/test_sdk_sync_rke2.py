"""Guards against mcp_tools/rke2.py and SKILL.md silently falling out of
sync with the real RKE2Cluster/RKE2Node/RKE2Backend shape. A field
rename/add/remove on the SDK side should fail one of these tests
immediately, not surface later as a confusing error against a real
request."""

from __future__ import annotations

import typing
from pathlib import Path

from multistack import RKE2Cluster, RKE2Node
from multistack.backends.rke2_client import RKE2Backend

from mcp_tools import rke2 as mcp_rke2
from orchestration.tools import build_rke2_cluster_plan_tool

SKILL_MD_TEXT = (
    Path(__file__).resolve().parent.parent / "src" / "skills" / "rke2-cluster" / "SKILL.md"
).read_text(encoding="utf-8")


def test_tool_takes_a_real_rke2cluster_not_hand_listed_fields():
    """build_rke2_cluster_plan takes one `cluster: RKE2Cluster` argument
    -- not cluster_name/cni/disable_kube_proxy/version/... individually
    re-listed as separate parameters. That means every current and
    future RKE2Cluster field (and its default) is automatically part of
    this tool's schema, with nothing here to edit when the SDK adds one.
    If this test fails, someone went back to listing fields individually,
    reintroducing exactly the drift this design removes structurally."""
    # get_type_hints(), not inspect.signature() -- mcp_tools/rke2.py has
    # `from __future__ import annotations`, so a raw signature's
    # .annotation is just the unevaluated string "RKE2Cluster".
    hints = typing.get_type_hints(mcp_rke2.build_rke2_cluster_plan)
    assert set(hints) == {"cluster", "return"}
    assert hints["cluster"] == RKE2Cluster

    # And prove it end to end: the schema the model actually receives
    # includes RKE2Cluster's (and nested RKE2Node's) real fields, not a
    # generic/untyped object.
    schema = build_rke2_cluster_plan_tool.args_schema.model_json_schema()
    cluster_schema = schema["$defs"]["RKE2Cluster"]["properties"]
    assert set(cluster_schema.keys()) == set(RKE2Cluster.model_fields.keys())
    node_schema = schema["$defs"]["RKE2Node"]["properties"]
    assert set(node_schema.keys()) == set(RKE2Node.model_fields.keys())


def test_node_fields_match_what_skill_md_documents():
    """SKILL.md's prose (rules, gotchas) is still hand-maintained even
    though the tool's own schema is now auto-derived -- this checks that
    prose hasn't drifted from the real field names. If this fails,
    RKE2Node changed shape: update SKILL.md, then update the expected set
    below."""
    node_field_names = set(RKE2Node.model_fields.keys())
    assert node_field_names == {"address", "user", "role", "ssh_key", "ssh_port"}

    for field_name in node_field_names:
        assert field_name in SKILL_MD_TEXT, f"RKE2Node.{field_name} isn't documented in SKILL.md"


def test_cluster_fields_match_what_skill_md_documents():
    """Same check as above, for RKE2Cluster. If this fails, update
    SKILL.md to match, then update the expected set below."""
    cluster_field_names = set(RKE2Cluster.model_fields.keys())
    assert cluster_field_names == {
        "name",
        "version",
        "nodes",
        "token",
        "kubeconfig_path",
        "cni",
        "disable_kube_proxy",
        "cilium_mtu",
        "pin_node_ip",
    }

    for field_name in cluster_field_names:
        assert field_name in SKILL_MD_TEXT, f"RKE2Cluster.{field_name} isn't documented in SKILL.md"


def test_backend_methods_mcp_server_relies_on_still_exist():
    """build_rke2_cluster_plan relies on RKE2Cluster.validate() existing,
    and its generated script literally writes out a call to
    RKE2Backend.create() as text (see mcp_tools/rke2.py's
    _render_script). A rename here would otherwise only surface once
    someone actually tries to run the generated script."""
    assert hasattr(RKE2Backend, "create")
    assert hasattr(RKE2Cluster, "validate")
