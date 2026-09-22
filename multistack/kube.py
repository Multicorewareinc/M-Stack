"""
Talking to a Kubernetes cluster, and verifying one is there.

Two things live here. The first is the cluster-dependency check every
capability inherits. The second is the small set of primitives for running
`kubectl` against a named cluster — because every capability needs them,
and before this they existed four times over (in the MinIO backend, the
vLLM backend, the Longhorn driver, and again in every example), which is
three copies too many to keep in step. They had already drifted: two
passed `stdin=DEVNULL` and two did not, two turned a timeout into a typed
error and two let `subprocess.TimeoutExpired` escape raw.

Nothing here imports a capability, and nothing here decides policy. A
`kubectl` runner is mechanism; *which* node to label and *what* to call a
Secret are the composition's business.

Most capabilities in this SDK deploy *into* a Kubernetes cluster, so they
depend on the `cluster` capability having run. That dependency is declared
on the spec (`REQUIRES = ("cluster",)`) and verified here.

Declaring it is not enough on its own. `kubeconfig_path` is a string, so
nothing stops it naming a cluster that was deleted an hour ago — and the
failure then surfaces as `x509: certificate signed by unknown authority`
from inside a Helm call, several minutes and one confusing stack trace
later. `require_cluster()` turns that into an immediate, specific error.

This module deliberately does not import the `cluster` capability. Storage
depends on *a* Kubernetes cluster, not on RKE2 — a kubeconfig from k3s,
kubeadm or a managed provider satisfies it equally. Importing
`multistack.cluster` here would couple every capability to one
implementation of another, which is exactly what the capability split
exists to prevent.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional

# Long enough for a slow API server, short enough that a hung call is not
# mistaken for a slow one. Callers with their own budget pass it.
DEFAULT_TIMEOUT = 300
DEFAULT_POLL_INTERVAL = 10


class ClusterDependencyError(RuntimeError):
    """Raised when a capability's cluster dependency isn't satisfied."""


class KubeCommandError(RuntimeError):
    """A local `kubectl` (or other CLI) call failed.

    The default `error_cls` below. A capability passes its own instead, so
    a caller can keep catching `MinIOError` or `StorageError` and still get
    one implementation of the mechanics underneath.
    """


def _completed(
    argv: List[str],
    *,
    timeout: int,
    stdin_text: Optional[str] = None,
) -> subprocess.CompletedProcess:
    """Runs `argv` and hands back the CompletedProcess.

    Raises only what subprocess itself raises — `FileNotFoundError` and
    `TimeoutExpired` — so a caller with better diagnostics than a generic
    message can produce them (see `require_cluster`). Everything else
    should use `run_local`.

    stdin is closed unless text is being piped in: a CLI that decides to
    prompt otherwise inherits a terminal and hangs until the timeout,
    which reads as a slow cluster rather than a stuck command.
    """
    kwargs: Dict[str, Any] = dict(capture_output=True, text=True, timeout=timeout)
    if stdin_text is None:
        kwargs["stdin"] = subprocess.DEVNULL
    else:
        kwargs["input"] = stdin_text
    return subprocess.run(argv, **kwargs)


def run_local(
    argv: List[str],
    *,
    check: bool = True,
    timeout: int = DEFAULT_TIMEOUT,
    error_cls: type = KubeCommandError,
    stdin_text: Optional[str] = None,
    label: Optional[str] = None,
) -> str:
    """Runs a local CLI — `kubectl`, `helm` — and returns its stdout.

    Raises `error_cls` for a missing binary, a timeout, or (when `check`)
    a non-zero exit. Failure text prefers stderr but falls back to stdout,
    because some tools report the useful part on the wrong stream and an
    empty error message is the worst possible outcome.

    `label` is what the error calls the command. It exists because the
    first few words of `argv` are the wrong thing to quote for anything
    kubeconfig-scoped: "kubectl --kubeconfig /tmp/kc.yaml failed" names
    the flag instead of the operation.
    """
    named = label or " ".join(argv[:3])
    try:
        result = _completed(argv, timeout=timeout, stdin_text=stdin_text)
    except FileNotFoundError as exc:
        raise error_cls(
            f"`{argv[0]}` was not found on PATH, and this operation needs it."
        ) from exc
    except subprocess.TimeoutExpired:
        raise error_cls(f"{named} timed out after {timeout}s") from None

    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or "no output"
        raise error_cls(f"{named} failed: {detail}")
    return result.stdout


def require_cli(
    name: str,
    *,
    error_cls: type = KubeCommandError,
    purpose: str = "this operation drives it directly",
) -> str:
    """Fails by name if `name` isn't on PATH, and returns its full path.

    `shutil.which` rather than running the binary: nothing is executed, so
    there is no shell to quote for, no `bash` dependency, and no risk of a
    presence check contacting a cluster. The three hand-rolled versions
    this replaces did all three of those things differently, and one
    interpolated the name into `bash -c` unquoted.
    """
    found = shutil.which(name)
    if found is None:
        raise error_cls(f"`{name}` was not found on PATH — {purpose}.")
    return found


def kubectl(
    kubeconfig_path: str,
    *args: str,
    check: bool = True,
    timeout: int = DEFAULT_TIMEOUT,
    error_cls: type = KubeCommandError,
) -> str:
    """Runs `kubectl` against the cluster `kubeconfig_path` names.

    Always with an explicit `--kubeconfig`. There is no ambient
    `$KUBECONFIG` / `~/.kube/config` fallback anywhere in this SDK, and
    routing every call through here is what makes that structural rather
    than a rule each backend has to remember.
    """
    return run_local(
        ["kubectl", "--kubeconfig", kubeconfig_path, *args],
        check=check, timeout=timeout, error_cls=error_cls,
        label=f"kubectl {' '.join(args[:3])}",
    )


def apply(
    kubeconfig_path: str,
    manifest: Any,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    error_cls: type = KubeCommandError,
) -> str:
    """Applies a manifest by piping it to `kubectl apply -f -`.

    Takes YAML/JSON text, a single object, or a list of objects — a list
    is wrapped in a `v1 List`, which is how several objects go in one
    call. Piping rather than writing a temp file keeps the SDK free of a
    Kubernetes client dependency and leaves nothing on disk to leak.
    """
    if isinstance(manifest, str):
        text = manifest
    elif isinstance(manifest, (list, tuple)):
        text = json.dumps({"apiVersion": "v1", "kind": "List", "items": list(manifest)})
    else:
        text = json.dumps(manifest)
    return run_local(
        ["kubectl", "--kubeconfig", kubeconfig_path, "apply", "-f", "-"],
        timeout=timeout, error_cls=error_cls, stdin_text=text,
        label="kubectl apply",
    )


def wait_for(
    check: Callable[[], bool],
    *,
    timeout: int,
    interval: int = DEFAULT_POLL_INTERVAL,
    description: str = "condition",
    error_cls: type = KubeCommandError,
) -> None:
    """Polls `check` until it returns True, or raises on timeout.

    `check` returns True when done and False to keep waiting; to fail
    fast it raises, which is how a Job that has already failed avoids
    being waited on for another half hour.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if check():
            return
        time.sleep(interval)
    raise error_cls(f"{description} did not complete within {timeout}s")


