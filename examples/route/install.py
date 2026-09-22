"""
Route a service through the ingress gateway on an existing RKE2 cluster.

A separate, user-triggered step — like `examples/ingress_gateway/install.py`,
which this depends on: `ingress_gateway` stands the front door up and
deliberately stops there, so nothing routes through it until you run this.

    pip install -e .                          # from the repo root
    python3 examples/route/install.py

Needs `kubectl` on PATH, a cluster whose kubeconfig is below already up
and reachable, and `ingress_gateway` already installed on it (this only
attaches to that front door — it does not stand one up).

Two implementations, picked with MULTISTACK_ROUTE_TYPE:

  httproute (default)   gateway.networking.k8s.io/v1 -- vendor-neutral,
                        needs the Gateway API CRDs installed once:
                          kubectl apply -f https://github.com/kubernetes-sigs/\
gateway-api/releases/download/v1.2.1/standard-install.yaml
  virtualservice        networking.istio.io/v1 -- no extra CRDs beyond
                        what `ingress_gateway`'s metallb_istio already
                        installs, for when you need Istio traffic
                        features Gateway API does not expose yet.
"""
import os

from multistack import Route, RouteBackend

# Not /tmp: the same kubeconfig examples/rke2/cluster.py wrote, and the
# same one examples/ingress_gateway/install.py used to stand up the front
# door this attaches to.
KUBECONFIG = os.path.expanduser(
    os.environ.get("MULTISTACK_KUBECONFIG", "~/.multistack/kubeconfig")
)

ROUTE_TYPE = os.environ.get("MULTISTACK_ROUTE_TYPE", "httproute")

# What this routes to. The defaults match model-gateway's own chart
# (examples/gateway/install.py) and its default namespace -- override all
# three together if you're routing a different service.
SERVICE = os.environ.get("MULTISTACK_ROUTE_SERVICE", "gateway-model-gateway")
NAMESPACE = os.environ.get("MULTISTACK_ROUTE_NS", "gateway")
PORT = int(os.environ.get("MULTISTACK_ROUTE_PORT", "8080"))

# Host-based routing. Empty means "any host", which is what a bare LAN
# address with no DNS needs -- and what you must move off once two
# routed services would otherwise both claim the same path. See
# multistack/route/README.md for what an empty hostnames list means when
# two routes share a Gateway.
HOSTNAMES = [h.strip() for h in
             os.environ.get("MULTISTACK_ROUTE_HOSTNAME", "").split(",") if h.strip()]

PATH_PREFIX = os.environ.get("MULTISTACK_ROUTE_PATH", "/v1")

# Defaults to SERVICE -- overridable in case two routes need to point at
# the same Service under different names, which the object model allows
# but nothing here needs by default.
NAME = os.environ.get("MULTISTACK_ROUTE_NAME", SERVICE)

route = Route(
    # Required -- no ambient $KUBECONFIG fallback, so this can only ever
    # act on the cluster you name.
    kubeconfig_path=KUBECONFIG,
    type=ROUTE_TYPE,

    # The route object's own name, and the namespace it lives in -- which
    # must be the Service's namespace (a route's backendRef is
    # namespace-local unless a ReferenceGrant says otherwise).
    name=NAME,
    namespace=NAMESPACE,

    service=SERVICE,
    port=PORT,
    path_prefix=PATH_PREFIX,
    hostnames=HOSTNAMES,

    # parent_name/parent_namespace/ingress_service all default to what
    # ingress_gateway's metallb_istio implementation actually installs
    # (istio-ingressgateway in istio-ingress) -- override them only if
    # that capability was installed with different names.
)

backend = RouteBackend()

problems = backend.check_prerequisites(route)
if problems:
    raise SystemExit(
        "Not ready to route:\n" + "\n".join(f"  - {p}" for p in problems)
    )

# Applies the parent Gateway/VirtualService if this is the first route
# through it, then this route -- a second call with the same name is an
# update, not a duplicate.
url = backend.create(route)

print(f"\n{ROUTE_TYPE} for {NAMESPACE}/{SERVICE} ready")
print(f"  url: {url}")
if not HOSTNAMES:
    print(
        "\n  No hostname set -- this matches on path alone, so it will "
        "catch anything not claimed by a more specific route on the "
        "same Gateway. Set MULTISTACK_ROUTE_HOSTNAME once you have a "
        "real domain and DNS (or /etc/hosts) pointing at the ingress "
        "address."
    )
