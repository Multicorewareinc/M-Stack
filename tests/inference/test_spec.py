"""Inference spec validation, manifest rendering, and state tracking.
Nothing shells out — kubectl/ssh calls are stubbed or never reached."""
import pytest

import multistack.state.tracking as tracking
from multistack import Inference, InferenceBackend
from multistack.inference.spec import DEFAULT_MIRROR_IMAGE, DEFAULT_MODEL
from multistack.inference.drivers.vllm import VLLMDriver
from multistack.stack import Stack

KC = "/tmp/kc.yaml"
KUBECONFIG = KC          # the ported tests below use this name


def inference(**kwargs) -> Inference:
    base = dict(kubeconfig_path=KC)
    base.update(kwargs)
    return Inference(**base)


# -- spec defaults and validation ----------------------------------------
def test_defaults_to_qwen_0_5_but_model_stays_a_normal_field():
    assert inference().model == DEFAULT_MODEL == "Qwen/Qwen2.5-0.5B-Instruct"
    assert inference(model="Qwen/Qwen2.5-1.5B-Instruct").model == "Qwen/Qwen2.5-1.5B-Instruct"


def test_kubeconfig_path_is_required():
    with pytest.raises(ValueError):
        Inference(kubeconfig_path="")


def test_namespace_defaults_from_the_type():
    assert inference().resolved_namespace == "inference"
    assert inference(namespace="custom").resolved_namespace == "custom"


def test_endpoint_is_the_in_cluster_service_dns_name():
    assert inference().endpoint == "http://vllm.inference.svc.cluster.local:8000"


def test_memory_must_exceed_kv_cache_plus_shm():
    with pytest.raises(ValueError, match="memory_gb"):
        inference(memory_gb=4, kv_cache_gb=2, shm_gb=2)


def test_gpu_device_needs_at_least_one_gpu():
    with pytest.raises(ValueError, match="gpu_count"):
        inference(device="gpu", gpu_count=0)


def test_s3_model_needs_endpoint_and_secret():
    with pytest.raises(ValueError, match="s3_endpoint_url"):
        inference(model="s3://models/qwen")
    with pytest.raises(ValueError, match="s3_secret_name"):
        inference(model="s3://models/qwen", s3_endpoint_url="http://minio:9000")


# -- Stack wiring ----------------------------------------------------------
def test_stack_publishes_the_endpoint_for_a_gateway_to_consume():
    stack = Stack(kubeconfig_path=KC)
    service = stack.build(Inference)
    stack.record(service)
    assert stack.get("inference_endpoint") == service.endpoint


# -- manifest rendering (VLLMDriver) ---------------------------------------
@pytest.fixture
def driver():
    return VLLMDriver()


def test_manifests_render_namespace_deployment_service(driver):
    kinds = [o["kind"] for o in driver.manifests(inference())]
    assert kinds == ["Namespace", "Deployment", "Service"]


def test_default_model_needs_no_mirror_init_container(driver):
    deployment = driver.manifests(inference())[1]
    assert "initContainers" not in deployment["spec"]["template"]["spec"]


def test_dtype_override_is_rendered_in_container_args(driver):
    deployment = driver.manifests(inference(), dtype="bfloat16")[1]
    args = deployment["spec"]["template"]["spec"]["containers"][0]["args"]
    assert "--dtype" in args and args[args.index("--dtype") + 1] == "bfloat16"


def test_requests_equal_limits_for_guaranteed_qos(driver):
    deployment = driver.manifests(inference(cpu_cores=6, memory_gb=12))[1]
    c = deployment["spec"]["template"]["spec"]["containers"][0]
    assert c["resources"]["requests"] == c["resources"]["limits"]


def test_s3_model_gets_a_mirror_init_container(driver):
    deployment = driver.manifests(inference(
        model="s3://models/qwen", s3_endpoint_url="http://minio:9000",
        s3_secret_name="minio-creds",
    ))[1]
    init = deployment["spec"]["template"]["spec"]["initContainers"][0]
    assert init["name"] == "fetch-weights"
    assert "mc mirror" in " ".join(init["command"])


# -- state tracking (multistack/state/tracking.py) -------------------------
@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTISTACK_STATE_DB", str(tmp_path / "state.db"))
    tracking._default = None
    yield
    tracking._default = None


def _seed_healthy_cluster(state):
    state.start("ai-cluster", component_type="cluster")
    state.mark_healthy("ai-cluster")


