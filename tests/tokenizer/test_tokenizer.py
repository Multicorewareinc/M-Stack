"""The tokenizer capability: spec, wiring, and the tiktoken driver."""
import pytest

from multistack.stack import CAPABILITY_OUTPUT, Stack
from multistack.tokenizer import Tokenizer, TokenizerPrerequisiteError
from multistack.tokenizer.drivers import tiktoken_service as tk
from multistack.tokenizer.spec import TiktokenOptions

KUBECONFIG = "/tmp/kc.yaml"


def spec(**kwargs) -> Tokenizer:
    return Tokenizer(kubeconfig_path=KUBECONFIG, **kwargs)


class _FakeRunner:
    def __init__(self, calls):
        self.calls = calls

    def install_or_upgrade(self, release, **kwargs):
        self.calls.append(("install", release, kwargs))

    def uninstall(self, release, **kwargs):
        self.calls.append(("uninstall", release, kwargs))


@pytest.fixture
def driver():
    return tk.TiktokenDriver()


@pytest.fixture
def calls(driver, monkeypatch):
    recorded = []
    monkeypatch.setattr(driver, "_helm", lambda t: _FakeRunner(recorded))
    monkeypatch.setattr(tk, "require_cli", lambda *a, **k: None)
    monkeypatch.setattr(tk, "require_cluster", lambda *a, **k: None)
    return recorded


def _install(calls):
    return next(c[2] for c in calls if c[0] == "install")


# -- spec -----------------------------------------------------------------


def test_kubeconfig_is_required():
    with pytest.raises(ValueError, match="kubeconfig_path is required"):
        Tokenizer(kubeconfig_path="")


def test_the_endpoint_is_credential_free_and_in_cluster():
    assert spec().endpoint == "http://tokenizer.policy.svc.cluster.local:8000"


def test_renaming_the_release_moves_the_endpoint():
    o = TiktokenOptions(release_name="tokenizer-v2")
    assert "tokenizer-v2." in spec(options=o).endpoint


def test_an_explicit_namespace_is_used():
    assert ".obs." in spec(namespace="obs").endpoint


def test_an_empty_default_encoding_is_rejected():
    # A blank default means every request with no encoding named is
    # counted by whatever the image falls back to.
    with pytest.raises(ValueError, match="must not be empty"):
        TiktokenOptions(default_encoding="")


def test_an_unsupported_type_is_rejected():
    with pytest.raises(ValueError):
        spec(type="sentencepiece").validate()


# -- wiring ---------------------------------------------------------------


def test_it_requires_only_a_cluster():
    assert Tokenizer.REQUIRES == ("cluster",)


def test_it_publishes_the_key_tpm_reads():
    assert Tokenizer.PROVIDES == {"tokenizer_url": "endpoint"}
    assert "tokenizer_url" in CAPABILITY_OUTPUT.values()


def test_recording_it_publishes_the_url():
    stack = Stack(KUBECONFIG)
    stack.record(spec())
    assert stack._values["tokenizer_url"] == (
        "http://tokenizer.policy.svc.cluster.local:8000"
    )


# -- driver ---------------------------------------------------------------


def test_create_installs_the_chart_and_returns_the_endpoint(driver, calls):
    assert driver.create(spec()) == spec().endpoint
    _, release, kwargs = next(c for c in calls if c[0] == "install")
    assert release == "tokenizer"
    assert kwargs["chart"] == "api/microservices/tokenizer/chart"
    assert kwargs["namespace"] == "policy"


def test_create_passes_the_default_encoding(driver, calls):
    driver.create(spec(options=TiktokenOptions(default_encoding="o200k_base")))
    assert _install(calls)["values"]["config"]["tokenizerDefault"] == "o200k_base"


def test_create_passes_replicas_and_port(driver, calls):
    driver.create(spec(replicas=3, service_port=9000))
    values = _install(calls)["values"]
    assert values["replicaCount"] == 3
    assert values["service"]["port"] == 9000


def test_an_image_tag_is_only_sent_when_set(driver, calls):
    driver.create(spec())
    assert "image" not in _install(calls)["values"]


def test_a_single_replica_warns(driver, calls):
    """The policy layer calls this on the request path, so one pod
    restarting takes token counting down with it."""
    warnings = driver.check_prerequisites(spec(replicas=1))
    assert any("replicas=1" in w for w in warnings)


def test_two_replicas_produce_no_warning(driver, calls):
    assert driver.check_prerequisites(spec(replicas=2)) == []


def test_delete_uninstalls_and_tolerates_absence(driver, calls):
    driver.delete(spec())
    _, release, kwargs = next(c for c in calls if c[0] == "uninstall")
    assert release == "tokenizer"
    assert kwargs["missing_ok"] is True


def test_a_missing_cluster_stops_create(driver, monkeypatch):
    monkeypatch.setattr(tk, "require_cli", lambda *a, **k: None)
    monkeypatch.setattr(
        tk, "require_cluster",
        lambda *a, **k: (_ for _ in ()).throw(TokenizerPrerequisiteError("no")),
    )
    monkeypatch.setattr(
        driver, "_helm", lambda t: pytest.fail("helm must not run")
    )
    with pytest.raises(TokenizerPrerequisiteError):
        driver.create(spec())


# -- scheduling -----------------------------------------------------------
#
# There is no in-cluster registry, so a service image exists only on the
# node it was imported to. A pod scheduled anywhere else stays
# ImagePullBackOff, and nothing in the release says why.


def test_a_node_selector_reaches_the_chart(driver, calls):
    driver.create(spec(node_selector={"kubernetes.io/hostname": "rke2-wrk-2"}))
    assert _install(calls)["values"]["nodeSelector"] == {
        "kubernetes.io/hostname": "rke2-wrk-2"
    }


def test_no_node_selector_means_the_key_is_absent(driver, calls):
    # Absent leaves the chart's own default in place; an explicit {} would
    # override it with "schedule anywhere", a different statement.
    driver.create(spec())
    assert "nodeSelector" not in _install(calls)["values"]


def test_an_empty_node_selector_is_sent_as_a_deliberate_override(driver, calls):
    driver.create(spec(node_selector={}))
    assert _install(calls)["values"]["nodeSelector"] == {}


def test_tolerations_reach_the_chart(driver, calls):
    tol = [{"key": "dedicated", "operator": "Equal", "value": "platform",
            "effect": "NoSchedule"}]
    driver.create(spec(tolerations=tol))
    assert _install(calls)["values"]["tolerations"] == tol
