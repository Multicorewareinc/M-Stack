"""Tests for the RKE2 declarative spec and the node-role properties. All
pure logic: no SSH, no subprocess, no fixtures.

The spec is a pydantic model, so construction is what validates —
`pytest.raises` wraps the constructor rather than a later validate() call.
validate() is still public and still called by the backend; it just can no
longer be the first place a problem shows up."""
import pytest

from multistack import RKE2Cluster, RKE2Node


def test_valid_cluster_passes_validation():
    cluster = RKE2Cluster(
        name="ok",
        nodes=[
            RKE2Node(address="10.0.0.1", role="server"),
            RKE2Node(address="10.0.0.2", role="agent"),
        ],
    )
    cluster.validate()


def test_rejects_cluster_with_no_nodes():
    with pytest.raises(ValueError, match="At least one RKE2 node"):
        RKE2Cluster(name="empty", nodes=[]).validate()


def test_rejects_cluster_with_no_server_node():
    with pytest.raises(ValueError, match="At least one RKE2 server node"):
        RKE2Cluster(
            name="agents-only", nodes=[RKE2Node(address="10.0.0.2", role="agent")]
        )


def test_rejects_unknown_role():
    with pytest.raises(ValueError, match="Invalid RKE2 node role"):
        RKE2Cluster(
            name="bad-role",
            nodes=[
                RKE2Node(address="10.0.0.1", role="server"),
                RKE2Node(address="10.0.0.2", role="worker"),
            ],
        )


def test_rejects_unsupported_cni():
    with pytest.raises(ValueError, match="Invalid cni"):
        RKE2Cluster(
            name="bad-cni",
            cni="weave",
            nodes=[RKE2Node(address="10.0.0.1", role="server")],
        )


@pytest.mark.parametrize("cni", RKE2Cluster.SUPPORTED_CNI)
def test_accepts_every_supported_cni(cni):
    cluster = RKE2Cluster(
        name="cni-ok", cni=cni, nodes=[RKE2Node(address="10.0.0.1", role="server")]
    )
    cluster.validate()


def test_rejects_disable_kube_proxy_without_cilium():
    # Only Cilium's kube-proxy replacement is wired up by RKE2Backend, so
    # any other CNI paired with disable_kube_proxy would leave the cluster
    # with neither kube-proxy nor a replacement.
    with pytest.raises(ValueError, match="requires cni='cilium'"):
        RKE2Cluster(
            name="no-cilium",
            cni="canal",
            disable_kube_proxy=True,
            nodes=[RKE2Node(address="10.0.0.1", role="server")],
        )


def test_accepts_disable_kube_proxy_with_cilium():
    cluster = RKE2Cluster(
        name="cilium",
        cni="cilium",
        disable_kube_proxy=True,
        nodes=[RKE2Node(address="10.0.0.1", role="server")],
    )
    cluster.validate()


def test_rejects_cilium_mtu_without_cilium():
    with pytest.raises(ValueError, match="cilium_mtu is set but cni is 'canal'"):
        RKE2Cluster(
            name="mtu-wrong-cni",
            cni="canal",
            cilium_mtu=1350,
            nodes=[RKE2Node(address="10.0.0.1", role="server")],
        )


@pytest.mark.parametrize("mtu", [0, 500, 9001])
def test_rejects_implausible_cilium_mtu(mtu):
    with pytest.raises(ValueError, match="outside the plausible range"):
        RKE2Cluster(
            name="mtu-range",
            cni="cilium",
            cilium_mtu=mtu,
            nodes=[RKE2Node(address="10.0.0.1", role="server")],
        )


def test_accepts_cilium_mtu_with_cilium():
    RKE2Cluster(
        name="mtu-ok",
        cni="cilium",
        cilium_mtu=1350,
        nodes=[RKE2Node(address="10.0.0.1", role="server")],
    ).validate()


def test_rejects_duplicate_node_addresses():
    # The backend keys join/diff/upgrade logic on address, so duplicates
    # would silently collapse into one node.
    with pytest.raises(ValueError, match="Duplicate node address"):
        RKE2Cluster(
            name="dupes",
            nodes=[
                RKE2Node(address="127.0.0.1", role="server"),
                RKE2Node(address="127.0.0.1", role="agent"),
            ],
        )


def test_role_properties_split_nodes_and_first_server_follows_list_order():
    server_a = RKE2Node(address="10.0.0.1", role="server")
    server_b = RKE2Node(address="10.0.0.2", role="server")
    agent = RKE2Node(address="10.0.0.3", role="agent")
    cluster = RKE2Cluster(name="roles", nodes=[server_a, server_b, agent])

    assert cluster.server_nodes == [server_a, server_b]
    assert cluster.agent_nodes == [agent]
    assert cluster.first_server is server_a
