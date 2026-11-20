"""`Inference.model_cache`: where the serving copy of the weights lives.

One setting, four implementations. These pin down three things: that each
mode is refused where it would silently do nothing, that each driver renders
the mode as its docstring says, and that "none" -- the default -- renders
exactly what it did before the setting existed.

Nothing here shells out; the driver seams are stubbed as in the sibling
test modules.
"""

import json

import pytest

from multistack.inference import Inference, KServeModelCache, KServePlatform
from multistack.inference.drivers import kserve as ks
from multistack.inference.drivers.kserve import KServeDriver
from multistack.inference.drivers.llamacpp import LlamaCppDriver
from multistack.inference.drivers.vllm import VLLMDriver
from multistack.inference.spec import CACHE_MARKER, DEFAULT_NODE_CACHE_PATH

KUBECONFIG = "/tmp/kc.yaml"
S3 = dict(
    model="s3://models/qwen2.5-0.5b/v1",
    s3_endpoint_url="http://seaweedfs-s3.objectstore:8333",
    s3_secret_name="seaweedfs-creds",
)


def spec(**kwargs) -> Inference:
    return Inference(kubeconfig_path=KUBECONFIG, **kwargs)


def deployment_pod(objects):
    return next(o for o in objects if o["kind"] == "Deployment")["spec"]["template"]["spec"]


def by_kind(objects, kind):
    return [o for o in objects if o["kind"] == kind]


# -- spec: refused where it would be ignored ---------------------------------
def test_none_is_the_default():
    assert spec().model_cache == "none"


def test_unknown_mode_is_refused():
    with pytest.raises(ValueError, match="model_cache"):
        spec(model_cache="disk")


def test_stream_needs_an_object_store_model():
    with pytest.raises(ValueError, match="s3://"):
        spec(model_cache="stream")


def test_stream_is_refused_for_llamacpp():
    with pytest.raises(ValueError, match="GGUF"):
        spec(type="llamacpp", model_cache="stream", **S3)


def test_stream_is_refused_for_classic_kserve_and_points_at_kserve_llm():
    # An InferenceService always runs the storage-initializer, so asking it
    # to stream would download anyway while looking like it streamed.
    with pytest.raises(ValueError, match="kserve_llm"):
        spec(type="kserve", model_cache="stream", **S3)


def test_kserve_node_cache_needs_a_model_size():
    with pytest.raises(ValueError, match="cache_size_gb"):
        spec(type="kserve", model_cache="node", **S3)


def test_namespace_scope_is_refused_outside_kserve():
    # Promising isolation on a host folder any pod can read would be a lie.
    with pytest.raises(ValueError, match="cache_scope"):
        spec(model_cache="node", cache_scope="namespace", **S3)


def test_node_group_is_refused_outside_kserve():
    with pytest.raises(ValueError, match="cache_node_group"):
        spec(model_cache="node", cache_node_group="gpu-local", **S3)


def test_cache_path_is_refused_on_kserve():
    with pytest.raises(ValueError, match="cache_path"):
        spec(type="kserve", model_cache="node", cache_size_gb=2, cache_path="/data", **S3)


def test_cache_settings_without_node_mode_are_refused_on_kserve():
    with pytest.raises(ValueError, match=r"only\s+apply to model_cache='node'"):
        spec(type="kserve", cache_size_gb=2, **S3)


def test_cache_path_must_be_absolute():
    with pytest.raises(ValueError, match="absolute"):
        spec(model_cache="node", cache_path="models", **S3)


def test_cache_path_without_node_mode_is_refused_for_vllm():
    with pytest.raises(ValueError, match="cache_path"):
        spec(cache_path="/data", **S3)


def test_cache_path_is_accepted_for_llamacpp_in_its_default_mode():
    # llama.cpp has always cached on the node, so "none" keeps its folder.
    assert spec(type="llamacpp", cache_path="/data/gguf").cache_path == "/data/gguf"