def wait_for_job(
    kubeconfig_path: str,
    name: str,
    namespace: str,
    *,
    timeout: int = 1800,
    interval: int = DEFAULT_POLL_INTERVAL,
    error_cls: type = KubeCommandError,
) -> None:
    """Waits for a Job to succeed, raising with its logs if it fails.

    A Job reports neither `succeeded` nor `failed` while running, so
    absence means "still going" and only `failed` means stop. Surfacing
    the logs at the point of failure matters: by the time anyone looks,
    the pod may be gone.
    """
    def finished() -> bool:
        # The counts, compared as numbers. An earlier version matched the
        # strings "1" and "/1", which is wrong in both directions: a Job
        # with backoffLimit > 1 reports failed=2 when it gives up (so a
        # failed Job was never detected and blocked for the full timeout),
        # and a parallel Job reports succeeded=N.
        raw = kubectl(
            kubeconfig_path, "get", "job", name, "-n", namespace,
            "-o", "jsonpath={.status.succeeded}|{.status.failed}"
                  "|{.spec.completions}|{.spec.backoffLimit}",
            check=False, error_cls=error_cls,
        ).strip()
        succeeded, failed, completions, backoff = (
            _as_int(part) for part in (raw.split("|") + ["", "", "", ""])[:4]
        )
        if succeeded >= max(completions, 1):
            return True
        if failed > backoff:
            logs = kubectl(
                kubeconfig_path, "logs", f"job/{name}", "-n", namespace,
                check=False, error_cls=error_cls,
            )
            raise error_cls(f"job {name} failed:\n{logs}")
        return False

    wait_for(
        finished, timeout=timeout, interval=interval,
        description=f"job {name}", error_cls=error_cls,
    )


