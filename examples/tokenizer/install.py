"""
Install the tokenizer on an existing RKE2 cluster.

Token counting for usage and quota. The `enricher` service (ADR-030)
calls it whenever an upstream returns a response with no `usage` block,
guaranteeing every event a usable count before the TPM rate limiter or
billing ever see it -- so it is a dependency of accurate billing rather
than an optional extra, even though neither of those calls it directly
anymore.

    pip install -e ".[helm]"                  # from the repo root
    python3 examples/tokenizer/install.py

Needs `helm` and `kubectl` on PATH, and a cluster whose kubeconfig is
below already up and reachable.
"""
import os

from multistack import Tokenizer, TokenizerBackend

# Not /tmp: this is the same kubeconfig examples/rke2/cluster.py wrote,
# and losing it leaves you with a cluster you cannot reach.
KUBECONFIG = os.path.expanduser(
    os.environ.get("MULTISTACK_KUBECONFIG", "~/.multistack/kubeconfig")
)

# There is no in-cluster registry, so the service image exists only on
# the node it was imported to; a pod scheduled anywhere else stays
# ImagePullBackOff. Empty leaves the chart's own default in place.
IMAGE_NODE = os.environ.get("MULTISTACK_IMAGE_NODE", "")
NODE_SELECTOR = {"kubernetes.io/hostname": IMAGE_NODE} if IMAGE_NODE else None

tokenizer = Tokenizer(
    # Required. Points at exactly one cluster — no ambient $KUBECONFIG
    # fallback, so this can't silently install into the wrong place.
    kubeconfig_path=KUBECONFIG,
    # Two by default. The policy layer calls this synchronously, so a
    # single pod restarting takes token counting down with it.
    replicas=2,
    # Chart, release name and the default encoding live on
    # `options=TiktokenOptions(...)`.
    node_selector=NODE_SELECTOR,
)

endpoint = TokenizerBackend().create(tokenizer)

print("\nTokenizer installed")
print(f"  endpoint:  {endpoint}")
print("\nWire it into the enricher service's TOKENIZER_URL (ADR-030) --")
print("enricher has no SDK capability yet, so this goes into whatever")
print("installs its chart, e.g. a Helm values override:")
print(f"  TOKENIZER_URL={endpoint!r}")