# -- served name ----------------------------------------------------------------
@pytest.mark.parametrize(
    "model, expected",
    [
        ("s3://models/qwen2.5-0.5b/v1", "qwen2.5-0.5b"),
        ("s3://models/qwen2.5-0.5b/v2.1/", "qwen2.5-0.5b"),
        ("s3://models/llama-8b/2026-10-01", "llama-8b"),
        ("s3://models/Qwen2.5-0.5B", "Qwen2.5-0.5B"),
        ("Qwen/Qwen2.5-0.5B-Instruct", "Qwen2.5-0.5B-Instruct"),
    ],
)
def test_a_trailing_version_folder_is_not_the_served_name(model, expected):
    # Versioned paths are the recommended layout; serving every one of them
    # as "v1" would make every model indistinguishable to clients.
    extra = S3 if model.startswith("s3://") else {}
    extra = {**extra, "model": model}
    assert spec(**extra).served_name == expected


# -- vllm: none is unchanged ---------------------------------------------------
def test_none_keeps_the_temporary_folder_and_mirror():
    pod = deployment_pod(VLLMDriver().manifests(spec(**S3)))
    assert {"name": "model-store", "emptyDir": {"sizeLimit": "20Gi"}} in pod["volumes"]
    assert pod["initContainers"][0]["name"] == "fetch-weights"
    assert CACHE_MARKER not in pod["initContainers"][0]["command"][2]


# -- vllm: node ----------------------------------------------------------------
def test_node_mode_mounts_a_host_folder():
    pod = deployment_pod(VLLMDriver().manifests(spec(model_cache="node", **S3)))
    volume = next(v for v in pod["volumes"] if v["name"] == "model-store")
    assert volume == {
        "name": "model-store",
        "hostPath": {
            "path": DEFAULT_NODE_CACHE_PATH,
            "type": "DirectoryOrCreate",
        },
    }


def test_node_mode_honours_cache_path():
    pod = deployment_pod(
        VLLMDriver().manifests(spec(model_cache="node", cache_path="/mnt/nvme/models", **S3))
    )
    volume = next(v for v in pod["volumes"] if v["name"] == "model-store")
    assert volume["hostPath"]["path"] == "/mnt/nvme/models"


def test_node_mode_keys_the_folder_by_object_path_not_served_name():
    # Two versions served under one name must never share a folder.
    v1 = spec(model_cache="node", **S3)
    v2 = spec(model_cache="node", **{**S3, "model": "s3://models/qwen2.5-0.5b/v2"})
    assert v1.served_name == v2.served_name
    assert v1.model_path != v2.model_path
    assert v1.model_path == "/models/models/qwen2.5-0.5b/v1"


def test_node_mode_skips_the_download_when_the_marker_matches():
    script = spec(model_cache="node", **S3).mirror_command()[2]
    check = script.index('if [ -f "$marker" ] && [ "$(cat "$marker")" = "$source" ]')
    sync = script.index("s3 sync")
    assert check < sync, "the marker must be checked before any download"
    assert 'source="s3://models/qwen2.5-0.5b/v1"' in script


def test_node_mode_downloads_beside_then_renames_then_marks():
    # A pod killed mid-download must leave nothing that looks finished.
    script = spec(model_cache="node", **S3).mirror_command()[2]
    sync = script.index("s3 sync")
    rename = script.index('mv "$partial" "$dest"')
    mark = script.index('echo "$source" > "$marker"')
    assert '"$partial" --no-progress' in script
    assert sync < rename < mark


def test_node_mode_gives_each_pod_its_own_partial_folder():
    script = spec(model_cache="node", **S3).mirror_command()[2]
    assert 'partial="$dest.partial-$HOSTNAME"' in script
    # Whoever finishes second keeps the first copy rather than renaming
    # over a model another replica is already serving.
    assert script.count('[ "$(cat "$marker")" = "$source" ]') == 2


def test_node_mode_carries_no_credentials_in_the_script():
    script = spec(model_cache="node", **S3).mirror_command()[2]
    assert "AWS_ACCESS_KEY_ID" not in script and "AWS_SECRET_ACCESS_KEY" not in script


def test_node_mode_without_a_node_selector_warns():
    warnings = VLLMDriver().check_prerequisites(spec(model_cache="node", **S3))
    assert any("node_selector" in w for w in warnings)


