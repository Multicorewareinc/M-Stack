"""The kserve_llm implementation: an LLMInferenceService.

The shapes asserted here come from KServe v0.20.0's own API types and its
`kserve-config-llm-template` preset, not from a live cluster -- which is
why several tests name the preset fact they depend on (the `main`
container, port 8000, the workload Service's name). If a live run proves
one of those wrong, the test that names it is the one to change.

Nothing shells out; `_kubectl` is the one seam, as in test_kserve.py.
"""

import json

import pytest

from multistack.inference import (
    Inference,
    InferenceBackend,
    InferencePrerequisiteError,
    KServeLLMOptions,
    KServePlatform,
)
from multistack.inference.drivers import kserve as ks
from multistack.inference.drivers import kserve_llm as kl
from multistack.inference.spec import DEFAULT_CPU_IMAGE

KUBECONFIG = "/tmp/kc.yaml"
S3 = dict(
    model="s3://models/qwen2.5-0.5b/v1",
    s3_endpoint_url="http://seaweedfs-s3.objectstore:8333",
    s3_secret_name="seaweedfs-creds",
)
ROCM_IMAGE = "dev.local/vllm-gfx1151:optimized"


def spec(**kwargs) -> Inference:
    defaults = dict(
        kubeconfig_path=KUBECONFIG,
        type="kserve_llm",
        name="qwen",
        device="gpu",
        gpu_count=1,
        gpu_resource="amd.com/gpu",
        image=ROCM_IMAGE,
        **S3,
    )
    return Inference(**{**defaults, **kwargs})


@pytest.fixture
def driver():
    return kl.KServeLLMDriver()


def llmisvc(driver, s):
    return next(o for o in driver.manifests(s) if o["kind"] == "LLMInferenceService")


def main_container(driver, s):
    return llmisvc(driver, s)["spec"]["template"]["containers"][0]


def stub_kubectl(monkeypatch, replies=None, record=None):
    replies = replies or {}
    for module in (ks, kl):
        monkeypatch.setattr(module, "require_cli", lambda *a, **k: "/usr/bin/kubectl")
        monkeypatch.setattr(module, "require_cluster", lambda *a, **k: "v1.32.5")

    def fake(self, inference, *args, check=True):
        joined = " ".join(args)
        if record is not None:
            record.append(joined)
        for needle, reply in replies.items():
            if needle in joined:
                return reply
        return ""

    monkeypatch.setattr(ks.KServeDriver, "_kubectl", fake)


INSTALLED = {
    f"crd {kl.LLMISVC_CRD}": "crd/x",
    f"llminferenceserviceconfig {kl.LLM_TEMPLATE_CONFIG}": "config/x",
}


# -- registration -----------------------------------------------------------
def test_kserve_llm_is_a_registered_type_with_its_own_options():
    assert "kserve_llm" in InferenceBackend.DRIVERS
    assert isinstance(spec().options, KServeLLMOptions)


def test_the_workload_service_is_kserves_name_for_it():
    # kmeta.ChildName(name, "-kserve-workload-svc") in the llmisvc controller.
    assert spec().endpoint == ("http://qwen-kserve-workload-svc.inference.svc.cluster.local:8000")


def test_tls_switches_the_endpoint_scheme():
    assert spec(options={"tls": True}).endpoint.startswith("https://")


# -- options ----------------------------------------------------------------
def test_scheduler_needs_a_gateway_route():
    with pytest.raises(ValueError, match="router='gateway'"):
        spec(options={"scheduler": True})


def test_gateway_name_needs_the_gateway_router():
    with pytest.raises(ValueError, match="no route would use them"):
        spec(options={"gateway_name": "inference-gw"})


def test_service_monitor_labels_are_refused_rather_than_guessed():
    with pytest.raises(ValueError, match="service_monitor_labels"):
        spec(service_monitor_labels={"release": "observability"})


# -- the resource -------------------------------------------------------------
def test_renders_the_v1alpha2_resource(driver):
    resource = llmisvc(driver, spec())
    assert resource["apiVersion"] == "serving.kserve.io/v1alpha2"
    assert resource["spec"]["model"] == {"uri": "s3://models/qwen2.5-0.5b/v1"}
    assert resource["spec"]["replicas"] == 1


def test_a_huggingface_model_becomes_an_hf_uri(driver):
    resource = llmisvc(
        driver, spec(model="Qwen/Qwen2.5-0.5B-Instruct", s3_endpoint_url=None, s3_secret_name=None)
    )
    assert resource["spec"]["model"]["uri"] == "hf://Qwen/Qwen2.5-0.5B-Instruct"


def test_our_image_goes_on_the_presets_main_container(driver):
    # Named "main", or KServe adds a second container instead of merging.
    main = main_container(driver, spec())
    assert main["name"] == "main"
    assert main["image"] == ROCM_IMAGE


