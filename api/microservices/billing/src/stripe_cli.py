"""Dev-only Stripe CLI webhook forwarder. Auto-starts `stripe listen --forward-to
.../webhooks/stripe` in the app lifespan so a developer doesn't have to run it by hand before
testing webhooks locally (ADR-006 rule 3 still applies: the endpoint stays inert until
STRIPE_WEBHOOK_SECRET is set — this only saves the manual `stripe listen` step).

Requires the Stripe CLI binary (https://stripe.com/docs/stripe-cli) on PATH. Never runs unless
explicitly enabled — production deployments register a real webhook endpoint instead, they don't
shell out to a CLI forwarder.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess

logger = logging.getLogger(__name__)

# Must track webhooks.HANDLED_EVENT_TYPES exactly — forwarding events the handler ignores
# just adds noise to local logs.
FORWARDED_EVENT_TYPES = (
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "invoice.payment_failed",
)


class StripeCLIManager:
    """Manages `stripe listen` as a subprocess."""

    def __init__(self, forward_to: str, api_key: str = "") -> None:
        self._forward_to = forward_to
        self._api_key = api_key
        self.process: subprocess.Popen | None = None
        self._monitor_task: asyncio.Task | None = None

    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def get_pid(self) -> int | None:
        return self.process.pid if self.process else None

    def _build_command(self) -> list[str]:
        cmd = ["stripe", "listen", "--forward-to", self._forward_to]
        for event in FORWARDED_EVENT_TYPES:
            cmd.extend(["--events", event])
        if self._api_key:
            cmd.extend(["--api-key", self._api_key])
        return cmd

    def start(self) -> bool:
        if self.is_running():
            logger.info("stripe_cli.already_running pid=%s", self.process.pid)
            return True

        try:
            subprocess.run(["stripe", "--version"], capture_output=True, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            logger.error(
                "stripe_cli.not_found install from https://stripe.com/docs/stripe-cli"
            )
            return False

        cmd = self._build_command()
        try:
            popen_kwargs: dict = dict(
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1
            )
            if os.name == "nt":
                popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                popen_kwargs["preexec_fn"] = os.setsid
            self.process = subprocess.Popen(cmd, **popen_kwargs)
        except Exception:
            logger.exception("stripe_cli.start_failed")
            return False

        logger.info("stripe_cli.started pid=%s forward_to=%s", self.process.pid, self._forward_to)
        self._monitor_task = asyncio.create_task(self._monitor_output())
        return True

    async def _monitor_output(self) -> None:
        if not self.process:
            return
        loop = asyncio.get_event_loop()
        while self.process and self.process.poll() is None:
            try:
                if self.process.stdout:
                    line = await loop.run_in_executor(None, self.process.stdout.readline)
                    if line:
                        logger.info("stripe_cli: %s", line.strip())
                if self.process.stderr:
                    line = await loop.run_in_executor(None, self.process.stderr.readline)
                    if line:
                        logger.warning("stripe_cli: %s", line.strip())
            except Exception:
                logger.exception("stripe_cli.monitor_error")
                break
            await asyncio.sleep(0.1)

    def stop(self) -> None:
        if not self.process:
            return
        pid = self.process.pid
        try:
            if os.name == "nt":
                self.process.terminate()
            else:
                try:
                    os.killpg(os.getpgid(pid), signal.SIGTERM)
                except ProcessLookupError:
                    self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    self.process.kill()
                else:
                    try:
                        os.killpg(os.getpgid(pid), signal.SIGKILL)
                    except ProcessLookupError:
                        self.process.kill()
            logger.info("stripe_cli.stopped pid=%s", pid)
        except Exception:
            logger.exception("stripe_cli.stop_failed pid=%s", pid)
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
        self.process = None
        self._monitor_task = None
