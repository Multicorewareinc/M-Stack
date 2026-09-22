"""The first-party Helm charts, checked by rendering them.

A chart has no unit-testable interior; what it has is output. So these
render the real charts with `helm template` and assert on the manifests,
which is the only way to catch the mistakes that matter here -- a
credential in a ConfigMap, a Service pointing at a port nothing listens
on, a limits document the service cannot parse.

Skipped when helm is not on PATH, the same way the pyhelm3 tests skip
without the extra.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

yaml = pytest.importorskip("yaml")

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None,
    reason="helm is not on PATH",
)

ROOT = Path(__file__).resolve().parents[1]
GATEWAY = ROOT / "api/microservices/model-gateway/chart"
LIMITER = ROOT / "api/microservices/rate-limiter-rpm/chart"
TPM_LIMITER = ROOT / "api/microservices/rate-limiter-tpm/chart"
TOKENIZER = ROOT / "api/microservices/tokenizer/chart"
ADMIN_CP = ROOT / "api/microservices/admin-control-plane/chart"
ORG_CP = ROOT / "api/microservices/organization-control-plane/chart"

# Not an api/microservices chart, and deliberately not in CHARTS below:
# a static bundle behind nginx has no env, no Secret and no envFrom, so
# the shared properties that suite asserts would have to be faked here
# to make it fit. Its own section is at the bottom.
PORTAL = ROOT / "ui/chart"
PORTAL_MIN = ["--set", "config.apiUpstream=http://org-cp.test.svc.cluster.local:8000"]

# The minimum each chart needs to render at all. Each refuses to install
# without a deliberate choice, which is itself asserted below.
GATEWAY_MIN = ["--set", "secret.existingSecret=gw-keys",
               "--set", "config.orgCpInternalUrl=http://org-cp:8000"]
LIMITER_MIN = ["--set", "config.events.backboneUrl=nats://nats:4222"]
TOKENIZER_MIN = ["--set", "config.tokenizerDefault=gpt2"]

# The two control planes need two separate things, and which of them is
# missing changes what should happen -- so they are kept apart here
# rather than being sliced back out of one list.
SECRET_SOURCE = ["--set", "secret.existingSecret=cp-secret"]
ADMIN_CP_CONFIG = ["--set", "config.orgCpInternalUrl=http://org-cp:8000"]
ORG_CP_CONFIG = ["--set", "config.adminCpInternalUrl=http://admin-cp:8000"]

ADMIN_CP_MIN = SECRET_SOURCE + ADMIN_CP_CONFIG
ORG_CP_MIN = SECRET_SOURCE + ORG_CP_CONFIG

# Everything secret.create=true requires. Each is `required` in
# templates/secret.yaml, so a new one breaks this list rather than
# silently rendering an empty value.
CREATED_SECRET = [
    "--set", "secret.create=true",
    "--set", "secret.databaseUrl=postgresql://u:p@db:5432/x",
    "--set", "secret.adminApiKey=a",
    "--set", "secret.serviceApiKey=s",
    "--set", "secret.valkeyUrl=redis://:p@valkey-primary.cache.svc:6379/1",
    "--set", "secret.jwtSecret=j",
    # organization-control-plane only; admin-control-plane's chart has no
    # such value and silently ignores it.
    "--set", "secret.mgServiceApiKey=m",
]

def without(setting: str) -> List[str]:
    """CREATED_SECRET minus one `--set key=value` pair."""
    kept: List[str] = []
    for flag, assignment in zip(CREATED_SECRET[::2], CREATED_SECRET[1::2]):
        if not assignment.startswith(f"{setting}="):
            kept += [flag, assignment]
    return kept


# The two that run a pre-install migration Job, which mounts the same
# ConfigMap and Secret the Deployment does.
CONTROL_PLANES = [
    pytest.param(ADMIN_CP, ADMIN_CP_CONFIG, id="admin-control-plane"),
    pytest.param(ORG_CP, ORG_CP_CONFIG, id="organization-control-plane"),
]


def render(chart: Path, *extra: str) -> List[Dict[str, Any]]:
    result = subprocess.run(
        ["helm", "template", "test", str(chart), *extra],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return [d for d in yaml.safe_load_all(result.stdout) if d]


def render_error(chart: Path, *extra: str) -> str:
    result = subprocess.run(
        ["helm", "template", "test", str(chart), *extra],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode != 0, "expected the render to be refused"
    return result.stderr


def one(docs: List[Dict[str, Any]], kind: str) -> Dict[str, Any]:
    found = [d for d in docs if d.get("kind") == kind]
    assert len(found) == 1, f"expected exactly one {kind}, got {len(found)}"
    return found[0]


def container(docs: List[Dict[str, Any]]) -> Dict[str, Any]:
    return one(docs, "Deployment")["spec"]["template"]["spec"]["containers"][0]


CHARTS = [
    pytest.param(GATEWAY, GATEWAY_MIN, 8080, id="model-gateway"),
    pytest.param(LIMITER, LIMITER_MIN, 8000, id="rate-limiter-rpm"),
    pytest.param(TPM_LIMITER, LIMITER_MIN, 8000, id="rate-limiter-tpm"),
    pytest.param(TOKENIZER, TOKENIZER_MIN, 8000, id="tokenizer"),
    pytest.param(ADMIN_CP, ADMIN_CP_MIN, 8000, id="admin-control-plane"),
    pytest.param(ORG_CP, ORG_CP_MIN, 8000, id="organization-control-plane"),
]

# Both limiter charts, for the properties that are identical by design.
LIMITERS = [
    pytest.param(LIMITER, "rpm", id="rate-limiter-rpm"),
    pytest.param(TPM_LIMITER, "tpm", id="rate-limiter-tpm"),
]


# -- both charts ----------------------------------------------------------
@pytest.mark.parametrize("chart,minimum,port", CHARTS)
def test_chart_lints(chart: Path, minimum: List[str], port: int):
    result = subprocess.run(
        ["helm", "lint", str(chart), *minimum],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("chart,minimum,port", CHARTS)
def test_no_credential_key_lands_in_a_configmap(chart: Path, minimum, port):
    """A ConfigMap is readable by anyone with get on the namespace.

    API_KEY and UPSTREAM_API_KEY are credentials outright. MODEL_ROUTES
    can carry a provider's api_key. RATE_LIMITS is keyed by principal,
    and the gateway sets the principal to the caller's bearer token
    (model-gateway/src/dependencies.py) -- so a limits document with a
    user override contains that user's API key.
    """
    config = one(render(chart, *minimum), "ConfigMap")["data"]
    for key in ("API_KEY", "UPSTREAM_API_KEY", "RATE_LIMITS"):
        assert key not in config, f"{key} must live in a Secret, not a ConfigMap"


@pytest.mark.parametrize("chart,minimum,port", CHARTS)
def test_the_service_targets_a_named_port(chart: Path, minimum, port):
    """By name, so a changed containerPort cannot leave the Service
    pointing at a port nothing listens on -- a mismatch that presents as
    an Endpoints list that is populated but refuses connections."""
    docs = render(chart, *minimum)
    service_port = one(docs, "Service")["spec"]["ports"][0]
    assert service_port["targetPort"] == "http"

    ports = {p["name"]: p["containerPort"] for p in container(docs)["ports"]}
    assert ports["http"] == port


@pytest.mark.parametrize("chart,minimum,port", CHARTS)
def test_probes_hit_health_on_that_port(chart: Path, minimum, port):
    """Liveness is always /health, and always unconditional: restarting a
    pod cannot clear a durable-bind conflict, so making liveness depend on
    counting state would produce a crash loop instead of a diagnosis
    (ADR-016).

    Readiness differs by service. rate-limiter-tpm has a /ready that
    returns 503 when a backbone is configured and its consumer is not
    counting, so its chart probes that — a pod answering /check while
    counting nothing leaves the Service instead of under-enforcing
    silently. The other two have no such endpoint yet, so /health is the
    honest choice there rather than a probe that cannot fail (API-2 in
    bugs.md).
    """
    spec = container(render(chart, *minimum))
    assert spec["livenessProbe"]["httpGet"] == {"path": "/health", "port": "http"}

    readiness = "/ready" if chart.parent.name == "rate-limiter-tpm" else "/health"
    assert spec["readinessProbe"]["httpGet"] == {"path": readiness, "port": "http"}


@pytest.mark.parametrize("chart,minimum,port", CHARTS)
def test_a_readiness_path_the_service_does_not_serve_would_never_be_ready(
    chart: Path, minimum, port,
):
    """The probe path has to exist in the image, or every pod stays
    NotReady forever with a chart that renders perfectly.

    Searches the whole source tree rather than a router.py: the two
    control planes declare /health on the app in main.py and split the
    rest into src/modules/, and looking in one file skipped them both.
    """
    spec = container(render(chart, *minimum))
    path = spec["readinessProbe"]["httpGet"]["path"]

    sources = sorted((chart.parent / "src").rglob("*.py"))
    if not sources:
        pytest.skip(f"{chart.parent.name} has no Python source to check")

    assert any(f'"{path}"' in f.read_text() for f in sources), (
        f"{chart.parent.name}'s chart probes {path}, which nothing under "
        f"{chart.parent.name}/src defines"
    )


@pytest.mark.parametrize("chart,minimum,port", CHARTS)
def test_pods_are_kept_off_the_control_plane(chart: Path, minimum, port):
    """Nothing else enforces this. rke2-cp01 carries no taint, so a chart
    with no affinity can put a workload next to etcd."""
    pod = one(render(chart, *minimum), "Deployment")["spec"]["template"]["spec"]
    terms = pod["affinity"]["nodeAffinity"][
        "requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]
    expressions = [e for t in terms for e in t["matchExpressions"]]
    assert {"key": "node-role.kubernetes.io/control-plane",
            "operator": "DoesNotExist"} in expressions


@pytest.mark.parametrize("chart,minimum,port", CHARTS)
def test_the_secret_is_applied_after_the_configmap(chart: Path, minimum, port):
    """Kubernetes gives the last envFrom source precedence for a
    duplicate key. That order is what lets a URL carrying a password, or
    routes carrying an api_key, override the non-secret default without
    a second env block.

    A chart with nothing to keep secret mounts no Secret at all, and an
    empty one added for symmetry would be worse than none -- so the
    order is asserted for the charts that have one.
    """
    sources = [list(s)[0] for s in container(render(chart, *minimum))["envFrom"]]

    if "secretRef" not in sources:
        # tokenizer: its whole .env.example is LOG_LEVEL and a tokenizer
        # name. No API key, no database, no upstream.
        assert sources == ["configMapRef"]
        return

    assert sources == ["configMapRef", "secretRef"]


@pytest.mark.parametrize("chart,minimum,port", CHARTS)
def test_changing_config_rolls_the_pods(chart: Path, minimum, port):
    """Without the checksum annotation, editing a value rewrites the
    ConfigMap and the running pods keep the old environment until
    something unrelated restarts them -- which reads as "the setting did
    not take"."""
    before = one(render(chart, *minimum), "Deployment")
    after = one(render(chart, *minimum, "--set", "config.logLevel=DEBUG"), "Deployment")
    annotations = before["spec"]["template"]["metadata"]["annotations"]
    assert "checksum/config" in annotations
    assert (annotations["checksum/config"]
            != after["spec"]["template"]["metadata"]["annotations"]["checksum/config"])


