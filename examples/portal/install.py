"""
Install both web portals on an existing RKE2 cluster.

One chart serves both: static files behind nginx, which also
reverse-proxies the API so the browser makes same-origin requests. The
admin portal points at the admin control plane, the organization portal
at the organization one.

    pip install -e ".[helm]"                  # from the repo root
    python3 examples/portal/install.py

Needs `helm` and `kubectl` on PATH, a reachable cluster, and the portal
images present on the node named below.
"""
import os

from multistack import ControlPlane, Portal, PortalBackend

KUBECONFIG = os.path.expanduser(
    os.environ.get("MULTISTACK_KUBECONFIG", "~/.multistack/kubeconfig")
)

# There is no in-cluster registry, so the portal images are built locally
# and sideloaded into one node's containerd. A pod scheduled anywhere
# else sits in ImagePullBackOff, which is why this pin is not optional.
IMAGE_NODE = os.environ.get("MULTISTACK_IMAGE_NODE", "rke2-wrk-2")

backend = PortalBackend()

for portal_type in ("admin", "organization"):
    # Each portal proxies to its own control plane. Derived from the
    # ControlPlane spec rather than typed out, so renaming a release or
    # moving a namespace cannot leave the portal pointing at nothing.
    api = ControlPlane(
        type=portal_type,
        kubeconfig_path=KUBECONFIG,
        existing_secret="unused-here",
    )

    portal = Portal(
        type=portal_type,
        kubeconfig_path=KUBECONFIG,
        api_upstream=api.endpoint,
        # Both prefixes matter. The client calls /v1/ for most routes but
        # hits /api/auth/refresh directly, and a refresh that falls
        # through to the SPA returns 200 with HTML — the user is silently
        # logged out with nothing in any log.
        api_prefixes=["/v1/", "/api/"],
        node_selector={"kubernetes.io/hostname": IMAGE_NODE},
    )

    endpoint = backend.create(portal)
    print(f"\n{portal_type} portal installed")
    print(f"  endpoint:  {endpoint}")
    print(f"  proxies:   {portal.api_upstream}")

print("\nReach one from your machine:")
print("  kubectl -n frontend port-forward svc/admin-portal 8008:80")
