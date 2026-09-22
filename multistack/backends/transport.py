"""
Shared node-command transport.

Every backend that configures machines rather than an API — RKE2 for the
cluster itself, Longhorn for storage, anything needing packages or systemd
units on nodes — needs the same primitives: run a command on a node
(locally or over SSH, with sudo when required), probe read-only facts about
it, and fan work out across nodes. This module holds them once so backends
compose them instead of each growing its own copy that drifts.

`NodeCommandMixin` is a mixin rather than a standalone client because the
methods it provides are the same ones each backend's internals already
call. A backend mixes it in, declares its own error types, and gets the
transport for free:

    class MyBackend(NodeCommandMixin):
        error_cls = MyError
        prerequisite_error_cls = MyPrerequisiteError
        log_prefix = "mybackend"

Nodes are duck-typed: anything with `address`, `user`, `ssh_key` and
`ssh_port` works, so `RKE2Node` serves storage backends too without those
backends importing a cluster type.
"""
from __future__ import annotations
import os
import shlex
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, List, Optional


class NodeCommandError(Exception):
    """A command on a node failed, timed out, or the node was unreachable."""


class NodePrerequisiteError(Exception):
    """A node doesn't meet a hard requirement for what's being installed."""


class NodeCommandMixin:
    """Runs commands on nodes — locally for loopback addresses, over SSH
    otherwise — with `sudo -n` where root is needed, plus read-only probes
    and bounded parallel execution.

    Expects the mixing class to set `ssh_timeout`, `command_timeout` and
    `max_parallel` attributes (see the defaults below), and may override
    `error_cls`/`prerequisite_error_cls`/`log_prefix`.
    """

    # Overridden by backends so their callers keep seeing backend-specific
    # exception types rather than these generic ones.
    error_cls = NodeCommandError
    prerequisite_error_cls = NodePrerequisiteError
    log_prefix = "multistack"

    ssh_timeout: int = 30
    command_timeout: int = 900
    max_parallel: int = 10

    # -- transport ------------------------------------------------------
    @staticmethod
    def _is_local(node) -> bool:
        """Whether `node` refers to the machine running this SDK — those
        commands run directly, without going over SSH."""
        return node.address in ("127.0.0.1", "localhost", "::1")

    def _ssh_base_args(self, node) -> List[str]:
        """The `ssh` argv prefix (options + `user@host`) for connecting to
        `node`, before the remote command itself is appended."""
        args = [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", f"ConnectTimeout={self.ssh_timeout}",
            # This SDK requires passwordless key-based SSH. Without
            # BatchMode, a key that isn't authorized makes ssh fall back to
            # an interactive password prompt — which it reads straight from
            # the TTY, bypassing the captured pipes — and the call blocks
            # forever, inside a worker thread where it can't even be
            # attributed to a node. BatchMode turns that into an immediate
            # "Permission denied (publickey)" we can report properly.
            "-o", "BatchMode=yes",
            "-p", str(node.ssh_port),
        ]
        if node.ssh_key:
            args += ["-i", node.ssh_key]
        args.append(f"{node.user}@{node.address}")
        return args

    def _run(self, node, command: str, check: bool = True, sudo: bool = True) -> str:
        """Runs `command` on `node` — directly via local `bash -c` if
        `node` is this machine, otherwise over SSH — wrapping it in
        non-interactive `sudo -n` first if the executing/SSH user isn't
        already root. Returns stdout; if `check` is True (the default),
        raises `error_cls` on a non-zero exit, with a hint about
        passwordless sudo or SSH keys if either looks like the cause.

        Pass `sudo=False` for commands that genuinely don't need root —
        read-only prerequisite probes, mainly. They'd otherwise fail on a
        node where passwordless sudo isn't configured yet, leaving the
        prerequisite check unable to report the very thing that's wrong.
        """
        needs_sudo = sudo and (
            os.geteuid() != 0 if self._is_local(node) else node.user != "root"
        )
        if needs_sudo:
            command = f"sudo -n bash -c {shlex.quote(command)}"

        argv = (
            # No SSH round-trip needed to talk to yourself — this also
            # avoids requiring sshd to be installed/running just to
            # configure a single local node.
            ["bash", "-c", command]
            if self._is_local(node)
            else [*self._ssh_base_args(node), command]
        )
        try:
            result = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                # Nothing here is interactive, and a command that inherits
                # the terminal can sit on a prompt indefinitely — closing
                # stdin makes anything that tries to read it fail instead.
                stdin=subprocess.DEVNULL,
                # Backstop: ConnectTimeout only bounds connection setup, so
                # without this a command that connects and then never
                # returns hangs a worker thread for good.
                timeout=self.command_timeout,
            )
        except subprocess.TimeoutExpired:
            raise self.error_cls(
                f"Command timed out after {self.command_timeout}s on "
                f"{node.address}: {command}"
            ) from None

        if check and result.returncode != 0:
            raise self.error_cls(
                f"Command failed on {node.address}: {command}\n"
                f"{result.stderr.strip()}{self._failure_hint(node, result.stderr)}"
            )
        return result.stdout

    def _failure_hint(self, node, stderr: str) -> str:
        """Turns the two failures that account for most first-run problems
        into actionable advice instead of a bare SSH error."""
        stderr = stderr.lower()
        if "permission denied" in stderr and "publickey" in stderr:
            return (
                "\n(hint: this SDK needs passwordless key-based SSH — "
                f"check that node.ssh_key is the right key for "
                f"{node.user}@{node.address} and that its public half is in "
                "that user's ~/.ssh/authorized_keys, e.g. "
                f"`ssh-copy-id -i <key>.pub {node.user}@{node.address}`)"
            )
        if "sudo" in stderr or "password is required" in stderr:
            return (
                "\n(hint: passwordless sudo is required — add a NOPASSWD "
                "entry in /etc/sudoers.d/, or run as root)"
            )
        return ""

    def _probe(self, node, command: str, what: str) -> str:
        """Runs a read-only command on `node` (no sudo) and returns its
        stripped stdout.

        Raises `prerequisite_error_cls`, quoting the real stderr, if the
        command couldn't run at all. Using `_run(..., check=False)` here
        instead would hand back an empty string, which callers can't
        distinguish from a genuine answer — that turns "SSH or sudo is
        broken" into a nonsense diagnosis, or silently skips the check.
        """
        try:
            return self._run(node, command, sudo=False).strip()
        except self.error_cls as exc:
            raise self.prerequisite_error_cls(
                f"{node.address}: couldn't determine {what} — the probe "
                f"itself failed, so this node's prerequisites are unverified.\n{exc}"
            ) from None

    def _check_passwordless_sudo(self, node) -> None:
        """Verifies the node can run `sudo -n` without a password, when
        this backend would need to. Everything that installs packages or
        touches systemd goes through `sudo -n`, so without this the failure
        surfaces much later and much less clearly."""
        needs_sudo = (
            os.geteuid() != 0 if self._is_local(node) else node.user != "root"
        )
        if not needs_sudo:
            return
        if self._run(node, "sudo -n true && echo ok", check=False, sudo=False).strip() != "ok":
            raise self.prerequisite_error_cls(
                f"{node.address}: user '{node.user}' can't run sudo without a "
                "password, which this SDK requires. Add a NOPASSWD entry on "
                "that node, e.g.\n"
                f"  echo '{node.user} ALL=(ALL) NOPASSWD:ALL' | "
                f"sudo tee /etc/sudoers.d/{node.user}\n"
                f"  sudo chmod 440 /etc/sudoers.d/{node.user}\n"
                "(or connect as root instead)"
            )

    def _run_parallel(
        self,
        nodes: List,
        fn: Callable,
        label: str,
        on_success: Optional[Callable] = None,
    ):
        """
        Runs `fn(node)` for each node in `nodes` concurrently, capped at
        `self.max_parallel` at once, and returns a dict of
        {node.address: fn(node)} for every node that succeeded. Only safe
        for operations where nodes are independent of each other.

        Collects every failure rather than stopping at the first, so one
        bad node doesn't hide problems on the others, and raises a single
        aggregated `error_cls` (or `prerequisite_error_cls`, if that's the
        only exception type seen) listing all of them if any occurred.

        `on_success`, if given, is called as `on_success(node, result)`
        immediately after each individual node succeeds — before the whole
        batch is known to have succeeded or failed, so callers can persist
        progress that a later failure shouldn't discard.
        """
        if not nodes:
            return {}

        results = {}
        errors: List[tuple] = []
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=min(len(nodes), self.max_parallel)) as executor:
            future_to_node = {executor.submit(fn, node): node for node in nodes}
            for future in as_completed(future_to_node):
                node = future_to_node[future]
                try:
                    result = future.result()
                    results[node.address] = result
                    # Elapsed since the batch fanned out, not since this
                    # node started — they all start together, so the
                    # spread *is* the per-node duration. Without it a node
                    # that takes minutes longer than its peers looks
                    # exactly like work that was never parallel at all.
                    print(f"[{self.log_prefix}] {label} succeeded for "
                          f"{node.address} ({time.monotonic() - started:.0f}s)")
                    if on_success is not None:
                        on_success(node, result)
                except Exception as exc:
                    print(f"[{self.log_prefix}] {label} failed for {node.address}: {exc}")
                    errors.append((node.address, exc))

        if errors:
            detail = "\n".join(f"  - {addr}: {exc}" for addr, exc in errors)
            # Preserve the prerequisite type when that's what every failure
            # was, so callers can still distinguish a prerequisite problem
            # from a general failure.
            cls = self.error_cls
            if all(isinstance(exc, self.prerequisite_error_cls) for _, exc in errors):
                cls = self.prerequisite_error_cls
            raise cls(f"{label} failed for {len(errors)} node(s):\n{detail}")

        return results

    @staticmethod
    def _parse_int(raw: str) -> Optional[int]:
        """Parses `raw` (stripped) as an int, or `None` if it isn't one —
        used for command output that might be empty/unparseable."""
        try:
            return int(raw.strip())
        except (TypeError, ValueError):
            return None