# -- model-gateway --------------------------------------------------------
def test_gateway_refuses_to_deploy_without_an_api_key_source():
    """The image defaults to the published key 'pf-local-dev-key', so a
    gateway deployed with no Secret authenticates on a credential that
    is in this repository."""
    # Everything else it needs except the Secret source, so the refusal
    # can only be about that -- otherwise Helm's first template error
    # wins, and that is orgCpInternalUrl's, not this one's.
    message = render_error(GATEWAY, "--set",
                           "config.orgCpInternalUrl=http://org-cp:8000")
    assert "secret.existingSecret" in message
    assert "secret.create=true" in message
    assert "pf-local-dev-key" in message


def test_gateway_creates_a_secret_only_when_asked():
    docs = render(GATEWAY, "--set", "secret.create=true",
                  "--set", "secret.apiKey=k",
                  "--set", "secret.orgVerifyApiKey=o",
                  "--set", "config.orgCpInternalUrl=http://org-cp:8000")
    assert one(docs, "Secret")["stringData"]["API_KEY"] == "k"

    # An existing Secret wins, and nothing is created alongside it.
    docs = render(GATEWAY, "--set", "secret.create=true",
                  "--set", "secret.apiKey=k",
                  "--set", "secret.orgVerifyApiKey=o",
                  "--set", "config.orgCpInternalUrl=http://org-cp:8000",
                  "--set", "secret.existingSecret=mine")
    assert not [d for d in docs if d["kind"] == "Secret"]
    assert container(docs)["envFrom"][1]["secretRef"]["name"] == "mine"


