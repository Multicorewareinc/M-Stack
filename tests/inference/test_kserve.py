"""The kserve implementation of the inference capability.

Nothing here shells out. The driver's one seam is `_kubectl`, so a test
patches that and asserts on the argv it was handed, or on the manifests
`manifests()` renders — which is how "no cluster command is ever unscoped"
stays asserted rather than merely intended.

The cases worth knowing about before reading: KServe names the Service it
generates (`<name>-predictor`), which is why the spec's own `endpoint`
has to know about this type at all, and readiness comes off parsed status
conditions rather than a jsonpath.
"""

import json
from time import sleep as _real_sleep

import pytest

from multistack.inference import (
    Inference,
    InferenceBackend,
    InferenceError,
    InferencePrerequisiteError,
    KServeOptions,
    KServePlatform,
    LlamaCppOptions,
    VLLMOptions,
)
from multistack.inference.drivers import kserve as ks
from multistack.inference.drivers import kserve_platform as kp

KUBECONFIG = "/tmp/kc.yaml"
MODEL = "Qwen/Qwen2.5-0.5B-Instruct"


def spec(**kwargs) -> Inference:
    defaults = dict(
        kubeconfig_path=KUBECONFIG,
        type="kserve",
        name="qwen",
        model=MODEL,
    )
    return Inference(**{**defaults, **kwargs})


@pytest.fixture
def driver():
    return ks.KServeDriver()


def _objects(driver, s):
    return {o["kind"]: o for o in driver.manifests(s)}


def _isvc(driver, s):
    return _objects(driver, s)["InferenceService"]


def _predictor(driver, s):
    return _isvc(driver, s)["spec"]["predictor"]


def stub_kubectl(driver, monkeypatch, replies=None, record=None):
    """Replaces the driver's one seam. `replies` maps a substring of the
    argv to the stdout that call returns; anything unmatched returns ''.

    Also stubs the two module-level guards, which reach the filesystem and
    a real API server — `test_every_cluster_call_is_kubeconfig_scoped`
    below is what asserts they are called with the right kubeconfig.
    """
    replies = replies or {}
    monkeypatch.setattr(ks, "require_cli", lambda *a, **k: "/usr/bin/kubectl")
    monkeypatch.setattr(ks, "require_cluster", lambda *a, **k: "v1.32.5")

    def fake(self, inference, *args, check=True):
        if record is not None:
            record.append(list(args))
        joined = " ".join(args)
        for needle, reply in replies.items():
            if needle in joined:
                return reply
        return ""

    monkeypatch.setattr(ks.KServeDriver, "_kubectl", fake)


def ready_json(status="True", message="", reason="", url=""):
    conditions = [{"type": "Ready", "status": status}]
    if message:
        conditions[0]["message"] = message
    if reason:
        conditions[0]["reason"] = reason
    body = {"status": {"conditions": conditions}}
    if url:
        body["status"]["address"] = {"url": url}
    return json.dumps(body)


# -- spec -----------------------------------------------------------------


def test_kserve_is_a_supported_type():
    assert "kserve" in Inference.SUPPORTED_TYPES


def test_it_gets_its_own_options_model():
    assert isinstance(spec().options, KServeOptions)


def test_options_from_another_implementation_are_refused():
    """The Union discriminator: best-fit resolution put the wrong options
    model on Policy, which is why `type` selects it instead."""
    with pytest.raises(ValueError):
        spec(options=VLLMOptions())
    with pytest.raises(ValueError):
        spec(options=LlamaCppOptions())


def test_an_unknown_deployment_mode_is_refused():
    with pytest.raises(ValueError, match="not a KServe"):
        spec(options=KServeOptions(deployment_mode="Raw"))


def test_a_max_below_min_is_refused():
    with pytest.raises(ValueError, match="no range"):
        spec(options=KServeOptions(min_replicas=3, max_replicas=2))


def test_a_canary_percentage_outside_0_100_is_refused():
    with pytest.raises(ValueError, match="percentage"):
        spec(options=KServeOptions(canary_traffic_percent=140))


def test_a_path_prefix_without_a_leading_slash_is_refused():
    with pytest.raises(ValueError, match="must start"):
        spec(options=KServeOptions(openai_path_prefix="openai"))


def test_a_misspelled_option_is_refused():
    """extra='forbid' — a silently-dropped field is how a spec ends up not
    doing what the file says."""
    with pytest.raises(ValueError):
        spec(options=KServeOptions(deployment_modes="Serverless"))


# -- the Service KServe names itself --------------------------------------


def test_the_service_is_the_predictor_not_the_deployment_name():
    """KServe generates `<name>-predictor`; nothing applies the plain name,
    so a Stack publishing the plain one would publish a dead URL."""
    assert spec().service_name == "qwen-predictor"


def test_the_endpoint_carries_the_route_the_runtime_actually_serves():
    """The endpoint is a base every consumer appends
    '/v1/chat/completions' to -- model-gateway hardcodes exactly that.
    KServe's huggingface runtime mounts the OpenAI routes under a prefix
    and keeps '/v1' for its own predict protocol, so publishing the bare
    Service URL made every real request 404 against a healthy deployment.
    """
    assert spec().endpoint == ("http://qwen-predictor.inference.svc.cluster.local:80/openai")


def test_appending_the_openai_path_to_the_endpoint_gives_the_real_route():
    """The property that was broken, stated the way a consumer uses it."""
    assert spec().endpoint + "/v1/chat/completions" == (
        "http://qwen-predictor.inference.svc.cluster.local:80/openai/v1/chat/completions"
    )


def test_the_other_implementations_have_no_base_path():
    """vllm and llamacpp serve /v1 at the root of their port, so a prefix
    there would break the same contract in the other direction."""
    vllm = Inference(kubeconfig_path=KUBECONFIG, model=MODEL, name="vllm")
    assert vllm.base_path == ""
    assert vllm.endpoint + "/v1/chat/completions" == (
        "http://vllm.inference.svc.cluster.local:8000/v1/chat/completions"
    )


def test_a_runtime_serving_at_the_root_can_clear_the_prefix():
    s = spec(options=KServeOptions(openai_path_prefix=""))
    assert s.base_path == ""
    assert s.endpoint.endswith(":80")


def test_a_trailing_slash_on_the_prefix_is_refused():
    """It is joined directly to '/v1/...', so this would double the
    separator."""
    with pytest.raises(ValueError, match="must not end"):
        spec(options=KServeOptions(openai_path_prefix="/openai/"))


def test_the_other_implementations_keep_their_own_service_name():
    """The branch in `service_name` must not follow vllm/llamacpp, whose
    drivers render the Service themselves."""
    vllm = Inference(kubeconfig_path=KUBECONFIG, model=MODEL, name="vllm")
    assert vllm.service_name == "vllm"
    assert vllm.endpoint == "http://vllm.inference.svc.cluster.local:8000"


def test_the_predictor_port_is_overridable():
    s = spec(options=KServeOptions(predictor_service_port=8080))
    assert s.endpoint == ("http://qwen-predictor.inference.svc.cluster.local:8080/openai")


# -- storageUri -----------------------------------------------------------


def test_a_huggingface_model_becomes_an_hf_uri(driver):
    assert driver.storage_uri(spec()) == f"hf://{MODEL}"


def test_an_s3_model_is_passed_through_unchanged(driver):
    s = spec(
        model="s3://models/qwen",
        s3_endpoint_url="http://minio:9000",
        s3_secret_name="minio-creds",
    )
    assert driver.storage_uri(s) == "s3://models/qwen"


def test_there_is_no_mirror_init_container_for_s3(driver):
    """The difference from the vllm driver: KServe's storage-initializer
    fetches the weights, so nothing here mirrors them first."""
    s = spec(
        model="s3://models/qwen",
        s3_endpoint_url="http://minio:9000",
        s3_secret_name="minio-creds",
    )
    rendered = json.dumps(driver.manifests(s))
    assert "initContainers" not in rendered
    assert "mc mirror" not in rendered


# -- the InferenceService -------------------------------------------------


def test_the_deployment_mode_is_annotated(driver):
    """The canonical spelling, not whatever the caller wrote. KServe 0.20
    renamed both modes and keeps the old names only as deprecated
    aliases."""
    annotations = _isvc(driver, spec())["metadata"]["annotations"]
    assert annotations[ks.DEPLOYMENT_MODE_ANNOTATION] == "Standard"


@pytest.mark.parametrize(
    "written, annotated",
    [
        ("RawDeployment", "Standard"),  # pre-0.20 spelling
        ("Standard", "Standard"),
        ("Serverless", "Knative"),  # pre-0.20 spelling
        ("Knative", "Knative"),
    ],
)
def test_a_pre_0_20_mode_is_annotated_under_its_new_name(driver, written, annotated):
    """A spec written against 0.15 keeps working, and still lands on the
    name 0.20 prefers -- so the deprecated spelling stops at this boundary
    rather than travelling into the cluster."""
    s = spec(options=KServeOptions(deployment_mode=written))
    assert _isvc(driver, s)["metadata"]["annotations"][ks.DEPLOYMENT_MODE_ANNOTATION] == annotated


def test_replica_bounds_are_omitted_when_unset(driver):
    """None leaves KServe's own defaults rather than writing a number the
    caller never asked for."""
    predictor = _predictor(driver, spec())
    assert "minReplicas" not in predictor
    assert "maxReplicas" not in predictor


def test_replica_bounds_are_written_when_set(driver):
    predictor = _predictor(driver, spec(options=KServeOptions(min_replicas=0, max_replicas=4)))
    assert predictor["minReplicas"] == 0
    assert predictor["maxReplicas"] == 4


