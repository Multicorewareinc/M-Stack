"""
Install the ingress gateway (MetalLB + Istio) on an existing RKE2 cluster.

A separate, user-triggered step — like `examples/minio/tenant.py` — run
once you have a cluster and know which addresses MetalLB may hand out on
its LAN (the VIP(s) below). `RKE2Backend` never deploys this on its own.

    pip install -e ".[helm]"                  # from the repo root
    python3 examples/ingress_gateway/install.py

Needs `helm` and `kubectl` on PATH, and a cluster whose kubeconfig is below
already up and reachable.
"""
from multistack.ingress_gateway import IngressGateway, IngressGatewayBackend
import os

# Not /tmp: this is the same kubeconfig examples/rke2/cluster.py wrote, and
# losing it leaves you with a cluster you cannot reach.
KUBECONFIG = os.path.expanduser(
    os.environ.get("MULTISTACK_KUBECONFIG", "~/.multistack/kubeconfig")
)
# MetalLB's address range on your LAN — the addresses it may hand to
# LoadBalancer Services, of which the Istio ingress gateway is the first.
# This SDK can't derive it: which addresses are free is a fact about your
# network, not the cluster. A single IP (start == end) works fine.
ADDRESS_POOL = [a.strip() for a in os.environ.get(
    "MULTISTACK_INGRESS_POOL", "192.0.2.240-192.0.2.250").split(",") if a.strip()]

# Which node hosts the gateway. Only matters on a cluster whose nodes are
# not all in one L2 segment — but there it matters completely: MetalLB
# answers ARP from a node holding a ready endpoint, so a gateway pod on a
# node with no interface on the pool's subnet gets an EXTERNAL-IP that
# nothing on the LAN can resolve, with no error anywhere.
INGRESS_NODE = os.environ.get("MULTISTACK_INGRESS_NODE", "")

gateway = IngressGateway(
    # Required. Points at exactly one cluster — no ambient $KUBECONFIG
    # fallback, so this can't silently install into the wrong place.
    kubeconfig_path=KUBECONFIG,
    address_pool=ADDRESS_POOL,
    node_selector={"kubernetes.io/hostname": INGRESS_NODE} if INGRESS_NODE else {},
    # Chart/release/namespace overrides live on `options=MetalLBIstioOptions(...)`;
    # the defaults (metallb-system, istio-system, istio-ingress) work for a
    # cluster that isn't already using those namespaces for something else.
)

backend = IngressGatewayBackend()

# Installs metallb, applies its IPAddressPool/L2Advertisement from
# address_pool, then istio-base, istiod and the ingress gateway itself —
# and waits for MetalLB to actually assign it an address before returning.
endpoint = backend.create(gateway)

print(f"\nIngress gateway ready at {endpoint}")
print(f"Try: KUBECONFIG={KUBECONFIG} kubectl -n istio-ingress get svc")
print(f"     curl -k https://{endpoint}/  # 404 from Istio is expected — nothing is routed through it yet")