def test_create_is_tracked_as_the_inference_component(isolated_state, monkeypatch):
    backend = InferenceBackend()
    _seed_healthy_cluster(tracking.default_state_manager())
    monkeypatch.setattr(
        backend, "driver_for",
        lambda spec: type("D", (), {"create": staticmethod(lambda s, nodes=None: "http://vllm:8000")})(),
    )

    service = inference(name="vllm-qwen")
    assert backend.create(service) == "http://vllm:8000"

    state = tracking.default_state_manager()
    deployment = state.get("vllm-qwen")
    assert deployment.component_type == "inference"
    assert deployment.status.value == "provisioned"


def test_failed_create_is_marked_failed(isolated_state, monkeypatch):
    backend = InferenceBackend()
    _seed_healthy_cluster(tracking.default_state_manager())

    def boom(spec):
        raise RuntimeError("no ready replicas")

    monkeypatch.setattr(
        backend, "driver_for",
        lambda spec: type("D", (), {"create": staticmethod(
            lambda s, nodes=None: boom(s))})(),
    )

    with pytest.raises(RuntimeError, match="no ready replicas"):
        backend.create(inference(name="vllm-qwen"))

    deployment = tracking.default_state_manager().get("vllm-qwen")
    assert deployment.status.value == "failed"


# ======================================================================
# Ported from tests/core/test_vllm.py, deleted when vLLM became the
# inference capability. tests/inference/test_spec.py as written covered 15
# of the 23 cases that file had, so these are the rest -- validation rules
# and rendering properties that the migration kept but stopped checking.
#
# Three were dropped as genuine duplicates of tests above:
# memory-must-cover-kv-cache-and-shm, and the two halves of
# s3-model-requires-endpoint-and-secret.
# ======================================================================


MODEL = "Qwen/Qwen2.5-0.5B-Instruct"


def inf(**kwargs) -> Inference:
    """The old file's `svc()` helper. `model` now has a default, so it is
    still passed explicitly here to keep these tests independent of it."""
    return Inference(kubeconfig_path=KUBECONFIG, model=MODEL, **kwargs)


def s3_svc(**kwargs) -> Inference:
    """An s3:// spec, which is the only shape that grows a mirror init
    container. The old file's helper of the same name."""
    base = dict(
        model="s3://models/qwen",
        s3_endpoint_url="https://minio.minio.svc",
        s3_secret_name="minio-creds",
    )
    base.update(kwargs)
    return Inference(kubeconfig_path=KUBECONFIG, **base)

def test_valid_spec_passes():
    inf().validate()


def test_kubeconfig_and_model_are_required():
    with pytest.raises(ValueError, match="kubeconfig_path is required"):
        Inference(kubeconfig_path="", model=MODEL).validate()
    with pytest.raises(ValueError, match="model is required"):
        Inference(kubeconfig_path=KUBECONFIG, model="").validate()


def test_rejects_unknown_device_and_dtype():
    with pytest.raises(ValueError, match="Invalid device"):
        inf(device="tpu").validate()
    with pytest.raises(ValueError, match="Invalid dtype"):
        inf(dtype="int4").validate()


def test_gpu_and_cpu_settings_must_agree():
    with pytest.raises(ValueError, match="needs gpu_count >= 1"):
        inf(device="gpu", gpu_count=0).validate()
    with pytest.raises(ValueError, match="device is 'cpu'"):
        inf(device="cpu", gpu_count=2).validate()


def test_shm_must_be_big_enough():
    # k8s gives 64Mi by default and vLLM's shm queue needs more, which is a
    # hard startup failure, so a sub-1GB request is rejected outright.
    with pytest.raises(ValueError, match="shm_gb"):
        inf(shm_gb=0).validate()


def test_host_network_conflicts_with_multiple_replicas():
    with pytest.raises(ValueError, match="contend for host port"):
        inf(replicas=2, host_network=True).validate()


def test_s3_endpoint_requires_an_s3_model_path():
    with pytest.raises(ValueError, match="isn't an s3:// path"):
        inf(s3_endpoint_url="http://minio:9000").validate()
    s3_svc().validate()


def test_s3_model_is_mirrored_to_a_local_path():
    # runai_model_streamer isn't in vLLM's CPU image, so vLLM must be
    # pointed at a local directory, not the s3:// URL.
    service = s3_svc()
    assert service.model_path == "/models/qwen"
    args = service.container_args()
    assert "--model" in args and args[args.index("--model") + 1] == "/models/qwen"


def test_served_model_name_hides_the_mirror_path():
    # Clients keep using the model name, not the local directory.
    args = s3_svc().container_args()
    assert args[args.index("--served-model-name") + 1] == "qwen"
    assert s3_svc(served_model_name="Qwen2.5").served_name == "Qwen2.5"