def test_a_named_runtime_pins_the_serving_runtime(driver):
    predictor = _predictor(driver, spec(options=KServeOptions(runtime="kserve-vllmserver")))
    assert predictor["model"]["runtime"] == "kserve-vllmserver"


def test_no_runtime_leaves_kserve_to_select_from_the_format(driver):
    model = _predictor(driver, spec())["model"]
    assert "runtime" not in model
    assert model["modelFormat"] == {"name": "huggingface"}


def test_gpu_requests_land_on_the_model_resources(driver):
    resources = _predictor(driver, spec(device="gpu", gpu_count=2))["model"]["resources"]
    assert resources["limits"]["nvidia.com/gpu"] == "2"
    assert resources["requests"]["nvidia.com/gpu"] == "2"


def test_a_non_nvidia_cluster_requests_its_own_gpu_resource(driver):
    """Hardcoding NVIDIA meant device='gpu' on an AMD cluster asked for a
    resource no node had, and the pod sat Pending forever."""
    resources = _predictor(
        driver,
        spec(
            device="gpu",
            gpu_count=1,
            gpu_resource="amd.com/gpu",
        ),
    )["model"]["resources"]
    assert resources["limits"]["amd.com/gpu"] == "1"
    assert "nvidia.com/gpu" not in resources["limits"]


def test_cpu_deployments_request_no_gpu(driver):
    resources = _predictor(driver, spec())["model"]["resources"]
    assert not any("gpu" in k for k in resources["limits"])


def test_node_selector_and_tolerations_reach_the_predictor(driver):
    predictor = _predictor(
        driver,
        spec(
            node_selector={"kubernetes.io/hostname": "gpu-01"},
            tolerations=[{"key": "nvidia.com/gpu", "operator": "Exists"}],
        ),
    )
    assert predictor["nodeSelector"] == {"kubernetes.io/hostname": "gpu-01"}
    assert predictor["tolerations"][0]["key"] == "nvidia.com/gpu"


def test_the_canary_percentage_is_written_when_set(driver):
    predictor = _predictor(driver, spec(options=KServeOptions(canary_traffic_percent=20)))
    assert predictor["canaryTrafficPercent"] == 20


# -- container args -------------------------------------------------------


def test_the_served_model_name_is_only_passed_when_set(driver):
    """KServe derives it from the InferenceService name otherwise, and
    overriding that would change what clients must send."""
    assert "--model_name" not in driver.container_args(spec())
    args = driver.container_args(spec(served_model_name="qwen-chat"))
    assert args[args.index("--model_name") + 1] == "qwen-chat"


def test_engine_args_are_only_emitted_for_the_huggingface_runtime(driver):
    """An arg a ClusterServingRuntime does not know is a crash loop, not a
    warning, so a custom format gets only what the caller stated."""
    args = driver.container_args(
        spec(options=KServeOptions(model_format="triton"), max_model_len=2048)
    )
    assert "--max_model_len" not in args


def test_the_context_window_is_forwarded_to_the_huggingface_runtime(driver):
    args = driver.container_args(spec(max_model_len=2048))
    assert args[args.index("--max_model_len") + 1] == "2048"


def test_auto_dtype_is_not_forwarded(driver):
    """'auto' is the runtime's own default; writing it says nothing."""
    assert "--dtype" not in driver.container_args(spec(dtype="auto"))


def test_extra_args_are_appended_last(driver):
    args = driver.container_args(spec(extra_args=["--quantization", "awq"]))
    assert args[-2:] == ["--quantization", "awq"]


def test_cpu_tuning_env_reaches_a_vllm_backed_runtime(driver):
    names = {e["name"] for e in driver.env(spec(device="cpu"))}
    assert "VLLM_CPU_KVCACHE_SPACE" in names


def test_no_vllm_env_on_gpu_or_a_foreign_runtime(driver):
    """Those variables mean nothing outside a vLLM engine, and setting one
    nothing reads is worse than leaving it off."""
    assert driver.env(spec(device="gpu", gpu_count=1)) == []
    assert driver.env(spec(options=KServeOptions(model_format="triton"))) == []


# -- ServiceAccount for private buckets -----------------------------------


def s3_spec(**kwargs) -> Inference:
    return spec(
        model="s3://models/qwen",
        s3_endpoint_url="http://minio:9000",
        s3_secret_name="minio-creds",
        **kwargs,
    )


def test_an_s3_model_gets_a_service_account_bound_to_the_secret(driver):
    account = _objects(driver, s3_spec())["ServiceAccount"]
    assert account["metadata"]["name"] == "qwen-storage"
    assert account["secrets"] == [{"name": "minio-creds"}]


def test_the_predictor_uses_that_service_account(driver):
    assert _predictor(driver, s3_spec())["serviceAccountName"] == "qwen-storage"


def test_a_caller_supplied_service_account_is_not_overwritten(driver):
    """Creating one over the top of theirs would drop whatever else it
    references."""
    s = s3_spec(options=KServeOptions(service_account_name="mine"))
    assert "ServiceAccount" not in _objects(driver, s)
    assert _predictor(driver, s)["serviceAccountName"] == "mine"


def test_a_huggingface_model_needs_no_service_account(driver):
    assert "ServiceAccount" not in _objects(driver, spec())
    assert "serviceAccountName" not in _predictor(driver, spec())


# -- NetworkPolicy and ServiceMonitor -------------------------------------


def test_nothing_is_rendered_until_a_caller_opts_in(driver):
    """ADR-006: features attach by config, not code — an existing
    deployment is untouched until asked."""
    kinds = _objects(driver, spec())
    assert "NetworkPolicy" not in kinds
    assert "ServiceMonitor" not in kinds


def test_the_network_policy_selects_kserves_own_label(driver):
    """Not `app: <name>` — that is the other drivers' label and nothing
    applies it here, so the policy would select no pods and deny all."""
    policy = _objects(driver, spec(allowed_client_labels=[{"app": "model-gateway"}]))[
        "NetworkPolicy"
    ]
    assert policy["spec"]["podSelector"]["matchLabels"] == {ks.ISVC_LABEL: "qwen"}


def test_the_network_policy_opens_the_port_kserve_actually_serves(driver):
    """Not `Inference.port`. KServe's runtimes bind 8080 and nothing
    forwards that field into the InferenceService, so a policy written
    against it opens a port the pod never serves and denies every real
    client. Confirmed against KServe v0.15.0, whose generated Service
    maps 80 -> targetPort 8080.
    """
    policy = _objects(driver, spec(allowed_client_labels=[{"app": "model-gateway"}]))[
        "NetworkPolicy"
    ]
    ports = policy["spec"]["ingress"][0]["ports"]
    assert [p["port"] for p in ports] == [8080]


def test_the_service_monitor_matches_kserves_label_and_named_port(driver):
    """The port name is `<name>-predictor`, not "http": KServe names the
    Service port after the Service. A name that does not match is the
    silent one — every target is relabeled into droppedTargets with no
    error anywhere."""
    monitor = _objects(driver, spec(service_monitor_labels={"release": "kube-prometheus-stack"}))[
        "ServiceMonitor"
    ]
    assert monitor["spec"]["selector"]["matchLabels"] == {ks.ISVC_LABEL: "qwen"}
    assert monitor["spec"]["endpoints"][0]["port"] == "qwen-predictor"
    assert monitor["metadata"]["labels"]["release"] == "kube-prometheus-stack"


def _runtime_json(port_name=None, image="kserve/huggingfaceserver:v0.15.0"):
    """A ClusterServingRuntime definition.

    The image defaults to KServe's own huggingfaceserver so that the port-name
    tests below stay about the port name: that runtime mounts the OpenAI routes
    under the default openai_path_prefix, so the prefix check stays quiet and
    does not add a second warning to assertions that are checking for one.
    """
    port = {"containerPort": 8080}
    if port_name:
        port["name"] = port_name
    return json.dumps({"spec": {"containers": [{"ports": [port], "image": image}]}})


def test_an_unnamed_runtime_port_warns(driver, monkeypatch):
    """KServe copies the container port's name onto the Service, and
    Prometheus resolves an endpoint by name. A runtime that declares no
    name yields an unnamed Service port, so the ServiceMonitor applies
    cleanly and the target is silently dropped. Observed live."""
    stub_kubectl(
        driver,
        monkeypatch,
        replies={
            ks.ISVC_CRD: "crd/isvc",
            "get clusterservingruntime": _runtime_json(port_name=None),
        },
    )
    s = spec(service_monitor_labels={"release": "kps"}, options=KServeOptions(runtime="custom-rt"))
    assert any("without a name" in w for w in driver.check_prerequisites(s))


def test_a_runtime_port_name_mismatch_warns(driver, monkeypatch):
    stub_kubectl(
        driver,
        monkeypatch,
        replies={
            ks.ISVC_CRD: "crd/isvc",
            "get clusterservingruntime": _runtime_json(port_name="http1"),
        },
    )
    s = spec(service_monitor_labels={"release": "kps"}, options=KServeOptions(runtime="custom-rt"))
    warnings = driver.check_prerequisites(s)
    assert any("names its port 'http1'" in w for w in warnings)


def test_a_matching_runtime_port_name_is_quiet(driver, monkeypatch):
    stub_kubectl(
        driver,
        monkeypatch,
        replies={
            ks.ISVC_CRD: "crd/isvc",
            "get clusterservingruntime": _runtime_json(port_name="http1"),
        },
    )
    s = spec(
        service_monitor_labels={"release": "kps"},
        options=KServeOptions(runtime="custom-rt", metrics_port_name="http1"),
    )
    assert driver.check_prerequisites(s) == []


