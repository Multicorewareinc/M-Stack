"""Which image the reference stack deploys.

Every first-party chart resolves its tag from `appVersion`, and every one
of them reads `0.1.0`. Nothing in `full_stack.py` ever set `image_tag`,
so the whole platform pinned itself to that tag while CI was building a
per-commit image for each service that nothing deployed -- on 2026-10-09
the portals in GHCR under `:0.1.0` were three weeks older than the UI
they were supposed to serve, and `model-gateway` had no other tag at all.

Two separate defects, so two kinds of test here.

`image_tag()` is executable, so it is tested by loading the module and
reading the value back off a real spec. The call sites are not: loading
the module does not run a stage, and running one needs a cluster. Those
are checked against the source instead, the same way `test_examples.py`
checks names and paths without executing anything -- a static test that
fails the moment a new service is added without a tag is worth more than
no test at all.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FULL_STACK = ROOT / "examples" / "full_stack.py"


def load(monkeypatch, **env):
    """Import `full_stack.py` fresh under a given environment.

    Fresh every time: the module reads `os.environ` at import, so a
    cached copy would answer for whichever test imported it first.
    """
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    sys.modules.pop("_full_stack_under_test", None)
    spec = importlib.util.spec_from_file_location("_full_stack_under_test", FULL_STACK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# -- the helper ----------------------------------------------------------
def test_unset_tag_leaves_the_chart_alone(monkeypatch):
    """None, not "" or "latest".

    None is what every capability's `image_tag` field already means:
    leave the chart's own appVersion in place. An empty string would
    render `image: repo:` and fail the pull.
    """
    monkeypatch.delenv("MULTISTACK_IMAGE_TAG", raising=False)
    assert load(monkeypatch).image_tag() is None


def test_an_explicit_tag_is_returned(monkeypatch):
    assert load(monkeypatch, MULTISTACK_IMAGE_TAG="168ebf5").image_tag() == "168ebf5"


def test_the_tag_reaches_a_service_spec(monkeypatch):
    """Through the options dict a stage actually builds.

    The helper returning a string proves nothing on its own -- what
    matters is that the value survives into the spec the backend renders
    values from.
    """
    from multistack import Tokenizer

    module = load(monkeypatch, MULTISTACK_IMAGE_TAG="168ebf5")
    tokenizer = Tokenizer(
        kubeconfig_path="/tmp/kubeconfig",
        options={
            "image_repository": module.image_repo("tokenizer"),
            "image_tag": module.image_tag(),
        },
    )
    assert tokenizer.options.image_tag == "168ebf5"


def test_the_tag_reaches_a_portal_spec(monkeypatch):
    """The portals are a separate path -- see `stage_portal`.

    Their options spell the repository out rather than taking it from
    `image_repo`, so they were the one pair the tag could not have
    reached even after the other seven were wired.
    """
    from multistack import Portal

    module = load(monkeypatch, MULTISTACK_IMAGE_TAG="168ebf5")
    portal = Portal(
        type="admin",
        kubeconfig_path="/tmp/kubeconfig",
        api_upstream="http://admin-control-plane.control-plane:8000",
        options={
            "image_repository": module.image_repo("admin-portal"),
            "image_tag": module.image_tag(),
        },
    )
    assert portal.options.image_tag == "168ebf5"


# -- the call sites ------------------------------------------------------
SOURCE = FULL_STACK.read_text()


def _service_names() -> list[str]:
    return sorted(set(re.findall(r'image_repo\(\s*f?"([^"]+)"', SOURCE)))


def test_there_are_image_repo_call_sites_to_check():
    """A regex that matched nothing would make the next test vacuous."""
    assert len(_service_names()) >= 6, _service_names()


@pytest.mark.parametrize("service", _service_names())
def test_every_first_party_image_also_carries_a_tag(service: str):
    """`image_repo(x)` and `image_tag()` travel together.

    Checked per call site rather than by counting, so adding a service
    and wiring only its repository fails this test by name instead of
    silently deploying whatever `appVersion` says.
    """
    window = 320
    for match in re.finditer(r'image_repo\(\s*f?"' + re.escape(service) + r'"', SOURCE):
        nearby = SOURCE[match.start() : match.start() + window]
        assert "image_tag()" in nearby, (
            f'image_repo("{service}") does not pass image_tag() -- that '
            f"service will deploy whatever its chart's appVersion names"
        )


def test_the_portals_derive_their_repository_from_the_registry():
    """Not from PortalOptions' hardcoded default.

    `PortalOptions` spells both repositories out and has to: `ui/chart`
    can only default to one of the two and defaults to the organization
    portal, so an admin portal left to the chart deploys the wrong image.
    But spelled out *in the spec* meant these two alone ignored
    MULTISTACK_IMAGE_REGISTRY, so a fork kept pulling this repo's
    portals. The stage has to pass a derived repository, keeping the
    spec's value as the fallback.
    """
    stage = SOURCE[SOURCE.index("def stage_portal") : SOURCE.index("def stage_route")]
    assert 'image_repo(f"{portal_type}-portal")' in stage, (
        "stage_portal does not derive its image repository, so the two "
        "portals ignore MULTISTACK_IMAGE_REGISTRY"
    )
    assert "image_tag()" in stage


def test_the_stage_still_parses_as_written():
    """Cheap guard on the conditional options dict.

    `stage_portal` now builds its options before the call and splats
    them in. That is the kind of edit that parses in isolation and
    breaks the module, so the whole file is parsed here too.
    """
    ast.parse(SOURCE)
