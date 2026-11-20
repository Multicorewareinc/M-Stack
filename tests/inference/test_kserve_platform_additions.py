"""What the KServe installer adds alongside KServe: LLMInferenceService and
the local model cache.

Both are additions rather than upgrades, so the rule the rest of the
installer holds -- never upgrade a KServe release it found -- is asserted
here for them too. Chart names and values are KServe v0.20.0's own.
"""

import json

import pytest

from multistack.inference import InferencePrerequisiteError, KServeModelCache, KServePlatform
from multistack.inference.drivers import kserve_platform as kp


class FakeHelm:
    def __init__(self):
        self.calls = []

    def install_or_upgrade(self, release, **kw):
        self.calls.append((release, kw))
        return release

    def releases(self):
        return [c[0] for c in self.calls]

    def values(self, release):
        return next(kw for name, kw in self.calls if name == release)["values"]


LOCAL_MODEL_ON = {"enabled": True, "jobNamespace": "kserve-localmodel-jobs"}
MISSING = object()


def installer(monkeypatch, present=(), local_model=LOCAL_MODEL_ON):
    """An installer over a fake cluster. `present` lists the CRDs and named
    objects that exist; installing a chart makes its CRD appear.
    `local_model` is inferenceservice-config's localModel block, or MISSING
    for a ConfigMap without one."""
    helm = FakeHelm()
    inst = kp.KServePlatformInstaller("/tmp/kc.yaml", helm=helm)
    cluster = set(present)
    inst.kubectl_calls, inst.applied, inst.jobs = [], [], []

    def fake_kubectl(self, *args, check=True):
        joined = " ".join(args)
        inst.kubectl_calls.append(joined)
        # Status queries first: they are `get`s too, and answering one as an
        # absent object leaves the installer's wait loop spinning forever.
        if "availableReplicas" in joined:
            return "1"
        if "data.localModel" in joined:
            return "" if local_model is MISSING else json.dumps(local_model)
        if joined.startswith("get clusterservingruntimes"):
            return "clusterservingruntime/x" if "clusterservingruntimes" in cluster else ""
        if joined.startswith("get "):
            name = args[2]
            return f"x/{name}" if name in cluster else ""
        return ""

    original_install = helm.install_or_upgrade

    def install_and_register(release, **kw):
        original_install(release, **kw)
        created = {
            kp.KSERVE_LLMISVC_CRD_RELEASE: kp.LLMISVC_CRD,
            kp.KSERVE_LOCALMODEL_CRD_RELEASE: kp.LOCALMODEL_CACHE_CRD,
            kp.KSERVE_CRD_RELEASE: kp.ISVC_CRD,
        }.get(release)
        if created:
            cluster.add(created)
        return release

    helm.install_or_upgrade = install_and_register
    monkeypatch.setattr(kp.KServePlatformInstaller, "_kubectl", fake_kubectl)
    monkeypatch.setattr(kp.time, "sleep", lambda _: None)
    monkeypatch.setattr(kp, "kube_apply", lambda kc, objects, **kw: inst.applied.extend(objects))
    monkeypatch.setattr(kp, "wait_for_job", lambda kc, name, ns, **kw: inst.jobs.append((name, ns)))
    return inst, helm


def cache_platform(**kwargs):
    cache = KServeModelCache(enabled=True, nodes=["gpu-1", "gpu-2"], **kwargs)
    return KServePlatform(model_cache=cache)


# -- LLMInferenceService ------------------------------------------------------
def test_llm_installs_gateway_api_then_its_charts_then_the_presets(monkeypatch):
    inst, helm = installer(monkeypatch)
    inst.ensure(KServePlatform(), "Standard", llm=True)

    releases = helm.releases()
    assert releases.index(kp.KSERVE_RELEASE) < releases.index(kp.KSERVE_LLMISVC_CRD_RELEASE)
    assert (
        releases.index(kp.KSERVE_LLMISVC_CRD_RELEASE)
        < releases.index(kp.KSERVE_LLMISVC_RELEASE)
        < releases.index(kp.KSERVE_LLM_CONFIGS_RELEASE)
    )
    assert any(
        c.startswith(
            "apply --server-side -f https://github.com/"
            "kubernetes-sigs/gateway-api/releases/download/v1.2.1/"
        )
        for c in inst.kubectl_calls
    )