def test_no_runtime_port_check_without_a_pinned_runtime(driver, monkeypatch):
    """Only a pinned runtime has a definition this can read."""
    stub_kubectl(driver, monkeypatch, replies={ks.ISVC_CRD: "crd/isvc"})
    s = spec(service_monitor_labels={"release": "kps"})
    assert driver.check_prerequisites(s) == []


def test_the_metrics_port_name_is_overridable(driver):
    """For a KServe configured to name the port otherwise."""
    monitor = _objects(
        driver,
        spec(
            service_monitor_labels={"release": "kps"},
            options=KServeOptions(metrics_port_name="http1"),
        ),
    )["ServiceMonitor"]
    assert monitor["spec"]["endpoints"][0]["port"] == "http1"


def test_the_container_port_is_overridable(driver):
    policy = _objects(
        driver,
        spec(
            allowed_client_labels=[{"app": "model-gateway"}],
            options=KServeOptions(predictor_container_port=9000),
        ),
    )["NetworkPolicy"]
    assert policy["spec"]["ingress"][0]["ports"][0]["port"] == 9000


# -- prerequisites --------------------------------------------------------


def test_a_cluster_without_kserve_reports_that_it_will_be_installed(driver, monkeypatch):
    """Not fatal any more: create() installs the platform, so this states
    what will happen rather than refusing."""
    stub_kubectl(driver, monkeypatch, replies={})
    warnings = driver.check_prerequisites(spec())
    assert any("will install it" in w for w in warnings)


def test_a_cluster_without_kserve_is_refused_when_install_is_off(driver, monkeypatch):
    """For a cluster where the platform team owns KServe and a driver
    must not create cluster-scoped resources."""
    stub_kubectl(driver, monkeypatch, replies={})
    s = spec(options=KServeOptions(platform=KServePlatform(install=False)))
    with pytest.raises(InferencePrerequisiteError, match=r"platform\.install is False"):
        driver.check_prerequisites(s)


def test_serverless_without_knative_reports_that_it_will_be_installed(driver, monkeypatch):
    stub_kubectl(driver, monkeypatch, replies={ks.ISVC_CRD: "crd/isvc"})
    s = spec(options=KServeOptions(deployment_mode="Serverless"))
    warnings = driver.check_prerequisites(s)
    assert any("Knative operator" in w for w in warnings)


def test_serverless_without_knative_is_refused_when_install_is_off(driver, monkeypatch):
    """The quiet one: KServe accepts it and it never becomes ready, with
    the reason only in the controller's log."""
    stub_kubectl(driver, monkeypatch, replies={ks.ISVC_CRD: "crd/isvc"})
    s = spec(
        options=KServeOptions(
            deployment_mode="Serverless",
            platform=KServePlatform(install=False),
        )
    )
    with pytest.raises(InferencePrerequisiteError, match="Knative"):
        driver.check_prerequisites(s)


def test_raw_deployment_does_not_need_knative(driver, monkeypatch):
    stub_kubectl(driver, monkeypatch, replies={ks.ISVC_CRD: "crd/isvc"})
    assert driver.check_prerequisites(spec()) == []


def test_a_missing_named_runtime_is_refused(driver, monkeypatch):
    stub_kubectl(driver, monkeypatch, replies={ks.ISVC_CRD: "crd/isvc"})
    s = spec(options=KServeOptions(runtime="nope"))
    with pytest.raises(InferencePrerequisiteError, match="ClusterServingRuntime"):
        driver.check_prerequisites(s)


def _lb_service(with_address):
    svc = {"spec": {"type": "LoadBalancer"}, "status": {"loadBalancer": {}}}
    if with_address:
        svc["status"]["loadBalancer"]["ingress"] = [{"ip": "10.0.0.9"}]
    return json.dumps(svc)


def test_a_pending_knative_load_balancer_warns(driver, monkeypatch):
    """Observed live: the Revision reported Ready with the pod serving,
    while the InferenceService sat at 'Waiting for load balancer to be
    ready' until create() timed out. The model works; the deployment
    never reports it."""
    stub_kubectl(
        driver,
        monkeypatch,
        replies={
            ks.ISVC_CRD: "crd/isvc",
            ks.KNATIVE_CRD: "crd/knative",
            "get service kourier": _lb_service(with_address=False),
        },
    )
    s = spec(options=KServeOptions(deployment_mode="Serverless"))
    assert any("LoadBalancer provider" in w for w in driver.check_prerequisites(s))


def test_a_knative_load_balancer_with_an_address_is_quiet(driver, monkeypatch):
    stub_kubectl(
        driver,
        monkeypatch,
        replies={
            ks.ISVC_CRD: "crd/isvc",
            ks.KNATIVE_CRD: "crd/knative",
            "get service kourier": _lb_service(with_address=True),
        },
    )
    s = spec(options=KServeOptions(deployment_mode="Serverless"))
    assert driver.check_prerequisites(s) == []


def test_a_non_loadbalancer_knative_ingress_is_quiet(driver, monkeypatch):
    """A cluster routing Knative some other way is not misconfigured."""
    stub_kubectl(
        driver,
        monkeypatch,
        replies={
            ks.ISVC_CRD: "crd/isvc",
            ks.KNATIVE_CRD: "crd/knative",
            "get service kourier": json.dumps({"spec": {"type": "ClusterIP"}}),
        },
    )
    s = spec(options=KServeOptions(deployment_mode="Serverless"))
    assert driver.check_prerequisites(s) == []


def test_raw_deployment_never_looks_at_the_knative_ingress(driver, monkeypatch):
    """It routes through its own Service, so a pending Knative LB is
    irrelevant and warning about it would be noise."""
    stub_kubectl(
        driver,
        monkeypatch,
        replies={
            ks.ISVC_CRD: "crd/isvc",
            "get service kourier": _lb_service(with_address=False),
        },
    )
    assert driver.check_prerequisites(spec()) == []


def test_scale_to_zero_in_raw_mode_warns_rather_than_refusing(driver, monkeypatch):
    """Which KServe versions support it is a question this cannot answer
    offline, and refusing a deploy that would work is worse than a note."""
    stub_kubectl(driver, monkeypatch, replies={ks.ISVC_CRD: "crd/isvc"})
    warnings = driver.check_prerequisites(spec(options=KServeOptions(min_replicas=0)))
    assert any("scale-to-zero" in w for w in warnings)


def test_the_runtime_class_reaches_the_predictor(driver):
    """Without it the container is handed the GPU as a scheduled resource
    and still cannot see it — the runtime mounts the CUDA devices, not the
    device plugin."""
    predictor = _predictor(
        driver,
        spec(
            device="gpu",
            gpu_count=1,
            runtime_class="nvidia",
        ),
    )
    assert predictor["runtimeClassName"] == "nvidia"


def test_no_runtime_class_key_when_unset(driver):
    assert "runtimeClassName" not in _predictor(driver, spec())


def test_a_gpu_without_a_runtime_class_warns_when_the_cluster_has_one(driver, monkeypatch):
    """Observed live on RKE2: the pod scheduled onto the GPU node with
    nvidia.com/gpu=1 and crash-looped on 'Failed to infer device type',
    which reads like an unsupported card rather than a missing runtime."""
    stub_kubectl(
        driver,
        monkeypatch,
        replies={
            ks.ISVC_CRD: "crd/isvc",
            "allocatable": "1 ",
            "get runtimeclass": "runtimeclass.node.k8s.io/crun\nruntimeclass.node.k8s.io/nvidia\n",
        },
    )
    warnings = driver.check_prerequisites(spec(device="gpu", gpu_count=1))
    assert any("runtime_class='nvidia'" in w for w in warnings)


def test_no_runtime_class_warning_once_one_is_named(driver, monkeypatch):
    stub_kubectl(
        driver,
        monkeypatch,
        replies={
            ks.ISVC_CRD: "crd/isvc",
            "allocatable": "1 ",
            "get runtimeclass": "runtimeclass.node.k8s.io/nvidia\n",
        },
    )
    s = spec(device="gpu", gpu_count=1, runtime_class="nvidia")
    assert driver.check_prerequisites(s) == []


def test_no_runtime_class_warning_when_the_cluster_has_none(driver, monkeypatch):
    """A cluster whose default runtime already exposes the card is not
    misconfigured, and warning there would be noise."""
    stub_kubectl(
        driver,
        monkeypatch,
        replies={
            ks.ISVC_CRD: "crd/isvc",
            "allocatable": "1 ",
            "get runtimeclass": "runtimeclass.node.k8s.io/crun\n",
        },
    )
    assert driver.check_prerequisites(spec(device="gpu", gpu_count=1)) == []


def _nodes_json(**resources):
    return json.dumps({"items": [{"status": {"allocatable": dict(resources)}}]})


def test_a_gpu_request_with_no_device_plugin_is_refused(driver, monkeypatch):
    stub_kubectl(
        driver,
        monkeypatch,
        replies={
            ks.ISVC_CRD: "crd/isvc",
            "get nodes": _nodes_json(cpu="8", memory="16Gi"),
        },
    )
    with pytest.raises(InferencePrerequisiteError, match="no GPU resource at all"):
        driver.check_prerequisites(spec(device="gpu", gpu_count=1))


