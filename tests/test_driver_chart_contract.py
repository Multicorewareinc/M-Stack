"""Does each driver send values its own chart accepts?

`test_charts.py` renders the charts with values written by hand, which is
right for what it asks -- does this chart put a credential in a ConfigMap,
does its Service name a port the container listens on. But it means the
one question it cannot ask is whether the driver sends those values at
all. The fixtures supply them, so the suite stays green while a driver
sends something else entirely.

That is not hypothetical. On 2026-10-09 both rate limiters failed to
install: the drivers filled in every field of `secret` -- the limits
document, the Valkey URL, the shared service key -- and never set
`secret.create`, which is the flag that asks for the Secret to exist. The
chart refused rather than point the pod's `envFrom` at a Secret nothing
wrote, which is correct, and `stage_policy` died on it. Twenty of
`test_charts.py`'s own cases were failing at the time for a different
reason (their `LIMITER_MIN` fixture omits the same flag), so the real
defect was indistinguishable from fixture drift.

So this file renders each chart from its *driver's* `_values()` and
asserts only that the render succeeds. It makes no claim about the
manifests -- that is the other suite's job. One assertion, ten pairs: the
deploy path either works or it does not.

Two more things fall out of it for free, and both are checked below: the
image each chart resolves to, and whether `MULTISTACK_IMAGE_TAG` reaches
Helm when it is set. Those are the values a cluster rebuild turns on.
"""

from __future__ import annotations

import importlib
import pkgutil
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

yaml = pytest.importorskip("yaml")

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None,
    reason="helm is not on PATH",
)

ROOT = Path(__file__).resolve().parents[1]
API = ROOT / "api/microservices"
KC = "/tmp/kubeconfig-under-test.yaml"

# Values every spec below needs and none of them should assert about. Real
# addresses rather than placeholders: a chart that validates a URL would
# otherwise fail here for a reason that has nothing to do with the driver.
NATS = "nats://nats.platform.svc.cluster.local:4222"
VALKEY = "redis://valkey.cache.svc.cluster.local:6379/0"
ADMIN_CP = "http://admin-control-plane.control-plane.svc.cluster.local:8000"
ORG_CP = "http://organization-control-plane.control-plane.svc.cluster.local:8000"
VLLM = "http://vllm.inference.svc.cluster.local:8000"


def _spec(name: str) -> Callable[[], Any]:
    """Build one spec the way `examples/full_stack.py` builds it.

    Imported inside the factory, not at module scope: a capability whose
    optional extra is missing should skip its own case rather than fail
    collection for all ten.
    """

    def admin_cp():
        from multistack import ControlPlane

        return ControlPlane(
            type="admin", kubeconfig_path=KC, existing_secret="admin-cp-secrets", peer_url=ORG_CP
        )

    def organization_cp():
        from multistack import ControlPlane

        return ControlPlane(
            type="organization",
            kubeconfig_path=KC,
            existing_secret="org-cp-secrets",
            peer_url=ADMIN_CP,
        )

    def gateway():
        from multistack import Gateway

        return Gateway(
            kubeconfig_path=KC,
            upstream_url=VLLM,
            api_key_secret="gateway-keys",
            org_cp_internal_url=ORG_CP,
        )

    def tokenizer():
        from multistack import Tokenizer

        return Tokenizer(kubeconfig_path=KC)

    def enricher():
        from multistack import Enricher

        return Enricher(kubeconfig_path=KC, event_backbone_url=NATS)

    def billing():
        from multistack import Billing

        return Billing(
            kubeconfig_path=KC, existing_secret="billing-secrets", event_backbone_url=NATS
        )

    def rpm():
        from multistack import Policy

        return Policy(
            type="rpm",
            kubeconfig_path=KC,
            cache_url=VALKEY,
            event_backbone_url=NATS,
            admin_cp_url=ADMIN_CP,
        )

    def tpm():
        from multistack import Policy

        return Policy(
            type="tpm",
            kubeconfig_path=KC,
            cache_url=VALKEY,
            event_backbone_url=NATS,
            admin_cp_url=ADMIN_CP,
        )

    def admin_portal():
        from multistack import Portal

        return Portal(type="admin", kubeconfig_path=KC, api_upstream=ADMIN_CP)

    def organization_portal():
        from multistack import Portal

        return Portal(type="organization", kubeconfig_path=KC, api_upstream=ORG_CP)

    return locals()[name]


# (id, capability package, chart directory, spec factory, expected image)
CASES: list[tuple[str, str, Path, Callable[[], Any], str]] = [
    ("gateway", "gateway", API / "model-gateway/chart", _spec("gateway"), "model-gateway"),
    ("tokenizer", "tokenizer", API / "tokenizer/chart", _spec("tokenizer"), "tokenizer"),
    ("enricher", "enricher", API / "enricher/chart", _spec("enricher"), "enricher"),
    ("billing", "billing", API / "billing/chart", _spec("billing"), "billing"),
    ("policy-rpm", "policy", API / "rate-limiter-rpm/chart", _spec("rpm"), "rate-limiter-rpm"),
    ("policy-tpm", "policy", API / "rate-limiter-tpm/chart", _spec("tpm"), "rate-limiter-tpm"),
    (
        "controlplane-admin",
        "controlplane",
        API / "admin-control-plane/chart",
        _spec("admin_cp"),
        "admin-control-plane",
    ),
    (
        "controlplane-org",
        "controlplane",
        API / "organization-control-plane/chart",
        _spec("organization_cp"),
        "organization-control-plane",
    ),
    ("portal-admin", "portal", ROOT / "ui/chart", _spec("admin_portal"), "admin-portal"),
    (
        "portal-org",
        "portal",
        ROOT / "ui/chart",
        _spec("organization_portal"),
        "organization-portal",
    ),
]