def wait_for_phase(
    kubeconfig_path: str,
    kind: str,
    name: str,
    namespace: str,
    phase: str,
    *,
    timeout: int = 300,
    interval: int = DEFAULT_POLL_INTERVAL,
    error_cls: type = KubeCommandError,
) -> None:
    """Waits until `kind/name` reports `.status.phase == phase`.

    Phase, not readiness — the two are different and the distinction
    matters. For a PVC, `Bound` is exactly the question being asked. For a
    *Pod* it is not: a pod is `Running` while its containers are unready,
    including between crash-loop restarts, so polling a pod's phase can
    report success on a workload that never came up. Use `--wait` on the
    installing tool, or a readiness field, for those.
    """
    def bound() -> bool:
        current = kubectl(
            kubeconfig_path, "get", kind, name, "-n", namespace,
            "-o", "jsonpath={.status.phase}",
            check=False, error_cls=error_cls,
        ).strip()
        return current == phase

    wait_for(
        bound, timeout=timeout, interval=interval,
        description=f"{kind}/{name} reaching {phase}", error_cls=error_cls,
    )


def wait_for_absent(
    kubeconfig_path: str,
    kind: str,
    name: str,
    namespace: str,
    *,
    timeout: int = 300,
    interval: int = DEFAULT_POLL_INTERVAL,
    error_cls: type = KubeCommandError,
) -> None:
    """Waits until `kind/name` is gone.

    Deletion is not instant when finalizers are involved — a PVC with a
    volume still attached stays Terminating until the CSI driver releases
    it, so "the delete call returned" and "the object is gone" are
    different moments.
    """
    def gone() -> bool:
        return not kubectl(
            kubeconfig_path, "get", kind, name, "-n", namespace,
            "-o", "name", check=False, error_cls=error_cls,
        ).strip()

    wait_for(
        gone, timeout=timeout, interval=interval,
        description=f"{kind}/{name} being removed", error_cls=error_cls,
    )