def test_asking_for_the_wrong_vendor_names_what_the_cluster_has(driver, monkeypatch):
    """The confusing case: the plugin is installed, just a different
    vendor's. 'Insufficient nvidia.com/gpu' names a vendor this cluster
    does not run, which sends someone installing the wrong thing."""
    stub_kubectl(
        driver,
        monkeypatch,
        replies={
            ks.ISVC_CRD: "crd/isvc",
            "get nodes": _nodes_json(**{"amd.com/gpu": "1", "cpu": "32"}),
        },
    )
    with pytest.raises(InferencePrerequisiteError) as exc:
        driver.check_prerequisites(spec(device="gpu", gpu_count=1))
    assert "amd.com/gpu=1" in str(exc.value)
    assert "gpu_resource='amd.com/gpu'" in str(exc.value)


def test_the_matching_gpu_resource_passes(driver, monkeypatch):
    stub_kubectl(
        driver,
        monkeypatch,
        replies={
            ks.ISVC_CRD: "crd/isvc",
            "get nodes": _nodes_json(**{"amd.com/gpu": "1"}),
        },
    )
    s = spec(device="gpu", gpu_count=1, gpu_resource="amd.com/gpu")
    assert driver.check_prerequisites(s) == []


def test_amd_is_not_warned_about_a_missing_runtime_class(driver, monkeypatch):
    """AMD reaches the card through the device plugin, which injects
    /dev/kfd and /dev/dri itself — there is no RuntimeClass to name, so
    warning would send someone chasing a problem that is not there."""
    stub_kubectl(
        driver,
        monkeypatch,
        replies={
            ks.ISVC_CRD: "crd/isvc",
            "get nodes": _nodes_json(**{"amd.com/gpu": "1"}),
            "get runtimeclass": "runtimeclass.node.k8s.io/nvidia\n",
        },
    )
    s = spec(device="gpu", gpu_count=1, gpu_resource="amd.com/gpu")
    assert driver.check_prerequisites(s) == []


def test_an_s3_secret_without_kserve_annotations_warns(driver, monkeypatch):
    """KServe configures its storage-initializer from those annotations,
    and this SDK does not own the Secret to add them."""
    secret = json.dumps({"metadata": {"name": "minio-creds", "annotations": {}}})
    stub_kubectl(
        driver,
        monkeypatch,
        replies={
            ks.ISVC_CRD: "crd/isvc",
            "get secret": secret,
        },
    )
    warnings = driver.check_prerequisites(s3_spec())
    assert any("annotations" in w for w in warnings)


def test_an_annotated_s3_secret_is_quiet(driver, monkeypatch):
    secret = json.dumps(
        {
            "metadata": {
                "annotations": {
                    "serving.kserve.io/s3-endpoint": "minio:9000",
                }
            }
        }
    )
    stub_kubectl(
        driver,
        monkeypatch,
        replies={
            ks.ISVC_CRD: "crd/isvc",
            "get secret": secret,
        },
    )
    assert driver.check_prerequisites(s3_spec()) == []


def test_a_missing_s3_secret_warns(driver, monkeypatch):
    stub_kubectl(driver, monkeypatch, replies={ks.ISVC_CRD: "crd/isvc"})
    assert any("was not found" in w for w in driver.check_prerequisites(s3_spec()))


# -- readiness ------------------------------------------------------------


def test_readiness_comes_from_the_parsed_ready_condition(driver, monkeypatch):
    stub_kubectl(driver, monkeypatch, replies={"get inferenceservice": ready_json()})
    driver._wait_until_ready(spec())


def test_a_not_ready_service_times_out_with_the_last_status(driver, monkeypatch):
    """The message is what makes the timeout actionable — without it the
    error says only that 1800s elapsed."""
    stub_kubectl(
        driver,
        monkeypatch,
        replies={
            "get inferenceservice": ready_json(status="False", message="Revision failed"),
        },
    )
    monkeypatch.setattr(ks.time, "sleep", lambda _: None)
    s = spec(options=KServeOptions(ready_timeout=0))
    with pytest.raises(InferenceError, match="was not ready"):
        driver._wait_until_ready(s)


def _events_json(*messages):
    return json.dumps(
        {
            "items": [
                {"message": m, "lastTimestamp": f"2026-10-01T0{i}:00:00Z"}
                for i, m in enumerate(messages)
            ]
        }
    )


def test_a_rejected_deployment_reports_the_event_not_just_the_timeout(driver, monkeypatch):
    """An InferenceService an admission webhook refused has no conditions
    at all — nothing reconciled it. Observed live: Knative rejected a GPU
    runtime's hostPath volumes and the deployment sat silent for the full
    timeout while the reason was plainly in the events."""
    denied = (
        "fails to reconcile predictor: admission webhook denied the "
        "request: HostPath volume support is disabled"
    )
    stub_kubectl(
        driver,
        monkeypatch,
        replies={
            "get inferenceservice": json.dumps({"status": {}}),
            "get events": _events_json("something older", denied),
        },
    )
    # A real (tiny) sleep so the deadline is reached after a few passes --
    # ready_timeout=0 would exit before the loop body ran at all.
    monkeypatch.setattr(ks.time, "sleep", lambda _: _real_sleep(0.05))
    s = spec(options=KServeOptions(ready_timeout=1))
    with pytest.raises(InferenceError) as exc:
        driver._wait_until_ready(s)
    assert "HostPath volume support is disabled" in str(exc.value)


def test_conditions_still_win_over_events(driver, monkeypatch):
    """Events are the fallback, not the primary source — while something
    is reconciling, its own status is the better answer."""
    stub_kubectl(
        driver,
        monkeypatch,
        replies={
            "get inferenceservice": ready_json(status="False", message="Revision failed"),
            "get events": _events_json("a noisy unrelated warning"),
        },
    )
    monkeypatch.setattr(ks.time, "sleep", lambda _: _real_sleep(0.05))
    s = spec(options=KServeOptions(ready_timeout=1))
    with pytest.raises(InferenceError) as exc:
        driver._wait_until_ready(s)
    assert "Revision failed" in str(exc.value)
    assert "noisy unrelated" not in str(exc.value)


def test_no_events_is_not_fatal(driver, monkeypatch):
    stub_kubectl(
        driver,
        monkeypatch,
        replies={
            "get inferenceservice": json.dumps({"status": {}}),
        },
    )
    monkeypatch.setattr(ks.time, "sleep", lambda _: None)
    s = spec(options=KServeOptions(ready_timeout=0))
    with pytest.raises(InferenceError, match="was not ready"):
        driver._wait_until_ready(s)


def _lb_blocked_status(deployment_ready="True"):
    return json.dumps(
        {
            "status": {
                "conditions": [
                    {"type": "LatestDeploymentReady", "status": deployment_ready},
                    {
                        "type": "PredictorReady",
                        "status": "Unknown",
                        "message": "Waiting for load balancer to be ready",
                    },
                    {
                        "type": "Ready",
                        "status": "Unknown",
                        "message": "Waiting for load balancer to be ready",
                    },
                ]
            }
        }
    )


def test_a_deployment_serving_without_an_address_is_not_a_failure(driver, monkeypatch):
    """Observed live: Knative's route, ingress and revision all went
    Ready, a request through the real gateway returned 200 from the
    model, and KServe still reported Unknown — so create() timed out and
    called a working deployment broken."""
    stub_kubectl(
        driver,
        monkeypatch,
        replies={
            "get inferenceservice": _lb_blocked_status(),
        },
    )
    monkeypatch.setattr(ks.time, "sleep", lambda _: _real_sleep(0.05))
    driver._wait_until_ready(spec(options=KServeOptions(ready_timeout=1)))


def test_a_pod_that_never_came_up_still_times_out(driver, monkeypatch):
    """The guard is narrow on purpose. Without LatestDeploymentReady the
    deployment is not up, whatever the message says, and a timeout is the
    right answer."""
    stub_kubectl(
        driver,
        monkeypatch,
        replies={
            "get inferenceservice": _lb_blocked_status(deployment_ready="False"),
        },
    )
    monkeypatch.setattr(ks.time, "sleep", lambda _: _real_sleep(0.05))
    with pytest.raises(InferenceError, match="was not ready"):
        driver._wait_until_ready(spec(options=KServeOptions(ready_timeout=1)))


def test_a_different_block_still_times_out(driver, monkeypatch):
    """Only the address is excused — a crash loop is not."""
    stub_kubectl(
        driver,
        monkeypatch,
        replies={
            "get inferenceservice": json.dumps(
                {
                    "status": {
                        "conditions": [
                            {"type": "LatestDeploymentReady", "status": "True"},
                            {
                                "type": "Ready",
                                "status": "False",
                                "message": "Revision failed with CrashLoopBackOff",
                            },
                        ]
                    }
                }
            ),
        },
    )
    monkeypatch.setattr(ks.time, "sleep", lambda _: _real_sleep(0.05))
    with pytest.raises(InferenceError, match="CrashLoopBackOff"):
        driver._wait_until_ready(spec(options=KServeOptions(ready_timeout=1)))


def test_warmup_says_it_was_skipped_when_the_name_does_not_resolve(driver, capsys):
    """It runs from the control-plane node, which has no cluster DNS, so
    this is the normal outcome there. Claiming 'the first request will be
    slow' implied warmup had been attempted; it had not."""
    monkeypatch_target = "urllib.request.urlopen"
    import unittest.mock as mock

    err = ks.urllib.error.URLError("[Errno -2] Name or service not known")
    with mock.patch(monkeypatch_target, side_effect=err):
        driver._warmup(spec())
    out = capsys.readouterr().out
    assert "warmup skipped" in out
    assert "only runs from inside the cluster" in out


def test_a_real_warmup_failure_is_still_reported(driver, capsys):
    import unittest.mock as mock

    err = ks.urllib.error.URLError("connection refused")
    with mock.patch("urllib.request.urlopen", side_effect=err):
        driver._warmup(spec())
    out = capsys.readouterr().out
    assert "warmup request failed" in out