IDS = [c[0] for c in CASES]


def drivers_of(package: str) -> list[Any]:
    """Every driver class in a capability that builds Helm values.

    Discovered rather than listed: a capability that gains a second
    implementation gets checked without this file being edited, which is
    the whole point of the driver contract.
    """
    found = []
    pkg = importlib.import_module(f"multistack.{package}.drivers")
    for _, name, _ in pkgutil.iter_modules(pkg.__path__):
        module = importlib.import_module(f"multistack.{package}.drivers.{name}")
        for value in vars(module).values():
            if (
                isinstance(value, type)
                and value.__module__ == module.__name__
                and hasattr(value, "_values")
            ):
                found.append(value)
    return found


def render(chart: Path, values: dict[str, Any]) -> subprocess.CompletedProcess:
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
        yaml.safe_dump(values, handle)
        path = handle.name
    return subprocess.run(
        ["helm", "template", "contract-test", str(chart), "-f", path],
        capture_output=True,
        text=True,
    )


def values_for(package: str, spec: Any) -> dict[str, Any]:
    candidates = drivers_of(package)
    assert candidates, f"no driver with _values found in multistack.{package}.drivers"
    for driver in candidates:
        try:
            return driver()._values(spec)
        except Exception:
            continue  # of a two-type capability; try the next
    pytest.fail(f"no driver in multistack.{package}.drivers accepted {spec!r}")


# -- the contract --------------------------------------------------------
@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_the_chart_accepts_what_its_driver_sends(case):
    """The one assertion this file exists for.

    No claim about the rendered manifests. A chart that refuses to
    render is a stage that fails at install, which is what happened to
    both limiters, and no amount of asserting on manifests that were
    never produced would have shown it.
    """
    _, package, chart, make_spec, _ = case
    result = render(chart, values_for(package, make_spec()))
    if result.returncode:
        reason = next(
            (line for line in result.stderr.splitlines() if "error" in line.lower()),
            result.stderr.strip(),
        )
        pytest.fail(
            f"{chart.parent.name}: the chart refuses the values its driver "
            f"sends, so this stage fails at install --\n  {reason}"
        )


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_the_resolved_image_is_the_one_this_service_wants(case):
    """Guards the chart/image coupling, in both directions.

    Every chart takes its tag from `appVersion`, and every one of them
    read `0.1.0` for long enough that the tag outlived the images behind
    it -- on 2026-10-09 the two portals under `:0.1.0` were three weeks
    older than the UI they served. A chart whose repository or tag drifts
    from what was built is the same class of failure, so both halves are
    checked: the repository names this service, and the tag is a real
    value rather than an empty string.
    """
    _, package, chart, make_spec, image = case
    result = render(chart, values_for(package, make_spec()))
    if result.returncode:
        pytest.skip("chart does not render; covered by the test above")

    lines = [
        line.strip() for line in result.stdout.splitlines() if line.strip().startswith("image:")
    ]
    assert lines, f"{chart} renders no image: line at all"

    reference = lines[0].split("image:", 1)[1].strip().strip('"')
    repository, _, tag = reference.rpartition(":")
    assert repository.endswith(f"/{image}"), (
        f"{chart.parent.name} resolves to {repository!r}, which does not name {image}"
    )
    assert tag, f"{chart.parent.name} resolves to {reference!r} with no tag"


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_an_explicit_image_tag_reaches_helm(case, monkeypatch):
    """What a rebuild turns on.

    `MULTISTACK_IMAGE_TAG` exists so a cluster rebuild can name one
    identifiable set of images instead of whatever a mutable tag pointed
    at that week. It is worth nothing unless it survives the whole way
    down: env var, `image_tag()`, the stage's options, the driver's
    values, the rendered manifest. This checks the last two links --
    `tests/test_full_stack_images.py` checks the first three.
    """
    _, package, chart, make_spec, image = case
    spec = make_spec()
    options = spec.options
    if options is None or not hasattr(options, "image_tag"):
        pytest.skip(f"{package} options carry no image_tag")

    # validate_assignment is on for some options classes, so go through
    # the model rather than around it.
    spec.options = options.model_copy(update={"image_tag": "contract-test-tag"})

    result = render(chart, values_for(package, spec))
    if result.returncode:
        pytest.skip("chart does not render; covered by the first test")
    assert f"{image}:contract-test-tag" in result.stdout, (
        f"{chart.parent.name} ignored an explicit image_tag, so a rebuild "
        "cannot pin which image it runs"
    )
