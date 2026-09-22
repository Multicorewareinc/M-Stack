"""
Seeds the platform's first super-admin, and one organization with its
owner user, on an already-running cluster — normally right after
`examples/full_stack.py`.

Why this is its own script rather than a stage of full_stack.py: that
file composes *infrastructure* (17 capabilities, safely re-runnable —
its own docstring calls every stage idempotent), and this is
*application data*. Re-running full_stack.py must never touch it, the
same way a `terraform apply` must never re-seed a database it happens
to sit in front of. Keeping the two apart is what lets full_stack.py
stay safe to re-run against a live, populated cluster.

Two different things happen here, and only the first is genuinely
one-time:

  1. Bootstrap the super-admin — literally invokes
     `api/microservices/admin-control-plane/src/seed_admin.py`, an
     operator-run CLI that already ships in that service's own image
     (SP-03). Single-use by design (a Postgres advisory lock plus an
     emptiness check on `admin_users`): a second run against an
     already-seeded database refuses rather than creating a second
     super-admin, so re-running *this* step is always safe.
  2. Create one organization and its owner user, through the API the
     super-admin now has access to. Not single-use the same way —
     nothing stops two organizations sharing a name — so this script
     lists existing organizations first (GET /v1/organizations) and
     skips creation if SEED_ORG_NAME already exists, rather than
     risking a duplicate on every re-run.

Everything here goes through `kubectl exec` into the already-running
admin-control-plane pod — the same "no direct connection, only through
kubectl" discipline `multistack.nats.deployment.NatsAdminClient`
already uses for NATS. That means this script needs no port-forward,
no cluster DNS reachable from wherever it runs, and no new network
path: the pod calls its own `localhost:8000`.

    MULTISTACK_KUBECONFIG=~/.multistack/kubeconfig \\
    SEED_ADMIN_EMAIL=admin@example.com \\
    SEED_ADMIN_PASSWORD=...                        \\
    SEED_ORG_NAME="Acme"                            \\
    SEED_ORG_OWNER_EMAIL=owner@acme.example         \\
    SEED_ORG_OWNER_USERNAME=owner                   \\
    SEED_ORG_OWNER_PASSWORD=...                     \\
    python3 examples/seed_admin.py

Every *_PASSWORD is read from its env var, or generated (and printed
once, like full_stack.py's own MinIO/Grafana credentials) when unset
and no terminal is attached to prompt — see
`multistack.credentials.prompt_secret`. SEED_ORG_NAME/_PLAN default to
"Acme Inc" / "Free" (one of the three tiers SEED_PLANS always creates,
api/microservices/admin-control-plane/src/seed.py) so this runs
unattended with nothing set beyond the two passwords.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import textwrap

from multistack.controlplane import AdminControlPlaneOptions, ControlPlane
from multistack.credentials import prompt_secret
from multistack.kube import KubeCommandError, kubectl, run_local

KUBECONFIG = os.environ.get(
    "MULTISTACK_KUBECONFIG", os.path.expanduser("~/.multistack/kubeconfig"))

# Read off the SDK's own spec rather than named again here, the same
# reason stage_controlplane() in full_stack.py does — a renamed release
# or namespace only has to change in one place.
NAMESPACE = ControlPlane.DEFAULT_NAMESPACES["admin"]
RELEASE_NAME = AdminControlPlaneOptions().release_name
POD_SELECTOR = f"app.kubernetes.io/name={RELEASE_NAME}"


class SeedError(RuntimeError):
    """Something here failed; nothing further was attempted."""


def _admin_pod() -> str:
    out = kubectl(
        KUBECONFIG, "get", "pods", "-n", NAMESPACE, "-l", POD_SELECTOR,
        "-o", "jsonpath={.items[0].metadata.name}",
        error_cls=SeedError,
    ).strip()
    if not out:
        raise SeedError(
            f"no {RELEASE_NAME} pod running in {NAMESPACE} — run "
            "examples/full_stack.py (at least through the controlplane "
            "stage) first"
        )
    return out


def _exec_py(pod: str, script: str, *, env: dict | None = None) -> str:
    """Runs `script` as `python3` inside `pod`, fed over stdin.

    No file ever touches the pod's disk or this machine's: the script
    is piped straight into the interpreter, the same way
    `multistack.kube.apply()` pipes a manifest into `kubectl apply -f
    -` rather than writing one out first.
    """
    argv = ["kubectl", "--kubeconfig", KUBECONFIG, "exec", "-i",
            "-n", NAMESPACE, pod, "--"]
    if env:
        # `kubectl exec` has no --env of its own; a `sh -c` prefix that
        # assigns each var is the same trick full_stack.py's own Secret
        # data dicts avoid needing, because those go through a manifest
        # instead of a shell.
        assignments = " ".join(
            f"{k}={_shell_quote(v)}" for k, v in env.items())
        argv += ["sh", "-c", f"{assignments} exec python3"]
    else:
        argv += ["python3"]
    return run_local(argv, stdin_text=script, error_cls=SeedError,
                      label=f"kubectl exec {pod}")


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def bootstrap_super_admin(email: str, bootstrap_password: str) -> bool:
    """Invokes the service's own `seed_admin` CLI inside its pod.

    `bootstrap_password` is throwaway, never the operator's real
    password: seed_admin.py always creates the account with
    must_change_password=TRUE (SP-02), and the service rejects a
    password "change" that isn't actually a change — so whatever this
    account is created with can never be logged into a second time
    regardless. `seed_organization` rotates it away in the same run.

    Exit codes are that module's own (EXIT_OK/EXIT_REFUSED/
    EXIT_TRANSIENT): 0 means a super-admin was just created (returns
    True); 2 means it refused (almost always because one already
    exists — the single-use guard this whole script leans on to stay
    re-run-safe; returns False); anything else is a real failure.
    `run_local` only ever returns stdout or raises, so this needs the
    raw `subprocess.run` underneath it to see which of the three
    happened.
    """
    import subprocess

    pod = _admin_pod()
    argv = ["kubectl", "--kubeconfig", KUBECONFIG, "exec", "-n", NAMESPACE, pod,
            "--", "sh", "-c",
            f"SEED_ADMIN_EMAIL={_shell_quote(email)} "
            f"SEED_ADMIN_PASSWORD={_shell_quote(bootstrap_password)} "
            "python -m seed_admin"]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise SeedError(f"seed_admin bootstrap: {exc}") from exc

    if proc.returncode == 0:
        print(f"[seed] super-admin {email} created")
        return True
    if proc.returncode == 2:
        print(f"[seed] super-admin bootstrap declined: "
              f"{(proc.stderr or proc.stdout).strip()}")
        print("[seed] treating this as already-seeded and continuing")
        return False
    raise SeedError(
        f"seed_admin exited {proc.returncode}: "
        f"{(proc.stderr or proc.stdout).strip()}"
    )


# Talks to the admin-control-plane API entirely from *inside* its own
# pod (http://localhost:8000) — see the module docstring's "no direct
# connection" rationale. stdlib-only (urllib): this runs inside a
# plain python:3.12-slim image, and nothing guarantees httpx's sync
# client is exercised there the same way its async one is.
_SEED_ORG_SCRIPT = textwrap.dedent("""
    import json
    import os
    import sys
    import time
    import urllib.error
    import urllib.request

    BASE = "http://localhost:8000"


    def call(method, path, *, token=None, body=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            BASE + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")


    email = os.environ["SEED_ADMIN_EMAIL"]
    password = os.environ["SEED_ADMIN_PASSWORD"]
    just_created = os.environ["SEED_ADMIN_JUST_CREATED"] == "1"
    bootstrap_password = os.environ.get("SEED_ADMIN_BOOTSTRAP_PASSWORD", "")
    org_name = os.environ["SEED_ORG_NAME"]
    plan_name = os.environ["SEED_PLAN_NAME"]
    owner = {
        "username": os.environ["SEED_ORG_OWNER_USERNAME"],
        "email": os.environ["SEED_ORG_OWNER_EMAIL"],
        "password": os.environ["SEED_ORG_OWNER_PASSWORD"],
    }

    # seed_admin's own CLI always creates an account with
    # must_change_password=TRUE (SP-02) — a brand-new account cannot
    # log in with its bootstrap password a second time (the service
    # rejects a no-op password "change"), so the very first login uses
    # the throwaway bootstrap password and immediately rotates to the
    # real one. A re-run against an already-seeded cluster skips all of
    # that: SEED_ADMIN_PASSWORD is already the live, rotated password.
    login_password = bootstrap_password if just_created else password
    status, body = call("POST", "/api/auth/login",
                         body={"email": email, "password": login_password})
    if status != 200:
        print(f"login failed ({status}): {body}", file=sys.stderr)
        sys.exit(1)
    token = body["access_token"]

    if just_created:
        status, resp = call(
            "POST", "/api/auth/password", token=token,
            body={"current_password": bootstrap_password,
                  "new_password": password},
        )
        if status != 204:
            print(f"password rotation failed ({status}): {resp}",
                  file=sys.stderr)
            sys.exit(1)
        # Rotation revokes refresh tokens, not the still-live access
        # token this request already carries — no re-login needed.

    # /v1/organizations and /v1/plans are require_admin_key routes
    # (the static ADMIN_API_KEY, normally) but that dependency also
    # accepts a valid JWT opportunistically -- so the super-admin's own
    # login token works here too, and this script never needs to read
    # ADMIN_API_KEY back out of its Secret.
    def find_org():
        status, orgs = call("GET", "/v1/organizations", token=token)
        if status != 200:
            print(f"listing organizations failed ({status}): {orgs}",
                  file=sys.stderr)
            sys.exit(1)
        return next((o for o in orgs if o.get("name") == org_name), None)

    org = find_org()
    already_had_owner = org is not None and org.get("user_count", 0) > 0

    # organization-control-plane runs multiple replicas, and the calls
    # admin-control-plane makes in sequence for a brand-new org (create
    # org, create its owner, look up + assign the org_admin role) can
    # each land on a different one a request apart -- if that replica's
    # session hasn't yet seen an earlier step's just-committed row, the
    # step 404s or 422s even though the previous one actually succeeded.
    # Retried here rather than in admin-control-plane's own
    # (synchronous, single-shot) workflow: short retry loops that
    # re-check what actually landed are enough to ride out replica lag,
    # and re-checking is what makes it safe to retry at all -- a blind
    # resend of "create this org" would either duplicate it or 409, not
    # fix a merely-incomplete one.
    owner_user_id = None
    last_error = None
    if not already_had_owner:
        for attempt in range(3):
            if org is None:
                status, plans = call("GET", "/v1/plans", token=token)
                if status != 200:
                    print(f"listing plans failed ({status}): {plans}",
                          file=sys.stderr)
                    sys.exit(1)
                plan = next((p for p in plans if p.get("name") == plan_name), None)
                if plan is None:
                    names = [p.get("name") for p in plans]
                    print(f"no plan named {plan_name!r} — have {names}",
                          file=sys.stderr)
                    sys.exit(1)

                status, created = call(
                    "POST", "/v1/organizations/with-owner", token=token,
                    body={"name": org_name, "plan_id": plan["id"], "owner": owner},
                )
                if status in (200, 201):
                    org = created["organization"]
                    owner_user_id = created["owner"]["id"]
                    break
                last_error = (status, created)
                # The org itself may have been created despite the
                # failure (see above) -- re-check rather than assume,
                # so the next iteration retries only what's missing.
                org = find_org()
            else:
                status, user = call(
                    "POST", f"/v1/organizations/{org['id']}/users",
                    token=token, body=owner,
                )
                if status == 201:
                    owner_user_id = user["id"]
                    break
                if status == 409:
                    # The owner already exists for this org -- a
                    # previous attempt (this run or an earlier one)
                    # already finished this step.
                    already_had_owner = True
                    break
                last_error = (status, user)

            if attempt < 2:
                time.sleep(2)
        else:
            print(f"creating organization/owner failed after 3 attempts: "
                  f"{last_error}", file=sys.stderr)
            sys.exit(1)

    # owner_user_id is only unset here when the owner already existed
    # (either from before this run, or a 409 just now) -- look it up so
    # the role-ensure step below still runs. It matters even for a
    # pre-existing owner: role assignment is its own separate call and
    # can be the specific step that raced and never landed (this is
    # exactly what happened seeding this script's first real org).
    if owner_user_id is None:
        status, users = call(
            "GET", f"/v1/organizations/{org['id']}/users", token=token)
        if status != 200:
            print(f"listing users failed ({status}): {users}", file=sys.stderr)
            sys.exit(1)
        match = next((u for u in users if u.get("email") == owner["email"]), None)
        if match is None:
            print(f"owner {owner['email']!r} not found in org {org['id']} "
                  "after creation", file=sys.stderr)
            sys.exit(1)
        owner_user_id = match["id"]

    # Talks to organization-control-plane directly (its own
    # SERVICE_API_KEY, already this pod's own env -- admin-control-plane
    # is calling itself here in the same way its own org_client does),
    # not back through admin-control-plane: the role list/assign pair
    # has no super-admin-facing proxy at all, only the internal one
    # _assign_org_admin already uses and that this mirrors.
    ORG_BASE = "http://organization-control-plane.control-plane.svc.cluster.local:8000"
    service_key = os.environ["SERVICE_API_KEY"]

    def call_org(method, path, *, body=None):
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {service_key}",
            "X-Organization-Id": org["id"],
        }
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            ORG_BASE + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    role_assigned = False
    role_error = None
    for attempt in range(3):
        status, roles = call_org("GET", "/v1/roles")
        if status == 200:
            role = next((r for r in roles if r.get("name") == "org_admin"), None)
            if role is not None:
                status, resp = call_org(
                    "PUT", f"/v1/users/{owner_user_id}/roles",
                    body={"role_ids": [role["id"]]},
                )
                if status == 200:
                    role_assigned = True
                    break
                role_error = (status, resp)
            else:
                role_error = (status, f"no org_admin role in {roles}")
        else:
            role_error = (status, roles)
        if attempt < 2:
            time.sleep(2)

    if not role_assigned:
        print(f"org_admin role assignment failed after 3 attempts: "
              f"{role_error}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps({"skipped": already_had_owner, "organization": org}))
""")


def seed_organization(
    email: str, password: str, *, just_created: bool, bootstrap_password: str,
    org_name: str, plan_name: str,
    owner_username: str, owner_email: str, owner_password: str,
) -> dict:
    pod = _admin_pod()
    out = _exec_py(pod, _SEED_ORG_SCRIPT, env={
        "SEED_ADMIN_EMAIL": email,
        "SEED_ADMIN_PASSWORD": password,
        "SEED_ADMIN_JUST_CREATED": "1" if just_created else "0",
        "SEED_ADMIN_BOOTSTRAP_PASSWORD": bootstrap_password,
        "SEED_ORG_NAME": org_name,
        "SEED_PLAN_NAME": plan_name,
        "SEED_ORG_OWNER_USERNAME": owner_username,
        "SEED_ORG_OWNER_EMAIL": owner_email,
        "SEED_ORG_OWNER_PASSWORD": owner_password,
    })
    return json.loads(out.strip().splitlines()[-1])


def main() -> int:
    email = prompt_secret(
        "Super-admin email", env_var="SEED_ADMIN_EMAIL",
        value=os.environ.get("SEED_ADMIN_EMAIL", "admin@example.com"),
    )
    password = prompt_secret(
        "Super-admin password", env_var="SEED_ADMIN_PASSWORD",
        generate=True, confirm=True,
    )
    org_name = os.environ.get("SEED_ORG_NAME", "Acme Inc")
    plan_name = os.environ.get("SEED_PLAN_NAME", "Free")
    owner_username = os.environ.get("SEED_ORG_OWNER_USERNAME", "owner")
    owner_email = os.environ.get(
        "SEED_ORG_OWNER_EMAIL", "owner@example.com")
    owner_password = prompt_secret(
        f"Owner password for {org_name!r}",
        env_var="SEED_ORG_OWNER_PASSWORD", generate=True, confirm=True,
    )

    # Never the operator's real password (see bootstrap_super_admin's
    # docstring) -- generated fresh every run, used at most once, and
    # never printed.
    bootstrap_password = secrets.token_urlsafe(24)

    try:
        just_created = bootstrap_super_admin(email, bootstrap_password)
        result = seed_organization(
            email, password, just_created=just_created,
            bootstrap_password=bootstrap_password,
            org_name=org_name, plan_name=plan_name,
            owner_username=owner_username, owner_email=owner_email,
            owner_password=owner_password,
        )
    except (SeedError, KubeCommandError) as exc:
        print(f"[seed] failed: {exc}", file=sys.stderr)
        return 1

    org = result["organization"]
    if result["skipped"]:
        print(f"[seed] organization {org_name!r} already exists "
              f"(id={org.get('id')}) — left untouched")
    else:
        print(f"[seed] organization {org_name!r} created "
              f"(id={org.get('id')}, plan={plan_name!r})")
        print(f"[seed] owner login: {owner_email} "
              f"(username={owner_username})")

    print(f"[seed] super-admin login: {email}")
    print("[seed] store these — passwords are generated per run and not "
          "recoverable from this output")
    return 0


if __name__ == "__main__":
    sys.exit(main())