def test_a_status_that_is_not_json_is_not_fatal(driver):
    """`kubectl get` with check=False returns empty on a missing object,
    and a half-written status must not raise out of a poll loop."""
    assert driver._status("") == {}
    assert driver._status("not json") == {}


def test_the_ready_condition_is_found_among_others(driver):
    status = json.loads(ready_json())["status"]
    status["conditions"].insert(0, {"type": "PredictorReady", "status": "False"})
    assert driver._condition(status, "Ready")["status"] == "True"


# -- endpoint -------------------------------------------------------------


def test_the_endpoint_prefers_the_address_kserve_published(driver, monkeypatch):
    """It reflects the deployment mode, which decides whether the address
    is the predictor Service or a Knative route."""
    stub_kubectl(
        driver,
        monkeypatch,
        replies={
            "get inferenceservice": ready_json(
                url="http://qwen-predictor.inference.svc.cluster.local/"
            ),
        },
    )
    assert driver.endpoint(spec()) == ("http://qwen-predictor.inference.svc.cluster.local/openai")


def test_the_endpoint_falls_back_to_the_static_form(driver, monkeypatch):
    """Before status is populated there is nothing to read, and the spec's
    own form is already correct for RawDeployment."""
    stub_kubectl(driver, monkeypatch, replies={})
    assert driver.endpoint(spec()) == spec().endpoint


# -- scoping and dispatch -------------------------------------------------


def test_every_cluster_call_is_kubeconfig_scoped(monkeypatch):
    """The property the thin `_kubectl` seam exists to make assertable."""
    seen = []

    def fake_kubectl(kubeconfig_path, *args, **kwargs):
        seen.append(kubeconfig_path)
        return "crd/isvc" if ks.ISVC_CRD in args else ""

    monkeypatch.setattr(ks, "kube_kubectl", fake_kubectl)
    monkeypatch.setattr(ks, "require_cli", lambda *a, **k: "/usr/bin/kubectl")
    monkeypatch.setattr(ks, "require_cluster", lambda *a, **k: "v1.32.5")

    ks.KServeDriver().check_prerequisites(spec())
    assert seen and all(path == KUBECONFIG for path in seen)


def test_delete_removes_the_inferenceservice_and_lets_kserve_collect(monkeypatch):
    """Deleting the generated Deployment and Service by hand would race
    KServe's controller rather than help it.

    The ServiceMonitor is the exception and goes explicitly, because
    nothing owns it: KServe collects what the InferenceService owns, and
    a monitor applied beside one is not in that set. It is attempted
    whether or not this spec carries `service_monitor_labels` -- see the
    comment on that call for why gating on them leaked the one resource
    whose name is fully derived from the spec.
    """
    calls = []
    stub_kubectl(None, monkeypatch, record=calls)
    ks.KServeDriver().delete(spec())

    assert calls[0] == [
        "delete",
        "inferenceservice",
        "qwen",
        "-n",
        "inference",
        "--ignore-not-found",
    ]
    kinds = [call[1] for call in calls]
    assert "deployment" not in kinds and "service" not in kinds, (
        f"deleting what KServe owns races its controller: {calls}"
    )
    assert "servicemonitor" in kinds


def test_delete_removes_what_this_driver_created(monkeypatch):
    calls = []
    stub_kubectl(None, monkeypatch, record=calls)
    ks.KServeDriver().delete(
        s3_spec(
            allowed_client_labels=[{"app": "model-gateway"}],
            service_monitor_labels={"release": "kps"},
        )
    )
    kinds = [c[1] for c in calls]
    assert kinds == [
        "inferenceservice",
        "serviceaccount",
        "networkpolicy",
        "servicemonitor",
    ]


# -- platform install -----------------------------------------------------


class FakeHelm:
    """Stands in for HelmRunner. pyhelm3 is an optional extra, so the
    real one must never be constructed here."""

    def __init__(self):
        self.calls = []

    def install_or_upgrade(self, release, **kw):
        self.calls.append((release, kw))
        return release

    def releases(self):
        return [c[0] for c in self.calls]


def installer(monkeypatch, present=(), helm=None, ingress=None):
    """An installer over a fake cluster that reports `present` CRDs.

    The cluster is mutable on purpose: applying the KnativeServing
    resource makes the Serving CRD appear, the way the operator
    reconciling it actually does. Without that the installer's wait loop
    is correct and simply never finishes.
    """
    helm = helm or FakeHelm()
    inst = kp.KServePlatformInstaller("/tmp/kc.yaml", helm=helm)
    cluster = set(present)
    inst.applied = []
    cluster_ingress = dict(
        ingress
        or {
            "localGatewayService": "knative-local-gateway.istio-system.svc.cluster.local",
            "knativeLocalGatewayService": "",
            "ingressClassName": "istio",
            "disableIstioVirtualHost": False,
            "urlScheme": "http",
        }
    )

    inst.kubectl_calls = []

    def fake_kubectl(self, *args, check=True):
        joined = " ".join(args)
        inst.kubectl_calls.append(joined)
        if "get crd" in joined:
            return "crd/x" if any(c in joined for c in cluster) else ""
        if "availableReplicas" in joined:
            return "1"
        if "knativeserving" in joined:
            # The operator has reconciled it once the resource exists.
            return "True" if kp.KNATIVE_SERVING_CRD in cluster else ""
        if "configmap" in joined and "data.ingress" in joined:
            # What KServe's chart actually ships: Istio's gateway, in a
            # namespace a Kourier cluster does not have.
            return json.dumps(cluster_ingress)
        return ""

    def fake_apply(kubeconfig, objects, **kw):
        inst.applied.extend(objects)
        if any(o.get("kind") == "KnativeServing" for o in objects):
            cluster.add(kp.KNATIVE_SERVING_CRD)
        return ""

    monkeypatch.setattr(kp.KServePlatformInstaller, "_kubectl", fake_kubectl)
    monkeypatch.setattr(kp.time, "sleep", lambda _: None)
    monkeypatch.setattr(kp, "kube_apply", fake_apply)
    return inst, helm


def test_an_existing_kserve_is_never_upgraded(monkeypatch):
    """Version pinning applies to what this installs, never to what it
    finds — upgrading a release the platform team owns is not ours to do.

    Scoped to the releases that carry the platform itself. The runtimes
    are deliberately not among them: they are additive cluster-scoped
    resources, they moved into their own chart at 0.20, and a cluster
    with none of them cannot serve a model at all — see
    `test_an_existing_install_with_no_runtimes_still_gets_them`. The
    assertion below was `helm.calls == []` until 2026-10-09, which read
    as "touch nothing" but meant "ship a cluster that cannot serve".
    """
    inst, helm = installer(monkeypatch, present=(kp.ISVC_CRD,))
    notes = inst.ensure(KServePlatform(), "RawDeployment")
    assert kp.KSERVE_RELEASE not in [name for name, _ in helm.calls]
    assert kp.KSERVE_CRD_RELEASE not in [name for name, _ in helm.calls]
    assert kp.CERT_MANAGER_RELEASE not in [name for name, _ in helm.calls]
    assert "already installed" in notes[0]


def test_raw_deployment_installs_cert_manager_crds_and_controller(monkeypatch):
    inst, helm = installer(monkeypatch)
    inst.ensure(KServePlatform(), "RawDeployment")
    assert helm.releases() == [
        kp.CERT_MANAGER_RELEASE,
        kp.KSERVE_CRD_RELEASE,
        # twice: the webhook race, see _install_controller
        kp.KSERVE_RELEASE,
        kp.KSERVE_RELEASE,
        # the built-in runtimes, their own chart since KServe 0.20
        kp.KSERVE_RUNTIME_CONFIGS_RELEASE,
    ]


def test_serverless_installs_knative_even_when_kserve_already_exists(monkeypatch):
    """The bug this guards: returning early on the KServe CRD skipped
    Knative entirely, so Serverless on a cluster installed for
    RawDeployment installed nothing, reported that it would, and left an
    InferenceService that could never go ready."""
    inst, helm = installer(monkeypatch, present=(kp.ISVC_CRD,))
    notes = inst.ensure(KServePlatform(), "Serverless")
    assert kp.KNATIVE_OPERATOR_RELEASE in helm.releases()
    # and still does not touch the KServe it found
    assert kp.KSERVE_RELEASE not in helm.releases()
    assert any("already installed" in n for n in notes)


def test_the_controller_is_restarted_when_knative_lands_after_it(monkeypatch):
    """A controller that came up before Serving existed does not know
    about it, because it decides at startup what it can reconcile."""
    inst, _ = installer(monkeypatch, present=(kp.ISVC_CRD,))
    restarts = []
    monkeypatch.setattr(
        kp.KServePlatformInstaller,
        "_restart_controller",
        lambda self, p: restarts.append(1) or ["restarted"],
    )
    inst.ensure(KServePlatform(), "Serverless")
    assert restarts == [1]


def test_no_restart_when_kserve_is_installed_fresh(monkeypatch):
    """Knative goes in before KServe in that order, so the controller
    already sees it and a restart would be pure churn.

    Istio, so the ingress config already matches and nothing patches it --
    that is the other reason `ensure` restarts, and this test is about the
    ordering one.
    """
    # With Istio's CRD, because that is what "Istio" means to this
    # installer now: `disableIstioVirtualHost` is keyed on whether the
    # cluster can serve a VirtualService, not on which ingress was asked
    # for, so a cluster without the CRD gets patched and restarted
    # however it was configured.
    inst, _ = installer(monkeypatch, present=(kp.ISTIO_VIRTUALSERVICE_CRD,))
    restarts = []
    monkeypatch.setattr(
        kp.KServePlatformInstaller,
        "_restart_controller",
        lambda self, p: restarts.append(1) or ["restarted"],
    )
    inst.ensure(KServePlatform(knative_ingress="istio"), "Serverless")
    assert restarts == []