def test_node_mode_with_a_huggingface_model_caches_the_hf_folder():
    pod = deployment_pod(VLLMDriver().manifests(spec(model_cache="node")))
    assert "initContainers" not in pod
    assert pod["containers"][0]["volumeMounts"][0]["mountPath"] == "/root/.cache/huggingface"
    assert next(v for v in pod["volumes"] if v["name"] == "model-store")["hostPath"]


# -- vllm: stream --------------------------------------------------------------
def test_stream_mode_has_no_mirror_and_no_model_volume():
    pod = deployment_pod(VLLMDriver().manifests(spec(model_cache="stream", image="x", **S3)))
    assert "initContainers" not in pod
    assert [v["name"] for v in pod["volumes"]] == ["dshm"]


def test_stream_mode_points_vllm_at_the_bucket_with_the_streamer():
    args = spec(model_cache="stream", image="x", **S3).container_args()
    assert args[args.index("--model") + 1] == "s3://models/qwen2.5-0.5b/v1"
    assert args[args.index("--load-format") + 1] == "runai_streamer"
    assert args[args.index("--served-model-name") + 1] == "qwen2.5-0.5b"


def test_stream_mode_gives_the_server_path_style_and_secret_refs():
    pod = deployment_pod(VLLMDriver().manifests(spec(model_cache="stream", image="x", **S3)))
    env = {e["name"]: e for e in pod["containers"][0]["env"]}
    assert env["RUNAI_STREAMER_S3_USE_VIRTUAL_ADDRESSING"]["value"] == "0"
    assert env["AWS_ENDPOINT_URL"]["value"] == S3["s3_endpoint_url"]
    # Credentials by reference only, never as values in the manifest.
    assert "value" not in env["AWS_SECRET_ACCESS_KEY"]
    assert env["AWS_SECRET_ACCESS_KEY"]["valueFrom"]["secretKeyRef"]["name"] == "seaweedfs-creds"


def test_stream_mode_with_the_default_image_warns():
    warnings = VLLMDriver().check_prerequisites(spec(model_cache="stream", **S3))
    assert any("vllm[runai]" in w for w in warnings)


def test_delete_never_touches_the_host_cache(monkeypatch, capsys):
    calls = []
    driver = VLLMDriver()
    monkeypatch.setattr(driver, "_require_cli", lambda name: None)
    monkeypatch.setattr(driver, "_kubectl", lambda inf, *a, **k: calls.append(a) or "")
    driver.delete(spec(model_cache="node", **S3), remove_cache=True)
    assert all(c[1] in ("deployment", "service", "servicemonitor") for c in calls)
    assert "left in place" in capsys.readouterr().out


# -- llamacpp --------------------------------------------------------------------
def test_llamacpp_cache_path_overrides_its_option():
    s = spec(
        type="llamacpp", model="Qwen/Qwen2.5-7B-Instruct-GGUF:Q4_K_M", cache_path="/mnt/nvme/gguf"
    )
    pod = deployment_pod(LlamaCppDriver().manifests(s))
    assert pod["volumes"][0]["hostPath"]["path"] == "/mnt/nvme/gguf"


def test_llamacpp_keeps_its_own_folder_by_default():
    s = spec(type="llamacpp", model="Qwen/Qwen2.5-7B-Instruct-GGUF:Q4_K_M")
    pod = deployment_pod(LlamaCppDriver().manifests(s))
    assert pod["volumes"][0]["hostPath"]["path"] == "/var/lib/llamacpp-models"


# -- kserve: LocalModelCache --------------------------------------------------
def kserve_spec(**kwargs) -> Inference:
    # The cluster scope: these tests were written for it, and it is the
    # one with a shared account. The default (namespace) has tests of its own.
    defaults = dict(
        type="kserve", name="qwen", model_cache="node", cache_size_gb=2, cache_scope="cluster", **S3
    )
    return spec(**{**defaults, **kwargs})