def test_gateway_policy_endpoints_join_into_one_variable():
    """The service reads POLICY_ENDPOINTS as a comma-separated string, so
    the list in values has to arrive as one."""
    docs = render(GATEWAY, *GATEWAY_MIN,
                  "--set", "config.policy.endpoints[0]=http://a:8000/check",
                  "--set", "config.policy.endpoints[1]=http://b:8000/check")
    config = one(docs, "ConfigMap")["data"]
    assert config["POLICY_ENDPOINTS"] == "http://a:8000/check,http://b:8000/check"

    # Empty by default: the chain is inert and the gateway is a plain proxy.
    assert one(render(GATEWAY, *GATEWAY_MIN), "ConfigMap")["data"]["POLICY_ENDPOINTS"] == ""


# -- rate-limiter-rpm -----------------------------------------------------
def test_limiter_refuses_to_deploy_with_nothing_counting():
    """Counting is asynchronous (ADR-013). With no backbone there is no
    consumer, the counters never advance, and /check allows everything --
    a limiter that is Ready and enforcing nothing."""
    message = render_error(LIMITER)
    assert "config.events.backboneUrl" in message
    assert "config.allowNoBackbone=true" in message


def test_limiter_can_be_deployed_deliberately_without_a_backbone():
    docs = render(LIMITER, "--set", "config.allowNoBackbone=true")
    assert one(docs, "ConfigMap")["data"]["EVENT_BACKBONE_URL"] == ""