def test_knative_install_waits_for_serving_to_be_ready(monkeypatch):
    """Not for the CRD it produces. The operator creates
    services.serving.knative.dev early and then spends another
    half-minute on the activator, autoscaler, webhook and ingress — so
    returning on the CRD hands back a Serving that cannot yet serve."""
    inst, _ = installer(monkeypatch)

    def never_ready(self, *args, check=True):
        joined = " ".join(args)
        if "get crd" in joined:
            return ""
        if "availableReplicas" in joined:
            return "1"
        return ""  # KnativeServing Ready never returns True

    monkeypatch.setattr(kp.KServePlatformInstaller, "_kubectl", never_ready)
    with pytest.raises(InferenceError, match="Knative Serving did not become ready"):
        inst.ensure(KServePlatform(install_timeout=1), "Serverless")


def test_serverless_with_install_off_refuses_when_knative_is_absent(monkeypatch):
    inst, helm = installer(monkeypatch, present=(kp.ISVC_CRD,))
    with pytest.raises(InferencePrerequisiteError, match="Knative"):
        inst.ensure(KServePlatform(install=False), "Serverless")
    assert helm.calls == []


def test_raw_deployment_does_not_install_knative(monkeypatch):
    inst, helm = installer(monkeypatch)
    inst.ensure(KServePlatform(), "RawDeployment")
    assert kp.KNATIVE_OPERATOR_RELEASE not in helm.releases()


def test_serverless_installs_the_knative_operator_first(monkeypatch):
    """Before KServe: its controller decides at startup what it can
    reconcile."""
    inst, helm = installer(monkeypatch)
    inst.ensure(KServePlatform(), "Serverless")
    releases = helm.releases()
    assert kp.KNATIVE_OPERATOR_RELEASE in releases
    assert releases.index(kp.KNATIVE_OPERATOR_RELEASE) < releases.index(kp.KSERVE_RELEASE)


def test_cert_manager_precedes_kserve(monkeypatch):
    """KServe's admission webhooks are served over TLS it issues."""
    inst, helm = installer(monkeypatch)
    inst.ensure(KServePlatform(), "RawDeployment")
    releases = helm.releases()
    assert releases.index(kp.CERT_MANAGER_RELEASE) < releases.index(kp.KSERVE_CRD_RELEASE)


def test_an_existing_cert_manager_is_left_alone(monkeypatch):
    inst, helm = installer(monkeypatch, present=(kp.CERT_MANAGER_CRD,))
    inst.ensure(KServePlatform(), "RawDeployment")
    assert kp.CERT_MANAGER_RELEASE not in helm.releases()


def test_cert_manager_can_be_turned_off(monkeypatch):
    inst, helm = installer(monkeypatch)
    inst.ensure(KServePlatform(cert_manager=False), "RawDeployment")
    assert kp.CERT_MANAGER_RELEASE not in helm.releases()


def test_the_controller_carries_the_deployment_mode(monkeypatch):
    inst, helm = installer(monkeypatch)
    inst.ensure(KServePlatform(), "RawDeployment")
    controller = next(kw for name, kw in helm.calls if name == kp.KSERVE_RELEASE)
    # Normalised on the way in, so the chart gets 0.20's spelling even
    # though the caller used the old one.
    assert controller["values"]["kserve"]["controller"]["deploymentMode"] == "Standard"


def test_the_controller_installs_non_atomically(monkeypatch):
    """Rolling back would discard the healthy controller along with the
    serving runtimes that could not apply while its webhook was starting."""
    inst, helm = installer(monkeypatch)
    inst.ensure(KServePlatform(), "RawDeployment")
    for name, kw in helm.calls:
        if name == kp.KSERVE_RELEASE:
            assert kw["atomic"] is False


def test_a_first_pass_failure_is_expected_and_retried(monkeypatch):
    """The chart's ClusterServingRuntimes go through a webhook the
    controller itself serves, so the first pass cannot apply them."""

    class FailsOnce(FakeHelm):
        def install_or_upgrade(self, release, **kw):
            super().install_or_upgrade(release, **kw)
            if (
                release == kp.KSERVE_RELEASE
                and len([c for c in self.calls if c[0] == kp.KSERVE_RELEASE]) == 1
            ):
                raise kp.HelmManagerError("connection refused")
            return release

    inst, helm = installer(monkeypatch, helm=FailsOnce())
    inst.ensure(KServePlatform(), "RawDeployment")
    assert helm.releases().count(kp.KSERVE_RELEASE) == 2


def test_a_second_pass_failure_is_real(monkeypatch):
    class AlwaysFails(FakeHelm):
        def install_or_upgrade(self, release, **kw):
            super().install_or_upgrade(release, **kw)
            if release == kp.KSERVE_RELEASE:
                raise kp.HelmManagerError("something else entirely")
            return release

    inst, _ = installer(monkeypatch, helm=AlwaysFails())
    with pytest.raises(kp.HelmManagerError, match="something else"):
        inst.ensure(KServePlatform(), "RawDeployment")


def test_install_false_refuses_rather_than_installing(monkeypatch):
    inst, helm = installer(monkeypatch)
    with pytest.raises(InferencePrerequisiteError, match=r"platform\.install is False"):
        inst.ensure(KServePlatform(install=False), "RawDeployment")
    assert helm.calls == []


def test_the_knative_serving_resource_selects_the_ingress(monkeypatch):
    inst, _ = installer(monkeypatch)
    objects = {o["kind"]: o for o in inst.knative_serving_manifests(KServePlatform())}
    serving = objects["KnativeServing"]
    assert serving["spec"]["ingress"] == {"kourier": {"enabled": True}}
    assert serving["spec"]["config"]["network"]["ingress-class"].startswith("kourier")


def test_the_ingress_service_type_reaches_the_knative_resource(monkeypatch):
    """The operator owns the gateway's Service and reconciles it back, so
    setting the type on the Service directly looks like it worked and is
    reverted within the minute — observed live. The resource is the only
    place it sticks."""
    inst, _ = installer(monkeypatch)
    objects = {
        o["kind"]: o
        for o in inst.knative_serving_manifests(
            KServePlatform(knative_ingress_service_type="NodePort")
        )
    }
    kourier = objects["KnativeServing"]["spec"]["ingress"]["kourier"]
    assert kourier == {"enabled": True, "service-type": "NodePort"}


def test_no_service_type_leaves_the_operator_default(monkeypatch):
    inst, _ = installer(monkeypatch)
    objects = {o["kind"]: o for o in inst.knative_serving_manifests(KServePlatform())}
    assert objects["KnativeServing"]["spec"]["ingress"]["kourier"] == {"enabled": True}


def test_an_unknown_ingress_service_type_is_refused():
    with pytest.raises(ValueError, match="not a Service type"):
        KServePlatform(knative_ingress_service_type="Nodeport")


def test_istio_is_selectable_as_the_knative_ingress(monkeypatch):
    inst, _ = installer(monkeypatch)
    objects = {
        o["kind"]: o
        for o in inst.knative_serving_manifests(KServePlatform(knative_ingress="istio"))
    }
    assert objects["KnativeServing"]["spec"]["ingress"] == {"istio": {"enabled": True}}


def test_an_unknown_knative_ingress_is_refused():
    with pytest.raises(ValueError, match="knative_ingress"):
        KServePlatform(knative_ingress="nginx")


def test_the_platform_is_installed_before_the_checks_run(monkeypatch):
    """Several checks ask about APIs that only exist once KServe does, so
    checking first would only report the absence create() is about to fix."""
    order = []
    monkeypatch.setattr(
        ks.KServeDriver,
        "install_platform",
        lambda self, inf: order.append("install") or [],
    )
    monkeypatch.setattr(
        ks.KServeDriver,
        "check_prerequisites",
        lambda self, inf, nodes=None: order.append("check") or [],
    )
    monkeypatch.setattr(ks, "require_cli", lambda *a, **k: "/usr/bin/kubectl")
    monkeypatch.setattr(ks, "require_cluster", lambda *a, **k: "v1.32.5")
    monkeypatch.setattr(ks, "kube_apply", lambda *a, **k: "")
    monkeypatch.setattr(ks.KServeDriver, "_wait_until_ready", lambda self, inf: None)
    monkeypatch.setattr(ks.KServeDriver, "endpoint", lambda self, inf: "http://x")

    ks.KServeDriver().create(spec(warmup=False))
    assert order == ["install", "check"]


def test_the_backend_dispatches_to_this_driver():
    assert InferenceBackend.driver_class("kserve") is ks.KServeDriver


def test_every_supported_type_has_a_driver_and_options():
    """The guard that caught a `type` registered ahead of its driver."""
    from multistack.inference import DRIVERS

    assert set(Inference.SUPPORTED_TYPES) == set(DRIVERS)
    for name in DRIVERS:
        assert name in Inference.OPTIONS_FOR_TYPE


# -- openai_path_prefix vs the runtime -----------------------------------
# KServe's huggingfaceserver mounts /openai/v1/...; a runtime wrapping vLLM's
# own OpenAI server mounts /v1/... at the root. The InferenceService reports
# Ready either way, because the predictor is healthy -- so the mismatch only
# shows up as a 404 at the first real request, far from its cause. Observed
# live on a hand-built ROCm runtime.


