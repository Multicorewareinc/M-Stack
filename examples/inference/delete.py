"""
Remove an inference deployment with the Multistack SDK.

`InferenceBackend.delete()` deletes the Deployment and Service only, which
is what frees the node's CPU/memory. It leaves the namespace alone —
`inference` may hold other models or Secrets that aren't this one's to
remove.

    pip install -e .                          # from the repo root
    python3 examples/inference/delete.py

Needs `kubectl` on PATH and the cluster kubeconfig. No SSH: nothing is
installed on the node itself.

Safe to re-run — `--ignore-not-found` means a second call is a no-op.
"""
from multistack import Inference, InferenceBackend
import os

KUBECONFIG = os.path.expanduser(
    os.environ.get("MULTISTACK_KUBECONFIG", "~/.multistack/kubeconfig")
)

# Only kubeconfig_path, name and namespace decide what gets deleted, but the
# spec still validates as a whole — keep this matching examples/inference/serve.py
# rather than inventing a placeholder, since a mismatched name or namespace
# silently deletes nothing.
service = Inference(
    kubeconfig_path=KUBECONFIG,
    name="vllm-qwen",
)

InferenceBackend().delete(service)

print(f"\nRemoved {service.name} from namespace {service.resolved_namespace}")
print("The namespace itself was left in place. To remove it too:")
print(f"  kubectl --kubeconfig {service.kubeconfig_path} delete namespace {service.resolved_namespace}")