def test_limiter_refuses_a_string_where_a_limits_map_belongs():
    """`--set secret.limits.userOverrides=vip-key=600` assigns a string,
    and it looks plausible. The service then calls dict() on it and
    raises on the first /check."""
    message = render_error(LIMITER, *LIMITER_MIN,
                           "--set", "secret.limits.userOverrides=vip=600")
    assert "must be a map" in message
    assert "secret.limits" in message


def test_limiter_refuses_a_non_numeric_default():
    message = render_error(LIMITER, *LIMITER_MIN,
                           "--set", "secret.limits.userDefault=lots")
    assert "must be a number" in message


def test_the_rendered_limits_are_what_the_service_parses():
    """The strongest check available without deploying: take the
    RATE_LIMITS the chart renders and hand it to the service's own
    Settings class. A document the service cannot read becomes {} inside
    it, so every scope silently goes unlimited -- there is no error to
    assert on at deploy time."""
    pytest.importorskip("pydantic_settings")

    docs = render(LIMITER, *LIMITER_MIN,
                  "--set", "secret.limits.userDefault=60",
                  "--set", "secret.limits.modelOverrides.limited-model=3",
                  "--set", "secret.limits.userModelOverrides.vip-key|gpt-4o=300")
    rendered = one(docs, "Secret")["stringData"]["RATE_LIMITS"]

    # Loaded under its own module name: the service's src/ is flat, and
    # "settings" is a name several services use.
    src = ROOT / "api/microservices/rate-limiter-rpm/src/settings.py"
    spec = importlib.util.spec_from_file_location("_rpm_settings", src)
    module = importlib.util.module_from_spec(spec)
    sys.modules["_rpm_settings"] = module
    spec.loader.exec_module(module)

    limits = module.Settings(rate_limits=rendered, _env_file=None).limits()
    assert limits.user_default == 60
    assert limits.model_overrides == {"limited-model": 3}
    assert limits.user_model_overrides == {"vip-key|gpt-4o": 300}
    # And it is a document, not the empty fallback.
    assert json.loads(rendered)["user_default"] == 60


