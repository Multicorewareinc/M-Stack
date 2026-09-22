---
name: accelerator
description: Use this skill when the user's request mentions a GPU, accelerator, nvidia, device plugin, CUDA, or making graphics cards schedulable for workloads. Covers the real MultiStack SDK Accelerator/AcceleratorBackend classes.
---

# MultiStack SDK — Accelerator / AcceleratorBackend

Real class signature (from `multistack.accelerator` — **not** re-exported
at the top level, unlike `Gateway`/`Policy`; always import from
`multistack.accelerator` directly, same as `IngressGateway`).

Kubernetes does not know a node has a GPU. Until something advertises one
as an extended resource, `nvidia.com/gpu` is not a thing a pod can ask
for — so a GPU workload stays `Pending`, and a cluster with a healthy
card in it looks exactly like a cluster with none. This capability
installs the thing that does the advertising.

```python
class Accelerator(CapabilitySpec):
    type: str = "nvidia_device_plugin"   # the only implementation today
    kubeconfig_path: str                 # required -- the EXISTING cluster
    namespace: Optional[str] = None      # defaults to "kube-system"
    options: Optional[DevicePluginOptions] = None
    node_selector: Optional[Dict[str, str]] = None
    tolerations: Optional[List[Dict[str, Any]]] = None
```

Rules when generating code:
- `kubeconfig_path` is required and has no ambient fallback.
- Accelerator depends on a cluster only — nothing else needs to exist first.
- **This is what a `device="gpu"` Inference deployment needs first.** Without it, `nvidia.com/gpu` is not a resource any pod can request, so the model server stays `Pending` with nothing explaining why. If the user asks for GPU serving, say this layer is needed too (see the `inference` skill).
- `node_selector`/`tolerations` restrict which nodes the plugin runs on. Set them when the user says which nodes actually hold the cards — a node name or label is a real fact about their cluster, never one to invent.
- The device plugin **advertises the resource and nothing else**. NVIDIA's GPU Operator is a different thing: it also owns the driver, the container toolkit and DCGM, so on a node whose drivers are already installed it needs `driver.enabled=false` or it fights them. It is deliberately not the default here.
- **This capability publishes nothing for other layers to consume** — what it changes is a node property, not an address. There is no endpoint to report or wire anywhere, so never invent one.
