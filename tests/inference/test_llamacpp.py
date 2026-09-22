"""The llamacpp implementation of the inference capability."""
import pytest

from multistack.inference import (
    Inference,
    InferenceBackend,
    InferencePrerequisiteError,
    LlamaCppOptions,
    VLLMOptions,
)
from multistack.inference.drivers import llamacpp as lc

KUBECONFIG = "/tmp/kc.yaml"
GPU_NODE = "gpu-01"


def spec(**kwargs) -> Inference:
    defaults = dict(
        kubeconfig_path=KUBECONFIG, type="llamacpp", name="llamacpp-qwen",
        model="bartowski/Qwen2.5-7B-Instruct-GGUF:Q4_K_M",
        port=8080, device="gpu", gpu_count=1, max_model_len=65536,
        node_selector={"kubernetes.io/hostname": GPU_NODE},
    )
    return Inference(**{**defaults, **kwargs})


@pytest.fixture
def driver():
    return lc.LlamaCppDriver()


def _objects(driver, s):
    return {o["kind"]: o for o in driver.manifests(s)}


def _pod(driver, s):
    return _objects(driver, s)["Deployment"]["spec"]["template"]["spec"]


# -- spec -----------------------------------------------------------------


def test_llamacpp_is_a_supported_type():
    assert "llamacpp" in Inference.SUPPORTED_TYPES


def test_it_gets_its_own_options_model():
    assert isinstance(spec().options, LlamaCppOptions)


def test_vllm_options_are_refused_for_llamacpp():
    """The reason `options` is a Union with a discriminator rather than a
    single model -- best-fit resolution put the wrong one on Policy."""
    with pytest.raises(ValueError):
        spec(options=VLLMOptions())


def test_the_image_is_pinned_by_digest():
    """llama.cpp publishes rolling tags, so a tag would change what the
    same spec deploys between two installs."""
    assert "@sha256:" in spec().options.image


def test_a_node_port_on_a_clusterip_service_is_refused():
    with pytest.raises(ValueError, match="no node ports"):
        spec(options=LlamaCppOptions(service_type="ClusterIP", node_port=30080))


def test_an_unknown_service_type_is_refused():
    with pytest.raises(ValueError, match="not a Service type"):
        spec(options=LlamaCppOptions(service_type="Nodeport"))


# -- arguments ------------------------------------------------------------


def test_the_context_window_comes_from_the_spec_not_the_options(driver):
    """llama.cpp's -c and vLLM's --max-model-len are the same thing, so it
    stays one field on the spec rather than two spellings."""
    args = driver.container_args(spec(max_model_len=4096))
    assert args[args.index("-c") + 1] == "4096"


def test_the_model_is_passed_as_an_hf_reference(driver):
    """-hf, not --model: llama.cpp fetches the GGUF itself, which is why
    this driver needs no S3 mirror step."""
    args = driver.container_args(spec())
    assert args[args.index("-hf") + 1].endswith("GGUF:Q4_K_M")


def test_a_cpu_deployment_offloads_no_layers(driver):
    args = driver.container_args(spec(device="cpu", gpu_count=0))
    assert args[args.index("-ngl") + 1] == "0"


def test_the_k_cache_quantisation_can_be_turned_off(driver):
    args = driver.container_args(spec(options=LlamaCppOptions(cache_type_k=None)))
    assert "--cache-type-k" not in args


def test_the_rendered_arguments_match_the_deployed_pod(driver):
    """Byte-for-byte against what runs on the cluster today -- the point
    of the driver is to reproduce it, not approximate it."""
    assert driver.container_args(spec()) == [
        "-hf", "bartowski/Qwen2.5-7B-Instruct-GGUF:Q4_K_M",
        "-ngl", "99", "--host", "0.0.0.0", "--port", "8080",
        "-c", "65536", "--cache-type-k", "q8_0", "-ub", "256", "-b", "512",
    ]


# -- manifests ------------------------------------------------------------


def test_the_gpu_is_requested_as_an_extended_resource(driver):
    container = _pod(driver, spec())["containers"][0]
    assert container["resources"]["limits"]["nvidia.com/gpu"] == "1"


def test_a_cpu_deployment_requests_no_gpu(driver):
    container = _pod(driver, spec(device="cpu", gpu_count=0))["containers"][0]
    assert "resources" not in container


def test_the_nvidia_runtime_class_is_asked_for(driver):
    """Without it the container starts and cannot see the card."""
    assert _pod(driver, spec())["runtimeClassName"] == "nvidia"


def test_the_runtime_class_can_be_omitted(driver):
    s = spec(options=LlamaCppOptions(runtime_class_name=None))
    assert "runtimeClassName" not in _pod(driver, s)


def test_the_weights_cache_is_a_host_path_that_outlives_the_cluster(driver):
    volume = _pod(driver, spec())["volumes"][0]
    assert volume["hostPath"]["path"] == "/var/lib/llamacpp-models"
    assert volume["hostPath"]["type"] == "DirectoryOrCreate"


def test_the_update_strategy_is_recreate(driver):
    """One card: a rolling update deadlocks, the new pod Pending on the
    GPU the old one still holds."""
    deployment = _objects(driver, spec())["Deployment"]
    assert deployment["spec"]["strategy"] == {"type": "Recreate"}


def test_there_is_no_liveness_probe(driver):
    """A cold start loads gigabytes onto the card; a liveness probe that
    fires during it restarts the pod forever."""
    container = _pod(driver, spec())["containers"][0]
    assert "livenessProbe" not in container
    assert container["readinessProbe"]["failureThreshold"] == 60


def test_the_node_port_is_pinned_when_asked(driver):
    """Published in agentic/backend/.env.example, so an allocation that drifts on
    rebuild leaves every copied .env pointing at nothing."""
    s = spec(options=LlamaCppOptions(node_port=30080))
    port = _objects(driver, s)["Service"]["spec"]["ports"][0]
    assert port["nodePort"] == 30080 and port["name"] == "http"


def test_no_node_port_is_rendered_by_default(driver):
    port = _objects(driver, spec())["Service"]["spec"]["ports"][0]
    assert "nodePort" not in port


# -- prerequisites --------------------------------------------------------


def test_a_cluster_advertising_no_gpu_is_refused(driver, monkeypatch):
    monkeypatch.setattr(lc, "require_cli", lambda *a, **k: None)
    monkeypatch.setattr(lc, "require_cluster", lambda *a, **k: None)
    monkeypatch.setattr(lc, "kube_kubectl", lambda *a, **k: "  ")
    with pytest.raises(InferencePrerequisiteError, match="nvidia.com/gpu"):
        driver.check_prerequisites(spec())


def test_an_unpinned_deployment_warns_about_the_cache(driver, monkeypatch):
    monkeypatch.setattr(lc, "require_cli", lambda *a, **k: None)
    monkeypatch.setattr(lc, "require_cluster", lambda *a, **k: None)
    monkeypatch.setattr(lc, "kube_kubectl", lambda *a, **k: "1 ")
    warnings = driver.check_prerequisites(spec(node_selector={}))
    assert any("re-downloads" in w for w in warnings)


# -- registry -------------------------------------------------------------


def test_the_registry_resolves_both_implementations():
    assert set(InferenceBackend.DRIVERS) == set(Inference.SUPPORTED_TYPES)
    assert InferenceBackend.resolve_all()["llamacpp"] is lc.LlamaCppDriver