def test_the_limiter_never_starts_a_second_pod_before_the_old_one_goes():
    """The counting consumer is a push consumer bound to a durable name,
    and JetStream permits one subscription per durable. A default rolling
    update starts the new pod first, its bind fails with "consumer is
    already bound to a subscription", and it comes up Ready with counting
    dead -- /check then answers allow with remaining == limit and the
    platform silently stops enforcing. Observed on the cluster.
    """
    spec = one(render(LIMITER, *LIMITER_MIN), "Deployment")["spec"]
    assert spec["strategy"]["rollingUpdate"]["maxSurge"] == 0
    # And one replica, for the same exclusivity reason.
    assert spec["replicas"] == 1


# -- both limiters --------------------------------------------------------
@pytest.mark.parametrize("chart,kind", LIMITERS)
def test_a_limiter_refuses_to_install_without_a_backbone(chart: Path, kind: str):
    """The failure this guard exists for is invisible: no backbone means no
    consumer, the counters never advance, and /check allows everything
    while the pod stays Ready."""
    assert "no event backbone configured" in render_error(chart)
    # ...unless the operator says so on purpose.
    docs = render(chart, "--set", "config.allowNoBackbone=true")
    assert one(docs, "ConfigMap")["data"]["EVENT_BACKBONE_URL"] == ""


@pytest.mark.parametrize("chart,kind", LIMITERS)
def test_the_limits_document_is_never_in_a_configmap(chart: Path, kind: str):
    """user_overrides is keyed by bearer token, so RATE_LIMITS is a
    credential whichever limiter holds it."""
    docs = render(chart, "--set", "config.events.backboneUrl=nats://nats:4222",
                  "--set", "secret.limits.userDefault=100")
    assert "RATE_LIMITS" not in one(docs, "ConfigMap")["data"]
    assert "RATE_LIMITS" in one(docs, "Secret")["stringData"]


@pytest.mark.parametrize("chart,kind", LIMITERS)
def test_each_limiter_has_its_own_durable(chart: Path, kind: str):
    """Both consumers read the same stream and subject. The durable name is
    the only thing giving each an independent cursor — share one and they
    split the stream, so each sees about half the events and both
    under-count with no error."""
    docs = render(chart, "--set", "config.events.backboneUrl=nats://nats:4222")
    assert one(docs, "ConfigMap")["data"]["EVENT_DURABLE_NAME"] == f"{kind}-counter"


# -- rate-limiter-tpm only ------------------------------------------------
def test_tpm_refuses_rpms_durable():
    """The one misconfiguration that produces two half-counting limiters
    and no error on either side."""
    error = render_error(
        TPM_LIMITER,
        "--set", "config.events.backboneUrl=nats://nats:4222",
        "--set", "config.events.durableName=rpm-counter",
    )
    assert "rpm-counter" in error and "under-count" in error


