"""
Install both control planes on an existing RKE2 cluster.

The admin plane owns plans, permissions and platform administration; the
organization plane owns an organization's users, keys and quotas. Each
calls the other over the cluster network, so they are installed together.

    pip install -e ".[helm]"                  # from the repo root
    python3 examples/controlplane/install.py

Needs `helm` and `kubectl` on PATH, a reachable cluster, a Postgres
database, a Valkey cache, and the Secret named below.

No credential appears in this file. Both charts read DATABASE_URL,
VALKEY_URL, JWT_SECRET and their API keys from a Kubernetes Secret, and
the spec carries only that Secret's name — a spec is a file people
commit. Create the Secret with `multistack.kube.apply`, which pipes the
manifest to stdin; `kubectl create secret --from-literal` puts the value
in the process arguments, where any local user can read it.
"""
import os

from multistack import ControlPlane, ControlPlaneBackend

KUBECONFIG = os.path.expanduser(
    os.environ.get("MULTISTACK_KUBECONFIG", "~/.multistack/kubeconfig")
)

# One Secret per service. Each must carry every key in
# multistack.controlplane.REQUIRED_SECRET_KEYS for its type — the backend
# checks before installing and names any that are missing, because the
# chart installs happily without them and the failure only shows up as a
# 500 on the first sign-in.
ADMIN_SECRET = os.environ.get("MULTISTACK_ADMIN_CP_SECRET", "admin-cp-secrets")
ORG_SECRET = os.environ.get("MULTISTACK_ORG_CP_SECRET", "org-cp-secrets")

# There is no in-cluster registry, so the service image exists only on
# the node it was imported to; a pod scheduled anywhere else stays
# ImagePullBackOff. Empty leaves the chart's own default in place.
IMAGE_NODE = os.environ.get("MULTISTACK_IMAGE_NODE", "")
NODE_SELECTOR = {"kubernetes.io/hostname": IMAGE_NODE} if IMAGE_NODE else None

backend = ControlPlaneBackend()

# Seeding runs on first install and is not concurrency-safe across pods,
# so the admin plane goes up with one replica and is scaled afterwards.
admin = ControlPlane(
    type="admin",
    kubeconfig_path=KUBECONFIG,
    existing_secret=ADMIN_SECRET,
    replicas=1,
    node_selector=NODE_SELECTOR,
)

organization = ControlPlane(
    type="organization",
    kubeconfig_path=KUBECONFIG,
    existing_secret=ORG_SECRET,
    replicas=2,
    node_selector=NODE_SELECTOR,
)

for spec in (admin, organization):
    endpoint = backend.create(spec)
    print(f"\n{spec.type} control plane installed")
    print(f"  endpoint:  {endpoint}")
    print(f"  peer:      {spec.resolved_peer_url}")

print("\nScale the admin plane once seeding has run:")
print("  admin.replicas = 2 && ControlPlaneBackend().create(admin)")