def _as_int(value: str) -> int:
    """A jsonpath field as a number. Absent fields come back empty, and an
    absent count means zero, not a failure to parse."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def node_name_for(
    kubeconfig_path: str,
    address: str,
    *,
    error_cls: type = KubeCommandError,
) -> str:
    """The Kubernetes node name for a node's IP address.

    This SDK addresses nodes by IP, because that is what you can SSH to
    before a cluster exists. Kubernetes addresses them by name, and the
    two coincide only if someone happened to name the hosts that way.
    Anything that labels, taints or selects a node has to cross that gap,
    so it is crossed here rather than in each caller.
    """
    listing = kubectl(
        kubeconfig_path, "get", "nodes",
        "-o", "jsonpath={range .items[*]}{.metadata.name} "
              "{.status.addresses[?(@.type=='InternalIP')].address}{'\\n'}{end}",
        error_cls=error_cls,
    )
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == address:
            return parts[0]
    raise error_cls(
        f"no cluster node is registered on {address}. Registered: "
        f"{' '.join(listing.split()) or 'none'}"
    )


def require_cluster(
    kubeconfig_path: str,
    capability: str = "this capability",
    timeout: int = 30,
) -> str:
    """Verifies `kubeconfig_path` names a reachable cluster.

    Returns the server version string on success. Raises
    `ClusterDependencyError` naming the capability and what to do, rather
    than letting a stale or missing kubeconfig fail later inside helm or
    kubectl.

    Checks, in the order that gives the most specific message:
      1. the path is set at all
      2. the file exists and is readable
      3. an API server answers
    """
    if not kubeconfig_path:
        raise ClusterDependencyError(
            f"{capability} requires a Kubernetes cluster, but kubeconfig_path "
            "is unset. Create a cluster first (see examples/rke2/) and pass "
            "the kubeconfig it returns."
        )

    if not os.path.isfile(kubeconfig_path):
        raise ClusterDependencyError(
            f"{capability} requires a Kubernetes cluster, but no kubeconfig "
            f"exists at {kubeconfig_path}. Either the cluster was never "
            "created, or it was deleted — create it first (see "
            "examples/rke2/)."
        )
    if not os.access(kubeconfig_path, os.R_OK):
        raise ClusterDependencyError(
            f"kubeconfig at {kubeconfig_path} is not readable by this user."
        )

    # Deliberately `_completed` rather than `kubectl()`: the three ways
    # this can fail each deserve their own diagnosis, which a generic
    # message would flatten.
    try:
        result = _completed(
            ["kubectl", "--kubeconfig", kubeconfig_path, "version",
             "-o", "json"],
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise ClusterDependencyError(
            "`kubectl` was not found on PATH, and the cluster dependency "
            "cannot be verified without it."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ClusterDependencyError(
            f"{capability} requires a Kubernetes cluster, but the API server "
            f"named in {kubeconfig_path} did not answer within {timeout}s. It "
            "may be down, or unreachable from here (VPN?)."
        ) from exc

    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        hint = detail[-1] if detail else "no output"
        # The specific case that bit us: a kubeconfig left over from a
        # cluster that has since been rebuilt, so its CA no longer matches.
        if "certificate signed by unknown authority" in hint:
            hint += (
                " — this kubeconfig is stale: its CA does not match the "
                "cluster now answering. Re-create it, or point at the "
                "current cluster's kubeconfig."
            )
        raise ClusterDependencyError(
            f"{capability} requires a reachable Kubernetes cluster, but "
            f"{kubeconfig_path} could not be used: {hint}"
        )

    return result.stdout.strip()


def require_storage_class(
    kubeconfig_path: str,
    name: Optional[str] = None,
    capability: str = "this capability",
    timeout: int = 30,
) -> str:
    """Verifies a usable StorageClass exists, and returns its name.

    The `storage` dependency in concrete terms. A capability that claims
    volumes needs a class that exists *now* — and the failure mode when it
    doesn't is the quiet one: the PVC is accepted, sits Pending forever,
    and the component that mounted it reports a readiness timeout. The
    blame lands on the wrong layer.

    With `name`, that class must exist. Without, there must be a default,
    since a claim naming no class only binds if one is marked default.
    """
    # `-o json` rather than a jsonpath: the annotation key
    # (storageclass.kubernetes.io/is-default-class) needs its dots escaped
    # for jsonpath, and the escaping has to survive a Python string too —
    # which is how the first version of this silently reported "no
    # StorageClass at all" on a cluster that had two.
    raw = kubectl(
        kubeconfig_path, "get", "sc", "-o", "json",
        check=False, timeout=timeout, error_cls=ClusterDependencyError,
    )
    try:
        items = json.loads(raw)["items"] if raw.strip() else []
    except (ValueError, KeyError):
        items = []

    classes = {
        item["metadata"]["name"]:
            item["metadata"].get("annotations", {}).get(
                "storageclass.kubernetes.io/is-default-class") == "true"
        for item in items
    }

    if not classes:
        raise ClusterDependencyError(
            f"{capability} requires block storage, but this cluster has no "
            "StorageClass at all. RKE2 ships none — install storage first "
            "(see examples/storage/install.py). Without one, any volume "
            "claim is accepted and then stays Pending forever."
        )

    if name:
        if name not in classes:
            raise ClusterDependencyError(
                f"{capability} names StorageClass '{name}', which does not "
                f"exist on this cluster. Present: {', '.join(sorted(classes))}."
            )
        return name

    default = next((c for c, is_default in classes.items() if is_default), None)
    if default is None:
        raise ClusterDependencyError(
            f"{capability} names no StorageClass and this cluster has no "
            f"default one (present: {', '.join(sorted(classes))}). Name one "
            "explicitly, or install storage with default_storage_class=True."
        )
    return default


def missing_requirements(spec) -> List[str]:
    """Which of `spec.REQUIRES` this spec cannot itself satisfy.

    Only `cluster` is checkable generically — every other dependency is
    something a capability verifies in its own terms. Storage's dependency
    on a cluster is a reachable API server; an object store's dependency on
    storage is a StorageClass that exists, which only that driver can
    check. So this returns the generic part and each driver adds its own.
    """
    unmet: List[str] = []
    for requirement in getattr(spec, "REQUIRES", ()):
        if requirement != "cluster":
            continue
        try:
            require_cluster(
                getattr(spec, "kubeconfig_path", ""),
                capability=getattr(spec, "CAPABILITY", "this capability"),
            )
        except ClusterDependencyError:
            unmet.append(requirement)
    return unmet
