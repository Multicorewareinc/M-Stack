"""Tests for VLLMDriver: manifest rendering, dtype selection from CPU
flags, prerequisite checks, and kubeconfig scoping. Nothing shells out.

Ported from tests/backends/test_vllm_client.py when vLLM moved into the
inference capability. The migration claimed behaviour was unchanged, and
these 28 tests are what makes that claim checkable — without them the
refactor would have traded 51 tests for 15 and called it equivalent.

They target the driver rather than InferenceBackend because that is where
the logic moved; the driver is dispatch plus state tracking, covered by
tests/test_backend_state_tracking.py.
"""
import json
import subprocess
import urllib.error
import urllib.request

import pytest

from multistack import Inference, RKE2Node
from multistack.inference.base import (
    InferenceError,
    InferencePrerequisiteError,
)
from multistack.inference.drivers.vllm import VLLMDriver

KUBECONFIG = "/tmp/kc.yaml"
MODEL = "Qwen/Qwen2.5-0.5B-Instruct"

# Real flag set from the lab's Skylake KVM guests: AVX-512 but no bf16.
FLAGS_NO_BF16 = "flags : avx2 avx512f avx512bw avx512dq avx512vl"
FLAGS_WITH_BF16 = FLAGS_NO_BF16 + " avx512_bf16"


@pytest.fixture
def driver():
    return VLLMDriver()


@pytest.fixture
def node():
    return RKE2Node(address="10.0.0.2", user="ubuntu", ssh_key="/keys/id")


def svc(**kwargs) -> Inference:
    return Inference(kubeconfig_path=KUBECONFIG, model=MODEL, **kwargs)


def stub_probes(driver, monkeypatch, flags=FLAGS_NO_BF16, cores="8", ram="15991", role="agent"):
    replies = {
        "cpuinfo": flags,
        "nproc": cores,
        "free -m": ram,
        "rancher/rke2/server/db": role,
    }

    def fake_probe(node, command, what):
        for needle, reply in replies.items():
            if needle in command:
                return reply
        return ""

    monkeypatch.setattr(driver, "_probe", fake_probe)


# -- dtype selection ------------------------------------------------------
def test_dtype_falls_back_to_float32_without_native_bf16(driver, node, monkeypatch):
    # vLLM would otherwise take bfloat16 from the model config and emulate
    # it in software — no error, just far slower than the hardware allows.
    stub_probes(driver, monkeypatch, flags=FLAGS_NO_BF16)
    assert driver.recommended_dtype(node) == "float32"


def test_dtype_uses_bfloat16_when_the_cpu_supports_it(driver, node, monkeypatch):
    stub_probes(driver, monkeypatch, flags=FLAGS_WITH_BF16)
    assert driver.recommended_dtype(node) == "bfloat16"


# -- prerequisite checks --------------------------------------------------
def test_requesting_more_cores_than_the_node_has_is_fatal(driver, node, monkeypatch):
    stub_probes(driver, monkeypatch, cores="8")
    with pytest.raises(InferencePrerequisiteError, match="Insufficient cpu"):
        driver.check_prerequisites(svc(cpu_cores=12), [node])


def test_requesting_every_core_warns(driver, node, monkeypatch):
    # This is what actually made the pod Pending: kubelet, CNI and
    # DaemonSets need some CPU too.
    stub_probes(driver, monkeypatch, cores="8")
    warnings = driver.check_prerequisites(svc(cpu_cores=8), [node])
    assert any("leaves nothing for" in w for w in warnings)


def test_requesting_more_memory_than_the_node_has_is_fatal(driver, node, monkeypatch):
    stub_probes(driver, monkeypatch, ram="15991")
    with pytest.raises(InferencePrerequisiteError, match="the node has"):
        driver.check_prerequisites(svc(memory_gb=64), [node])


def test_control_plane_node_is_flagged(driver, node, monkeypatch):
    # Starving etcd/apiserver breaks the cluster, not just the workload.
    stub_probes(driver, monkeypatch, role="server")
    warnings = driver.check_prerequisites(svc(cpu_cores=4), [node])
    assert any("control-plane" in w for w in warnings)


def test_worker_node_is_not_flagged_as_control_plane(driver, node, monkeypatch):
    stub_probes(driver, monkeypatch, role="agent")
    warnings = driver.check_prerequisites(svc(cpu_cores=4), [node])
    assert not any("control-plane" in w for w in warnings)


def test_float32_fallback_is_reported_as_a_warning(driver, node, monkeypatch):
    stub_probes(driver, monkeypatch, flags=FLAGS_NO_BF16)
    warnings = driver.check_prerequisites(svc(cpu_cores=4), [node])
    assert any("no native bf16" in w for w in warnings)


