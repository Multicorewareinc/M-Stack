"""
Serve a model through KServe's LLMInferenceService, with its weights kept
on the GPU machine's own disk so a restart or reboot does not download them
again.

    pip install -e ".[helm]"              # from the repo root
    python3 examples/inference/serve_kserve_llm.py

The weights come from an object store the stack already has (SeaweedFS in
examples/full_stack.py), under a versioned path that never changes:
s3://models/<model>/<version>/. A new version is a new path.

What create() does on a cluster that has none of this yet: installs KServe
(with its local model cache switched on), the LLMInferenceService controller
and presets, and the Gateway API CRDs; labels the cache nodes and prepares
the cache folder on each; then creates a LocalModelCache for the model and
the LLMInferenceService itself. A cluster whose KServe was installed by
someone else is never upgraded -- create() reports what it could not add.

Set MODEL_CACHE=stream instead to read the weights straight from the
object store into GPU memory. That needs an image built with `vllm[runai]`.

To remove it again (the model cache stays unless asked):
    InferenceBackend().delete(service, remove_cache=True)
"""

import os

from multistack import Inference, InferenceBackend
from multistack.inference import KServeLLMOptions, KServeModelCache, KServePlatform

KUBECONFIG = os.path.expanduser(os.environ.get("MULTISTACK_KUBECONFIG", "~/.multistack/kubeconfig"))
# Kubernetes node names of the GPU machines that keep model copies.
CACHE_NODES = os.environ.get("MULTISTACK_CACHE_NODES", "gpu-node-1").split(",")
MODEL_CACHE = os.environ.get("MODEL_CACHE", "node")  # or "stream"

platform = KServePlatform(
    model_cache=KServeModelCache(
        enabled=MODEL_CACHE == "node",
        nodes=CACHE_NODES,
        # The copy lives in /models on each node: KServe's node agent
        # hardcodes that path, so `path` is left at its default. For disk
        # speed, mount a dedicated NVMe disk *at* /models.
        size_gb=200,
    ),
)

cache = (
    {"model_cache": "node", "cache_size_gb": 2}
    if MODEL_CACHE == "node"
    else {"model_cache": "stream"}
)

service = Inference(
    type="kserve_llm",
    kubeconfig_path=KUBECONFIG,
    name="qwen",
    model="s3://models/qwen2.5-0.5b/v1",
    s3_endpoint_url=os.environ.get(
        "MULTISTACK_S3_ENDPOINT", "http://seaweedfs-s3.seaweedfs.svc.cluster.local:8333"
    ),
    # The Secret's NAME, holding AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY.
    s3_secret_name="seaweedfs-creds",
    device="gpu",
    gpu_count=1,
    # What the AMD device plugin advertises; NVIDIA clusters use the default.
    gpu_resource="amd.com/gpu",
    # The ROCm vLLM build for gfx1151; None keeps KServe's preset image.
    image=os.environ.get("MULTISTACK_VLLM_IMAGE") or None,
    options=KServeLLMOptions(platform=platform),
    **cache,
)

endpoint = InferenceBackend().create(service)
print(f"serving at {endpoint}")