def test_node_mode_renders_a_cache_whose_uri_matches_the_storage_uri():
    driver = KServeDriver()
    objects = driver.manifests(kserve_spec())
    cache = by_kind(objects, "LocalModelCache")[0]
    isvc = by_kind(objects, "InferenceService")[0]
    # String equality is KServe's only link between the two.
    assert cache["spec"]["sourceModelUri"] == isvc["spec"]["predictor"]["model"]["storageUri"]
    assert cache["spec"]["modelSize"] == "2Gi"
    assert cache["spec"]["nodeGroups"] == ["gpu-local"]


def test_the_cache_comes_before_the_inference_service():
    kinds = [o["kind"] for o in KServeDriver().manifests(kserve_spec())]
    assert kinds.index("LocalModelCache") < kinds.index("InferenceService")


def test_a_namespace_cache_is_named_apart_from_the_cluster_one():
    # Same model, both scopes, one namespace: KServe names the volume claim
    # after the cache, so equal names collided on a live cluster.
    driver = KServeDriver()
    cluster = driver.cache_name(kserve_spec())
    namespaced = driver.cache_name(kserve_spec(cache_scope="namespace"))
    assert namespaced == cluster + "-ns"


def test_the_cache_is_named_after_the_model_so_deployments_share_it():
    driver = KServeDriver()
    a = driver.cache_name(kserve_spec(name="a"))
    b = driver.cache_name(kserve_spec(name="b"))
    other = driver.cache_name(kserve_spec(model="s3://models/qwen2.5-0.5b/v2"))
    assert a == b != other
    assert a.startswith("model-") and len(a) == len("model-") + 16


def test_a_cluster_cache_downloads_as_its_own_account_in_the_job_namespace():
    objects = KServeDriver().manifests(kserve_spec())
    account = [
        o
        for o in by_kind(objects, "ServiceAccount")
        if o["metadata"]["namespace"] == "kserve-localmodel-jobs"
    ]
    name = account[0]["metadata"]["name"]
    assert account and account[0]["secrets"] == [{"name": name}]
    cache = by_kind(objects, "LocalModelCache")[0]
    assert cache["spec"]["serviceAccountName"] == name


def test_a_namespace_cache_downloads_in_the_job_namespace_too():
    """KServe runs a namespace cache's download in its job namespace as
    well, and looks the account up there: an account in the deployment's
    own namespace leaves the Job on `default`, with no credentials."""
    objects = KServeDriver().manifests(kserve_spec(cache_scope="namespace"))
    assert not by_kind(objects, "LocalModelCache")
    cache = by_kind(objects, "LocalModelNamespaceCache")[0]
    assert cache["metadata"]["namespace"] == "inference"
    account = cache["spec"]["serviceAccountName"]
    assert any(
        o["metadata"]
        == {
            "name": account,
            "namespace": "kserve-localmodel-jobs",
            "labels": {
                "multistack.io/model-cache": KServeDriver().cache_name(
                    kserve_spec(cache_scope="namespace")
                )
            },
        }
        for o in by_kind(objects, "ServiceAccount")
    )


def test_two_namespaces_never_share_a_download_account():
    """Both can have a `seaweedfs-creds` with different keys; one shared
    copy would download one organisation's weights with the other's."""
    driver = KServeDriver()
    a = driver.cache_service_account(kserve_spec(cache_scope="namespace", namespace="org-a"))
    b = driver.cache_service_account(kserve_spec(cache_scope="namespace", namespace="org-b"))
    assert a != b


def test_cache_node_group_overrides_the_platform_default():
    cache = by_kind(
        KServeDriver().manifests(kserve_spec(cache_node_group="nvme")), "LocalModelCache"
    )[0]
    assert cache["spec"]["nodeGroups"] == ["nvme"]


def test_none_renders_no_cache():
    objects = KServeDriver().manifests(spec(type="kserve", name="qwen", **S3))
    assert not by_kind(objects, "LocalModelCache")


SOURCE_SECRET = json.dumps(
    {
        "type": "Opaque",
        "metadata": {
            "name": "seaweedfs-creds",
            "namespace": "inference",
            "annotations": {
                "serving.kserve.io/s3-endpoint": "seaweedfs-s3:8333",
                "kubectl.kubernetes.io/last-applied-configuration": "{...}",
            },
        },
        "data": {"AWS_ACCESS_KEY_ID": "a2V5", "AWS_SECRET_ACCESS_KEY": "c2VjcmV0"},
    }
)