def test_tpm_carries_the_tokenizer_fallback_settings():
    """Empty is valid — it makes the fallback inert, so events with no
    usage block are skipped rather than estimated. The value still has to
    reach the container, or configuring it later does nothing."""
    docs = render(TPM_LIMITER, *LIMITER_MIN)
    data = one(docs, "ConfigMap")["data"]
    assert data["TOKENIZER_URL"] == ""
    assert data["TOKENIZER_TIMEOUT_MS"] == "2000"

    docs = render(TPM_LIMITER, *LIMITER_MIN,
                  "--set", "config.tokenizer.url=http://tokenizer:8000/tokenize")
    assert one(docs, "ConfigMap")["data"]["TOKENIZER_URL"] == \
        "http://tokenizer:8000/tokenize"


# -- the two control planes -----------------------------------------------
@pytest.mark.parametrize("chart,config", CONTROL_PLANES)
def test_a_control_plane_refuses_to_deploy_with_no_secret_source(
    chart: Path, config: List[str],
):
    """Both charts default to secret.create=false and existingSecret="",
    and nothing used to guard the case where neither was set. The render
    succeeded, naming a Secret that was never created, and the install
    then died on the pre-install hook with a CreateContainerConfigError
    -- a Kubernetes error about a missing object, rather than a sentence
    saying which value to set.
    """
    # Everything it needs except the Secret source, so the refusal can
    # only be about that.
    message = render_error(chart, *config)

    assert "secret.existingSecret" in message
    assert "secret.create=true" in message


@pytest.mark.parametrize("chart,config", CONTROL_PLANES)
def test_a_control_plane_creates_a_secret_only_when_asked(
    chart: Path, config: List[str],
):
    """And an existingSecret wins outright, with nothing created
    alongside it to drift from it."""
    docs = render(chart, *config, *SECRET_SOURCE)
    assert not [d for d in docs if d["kind"] == "Secret"]
    assert container(docs)["envFrom"][1]["secretRef"]["name"] == "cp-secret"

    docs = render(chart, *config, *CREATED_SECRET)
    assert one(docs, "Secret")["stringData"]["DATABASE_URL"].startswith("postgresql")


