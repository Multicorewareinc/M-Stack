"""What the reference stack's KServe branch does, checked against its source.

Split out of `test_kserve.py` because these two assert on
`examples/full_stack.py` rather than on the driver. That file and this
one change together, and keeping them in the driver's suite meant the
driver could not be reviewed without the example in the same change --
which is exactly what happened: the inference fixes and the example
rewrite are separate pieces of work and were landing as separate PRs.

Static, like `tests/test_examples.py`: running either stage needs a
cluster, so the source is what gets checked.
"""


# -- the reference stack's KServe branch ---------------------------------
def test_the_example_pins_its_predictor_to_a_worker():
    """A driver capability nothing used.

    `test_node_selector_and_tolerations_reach_the_predictor` above has
    passed since this driver was written -- the fields work. The KServe
    branch of `examples/full_stack.py` simply never set them, on the
    stated grounds that "KServe places the predictor itself", while the
    vLLM branch beside it reserved a node. On 2026-10-09 KServe placed a
    4-CPU/12Gi predictor on rke2-cp01, which this cluster leaves
    untainted, and that is the one node these deployments are supposed to
    stay off.

    Static, like `tests/test_examples.py`: running the stage needs a
    cluster, so the source is what gets checked.
    """
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "examples" / "full_stack.py").read_text()
    branch = source[source.index("def _inference_kserve") : source.index("def stage_route")]

    assert "node_selector=" in branch, (
        "the KServe branch sets no node_selector, so the scheduler may "
        "put the predictor on the control plane"
    )
    assert "tolerations=" in branch, (
        "the KServe branch selects a reserved node without tolerating the "
        "taint that reserves it, so the predictor stays Pending"
    )


def test_the_metrics_stage_follows_the_inference_type():
    """It deployed a vLLM onto a KServe cluster.

    `stage_vllm_metrics` re-applies the inference spec to attach a
    ServiceMonitor, and `InferenceBackend().create()` is a full manifest
    apply -- so building that spec from `_vllm_qwen_kwargs`
    unconditionally does not add a monitor to a KServe predictor, it
    stands up a second server. On 2026-10-09 `vllm-qwen` appeared beside
    a healthy `kserve-qwen-predictor`: two deployments of one model,
    4 CPU and 12Gi each.

    Both drivers render a ServiceMonitor from `service_monitor_labels`,
    so the stage only ever needed to build the spec its own inference
    stage builds. The shared factory is what keeps the two from drifting
    -- the same fix the vLLM branch got on 2026-09-25, which the KServe
    branch never received.
    """
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "examples" / "full_stack.py").read_text()
    stage = source[source.index("def stage_vllm_metrics") : source.index("def main")]

    assert "_kserve_qwen_kwargs" in stage, (
        "the metrics stage builds a vLLM spec whatever the inference "
        "type is, so on a KServe cluster it deploys a second server "
        "instead of attaching a ServiceMonitor"
    )
    assert 'INFERENCE_TYPE == "kserve"' in stage

    # And the KServe branch has to go through the same factory, or the
    # two drift apart again -- which is the whole failure mode.
    branch = source[source.index("def _inference_kserve") : source.index("def _kserve_qwen_kwargs")]
    assert "_kserve_qwen_kwargs(" in branch