def test_a_custom_runtime_with_the_default_prefix_warns(driver, monkeypatch):
    stub_kubectl(
        driver,
        monkeypatch,
        replies={
            ks.ISVC_CRD: "crd/isvc",
            "get clusterservingruntime": _runtime_json(
                port_name="http1", image="dev.local/vllm-gfx1151:optimized"
            ),
        },
    )
    s = spec(options=KServeOptions(runtime="vllm-gfx1151-dp"))
    warnings = driver.check_prerequisites(s)
    assert any("one segment too deep" in w for w in warnings)


def test_the_builtin_runtime_with_the_default_prefix_is_quiet(driver, monkeypatch):
    stub_kubectl(
        driver,
        monkeypatch,
        replies={
            ks.ISVC_CRD: "crd/isvc",
            "get clusterservingruntime": _runtime_json(port_name="http1"),
        },
    )
    s = spec(options=KServeOptions(runtime="kserve-huggingfaceserver"))
    assert driver.check_prerequisites(s) == []


def test_an_explicit_prefix_is_not_second_guessed(driver, monkeypatch):
    """Setting it is the decision; the warning exists only for the default."""
    stub_kubectl(
        driver,
        monkeypatch,
        replies={
            ks.ISVC_CRD: "crd/isvc",
            "get clusterservingruntime": _runtime_json(
                port_name="http1", image="dev.local/vllm-gfx1151:optimized"
            ),
        },
    )
    s = spec(options=KServeOptions(runtime="vllm-gfx1151-dp", openai_path_prefix=""))
    assert driver.check_prerequisites(s) == []


def test_no_prefix_check_without_a_pinned_runtime(driver, monkeypatch):
    """Only a pinned runtime has a definition this can read."""
    stub_kubectl(driver, monkeypatch, replies={ks.ISVC_CRD: "crd/isvc"})
    assert driver.check_prerequisites(spec()) == []


def test_an_empty_prefix_keeps_the_endpoint_at_the_root(driver):
    """What the warning is telling the caller to do, and its effect."""
    s = spec(name="qwen", namespace="inference", options=KServeOptions(openai_path_prefix=""))
    assert s.endpoint == ("http://qwen-predictor.inference.svc.cluster.local:80")


# -- KServe's ingress config vs the Knative ingress actually installed ----
# KServe's chart defaults name Istio's gateway. This driver installs Knative
# with Kourier by default, so the default combination left KServe resolving
# `knative-local-gateway.istio-system.svc.cluster.local` on a cluster with no
# istio-system namespace. The InferenceService then reports
#   Ready  Unknown  "Waiting for load balancer to be ready"
# which names neither Istio nor the real problem. Observed live: the Knative
# Route was Ready with a working cluster-local URL while the InferenceService
# above it waited on a gateway that did not exist.


def _ingress_patch(inst):
    """The ingress JSON this run patched in, or None."""
    for call in inst.kubectl_calls:
        if "patch configmap" in call and "data" in call:
            start = call.index("{")
            return json.loads(json.loads(call[start:])["data"]["ingress"])
    return None


def test_kourier_repoints_kserve_at_the_gateway_that_exists(monkeypatch):
    inst, _ = installer(monkeypatch, present=(kp.ISVC_CRD,))
    notes = inst.ensure(KServePlatform(knative_ingress="kourier"), "Serverless")

    patched = _ingress_patch(inst)
    assert patched is not None, "the ingress config was never patched"
    expected = "kourier-internal.knative-serving.svc.cluster.local"
    assert patched["localGatewayService"] == expected
    assert patched["knativeLocalGatewayService"] == expected
    assert patched["ingressClassName"] == "kourier"
    # With Kourier there is no Istio VirtualService to create, and leaving
    # this false is what makes KServe block.
    assert patched["disableIstioVirtualHost"] is True
    assert any("replacing the chart's Istio defaults" in n for n in notes)


def test_keys_the_patch_does_not_own_are_left_alone(monkeypatch):
    """Only the implementation-specific keys change; the rest survive."""
    inst, _ = installer(monkeypatch, present=(kp.ISVC_CRD,))
    inst.ensure(KServePlatform(knative_ingress="kourier"), "Serverless")
    assert _ingress_patch(inst)["urlScheme"] == "http"


def test_the_serving_namespace_is_honoured(monkeypatch):
    inst, _ = installer(monkeypatch, present=(kp.ISVC_CRD,))
    inst.ensure(
        KServePlatform(knative_ingress="kourier", knative_serving_namespace="kn"), "Serverless"
    )
    assert _ingress_patch(inst)["localGatewayService"] == ("kourier-internal.kn.svc.cluster.local")


def test_istio_is_left_as_the_chart_shipped_it(monkeypatch):
    """An Istio cluster already matches, and rewriting these would undo a
    platform team's own settings."""
    inst, _ = installer(monkeypatch, present=(kp.ISVC_CRD, kp.ISTIO_VIRTUALSERVICE_CRD))
    inst.ensure(KServePlatform(knative_ingress="istio"), "Serverless")
    assert _ingress_patch(inst) is None


def test_an_already_aligned_config_is_not_patched_again(monkeypatch):
    """Idempotent by inspection, like every other step in this installer."""
    aligned = {
        "localGatewayService": "kourier-internal.knative-serving.svc.cluster.local",
        "knativeLocalGatewayService": "kourier-internal.knative-serving.svc.cluster.local",
        "ingressClassName": "kourier",
        "disableIstioVirtualHost": True,
    }
    inst, _ = installer(monkeypatch, present=(kp.ISVC_CRD,), ingress=aligned)
    inst.ensure(KServePlatform(knative_ingress="kourier"), "Serverless")
    assert _ingress_patch(inst) is None


def test_raw_deployment_leaves_the_knative_gateway_keys_alone(monkeypatch):
    """RawDeployment has no Knative route, so the gateway keys do not apply.

    Istio present, so the one key that *does* apply in every mode --
    `disableIstioVirtualHost`, below -- is already right and nothing is
    patched at all.
    """
    inst, _ = installer(monkeypatch, present=(kp.ISVC_CRD, kp.ISTIO_VIRTUALSERVICE_CRD))
    inst.ensure(KServePlatform(knative_ingress="kourier"), "RawDeployment")
    assert _ingress_patch(inst) is None


def test_a_cluster_with_no_istio_stops_waiting_for_a_virtualservice(monkeypatch):
    """The bug the test above used to assert.

    `disableIstioVirtualHost` defaults to false, so the controller tries
    to reconcile an Istio VirtualService for every InferenceService. With
    no Istio it logs `VirtualService CRD not present; VirtualService
    reconciliation skipped` -- which reads as advice -- and then holds the
    InferenceService at `IngressReady False "Predictor ingress not
    created"` for good. A predictor whose model loaded perfectly never
    goes ready.

    Alignment used to be gated on Knative-with-Kourier, which left
    Standard mode -- the default, and the mode with no Knative at all --
    pointed at an Istio that was never installed. Observed live on
    2026-10-09.
    """
    inst, _ = installer(monkeypatch, present=(kp.ISVC_CRD,))
    inst.ensure(KServePlatform(), "Standard")

    patch = _ingress_patch(inst)
    assert patch is not None, (
        "a cluster with no Istio was left reconciling VirtualServices, so "
        "every InferenceService stays 'Predictor ingress not created'"
    )
    assert patch["disableIstioVirtualHost"] is True
    # Only that key. The Knative gateway keys are Kourier's business and
    # this cluster has no Knative at all.
    assert patch["ingressClassName"] == "istio"


def test_the_virtualhost_setting_is_reached_on_an_existing_install(monkeypatch):
    """The path a rebuild actually takes.

    `ensure` returns early on an existing KServe, and in Standard mode
    that early return skipped ingress alignment entirely -- so the one
    cluster shape that needed this most never got it.
    """
    inst, _ = installer(monkeypatch, present=(kp.ISVC_CRD,))
    inst.ensure(KServePlatform(), "Standard")
    assert _ingress_patch(inst)["disableIstioVirtualHost"] is True


def test_a_patched_config_restarts_the_controller_exactly_once(monkeypatch):
    """The controller reads this at startup, so a patch alone changes
    nothing -- and `ensure` may already owe a restart because Serving landed
    after the controller. Two rolling restarts is one more than needed."""
    inst, _ = installer(monkeypatch, present=(kp.ISVC_CRD,))
    restarts = []
    monkeypatch.setattr(
        kp.KServePlatformInstaller,
        "_restart_controller",
        lambda self, p: restarts.append(1) or ["restarted"],
    )
    inst.ensure(KServePlatform(knative_ingress="kourier"), "Serverless")
    assert restarts == [1]


def test_an_unreadable_config_is_reported_not_patched(monkeypatch):
    """Freshly installed KServe always has this ConfigMap, so an empty read
    means something else is wrong -- say so rather than patch blind."""
    inst, _ = installer(monkeypatch, present=(kp.ISVC_CRD,))
    monkeypatch.setattr(kp.KServePlatformInstaller, "_kubectl", lambda self, *a, **k: "")
    notes, changed = inst._align_ingress_config(KServePlatform(knative_ingress="kourier"))
    assert changed is False
    assert any("may still be pointed at Istio" in n for n in notes)


# -- KServe 0.20: renamed modes, renamed controller chart ----------------
# 0.20 renamed "Serverless" to "Knative" and "RawDeployment" to "Standard",
# keeping the old pair as deprecated aliases. It also stopped publishing the
# controller chart as `charts/kserve` (last tag v0.16.0); from v0.17.0 it is
# `charts/kserve-resources`. Pinning the version without the new path fails
# on a tag that does not exist, which reads as a bad version rather than a
# moved chart.