@pytest.mark.parametrize("chart,config", CONTROL_PLANES)
def test_the_migration_job_is_kept_off_the_control_plane_too(
    chart: Path, config: List[str],
):
    """The Deployment carried the anti-affinity and the Job did not, so
    the one pod in these charts that could land on rke2-cp01 was the one
    running alembic against the database -- next to etcd.

    Asserted separately from test_pods_are_kept_off_the_control_plane
    because that one reads the Deployment, which was never the problem.
    """
    job = one(render(chart, *config, *SECRET_SOURCE), "Job")
    pod = job["spec"]["template"]["spec"]

    terms = pod["affinity"]["nodeAffinity"][
        "requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]
    expressions = [e for t in terms for e in t["matchExpressions"]]
    assert {"key": "node-role.kubernetes.io/control-plane",
            "operator": "DoesNotExist"} in expressions


@pytest.mark.parametrize("chart,config", CONTROL_PLANES)
def test_the_migration_job_is_scheduled_from_the_same_values(
    chart: Path, config: List[str],
):
    """One set of scheduling values for the chart, not two. Pinning the
    Deployment to a node pool and leaving the migration free to run
    anywhere is not a configuration anyone wants."""
    docs = render(chart, *config, *SECRET_SOURCE,
                  "--set", "nodeSelector.kubernetes\\.io/hostname=rke2-wrk-2")

    for kind in ("Deployment", "Job"):
        pod = one(docs, kind)["spec"]["template"]["spec"]
        assert pod["nodeSelector"] == {"kubernetes.io/hostname": "rke2-wrk-2"}, (
            f"{kind} did not take the chart's nodeSelector"
        )


# -- the portal chart -----------------------------------------------------
def portal_conf(*extra: str) -> str:
    """The nginx config the chart renders, as nginx would receive it."""
    return one(render(PORTAL, *extra), "ConfigMap")["data"]["default.conf"]


def test_portal_lints():
    result = subprocess.run(
        ["helm", "lint", str(PORTAL), *PORTAL_MIN],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_portal_refuses_to_deploy_with_no_api():
    """The bundle calls its API on its own origin, so with no proxy the
    portal serves, every request 404s, and the login page it opens on
    cannot complete -- which looks like a working deployment right up
    until someone tries to sign in."""
    message = render_error(PORTAL)
    assert "config.apiUpstream" in message
    assert "config.allowNoApi=true" in message


def test_portal_can_be_deployed_without_an_api_deliberately():
    conf = portal_conf("--set", "config.allowNoApi=true")
    assert "proxy_pass" not in conf
    assert "try_files" in conf, "the bundle should still be served"


def test_portal_proxies_every_prefix_the_bundle_uses():
    """Two, not one. Nearly every call is getBaseUrl() + path, which is
    /v1/..., but the silent token refresh in src/api/client.ts calls
    '/api/auth/refresh' directly and bypasses the base URL. Proxy only
    /v1/ and that request falls through to the SPA fallback, which
    answers 200 with index.html -- so the refresh does not fail, it
    succeeds and returns HTML.
    """
    conf = portal_conf(*PORTAL_MIN)
    for prefix in ("/v1/", "/api/"):
        assert f"location {prefix} {{" in conf, f"{prefix} is not proxied"


def test_the_spa_fallback_comes_last():
    """`location /` matches everything. Ahead of the proxies it would
    serve index.html for API calls too."""
    conf = portal_conf(*PORTAL_MIN)
    fallback = conf.index("location / {")
    assert fallback > conf.index("location /v1/ {")
    assert fallback > conf.index("location /api/ {")
    assert fallback > conf.index("location = /health {")


def test_the_upstream_is_reached_through_a_variable():
    """A literal proxy_pass is resolved once at startup, so a portal
    installed before its API exists crash-loops on 'host not found in
    upstream'. Through a variable it is resolved per request, which is
    what lets the portal go up first."""
    conf = portal_conf(*PORTAL_MIN)
    assert "proxy_pass $api_upstream$request_uri;" in conf
    assert "proxy_pass http://" not in conf


def test_the_resolver_is_an_address_not_a_name():
    """nginx resolves the resolver's own address at startup, so a
    Service name there fails the config outright:

        [emerg] host not found in resolver
          "rke2-coredns-rke2-coredns.kube-system.svc.cluster.local"
    """
    conf = portal_conf(*PORTAL_MIN)
    resolver = [l for l in conf.splitlines() if "resolver " in l][0].split()[1]
    assert all(part.isdigit() for part in resolver.split(".")), (
        f"resolver {resolver} is a name; nginx -t rejects that"
    )


def test_the_portal_probes_something_nginx_answers():
    """A 200 at / says only that index.html exists, which is also true
    of a half-built image."""
    conf = portal_conf(*PORTAL_MIN)
    assert "location = /health {" in conf and "return 200" in conf

    spec = container(render(PORTAL, *PORTAL_MIN))
    for probe in ("livenessProbe", "readinessProbe"):
        assert spec[probe]["httpGet"] == {"path": "/health", "port": "http"}


def test_the_portal_is_pinned_to_the_node_holding_its_image():
    """There is no registry: images are built locally and sideloaded
    into one node's containerd. Unpinned, the pod schedules anywhere and
    sits in ImagePullBackOff -- which is where chart-test/tokenizer has
    been for 20 hours, on mcw-all-series, with its image on rke2-wrk-2.
    """
    pod = one(render(PORTAL, *PORTAL_MIN), "Deployment")["spec"]["template"]["spec"]
    assert pod["nodeSelector"], "nothing pins this to the node with the image"
    assert pod["containers"][0]["imagePullPolicy"] == "IfNotPresent", (
        "a sideloaded image must never be pulled"
    )


def test_the_portal_is_kept_off_the_control_plane():
    pod = one(render(PORTAL, *PORTAL_MIN), "Deployment")["spec"]["template"]["spec"]
    terms = pod["affinity"]["nodeAffinity"][
        "requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]
    expressions = [e for t in terms for e in t["matchExpressions"]]
    assert {"key": "node-role.kubernetes.io/control-plane",
            "operator": "DoesNotExist"} in expressions


def test_the_portal_service_targets_a_named_port():
    docs = render(PORTAL, *PORTAL_MIN)
    assert one(docs, "Service")["spec"]["ports"][0]["targetPort"] == "http"
    ports = {p["name"]: p["containerPort"] for p in container(docs)["ports"]}
    assert ports["http"] == 80


def test_changing_the_api_upstream_rolls_the_portal():
    """The nginx config is a mounted file, so without the checksum the
    running pods keep serving the old upstream until something unrelated
    restarts them."""
    before = one(render(PORTAL, *PORTAL_MIN), "Deployment")
    after = one(render(PORTAL, "--set", "config.apiUpstream=http://other:8000"),
                "Deployment")
    annotations = before["spec"]["template"]["metadata"]["annotations"]
    assert "checksum/config" in annotations
    assert (annotations["checksum/config"]
            != after["spec"]["template"]["metadata"]["annotations"]["checksum/config"])


# -- the settings the control-plane charts used to leave out ---------------
@pytest.mark.parametrize("chart,config", CONTROL_PLANES)
def test_a_control_plane_secret_carries_the_login_settings(
    chart: Path, config: List[str],
):
    """Neither VALKEY_URL nor JWT_SECRET was in these charts at all --
    not in values.yaml, not in the ConfigMap. So an install came up
    healthy, passed both probes, and returned 500 on every sign-in:
    `lockout.check_locked` connects to settings.py's localhost default
    before the password is even verified, and `jwt.py` raises
    JwtSecretUnsetError rather than issue an unsigned token.
    """
    data = one(render(chart, *config, *CREATED_SECRET), "Secret")["stringData"]
    assert data["VALKEY_URL"].startswith("redis://")
    assert data["JWT_SECRET"] == "j"


@pytest.mark.parametrize("chart,config", CONTROL_PLANES)
def test_neither_login_setting_can_be_left_out_of_a_created_secret(
    chart: Path, config: List[str],
):
    """Both fail the render rather than the sign-in. A chart cannot
    inspect an existingSecret, so this is the one path where the
    omission is catchable -- and NOTES.txt covers the other."""
    assert "secret.valkeyUrl is required" in render_error(
        chart, *config, *without("secret.valkeyUrl"))
    assert "secret.jwtSecret is required" in render_error(
        chart, *config, *without("secret.jwtSecret"))


@pytest.mark.parametrize("chart,config", CONTROL_PLANES)
def test_the_notes_list_what_an_existing_secret_must_hold(
    chart: Path, config: List[str],
):
    """The only lever for the existingSecret path: the chart never reads
    it, and a Secret missing a key installs cleanly and passes probes.

    NOTES.txt is read off disk rather than rendered, because rendering
    it needs `helm install`, which contacts the cluster even with
    --dry-run -- and these tests run without one. So this checks the
    key names are present in the template, not the formatting.
    """
    notes = (chart / "templates" / "NOTES.txt").read_text()

    rendered = one(render(chart, *config, *CREATED_SECRET), "Secret")["stringData"]

    for key in rendered:
        assert key in notes, (
            f"secret.yaml renders {key} but NOTES.txt does not tell an "
            f"operator their existingSecret needs it"
        )