def _full_stack(monkeypatch, inference_type):
    """examples/full_stack.py imported as a module under one inference type."""
    import importlib.util
    from pathlib import Path

    monkeypatch.setenv("MULTISTACK_INFERENCE_TYPE", inference_type)
    path = Path(__file__).resolve().parents[2] / "examples" / "full_stack.py"
    spec = importlib.util.spec_from_file_location("full_stack_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_kserve_llm_is_built_from_the_kserve_fields(monkeypatch):
    """kserve_llm shares the KServe branch's model, resources and node
    reservation -- built on `_kserve_qwen_kwargs`, so the two cannot drift."""
    fs = _full_stack(monkeypatch, "kserve_llm")
    endpoint = "http://seaweedfs-s3.seaweedfs.svc.cluster.local:8333"
    kserve = fs._kserve_qwen_kwargs(s3_endpoint_url=endpoint)
    llm = fs._kserve_llm_qwen_kwargs(s3_endpoint_url=endpoint)

    assert llm["type"] == "kserve_llm"
    for field in (
        "model",
        "s3_secret_name",
        "cpu_cores",
        "memory_gb",
        "node_selector",
        "tolerations",
    ):
        assert llm[field] == kserve[field], field
    # The gateway is pointed at what this stage deploys.
    assert fs._inference_probe().endpoint.startswith("http://llm-qwen-kserve-workload-svc.")
    assert fs._served_model_name() == "llm-qwen"


def test_an_unknown_inference_type_is_refused(monkeypatch):
    """Anything else used to fall through to vLLM and deploy the wrong thing."""
    import pytest

    with pytest.raises(SystemExit, match="kserve-llm"):
        _full_stack(monkeypatch, "kserve-llm")


def test_per_node_ssh_credentials(monkeypatch):
    """A node with its own login is reached with it; the GPU node and the
    rest keep theirs. Without this a fifth node with a different user
    could not be part of the cluster stage at all."""
    monkeypatch.setenv("MULTISTACK_SERVER", "10.0.0.1")
    monkeypatch.setenv("MULTISTACK_AGENTS", "10.0.0.2,10.0.0.3,10.0.0.4")
    monkeypatch.setenv("MULTISTACK_SSH_USER", "ubuntu")
    monkeypatch.setenv("MULTISTACK_SSH_KEY", "~/.ssh/default")
    monkeypatch.setenv("MULTISTACK_GPU_NODE", "10.0.0.3")
    monkeypatch.setenv("MULTISTACK_GPU_SSH_USER", "gpuuser")
    monkeypatch.setenv("MULTISTACK_GPU_SSH_KEY", "~/.ssh/gpu")
    monkeypatch.setenv("MULTISTACK_NODE_SSH", "10.0.0.4=operator:~/.ssh/id_ed25519")
    fs = _full_stack(monkeypatch, "vllm")
    creds = {n.address: (n.user, n.ssh_key) for n in fs.NODES}
    assert creds["10.0.0.1"] == ("ubuntu", "~/.ssh/default")
    assert creds["10.0.0.3"] == ("gpuuser", "~/.ssh/gpu")
    assert creds["10.0.0.4"] == ("operator", "~/.ssh/id_ed25519")


def test_a_malformed_node_ssh_entry_is_refused(monkeypatch):
    import pytest

    monkeypatch.setenv("MULTISTACK_NODE_SSH", "10.0.0.4=operator")
    with pytest.raises(SystemExit, match="address=user:key"):
        _full_stack(monkeypatch, "vllm")


def test_route_brings_the_front_door_along(monkeypatch):
    """A resume that names `route` but not `ingress` still routes: the
    ingress stage only adopts what is live, and without it route skips."""
    monkeypatch.setenv("MULTISTACK_STAGES", "database,route")
    assert "ingress" in _full_stack(monkeypatch, "kserve_llm").STAGES


def test_ingress_is_not_added_when_route_is_not_run(monkeypatch):
    monkeypatch.setenv("MULTISTACK_STAGES", "inference")
    assert _full_stack(monkeypatch, "kserve_llm").STAGES == ["inference"]


def test_kserve_llm_keeps_its_weights_on_the_inference_node(monkeypatch):
    """MULTISTACK_MODEL_CACHE=node (the default) caches on exactly the node
    the pod is pinned to -- a cache anywhere else leaves it Pending."""
    fs = _full_stack(monkeypatch, "kserve_llm")
    endpoint = "http://seaweedfs-s3.seaweedfs.svc.cluster.local:8333"
    llm = fs._kserve_llm_qwen_kwargs(s3_endpoint_url=endpoint, cache_node="worker-1")

    assert llm["model_cache"] == "node"
    cache = llm["options"].platform.model_cache
    assert cache.enabled and cache.nodes == ["worker-1"]
    assert cache.size_gb == 50
    assert llm["cache_size_gb"] == 2


def test_kserve_llm_without_a_cache_node_reads_from_object_storage(monkeypatch):
    fs = _full_stack(monkeypatch, "kserve_llm")
    llm = fs._kserve_llm_qwen_kwargs(s3_endpoint_url="http://s3:8333")

    assert llm["model_cache"] == "none"
    assert not llm["options"].platform.model_cache.enabled


def test_an_unknown_model_cache_is_refused(monkeypatch):
    import pytest

    monkeypatch.setenv("MULTISTACK_MODEL_CACHE", "stream")
    with pytest.raises(SystemExit, match="MULTISTACK_MODEL_CACHE"):
        _full_stack(monkeypatch, "kserve_llm")


def test_the_gateway_is_handed_the_cache(monkeypatch):
    """Without VALKEY_URL in its Secret the gateway falls back to the
    chart's redis://localhost and runs with no API-key cache."""
    import inspect

    fs = _full_stack(monkeypatch, "kserve_llm")
    assert "cache_auth_url" in inspect.signature(fs.stage_gateway).parameters
    main = inspect.getsource(fs.main)
    assert "stage_gateway(upstream, policy_endpoints, cache_auth_url)" in main
    assert '{"policy", "gateway", "controlplane"}' in main
    assert '"VALKEY_URL"' in inspect.getsource(fs.stage_gateway)