def stub_kserve(monkeypatch, replies=None, record=None):
    replies = {"get secret seaweedfs-creds": SOURCE_SECRET, **(replies or {})}
    monkeypatch.setattr(ks, "require_cli", lambda *a, **k: "/usr/bin/kubectl")
    monkeypatch.setattr(ks, "require_cluster", lambda *a, **k: "v1.32.5")

    def fake(self, inference, *args, check=True):
        if record is not None:
            record.append(" ".join(args))
        joined = " ".join(args)
        for needle, reply in replies.items():
            if needle in joined:
                return reply
        return ""

    monkeypatch.setattr(KServeDriver, "_kubectl", fake)


def test_delete_keeps_the_cache_unless_asked(monkeypatch):
    calls = []
    stub_kserve(monkeypatch, record=calls)
    KServeDriver().delete(kserve_spec())
    assert not any("localmodelcache" in c for c in calls)

    calls.clear()
    KServeDriver().delete(kserve_spec(), remove_cache=True)
    assert any(c.startswith("delete localmodelcache model-") for c in calls)


def test_a_missing_cache_crd_is_fatal_when_nothing_may_install_it(monkeypatch):
    stub_kserve(monkeypatch, replies={f"crd {ks.ISVC_CRD}": "crd/x"})
    s = kserve_spec(options={"platform": KServePlatform(install=False)})
    with pytest.raises(ks.InferencePrerequisiteError, match="local model cache"):
        KServeDriver().check_prerequisites(s)


def test_the_download_secret_is_copied_with_only_kserves_annotations(monkeypatch):
    stub_kserve(monkeypatch)
    copy = KServeDriver().download_secret_manifest(kserve_spec())
    assert copy["metadata"]["namespace"] == "kserve-localmodel-jobs"
    assert copy["metadata"]["name"] == KServeDriver().cache_service_account(kserve_spec())
    assert copy["data"] == json.loads(SOURCE_SECRET)["data"]
    # The endpoint annotation is what keeps the download off AWS.
    assert copy["metadata"]["annotations"] == {"serving.kserve.io/s3-endpoint": "seaweedfs-s3:8333"}


def test_a_missing_source_secret_is_refused_before_the_cache(monkeypatch):
    stub_kserve(monkeypatch, replies={"get secret seaweedfs-creds": ""})
    applied = []
    monkeypatch.setattr(ks, "kube_apply", lambda kc, objs, **k: applied.extend(objs))
    with pytest.raises(ks.InferencePrerequisiteError, match="seaweedfs-creds"):
        KServeDriver()._apply_model_cache_first(kserve_spec())
    assert applied == []


def test_a_cache_with_the_local_model_switch_off_is_refused_when_nothing_may_fix_it(monkeypatch):
    stub_kserve(
        monkeypatch,
        replies={
            f"crd {ks.ISVC_CRD}": "crd/x",
            f"crd {ks.LOCALMODEL_CACHE_CRD}": "crd/y",
            "data.localModel": '{"enabled": false}',
        },
    )
    s = kserve_spec(options={"platform": KServePlatform(install=False)})
    with pytest.raises(ks.InferencePrerequisiteError, match=r"localModel\.enabled"):
        KServeDriver().check_prerequisites(s)


def test_a_cache_with_the_local_model_switch_on_passes(monkeypatch):
    stub_kserve(
        monkeypatch,
        replies={
            f"crd {ks.ISVC_CRD}": "crd/x",
            f"crd {ks.LOCALMODEL_CACHE_CRD}": "crd/y",
            "localmodelnodegroup gpu-local": "localmodelnodegroup/gpu-local",
            "data.localModel": '{"enabled": true}',
        },
    )
    s = kserve_spec(options={"platform": KServePlatform(install=False)})
    KServeDriver().check_prerequisites(s)


def test_the_platform_cache_must_name_its_nodes():
    with pytest.raises(ValueError, match="nodes"):
        KServeModelCache(enabled=True)