def test_the_presets_image_is_kept_when_none_is_given(driver):
    assert "image" not in main_container(driver, spec(image=None))


def test_the_gpu_resource_is_the_clusters_own(driver):
    resources = main_container(driver, spec())["resources"]
    assert resources["limits"]["amd.com/gpu"] == "1"
    assert "nvidia.com/gpu" not in resources["limits"]


def test_flags_are_appended_to_the_presets_command(driver):
    main = main_container(driver, spec(max_model_len=8192, extra_args=["--enforce-eager"]))
    assert "command" not in main, "the preset's command must be kept outside stream mode"
    assert main["args"] == ["--max-model-len", "8192", "--enforce-eager"]


def test_served_model_name_sets_the_model_name(driver):
    resource = llmisvc(driver, spec(served_model_name="qwen-small"))
    assert resource["spec"]["model"]["name"] == "qwen-small"


def test_s3_models_get_a_storage_account_bound_to_the_secret(driver):
    objects = driver.manifests(spec())
    account = next(o for o in objects if o["kind"] == "ServiceAccount")
    assert account["metadata"]["name"] == "qwen-storage"
    assert account["secrets"] == [{"name": "seaweedfs-creds"}]
    assert llmisvc(driver, spec())["spec"]["template"]["serviceAccountName"] == "qwen-storage"


def test_multi_gpu_sets_tensor_parallelism(driver):
    resource = llmisvc(driver, spec(gpu_count=2))
    assert resource["spec"]["parallelism"] == {"tensor": 2}


def test_router_none_leaves_only_the_workload_service(driver):
    assert "router" not in llmisvc(driver, spec())["spec"]


def test_router_gateway_asks_for_a_managed_route(driver):
    resource = llmisvc(
        driver,
        spec(
            options={
                "router": "gateway",
                "gateway_name": "inference-gw",
                "gateway_namespace": "istio-system",
                "scheduler": True,
            }
        ),
    )
    assert resource["spec"]["router"] == {
        "gateway": {"refs": [{"name": "inference-gw", "namespace": "istio-system"}]},
        "route": {},
        "scheduler": {},
    }


def test_network_policy_selects_pods_by_our_own_label(driver):
    objects = driver.manifests(spec(allowed_client_labels=[{"app": "model-gateway"}]))
    policy = next(o for o in objects if o["kind"] == "NetworkPolicy")
    assert policy["spec"]["podSelector"]["matchLabels"] == {kl.POD_LABEL: "qwen"}
    assert llmisvc(driver, spec())["spec"]["labels"] == {kl.POD_LABEL: "qwen"}


# -- model_cache="node" ------------------------------------------------------
def test_node_mode_renders_a_matching_cache_before_the_resource(driver):
    objects = driver.manifests(spec(model_cache="node", cache_size_gb=2, cache_scope="cluster"))
    kinds = [o["kind"] for o in objects]
    assert kinds.index("LocalModelCache") < kinds.index("LLMInferenceService")
    cache = next(o for o in objects if o["kind"] == "LocalModelCache")
    assert (
        cache["spec"]["sourceModelUri"]
        == llmisvc(driver, spec(model_cache="node", cache_size_gb=2, cache_scope="cluster"))[
            "spec"
        ]["model"]["uri"]
    )


# -- model_cache="stream" ----------------------------------------------------
def test_stream_turns_off_the_storage_initializer(driver):
    resource = llmisvc(driver, spec(model_cache="stream"))
    assert resource["spec"]["storageInitializer"] == {"enabled": False}


def test_stream_replaces_the_command_with_the_streamer(driver):
    # The preset serves /mnt/models, which nothing fills with the
    # storage-initializer off.
    main = main_container(driver, spec(model_cache="stream"))
    assert main["command"][:3] == ["vllm", "serve", "s3://models/qwen2.5-0.5b/v1"]
    assert main["command"][main["command"].index("--load-format") + 1] == "runai_streamer"
    assert main["command"][main["command"].index("--port") + 1] == "8000"
    assert main["command"][main["command"].index("--served-model-name") + 1] == "qwen"


def test_stream_with_tls_serves_the_presets_certificate(driver):
    command = main_container(driver, spec(model_cache="stream", options={"tls": True}))["command"]
    assert command[command.index("--ssl-certfile") + 1] == kl.TLS_CERT


def test_stream_passes_credentials_by_reference_only(driver):
    env = {e["name"]: e for e in main_container(driver, spec(model_cache="stream"))["env"]}
    assert env["RUNAI_STREAMER_S3_USE_VIRTUAL_ADDRESSING"]["value"] == "0"
    assert "value" not in env["AWS_ACCESS_KEY_ID"]
    assert env["AWS_ACCESS_KEY_ID"]["valueFrom"]["secretKeyRef"]["name"] == "seaweedfs-creds"