def test_non_s3_model_is_passed_through_unchanged():
    args = inf().container_args()
    assert args[args.index("--model") + 1] == MODEL
    assert "--served-model-name" not in args


def test_mirror_command_carries_no_credentials():
    # They come from a Secret as env vars; the manifest must stay clean.
    shell = " ".join(s3_svc().mirror_command())
    assert "$AWS_ACCESS_KEY_ID" in shell and "$AWS_SECRET_ACCESS_KEY" in shell
    assert "minio-creds" not in shell
    assert "src/models/qwen" in shell and "/models/qwen" in shell


def test_insecure_tls_is_opt_in():
    # Self-signed MinIO autocert needs it; a real endpoint must not get it.
    assert "--insecure" not in " ".join(s3_svc().mirror_command())
    assert "--insecure" in " ".join(s3_svc(s3_insecure_tls=True).mirror_command())


def test_image_defaults_per_device():
    assert "cpu" in inf().container_image
    assert "cpu" not in inf(device="gpu", gpu_count=1).container_image
    assert inf(image="my/vllm:1").container_image == "my/vllm:1"


def test_container_args_carry_model_and_dtype_override():
    args = inf(dtype="bfloat16").container_args(dtype="float32")
    assert "--model" in args and MODEL in args
    # The backend's node-derived dtype wins over the spec's.
    assert args[args.index("--dtype") + 1] == "float32"


def test_multi_gpu_sets_tensor_parallelism():
    args = inf(device="gpu", gpu_count=4).container_args()
    assert args[args.index("--tensor-parallel-size") + 1] == "4"


def test_extra_args_are_appended():
    assert "--enforce-eager" in inf(extra_args=["--enforce-eager"]).container_args()


def test_cpu_env_includes_the_non_obvious_required_settings():
    env = inf(cpu_cores=6, kv_cache_gb=4).env()

    # Required by the CPU backend.
    assert env["VLLM_CPU_KVCACHE_SPACE"] == "4"
    # Without this vLLM silently drops to a single serving thread.
    assert env["OMP_NUM_THREADS"] == "6"
    assert env["VLLM_CPU_OMP_THREADS_BIND"] == "0-5"


def test_gpu_env_omits_cpu_only_settings():
    env = inf(device="gpu", gpu_count=1).env()
    assert "VLLM_CPU_KVCACHE_SPACE" not in env
    assert "OMP_NUM_THREADS" not in env


def test_serving_container_gets_no_s3_env():
    # With a mirrored model the serving container reads local disk and
    # never talks to the object store, so it needs no endpoint or keys.
    env = s3_svc().env()
    assert not any(k.startswith("AWS_") or k.startswith("RUNAI_") for k in env)


def test_default_mirror_image_is_pinned_and_pullable_anonymously():
    """The default weights-mirror image must not rot silently.

    `minio/mc:latest` was the default for months and nothing referenced it
    in tests or docs. When MinIO stopped serving anonymous pulls from
    Docker Hub, every s3:// vLLM deployment started failing on the init
    container with `insufficient_scope: authorization failed` — a registry
    change with no commit in this repo to point at.

    Two properties, both of which would have caught it:

    - a registry that serves anonymous pulls. docker.io/minio/* no longer
      does, for any tag.
    - an explicit tag. `:latest` means the image a deployment gets is
      whatever the registry last pushed, so a working build and a broken
      one are the same source tree.
    """
    image = DEFAULT_MIRROR_IMAGE
    registry, _, tagged = image.partition("/")
    assert registry == "quay.io", (
        f"{image} is not on quay.io — docker.io/minio/mc returns 401 to "
        f"anonymous pulls, which is unpullable from a cluster with no "
        f"registry credentials"
    )
    _, _, tag = tagged.rpartition(":")
    assert tag and tag != "latest", f"{image} must pin a tag, not 'latest'"
    # On options now, not the spec: deploy mechanics moved to VLLMOptions
    # in the inference migration, which is also where the old literal
    # survived in the driver as an unreachable `if options else` fallback.
    assert s3_svc().options.s3_mirror_image == image

    # And the driver must read it from there rather than carry its own
    # copy -- that duplicate is exactly what kept minio/mc:latest alive
    # after the default moved.
    init = next(
        c for c in VLLMDriver().manifests(s3_svc())[1]["spec"]["template"]
        ["spec"]["initContainers"]
    )
    assert init["image"] == image