def test_a_download_error_stops_the_wait_with_where_to_look(monkeypatch):
    stub_kserve(
        monkeypatch,
        replies={
            "get localmodelcache": '{"status": {"nodeStatus": {"rke2-wrk-2": "NodeDownloadError"}}}',
        },
    )
    monkeypatch.setattr(ks, "kube_apply", lambda *a, **k: "")
    with pytest.raises(ks.InferenceError, match="logs"):
        KServeDriver()._apply_model_cache_first(kserve_spec())


def test_the_wait_returns_once_every_node_has_the_model(monkeypatch):
    calls = []
    stub_kserve(
        monkeypatch,
        record=calls,
        replies={
            "get localmodelcache": '{"status": {"nodeStatus": {"a": "NodeDownloaded", "b": "NodeDownloaded"}}}',
        },
    )
    monkeypatch.setattr(ks, "kube_apply", lambda *a, **k: "")
    KServeDriver()._apply_model_cache_first(kserve_spec())
    assert any(c.startswith("get localmodelcache model-") for c in calls)


def test_a_namespace_cache_is_waited_on_in_its_namespace(monkeypatch):
    calls = []
    stub_kserve(
        monkeypatch,
        record=calls,
        replies={
            "get localmodelnamespacecache": '{"status": {"nodeStatus": {"a": "NodeDownloaded"}}}',
        },
    )
    monkeypatch.setattr(ks, "kube_apply", lambda *a, **k: "")
    KServeDriver()._apply_model_cache_first(kserve_spec(cache_scope="namespace"))
    assert any(
        c.startswith("get localmodelnamespacecache model-") and "-n inference" in c for c in calls
    )


def test_without_node_mode_nothing_is_applied_early(monkeypatch):
    applied = []
    monkeypatch.setattr(ks, "kube_apply", lambda *a, **k: applied.append(a))
    KServeDriver()._apply_model_cache_first(spec(type="kserve", name="qwen", **S3))
    assert applied == []


def test_the_download_credentials_are_applied_before_the_cache(monkeypatch):
    # KServe resolves the account when it creates the download Job and does
    # not revisit it: applied after the cache, the download has none.
    stub_kserve(
        monkeypatch,
        replies={
            "get localmodelnamespacecache": '{"status": {"nodeStatus": {"a": "NodeDownloaded"}}}',
        },
    )
    applied = []
    monkeypatch.setattr(ks, "kube_apply", lambda kc, objs, **k: applied.extend(objs))
    KServeDriver()._apply_model_cache_first(kserve_spec(cache_scope="namespace"))
    kinds = [o["kind"] for o in applied]
    cache = kinds.index("LocalModelNamespaceCache")
    assert kinds.index("Secret") < cache and kinds.index("ServiceAccount") < cache
    for kind in ("Secret", "ServiceAccount"):
        obj = next(o for o in applied if o["kind"] == kind)
        assert obj["metadata"]["namespace"] == "kserve-localmodel-jobs"


def test_removing_the_cache_removes_its_download_credentials(monkeypatch):
    calls = []
    stub_kserve(monkeypatch, record=calls)
    account = KServeDriver().cache_service_account(kserve_spec(cache_scope="namespace"))
    KServeDriver().delete(kserve_spec(cache_scope="namespace"))
    assert not any(account in c for c in calls), "kept unless the cache is removed"
    KServeDriver().delete(kserve_spec(cache_scope="namespace"), remove_cache=True)
    assert (
        f"delete serviceaccount,secret {account} -n kserve-localmodel-jobs --ignore-not-found"
    ) in calls


RELEASED_PV = json.dumps(
    {
        "spec": {"claimRef": {"namespace": "inference", "name": "{cache}-gpu-local", "uid": "old"}},
        "status": {"phase": "Released"},
    }
)