def test_the_llm_controller_does_not_recreate_shared_resources(monkeypatch):
    # kserve-resources already owns them; a second owner fails Helm's
    # ownership check.
    inst, helm = installer(monkeypatch)
    inst.ensure(KServePlatform(), "Standard", llm=True)
    assert helm.values(kp.KSERVE_LLMISVC_RELEASE) == {"kserve": {"createSharedResources": False}}


def test_the_presets_are_a_release_of_their_own_with_runtimes_off(monkeypatch):
    # Never upgrades the runtimes' release, which could remove runtimes.
    inst, helm = installer(monkeypatch)
    inst.ensure(KServePlatform(), "Standard", llm=True)
    assert helm.values(kp.KSERVE_LLM_CONFIGS_RELEASE) == {
        "kserve": {
            "servingruntime": {"enabled": False},
            "llmisvcConfigs": {"enabled": True},
        }
    }


def test_llm_is_added_to_a_kserve_that_was_found(monkeypatch):
    inst, helm = installer(monkeypatch, present={kp.ISVC_CRD})
    inst.ensure(KServePlatform(), "Standard", llm=True)
    assert kp.KSERVE_RELEASE not in helm.releases(), "found KServe must not be upgraded"
    assert kp.KSERVE_LLMISVC_RELEASE in helm.releases()


def test_nothing_is_reinstalled_when_llm_is_already_there(monkeypatch):
    inst, helm = installer(
        monkeypatch,
        present={
            kp.ISVC_CRD,
            kp.LLMISVC_CRD,
            kp.GATEWAY_API_CRD,
            kp.LLM_TEMPLATE_CONFIG,
            "clusterservingruntimes",
        },
    )
    inst.ensure(KServePlatform(), "Standard", llm=True)
    assert helm.calls == []
    assert not any(c.startswith("apply") for c in inst.kubectl_calls)


def test_gateway_api_can_be_left_to_the_cluster(monkeypatch):
    inst, _ = installer(monkeypatch, present={kp.ISVC_CRD})
    inst.ensure(KServePlatform(gateway_api_version=None), "Standard", llm=True)
    assert not any("gateway-api" in c for c in inst.kubectl_calls)


def test_without_llm_none_of_it_is_installed(monkeypatch):
    inst, helm = installer(monkeypatch)
    inst.ensure(KServePlatform(), "Standard")
    assert kp.KSERVE_LLMISVC_RELEASE not in helm.releases()
    assert kp.KSERVE_LLM_CONFIGS_RELEASE not in helm.releases()


# -- the model cache ----------------------------------------------------------
def test_a_fresh_install_switches_the_cache_on_in_kserves_own_values(monkeypatch):
    inst, helm = installer(monkeypatch)
    inst.ensure(cache_platform(), "Standard", model_cache=True)
    # Only the switch: in v0.20.0 kserve-resources reads nothing else here,
    # and `agent.hostPath` in particular is read by no template at all.
    assert helm.values(kp.KSERVE_RELEASE)["kserve"]["localmodel"] == {
        "enabled": True,
        "jobNamespace": "kserve-localmodel-jobs",
    }


def test_the_cache_controller_and_agent_are_their_own_chart(monkeypatch):
    # Found live: enabling the cache in kserve-resources deploys neither.
    inst, helm = installer(monkeypatch, present={kp.ISVC_CRD, kp.LOCALMODEL_CACHE_CRD})
    inst.ensure(cache_platform(), "Standard", model_cache=True)
    assert kp.KSERVE_LOCALMODEL_RELEASE in helm.releases()
    assert helm.values(kp.KSERVE_LOCALMODEL_RELEASE) == {"kserve": {"createSharedResources": False}}