def test_stream_needs_no_storage_account(driver):
    objects = driver.manifests(spec(model_cache="stream"))
    assert not any(o["kind"] == "ServiceAccount" for o in objects)
    assert (
        "serviceAccountName" not in llmisvc(driver, spec(model_cache="stream"))["spec"]["template"]
    )


def test_stream_multi_gpu_puts_parallelism_in_its_own_command(driver):
    resource = llmisvc(driver, spec(model_cache="stream", gpu_count=2))
    assert "parallelism" not in resource["spec"]
    command = resource["spec"]["template"]["containers"][0]["command"]
    assert command[command.index("--tensor-parallel-size") + 1] == "2"


# -- prerequisites -------------------------------------------------------------
def test_a_missing_crd_is_fatal_when_nothing_may_install_it(driver, monkeypatch):
    stub_kubectl(monkeypatch)
    with pytest.raises(InferencePrerequisiteError, match="LLMInferenceService"):
        driver.check_prerequisites(spec(options={"platform": KServePlatform(install=False)}))


def test_a_missing_crd_is_a_note_when_create_will_install_it(driver, monkeypatch):
    stub_kubectl(monkeypatch)
    warnings = driver.check_prerequisites(spec())
    assert any("will install" in w for w in warnings)


def test_missing_presets_are_fatal_when_nothing_may_install_them(driver, monkeypatch):
    stub_kubectl(monkeypatch, replies={f"crd {kl.LLMISVC_CRD}": "crd/x"})
    with pytest.raises(InferencePrerequisiteError, match=kl.LLM_TEMPLATE_CONFIG):
        driver.check_prerequisites(spec(options={"platform": KServePlatform(install=False)}))


def test_a_tls_mismatch_with_the_cluster_warns(driver, monkeypatch):
    stub_kubectl(
        monkeypatch,
        replies={
            **INSTALLED,
            "nodes -o json": json.dumps(
                {"items": [{"status": {"allocatable": {"amd.com/gpu": "1"}}}]}
            ),
            "data.ingress": json.dumps({"enableLLMInferenceServiceTLS": True}),
        },
    )
    warnings = driver.check_prerequisites(spec())
    assert any("enableLLMInferenceServiceTLS" in w for w in warnings)


def test_a_missing_named_gateway_is_fatal(driver, monkeypatch):
    stub_kubectl(
        monkeypatch,
        replies={
            **INSTALLED,
            f"crd {kl.GATEWAY_API_CRD}": "crd/gw",
            "nodes -o json": json.dumps(
                {"items": [{"status": {"allocatable": {"amd.com/gpu": "1"}}}]}
            ),
        },
    )
    s = spec(options={"router": "gateway", "gateway_name": "inference-gw"})
    with pytest.raises(InferencePrerequisiteError, match="inference-gw"):
        driver.check_prerequisites(s)


def test_stream_with_the_presets_image_warns(driver, monkeypatch):
    stub_kubectl(
        monkeypatch,
        replies={
            **INSTALLED,
            "nodes -o json": json.dumps(
                {"items": [{"status": {"allocatable": {"amd.com/gpu": "1"}}}]}
            ),
        },
    )
    warnings = driver.check_prerequisites(spec(model_cache="stream", image=None))
    assert any("vllm[runai]" in w for w in warnings)


# -- lifecycle -----------------------------------------------------------------
def test_endpoint_prefers_kserves_reported_url(driver, monkeypatch):
    stub_kubectl(
        monkeypatch,
        replies={
            "get llminferenceservice qwen": json.dumps(
                {"status": {"url": "http://qwen.inference.example/"}}
            ),
        },
    )
    assert driver.endpoint(spec()) == "http://qwen.inference.example"


def test_endpoint_falls_back_to_the_workload_service(driver, monkeypatch):
    stub_kubectl(monkeypatch)
    assert driver.endpoint(spec()) == spec().endpoint


def test_readiness_is_read_off_the_llm_resource(driver, monkeypatch):
    calls = []
    stub_kubectl(
        monkeypatch,
        record=calls,
        replies={
            "get llminferenceservice qwen": json.dumps(
                {"status": {"conditions": [{"type": "Ready", "status": "True"}]}}
            ),
        },
    )
    driver._wait_until_ready(spec())
    assert calls and calls[0].startswith("get llminferenceservice qwen")