def test_the_controller_chart_is_the_one_published_at_this_version():
    from multistack.inference.spec import (
        DEFAULT_KSERVE_VERSION,
        KSERVE_CONTROLLER_CHART,
    )

    assert KSERVE_CONTROLLER_CHART.endswith("/kserve-resources")
    assert DEFAULT_KSERVE_VERSION == "v0.20.0"


@pytest.mark.parametrize(
    "written, canonical, knative",
    [
        ("Serverless", "Knative", True),
        ("Knative", "Knative", True),
        ("RawDeployment", "Standard", False),
        ("Standard", "Standard", False),
    ],
)
def test_both_spellings_resolve_to_one_mode(written, canonical, knative):
    options = KServeOptions(deployment_mode=written)
    assert options.canonical_deployment_mode == canonical
    assert options.uses_knative is knative


def test_an_unknown_mode_names_every_accepted_spelling():
    """Including that the old pair still works, so a 0.15-era spec that hits
    a typo is not also told its mode was removed."""
    from multistack.inference.spec import SUPPORTED_DEPLOYMENT_MODES

    with pytest.raises(ValueError) as exc:
        Inference(
            kubeconfig_path="/tmp/kc.yaml",
            type="kserve",
            options=KServeOptions(deployment_mode="Nonsense"),
        )
    message = str(exc.value)
    for mode in SUPPORTED_DEPLOYMENT_MODES:
        assert repr(mode) in message


def test_knative_is_installed_for_the_new_spelling_too(monkeypatch):
    """The installer takes the mode as a plain string, so it normalises on
    the way in -- a caller passing 0.20's name must still get Knative."""
    inst, _helm = installer(monkeypatch, present=(kp.ISVC_CRD,))
    notes = inst.ensure(KServePlatform(), "Knative")
    assert any("Knative" in n or "knative" in n for n in notes)


# -- the built-in runtimes, split out in KServe 0.20 ---------------------
# `kserve-resources` shipped thirteen ClusterServingRuntimes at v0.15 and
# ships one at v0.20; the rest moved to `kserve-runtime-configs`. A default
# spec -- model_format="huggingface", no explicit `runtime` -- relies on
# KServe matching one by format, so without that chart the InferenceService
# waits on a runtime nobody installed.


def test_the_runtime_configs_chart_is_installed_by_default(monkeypatch):
    inst, helm = installer(monkeypatch)
    inst.ensure(KServePlatform(), "Standard")

    assert kp.KSERVE_RUNTIME_CONFIGS_RELEASE in helm.releases()
    call = next(kw for name, kw in helm.calls if name == kp.KSERVE_RUNTIME_CONFIGS_RELEASE)
    assert call["chart"].endswith("/kserve-runtime-configs")


def test_the_runtime_configs_chart_is_told_to_render_the_runtimes(monkeypatch):
    """Installing it with no values renders nothing at all.

    `kserve.servingruntime.enabled` defaults to **false** in that chart, so
    `values={}` produces a release that reaches `deployed` while the cluster
    gets zero ClusterServingRuntimes -- and an InferenceService with no
    explicit `runtime` then has nothing to match on and never goes ready.

    Observed on a live cluster on 2026-10-09, and the test above did not
    catch it because asserting the chart name and version says nothing about
    whether the chart was asked to do anything. The empty release also hid
    the error: Helm omits `manifest` from `status --output json` when a
    release renders nothing, pyhelm3 reads `status["manifest"]` unguarded,
    and the successful install surfaced as
    `HelmDeploymentError: ... 'manifest'`.
    """
    inst, helm = installer(monkeypatch)
    inst.ensure(KServePlatform(), "Standard")

    call = next(kw for name, kw in helm.calls if name == kp.KSERVE_RUNTIME_CONFIGS_RELEASE)
    values = call["values"] or {}
    assert values.get("kserve", {}).get("servingruntime", {}).get("enabled") is True, (
        "the runtime-configs chart was installed without "
        "kserve.servingruntime.enabled=true, so it renders no runtimes"
    )
    # The LLM presets are their own release, never this one's.
    assert values["kserve"]["llmisvcConfigs"]["enabled"] is False


def test_the_runtimes_are_pinned_to_the_same_version_as_the_controller(monkeypatch):
    """A runtime chart ahead of its controller is a combination nobody
    tests upstream."""
    inst, helm = installer(monkeypatch)
    inst.ensure(KServePlatform(chart_version="v0.20.0"), "Standard")

    by_release = {name: kw for name, kw in helm.calls}
    assert by_release[kp.KSERVE_RUNTIME_CONFIGS_RELEASE]["chart_version"] == "v0.20.0"
    assert by_release[kp.KSERVE_RELEASE]["chart_version"] == "v0.20.0"


def test_the_runtimes_go_in_after_the_webhook_answers(monkeypatch):
    """They are webhook-validated, so applying them before the controller
    is serving fails with connection refused -- the same race the
    controller's own second pass exists for."""
    inst, helm = installer(monkeypatch)
    inst.ensure(KServePlatform(), "Standard")

    releases = helm.releases()
    last_controller = len(releases) - 1 - releases[::-1].index(kp.KSERVE_RELEASE)
    assert releases.index(kp.KSERVE_RUNTIME_CONFIGS_RELEASE) > last_controller


def test_the_runtimes_can_be_left_to_someone_else(monkeypatch):
    inst, helm = installer(monkeypatch)
    notes = inst.ensure(KServePlatform(install_runtime_configs=False), "Standard")

    assert kp.KSERVE_RUNTIME_CONFIGS_RELEASE not in helm.releases()
    assert any("does not name" in n for n in notes)


def test_an_existing_install_with_no_runtimes_still_gets_them(monkeypatch):
    """The shortcut skipped the one chart a partial install is missing.

    `ensure` returns early on the InferenceService CRD -- rightly, since
    it will not upgrade a release it may not have created -- but the
    runtimes were installed inside `_install_controller`, below that
    return. So a cluster carrying the CRD and nothing else got the note
    "KServe is already installed; leaving it as it is" and zero
    ClusterServingRuntimes, and the next `apply` failed with
    `no runtime found to support predictor with model type:
    {huggingface <nil>}` -- a message that names the model rather than
    the missing chart.

    Observed on a live cluster on 2026-10-09, immediately after fixing
    the empty-render bug above: the fix was correct and never ran.
    """
    inst, helm = installer(monkeypatch, present=(kp.ISVC_CRD,))
    notes = inst.ensure(KServePlatform(), "Standard")

    installed = [name for name, _ in helm.calls]
    assert kp.KSERVE_RUNTIME_CONFIGS_RELEASE in installed, (
        "an existing KServe install with no runtimes was left with none, "
        f"so nothing in this cluster can serve a model: {notes}"
    )
    # Still no touching the controller release -- that is what the
    # shortcut is for, and this must not become an upgrade of it.
    assert kp.KSERVE_RELEASE not in installed


def test_an_existing_install_that_has_its_own_runtimes_is_left_alone(monkeypatch):
    """`leaving it as it is` has to keep meaning that.

    A cluster whose runtimes someone else manages -- a vendor
    distribution, a hand-built set -- must not have ours laid over the
    top. Presence of any ClusterServingRuntime is the signal.
    """
    inst, helm = installer(monkeypatch, present=(kp.ISVC_CRD,))
    monkeypatch.setattr(kp.KServePlatformInstaller, "_serving_runtimes_exist", lambda self: True)

    inst.ensure(KServePlatform(), "Standard")
    assert kp.KSERVE_RUNTIME_CONFIGS_RELEASE not in [n for n, _ in helm.calls]


def test_the_runtime_opt_out_is_still_honoured_on_an_existing_install(monkeypatch):
    """install_runtime_configs=False means it on both paths."""
    inst, helm = installer(monkeypatch, present=(kp.ISVC_CRD,))
    notes = inst.ensure(KServePlatform(install_runtime_configs=False), "Standard")

    assert kp.KSERVE_RUNTIME_CONFIGS_RELEASE not in [n for n, _ in helm.calls]
    assert any("install_runtime_configs=False" in note for note in notes)


def test_the_served_name_is_the_inferenceservice_name():
    """What a client is allowed to put in `model`.

    KServe's OpenAI endpoint answers to the InferenceService's own name
    and nothing else -- confirmed against a live predictor on
    2026-10-12, which advertised `{"id": "kserve-qwen"}`. `served_name`
    returned the vLLM answer (the last segment of `model`) for both, so
    the gateway advertised "Qwen2.5-0.5B-Instruct" in GET /v1/models,
    the org portal's picker offered it as the only choice, and the
    completion came back `404 Model with name Qwen2.5-0.5B-Instruct
    does not exist`.

    Discover-then-use is how every OpenAI-compatible client works, so a
    model list the upstream rejects breaks all of them.
    """
    spec = s3_spec(name="kserve-qwen")
    assert spec.served_name == "kserve-qwen"


def test_an_explicit_served_model_name_renames_a_kserve_model():
    """The driver passes it to the runtime as --model_name, and the runtime
    then answers to it alone -- confirmed live on 2026-10-13: served
    "custom-qwen", 404 for the InferenceService's own name. `served_name`
    and the runtime have to agree, or the gateway publishes a name the
    runtime rejects."""
    spec = s3_spec(name="kserve-qwen", served_model_name="something-else")
    assert spec.served_name == "something-else"
    args = ks.KServeDriver().container_args(spec)
    assert args[args.index("--model_name") + 1] == spec.served_name