# -- manifest rendering ---------------------------------------------------
def container(driver, service, dtype=None):
    objs = driver.manifests(service, dtype=dtype)
    deployment = next(o for o in objs if o["kind"] == "Deployment")
    return deployment, deployment["spec"]["template"]["spec"]["containers"][0]


def test_manifests_render_namespace_deployment_service(driver):
    kinds = [o["kind"] for o in driver.manifests(svc())]
    assert kinds == ["Namespace", "Deployment", "Service"]


def test_no_service_monitor_by_default(driver):
    # ADR-006: empty service_monitor_labels means inert, same shape as
    # allowed_client_labels -- nothing rendered, nothing to apply.
    kinds = [o["kind"] for o in driver.manifests(svc())]
    assert "ServiceMonitor" not in kinds


def test_service_monitor_selector_actually_matches_the_services_own_labels(driver):
    # A ServiceMonitor's selector matches a Service's own metadata labels
    # -- not spec.selector, which is how the Service finds its pods, and
    # says nothing about the Service object itself. Confirmed against a
    # live cluster: get this pairing wrong and the ServiceMonitor applies
    # cleanly, Prometheus discovers it, and every candidate target is
    # silently relabeled into droppedTargets -- no error, no metrics,
    # nothing to notice short of checking Prometheus's own target list.
    objs = driver.manifests(svc(service_monitor_labels={"release": "kube-prometheus-stack"}))
    service = next(o for o in objs if o["kind"] == "Service")
    monitor = next(o for o in objs if o["kind"] == "ServiceMonitor")
    assert monitor["spec"]["selector"]["matchLabels"].items() <= service["metadata"].get("labels", {}).items()


def test_service_monitor_port_name_matches_the_services_own_port_name(driver):
    # Same failure mode as the label check above, for the other half of
    # a ServiceMonitor's contract: it resolves by Service port *name*,
    # not number.
    objs = driver.manifests(svc(service_monitor_labels={"release": "kube-prometheus-stack"}))
    service = next(o for o in objs if o["kind"] == "Service")
    monitor = next(o for o in objs if o["kind"] == "ServiceMonitor")
    service_port_names = {p["name"] for p in service["spec"]["ports"]}
    monitor_port_names = {e["port"] for e in monitor["spec"]["endpoints"]}
    assert monitor_port_names <= service_port_names


def test_dev_shm_is_always_mounted_and_memory_backed(driver):
    # k8s's 64Mi default is a hard startup failure for vLLM.
    deployment, c = container(driver, svc(shm_gb=2))
    volumes = {v["name"]: v for v in deployment["spec"]["template"]["spec"]["volumes"]}

    assert any(m["mountPath"] == "/dev/shm" for m in c["volumeMounts"])
    assert volumes["dshm"]["emptyDir"]["medium"] == "Memory"
    assert volumes["dshm"]["emptyDir"]["sizeLimit"] == "2Gi"


def test_requests_equal_limits_for_guaranteed_qos(driver):
    # Mismatched cpu limit makes cgroup throttling fight OpenMP threads.
    _, c = container(driver, svc(cpu_cores=6, memory_gb=12))
    assert c["resources"]["requests"] == c["resources"]["limits"]
    assert c["resources"]["limits"]["cpu"] == "6"


def test_gpu_resources_only_requested_for_gpu_device(driver):
    _, cpu = container(driver, svc())
    assert "nvidia.com/gpu" not in cpu["resources"]["limits"]

    _, gpu = container(driver, svc(device="gpu", gpu_count=2))
    assert gpu["resources"]["limits"]["nvidia.com/gpu"] == "2"


def test_host_network_forces_recreate_strategy(driver):
    # A rolling update would put two pods on the same host port.
    deployment, _ = container(driver, svc(host_network=True))
    assert deployment["spec"]["template"]["spec"]["hostNetwork"] is True
    assert deployment["spec"]["strategy"]["type"] == "Recreate"


def test_no_strategy_override_without_host_network(driver):
    deployment, _ = container(driver, svc())
    assert "strategy" not in deployment["spec"]


def test_node_selector_and_tolerations_are_passed_through(driver):
    deployment, _ = container(
        driver,
        svc(
            node_selector={"multistack.io/workload": "inference"},
            tolerations=[{"key": "dedicated", "operator": "Equal",
                          "value": "inference", "effect": "NoSchedule"}],
        ),
    )
    pod = deployment["spec"]["template"]["spec"]
    assert pod["nodeSelector"] == {"multistack.io/workload": "inference"}
    assert pod["tolerations"][0]["key"] == "dedicated"


def test_readiness_budget_allows_for_compilation(driver):
    # Cold start is image pull + weight download + model compilation.
    _, c = container(driver, svc())
    probe = c["readinessProbe"]
    budget = probe["initialDelaySeconds"] + probe["periodSeconds"] * probe["failureThreshold"]
    assert budget >= 600