def test_delete_removes_what_this_driver_made(driver, monkeypatch):
    calls = []
    stub_kubectl(monkeypatch, record=calls)
    driver.delete(spec(allowed_client_labels=[{"app": "gw"}]))
    assert "delete llminferenceservice qwen -n inference --ignore-not-found" in calls
    assert any(c.startswith("delete serviceaccount qwen-storage") for c in calls)
    assert any(c.startswith("delete networkpolicy qwen-allow-clients") for c in calls)
    assert not any("localmodelcache" in c for c in calls)


def test_create_installs_with_llm_and_standard_mode(driver, monkeypatch):
    seen = {}

    class FakeInstaller:
        def __init__(self, *a, **k):
            pass

        def ensure(self, platform, mode, llm=False, model_cache=False):
            seen.update(mode=mode, llm=llm, model_cache=model_cache)
            return []

    stub_kubectl(
        monkeypatch,
        replies={
            **INSTALLED,
            "nodes -o json": json.dumps(
                {"items": [{"status": {"allocatable": {"amd.com/gpu": "1"}}}]}
            ),
            "get llminferenceservice qwen": json.dumps(
                {"status": {"conditions": [{"type": "Ready", "status": "True"}]}}
            ),
            "get localmodelcache model-": json.dumps(
                {"status": {"nodeStatus": {"gpu-1": "NodeDownloaded"}}}
            ),
            # Copied into KServe's job namespace for the cache's download.
            "get secret": json.dumps({"data": {"AWS_ACCESS_KEY_ID": "eA=="}}),
        },
    )
    monkeypatch.setattr(kl, "KServePlatformInstaller", FakeInstaller)
    applied = []
    monkeypatch.setattr(
        kl, "kube_apply", lambda kc, objs, **k: applied.append([o["kind"] for o in objs])
    )
    monkeypatch.setattr(
        ks, "kube_apply", lambda kc, objs, **k: applied.append([o["kind"] for o in objs])
    )
    driver.create(spec(warmup=False, model_cache="node", cache_size_gb=2, cache_scope="cluster"))
    assert seen == {"mode": "Standard", "llm": True, "model_cache": True}
    # The cache is applied (and waited on) before the model that reads it.
    assert "LocalModelCache" in applied[0] and "LLMInferenceService" not in applied[0]
    assert applied[0].index("Secret") < applied[0].index("LocalModelCache")
    assert "LLMInferenceService" in applied[1]


# -- security context --------------------------------------------------------
def test_a_root_image_can_be_given_a_user_and_the_gpu_groups(driver):
    """KServe's preset sets runAsNonRoot, which refuses an image that runs
    as root -- the ROCm vLLM builds do -- unless a numeric user is set."""
    s = spec(
        options=KServeLLMOptions(
            security_context={"runAsUser": 1000, "runAsGroup": 1000},
            pod_security_context={"supplementalGroups": [44, 992]},
        )
    )
    assert main_container(driver, s)["securityContext"] == {"runAsUser": 1000, "runAsGroup": 1000}
    assert llmisvc(driver, s)["spec"]["template"]["securityContext"] == {
        "supplementalGroups": [44, 992]
    }


def test_no_security_context_leaves_the_presets_alone(driver):
    template = llmisvc(driver, spec())["spec"]["template"]
    assert "securityContext" not in template
    assert "securityContext" not in template["containers"][0]


# -- CPU: the preset's image is the CUDA build ------------------------------
def cpu_spec(**kwargs):
    defaults = dict(device="cpu", gpu_count=0, image=None, cpu_cores=4, memory_gb=8, kv_cache_gb=2)
    return spec(**{**defaults, **kwargs})


def test_cpu_without_an_image_gets_the_cpu_build(driver):
    # The preset's ghcr.io/llm-d/llm-d-cuda crash-loops on CPU with
    # "Failed to infer device type" -- seen on a live cluster.
    assert driver.container(cpu_spec())["image"] == DEFAULT_CPU_IMAGE


def test_the_cpu_build_is_allowed_to_run_as_root(driver):
    # It runs as root, which the preset's runAsNonRoot refuses at start.
    assert driver.container(cpu_spec())["securityContext"] == {
        "runAsNonRoot": False,
        "runAsUser": 0,
    }


def test_a_callers_security_context_wins_on_cpu(driver):
    ctx = {"runAsUser": 1000, "runAsNonRoot": True}
    main = driver.container(cpu_spec(options=KServeLLMOptions(security_context=ctx)))
    assert main["securityContext"] == ctx


def test_a_callers_cpu_image_is_not_given_root(driver):
    main = driver.container(cpu_spec(image="dev.local/vllm-cpu-nonroot:1"))
    assert main["image"] == "dev.local/vllm-cpu-nonroot:1"
    assert "securityContext" not in main


def test_gpu_without_an_image_keeps_the_preset(driver):
    main = driver.container(spec(image=None))
    assert "image" not in main and "securityContext" not in main