def test_the_cache_chart_is_skipped_when_its_controller_runs(monkeypatch):
    inst, helm = installer(
        monkeypatch, present={kp.ISVC_CRD, kp.LOCALMODEL_CACHE_CRD, kp.LOCALMODEL_CONTROLLER}
    )
    inst.ensure(cache_platform(), "Standard", model_cache=True)
    assert kp.KSERVE_LOCALMODEL_RELEASE not in helm.releases()


def test_a_cache_path_the_agent_never_reads_is_refused(monkeypatch):
    # KServe v0.20.0's agent chart hardcodes /models; a node group anywhere
    # else is a cache the agent re-downloads forever.
    inst, _ = installer(monkeypatch, present={kp.ISVC_CRD, kp.LOCALMODEL_CACHE_CRD})
    with pytest.raises(InferencePrerequisiteError, match="/models"):
        inst.ensure(cache_platform(path="/mnt/nvme/models"), "Standard", model_cache=True)


def test_the_cache_values_are_absent_unless_asked_for(monkeypatch):
    inst, helm = installer(monkeypatch)
    inst.ensure(cache_platform(), "Standard")
    assert "localmodel" not in helm.values(kp.KSERVE_RELEASE)["kserve"]


def test_asking_for_a_cache_the_platform_has_not_enabled_is_refused(monkeypatch):
    inst, _ = installer(monkeypatch, present={kp.ISVC_CRD})
    with pytest.raises(InferencePrerequisiteError, match=r"model_cache\.enabled"):
        inst.ensure(KServePlatform(), "Standard", model_cache=True)


def test_the_cache_nodes_are_labelled_for_the_agent_and_the_group(monkeypatch):
    inst, _ = installer(monkeypatch, present={kp.ISVC_CRD, kp.LOCALMODEL_CACHE_CRD})
    inst.ensure(cache_platform(node_group="nvme"), "Standard", model_cache=True)
    labels = [c for c in inst.kubectl_calls if c.startswith("label node")]
    assert labels == [
        f"label node {n} kserve/localmodel=worker {kp.MODEL_CACHE_GROUP_LABEL}=nvme --overwrite"
        for n in ("gpu-1", "gpu-2")
    ]


def test_each_cache_folder_is_prepared_on_its_own_node(monkeypatch):
    inst, _ = installer(monkeypatch, present={kp.ISVC_CRD, kp.LOCALMODEL_CACHE_CRD})
    inst.ensure(cache_platform(), "Standard", model_cache=True)
    jobs = [o for o in inst.applied if o["kind"] == "Job"]
    assert [j["spec"]["template"]["spec"]["nodeName"] for j in jobs] == ["gpu-1", "gpu-2"]
    pod = jobs[0]["spec"]["template"]["spec"]
    assert pod["volumes"][0]["hostPath"]["path"] == "/models"
    command = pod["containers"][0]["command"][2]
    assert "chown -R 1000:1000 /cache" in command
    # setgid on models/: a model folder the kubelet creates as root (a pod
    # mounting it before the agent made it) still lands in group 1000, which
    # KServe's download Job can write. Found live as "Permission denied".
    assert "mkdir -p /cache/models" in command
    assert "chmod 2775 /cache /cache/models" in command
    # Waited for before the node group exists to use the folders.
    assert [name for name, _ in inst.jobs] == [j["metadata"]["name"] for j in jobs]


def test_the_node_groups_volume_is_pinned_to_the_groups_machines(monkeypatch):
    inst, _ = installer(monkeypatch, present={kp.ISVC_CRD, kp.LOCALMODEL_CACHE_CRD})
    inst.ensure(cache_platform(size_gb=500), "Standard", model_cache=True)
    group = next(o for o in inst.applied if o["kind"] == "LocalModelNodeGroup")
    assert group["metadata"]["name"] == "gpu-local"
    assert group["spec"]["storageLimit"] == "500Gi"
    volume = group["spec"]["persistentVolumeSpec"]
    assert volume["local"] == {"path": "/models"}
    term = volume["nodeAffinity"]["required"]["nodeSelectorTerms"][0]["matchExpressions"][0]
    assert term == {"key": kp.MODEL_CACHE_GROUP_LABEL, "operator": "In", "values": ["gpu-local"]}
    storage_class = next(o for o in inst.applied if o["kind"] == "StorageClass")
    assert storage_class["volumeBindingMode"] == "WaitForFirstConsumer"