def test_a_released_cache_volume_is_freed_for_the_redeploy(monkeypatch):
    # KServe keeps the volume (Retain) but remembers the deleted claim, so a
    # redeploy's new claim of the same name never binds.
    driver = KServeDriver()
    cache = driver.cache_name(kserve_spec())
    calls = []
    stub_kserve(
        monkeypatch,
        record=calls,
        replies={
            "get localmodelcache": '{"status": {"nodeStatus": {"a": "NodeDownloaded"}}}',
            f"get pv {cache}-gpu-local-inference": RELEASED_PV.replace("{cache}", cache),
        },
    )
    monkeypatch.setattr(ks, "kube_apply", lambda *a, **k: "")
    driver._apply_model_cache_first(kserve_spec())
    patch = [c for c in calls if c.startswith("patch pv")]
    assert patch == [
        f"patch pv {cache}-gpu-local-inference --type json "
        '-p [{"op": "remove", "path": "/spec/claimRef"}]'
    ]


def test_a_bound_or_foreign_volume_is_left_alone(monkeypatch):
    driver = KServeDriver()
    cache = driver.cache_name(kserve_spec())
    for pv in (
        RELEASED_PV.replace("{cache}", cache).replace("Released", "Bound"),
        RELEASED_PV.replace("{cache}", "someone-else"),
    ):
        calls = []
        stub_kserve(
            monkeypatch,
            record=calls,
            replies={
                "get localmodelcache": '{"status": {"nodeStatus": {"a": "NodeDownloaded"}}}',
                f"get pv {cache}-gpu-local-inference": pv,
            },
        )
        monkeypatch.setattr(ks, "kube_apply", lambda *a, **k: "")
        driver._apply_model_cache_first(kserve_spec())
        assert not any(c.startswith("patch pv") for c in calls)


# -- the default scope, and one store's URI per node copy -------------------
def test_a_kserve_node_cache_is_namespace_scoped_by_default():
    s = spec(type="kserve", name="qwen", model_cache="node", cache_size_gb=2, **S3)
    assert s.cache_scope is None and s.resolved_cache_scope == "namespace"
    kinds = [o["kind"] for o in KServeDriver().model_cache_manifests(s)]
    assert "LocalModelNamespaceCache" in kinds and "LocalModelCache" not in kinds


def test_default_namespace_caches_do_not_share_a_download_account():
    # The shared cluster account is what let one namespace overwrite
    # another's credentials and delete its cache -- reproduced live.
    def account(ns):
        return KServeDriver().cache_service_account(
            spec(
                type="kserve", name="qwen", namespace=ns, model_cache="node", cache_size_gb=2, **S3
            )
        )

    assert account("org-a") != account("org-b")


def test_vllm_and_llamacpp_specs_need_no_cache_scope():
    # The old default ("cluster") doubled as "unset"; None is the unset now.
    spec(type="vllm", name="m", model_cache="node", **S3)
    with pytest.raises(ValueError, match="cache_scope"):
        spec(type="vllm", name="m", model_cache="node", cache_scope="cluster", **S3)


def test_the_cache_records_its_endpoint():
    cache = KServeDriver().model_cache_manifests(kserve_spec())[-1]
    assert cache["metadata"]["annotations"][ks.CACHE_ENDPOINT_ANNOTATION] == S3["s3_endpoint_url"]


def _existing_cache(endpoint):
    annotations = {} if endpoint is None else {ks.CACHE_ENDPOINT_ANNOTATION: endpoint}
    return json.dumps(
        {
            "items": [
                {
                    "kind": "LocalModelCache",
                    "metadata": {"name": "model-x", "annotations": annotations},
                    "spec": {"sourceModelUri": S3["model"]},
                }
            ]
        }
    )


def test_the_same_uri_from_another_store_is_refused(monkeypatch):
    stub_kserve(
        monkeypatch,
        replies={
            "localmodelcaches,localmodelnamespacecaches": _existing_cache(
                "http://minio.other:9000"
            ),
        },
    )
    with pytest.raises(ks.InferencePrerequisiteError, match="other store's files"):
        KServeDriver()._check_cache_source(kserve_spec())


@pytest.mark.parametrize("endpoint", [S3["s3_endpoint_url"], None])
def test_the_same_store_or_an_unrecorded_one_is_allowed(monkeypatch, endpoint):
    stub_kserve(
        monkeypatch,
        replies={
            "localmodelcaches,localmodelnamespacecaches": _existing_cache(endpoint),
        },
    )
    KServeDriver()._check_cache_source(kserve_spec())
