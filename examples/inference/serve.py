"""
Serve a model with vLLM through the Multistack SDK's `inference` capability.

`model` defaults to Qwen2.5-0.5B-Instruct (the only model this deployment
is sized/tested for today) but is a normal field — pass a different one
once more are supported, e.g. from a user request in a later layer.

    pip install -e .                      # from the repo root
    python3 examples/inference/serve.py

Needs `kubectl` on PATH, a cluster kubeconfig, and passwordless SSH to the
target node if you want the prerequisite/CPU-flag checks (recommended).

To remove it again:
    InferenceBackend().delete(service)   # same kubeconfig_path/name/namespace
"""
from multistack import Inference, InferenceBackend, RKE2Node
import os

KUBECONFIG = os.path.expanduser(
    os.environ.get("MULTISTACK_KUBECONFIG", "~/.multistack/kubeconfig")
)

# The node this is pinned to, so prerequisites can be checked and the dtype
# chosen from its actual CPU flags. Label/taint it first:
#   kubectl label node <node> multistack.io/workload=inference
#   kubectl taint node <node> dedicated=inference:NoSchedule
target = RKE2Node(
    address=os.environ.get("MULTISTACK_INFERENCE_NODE", "192.0.2.11"),
    user=os.environ.get("MULTISTACK_SSH_USER", "ubuntu"),
    role="agent",
    ssh_key=os.environ.get("MULTISTACK_SSH_KEY", "~/.ssh/id_ed25519"),
)

service = Inference(
    # Required — no ambient $KUBECONFIG fallback.
    kubeconfig_path=KUBECONFIG,

    # Leave `model` unset to get Qwen2.5-0.5B-Instruct, pulled straight from
    # the model registry (no S3 mirror needed for this small a model).
    name="vllm-qwen",

    device="cpu",
    # None: the backend picks dtype from the node's CPU flags rather than
    # taking bfloat16 from the model config and emulating it in software.
    dtype=None,
    max_model_len=4096,

    # 6 of 8 cores, leaving room for kubelet/CNI/DaemonSets.
    cpu_cores=6,
    memory_gb=12,
    kv_cache_gb=4,

    node_selector={"multistack.io/workload": "inference"},
    tolerations=[{
        "key": "dedicated", "operator": "Equal",
        "value": "inference", "effect": "NoSchedule",
    }],

    # Reachable from outside the cluster for this standalone demo; a
    # gateway consuming `service.endpoint` in-cluster would leave this off.
    host_network=True,
)

backend = InferenceBackend()

# Checks prerequisites, applies the manifests, waits for readiness, then
# sends a warmup request.
endpoint = backend.create(service, nodes=[target])

print(f"\nServing at {endpoint}")
print("Try:")
print(f"""  curl {endpoint}/v1/chat/completions \\
    -H 'Content-Type: application/json' \\
    -d '{{"model":"{service.served_name}","messages":[{{"role":"user","content":"hi"}}],"max_tokens":32}}'""")