def s3_service(**kwargs) -> Inference:
    defaults = dict(
        kubeconfig_path=KUBECONFIG,
        model="s3://models/qwen",
        s3_endpoint_url="http://minio:9000",
        s3_secret_name="minio-creds",
    )
    return Inference(**{**defaults, **kwargs})


def init_container(driver, service):
    deployment, _ = container(driver, service)
    return deployment["spec"]["template"]["spec"]["initContainers"][0]


def test_s3_model_gets_a_mirror_init_container(driver):
    # runai_model_streamer is absent from vLLM's CPU image, so weights are
    # copied to local disk before the engine starts.
    init = init_container(driver, s3_service())
    assert init["name"] == "fetch-weights"
    assert "mc mirror" in " ".join(init["command"])


def test_no_init_container_without_an_s3_model(driver):
    deployment, _ = container(driver, svc())
    assert "initContainers" not in deployment["spec"]["template"]["spec"]


def test_s3_credentials_come_from_a_secret_not_the_spec(driver):
    init = init_container(driver, s3_service())
    secret_envs = [e for e in init["env"] if "valueFrom" in e]
    assert {e["name"] for e in secret_envs} == {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"}
    # Never inlined into the manifest.
    assert not any("admin" in str(e.get("value", "")) for e in init["env"])


def test_serving_container_never_gets_the_credentials(driver):
    # It reads the mirrored weights off local disk, so handing it the keys
    # would widen their blast radius for nothing.
    _, c = container(driver, s3_service())
    assert not any("valueFrom" in e for e in c["env"])


def test_mirror_and_serving_containers_share_the_model_volume(driver):
    service = s3_service()
    deployment, c = container(driver, service)
    init = deployment["spec"]["template"]["spec"]["initContainers"][0]

    served = next(m for m in c["volumeMounts"] if m["mountPath"] == "/models")
    fetched = next(m for m in init["volumeMounts"] if m["mountPath"] == "/models")
    assert served["name"] == fetched["name"]

    volumes = {v["name"]: v for v in deployment["spec"]["template"]["spec"]["volumes"]}
    # Re-fetchable data: node-local scratch, not replicated storage.
    assert "emptyDir" in volumes[served["name"]]


# -- kubeconfig scoping ---------------------------------------------------
def test_apply_and_query_are_kubeconfig_scoped(driver, monkeypatch):
    # Never ambient $KUBECONFIG: that silently targets whatever cluster the
    # environment happens to point at, including a stale one.
    argvs = []

    def fake_run(argv, **kwargs):
        argvs.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="1", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    service = svc()
    driver._kubectl_apply(service, driver.manifests(service))
    driver._kubectl(service, "get", "deployment", service.name)

    assert len(argvs) == 2
    for argv in argvs:
        assert argv[0] == "kubectl"
        assert argv[argv.index("--kubeconfig") + 1] == KUBECONFIG


def test_endpoint_uses_cluster_dns_by_default(driver):
    assert driver.endpoint(svc()) == (
        "http://vllm.inference.svc.cluster.local:8000"
    )


def test_endpoint_uses_node_ip_with_host_network(driver, monkeypatch):
    monkeypatch.setattr(driver, "_kubectl", lambda s, *a, **k: "192.0.2.11")
    assert driver.endpoint(svc(host_network=True)) == "http://192.0.2.11:8000"


def test_create_validates_before_touching_the_cluster(driver, monkeypatch):
    monkeypatch.setattr(
        driver, "_require_cli", lambda name: pytest.fail("should not reach the CLI")
    )
    with pytest.raises(ValueError, match="kubeconfig_path is required"):
        driver.create(Inference(kubeconfig_path="", model=MODEL))


def test_warmup_uses_the_served_model_name_not_the_s3_path(driver, monkeypatch):
    # An s3:// path is not an id the API answers to — sending it gets a 404,
    # which silently skips the warmup and leaves the first caller paying it.
    sent = {}

    def fake_urlopen(request, timeout=None):
        sent["body"] = json.loads(request.data)
        raise urllib.error.URLError("stop here")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(driver, "endpoint", lambda s: "http://node:8000")
    driver._warmup(s3_service())

    assert sent["body"]["model"] == "qwen"


def test_runtime_class_is_rendered_when_set(driver):
    # GPU serving on RKE2 needs runtimeClassName: nvidia unless the cluster
    # uses CDI — without it the pod schedules and then finds no GPU.
    deployment, _ = container(driver, svc(device="gpu", gpu_count=1,
                                           runtime_class="nvidia"))
    assert deployment["spec"]["template"]["spec"]["runtimeClassName"] == "nvidia"


def test_no_runtime_class_key_when_unset(driver):
    deployment, _ = container(driver, svc())
    assert "runtimeClassName" not in deployment["spec"]["template"]["spec"]