def test_the_cache_crds_are_installed_only_when_missing(monkeypatch):
    inst, helm = installer(monkeypatch, present={kp.ISVC_CRD})
    inst.ensure(cache_platform(), "Standard", model_cache=True)
    assert kp.KSERVE_LOCALMODEL_CRD_RELEASE in helm.releases()

    inst, helm = installer(monkeypatch, present={kp.ISVC_CRD, kp.LOCALMODEL_CACHE_CRD})
    inst.ensure(cache_platform(), "Standard", model_cache=True)
    assert kp.KSERVE_LOCALMODEL_CRD_RELEASE not in helm.releases()


def test_a_found_kserve_with_the_cache_off_has_it_switched_on(monkeypatch):
    """Off, the cache fills and the model still downloads on every start:
    nothing links the pod to it. Seen on a live KServe 0.20 cluster."""
    inst, helm = installer(
        monkeypatch,
        present={
            kp.ISVC_CRD,
            kp.LOCALMODEL_CACHE_CRD,
            "kserve-controller-manager",
            kp.LLMISVC_CONTROLLER,
        },
        local_model={"enabled": False, "jobNamespace": "kserve-localmodel-jobs", "fsGroup": 1000},
    )
    notes = inst.ensure(cache_platform(), "Standard", model_cache=True)

    assert kp.KSERVE_RELEASE not in helm.releases(), "a found KServe is patched, not upgraded"
    patch = next(
        c for c in inst.kubectl_calls if c.startswith(f"patch configmap {kp.ISVC_CONFIGMAP}")
    )
    written = json.loads(json.loads(patch.split(" -p ", 1)[1])["data"]["localModel"])
    assert written == {
        "enabled": True,
        "jobNamespace": "kserve-localmodel-jobs",
        "fsGroup": 1000,
    }, "only the switch changes"
    restarted = [c for c in inst.kubectl_calls if c.startswith("rollout restart")]
    assert any("kserve-controller-manager" in c for c in restarted)
    assert any(kp.LLMISVC_CONTROLLER in c for c in restarted)
    assert any("switched on" in n for n in notes)


def test_an_unreadable_local_model_setting_is_refused(monkeypatch):
    inst, _ = installer(
        monkeypatch, present={kp.ISVC_CRD, kp.LOCALMODEL_CACHE_CRD}, local_model=MISSING
    )
    with pytest.raises(InferencePrerequisiteError, match="localModel"):
        inst.ensure(cache_platform(), "Standard", model_cache=True)


def test_a_found_kserve_with_the_cache_on_says_nothing_about_it(monkeypatch):
    inst, _ = installer(
        monkeypatch, present={kp.ISVC_CRD, kp.LOCALMODEL_CACHE_CRD}, local_model={"enabled": True}
    )
    notes = inst.ensure(cache_platform(), "Standard", model_cache=True)
    assert not any("switched on" in n for n in notes)
    assert not any(c.startswith("patch configmap") for c in inst.kubectl_calls)


def test_install_false_adds_nothing(monkeypatch):
    inst, helm = installer(monkeypatch, present={kp.ISVC_CRD})
    inst.ensure(KServePlatform(install=False), "Standard", llm=True)
    assert helm.calls == []


def test_an_earlier_prep_job_is_replaced_not_reapplied(monkeypatch):
    # A Job's template is immutable: re-applying a changed one fails.
    inst, _ = installer(monkeypatch, present={kp.ISVC_CRD, kp.LOCALMODEL_CACHE_CRD})
    inst.ensure(cache_platform(), "Standard", model_cache=True)
    deletes = [c for c in inst.kubectl_calls if c.startswith("delete job model-cache-prep-")]
    assert deletes == [
        f"delete job model-cache-prep-{n} -n kserve-localmodel-jobs --ignore-not-found --wait=true"
        for n in ("gpu-1", "gpu-2")
    ]
