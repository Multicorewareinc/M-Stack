#!/usr/bin/env bash
#
# Deploy the API layer and verify its NATS wiring.
#
# The API layer is an event pipeline, not a set of independent services:
# the gateway publishes to JetStream, the enricher consumes that and
# republishes a derived stream, and the tpm limiter and billing consume
# the derived one (ADR-030). Deploying the services is the easy half --
# this script's real job is proving every durable consumer actually bound,
# because a service whose consumer failed to bind stays Running and Ready
# while silently metering nothing.
#
#   ./scripts/deploy-api.sh                 # preflight, deploy, verify
#   ./scripts/deploy-api.sh --verify-only   # verify a deployment that exists
#   ./scripts/deploy-api.sh --dry-run       # preflight only, change nothing
#
# See docs/mop-api-deploy.md for the procedure this automates, including
# what to do when a step fails.
set -euo pipefail

KUBECONFIG_PATH="${MULTISTACK_KUBECONFIG:-$HOME/.multistack/kubeconfig}"
NATS_NAMESPACE="${MULTISTACK_NATS_NAMESPACE:-platform}"
BACKBONE_URL="${MULTISTACK_BACKBONE:-nats://nats.platform.svc:4222}"
NATS_BOX="${MULTISTACK_NATS_BOX:-nats-box}"

# The event path, in dependency order. queue must precede every consumer:
# a service that starts with no stream to bind to needs a restart, not
# just time. controlplane/portal are deliberately absent -- they serve the
# platform's own API and touch none of this wiring. Override to add them.
# `registry` leads: every first-party chart defaults to
# `imagePullSecrets: [ghcr-creds]`, the secret is namespace-scoped, and a
# pod that starts without it sits ImagePullBackOff with `unauthorized` --
# which reads like a missing image rather than a missing credential.
STAGES="${MULTISTACK_STAGES:-cache,database,registry,tokenizer,queue,enricher,policy,gateway,billing}"

PULL_SECRET="${MULTISTACK_PULL_SECRET:-ghcr-creds}"
# Namespaces this script's stages pull first-party images into. Not
# `platform` or `inference`: NATS and vLLM run public images and hold no
# pull secret.
PULL_SECRET_NAMESPACES="policy billing gateway"

# Where the first-party images are pulled FROM, passed to full_stack.py as
# MULTISTACK_IMAGE_REGISTRY. The charts carry a default too, but a chart
# default belongs to the chart, not to this deployment: deploying from a
# fork or from the public repo must not keep pulling another account's
# images. Derived from `origin` so it follows whichever repo you are in,
# the same rule the Makefile's GH_OWNER/GH_REPO use.
IMAGE_REGISTRY="${MULTISTACK_IMAGE_REGISTRY:-}"
if [ -z "$IMAGE_REGISTRY" ]; then
    _url="$(git config --get remote.origin.url 2>/dev/null || true)"
    _owner="$(printf '%s' "$_url" | sed -E 's,^(git@|https://|ssh://git@)github\.com[:/]+([^/]+)/.*,\2,')"
    _repo="$(basename "$_url" .git 2>/dev/null || true)"
    if [ -n "$_owner" ] && [ -n "$_repo" ] && [ "$_owner" != "$_url" ]; then
        IMAGE_REGISTRY="ghcr.io/$(printf '%s/%s' "$_owner" "$_repo" | tr '[:upper:]' '[:lower:]')"
    fi
fi

RAW_STREAM="GATEWAY_EVENTS"
ENRICHED_STREAM="GATEWAY_EVENTS_ENRICHED"

# stream -> durable consumers that must be bound to it. These names come
# from the SDK's spec defaults, NOT the charts' own values.yaml: the tpm
# chart defaults to the raw stream, and multistack/policy/spec.py
# overrides it to the enriched one. Deploying a chart directly with helm
# would wire tpm to the wrong stream and it would still look healthy.
RAW_CONSUMERS="enricher rpm-counter"
ENRICHED_CONSUMERS="tpm-counter-enriched billing-metering"

# Services that hold a durable JetStream push consumer. JetStream allows
# one subscription per durable, so a second replica cannot bind and sits
# there doing nothing -- see api/microservices/enricher/chart/values.yaml.
#
# Addressed by `app.kubernetes.io/name` label rather than by Deployment
# name: the charts' fullname template makes those depend on the release
# name (release `tpm` + chart `rate-limiter-tpm` renders
# `tpm-rate-limiter-tpm`), so a renamed release would silently skip the
# check. Note all three limiters/enricher live in `policy`, not one
# namespace each.
SINGLETON_CONSUMERS="enricher:policy rate-limiter-tpm:policy rate-limiter-rpm:policy billing:billing"

DRY_RUN=false
VERIFY_ONLY=false
FAILURES=0

for arg in "$@"; do
    case "$arg" in
        --dry-run)     DRY_RUN=true ;;
        --verify-only) VERIFY_ONLY=true ;;
        -h|--help)     sed -n '2,18p' "$0" | sed 's/^# \?//'; exit 0 ;;
        *)             echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '  \033[32mok\033[0m    %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; FAILURES=$((FAILURES + 1)); }
warn() { printf '  \033[33mwarn\033[0m  %s\n' "$*"; }

kc() { kubectl --kubeconfig "$KUBECONFIG_PATH" "$@"; }

# `nats` has no binary on the host; it lives in the nats-box admin pod the
# SDK leaves running, which is how multistack.queue.drivers.nats.deployment reaches it.
nats_cli() {
    kc exec -n "$NATS_NAMESPACE" "$NATS_BOX" -- \
        nats --server "$BACKBONE_URL" "$@" 2>/dev/null
}

# `nats ... ls -j` prints a bare JSON array of names -- and the literal
# `null`, not `[]`, when a stream list is empty, which is exactly the
# from-empty first deploy. Same handling as
# multistack/queue/drivers/nats/deployment/streams.py.
json_names() {
    python3 -c 'import json,sys
raw = sys.stdin.read().strip()
if raw:
    parsed = json.loads(raw)
    for n in (parsed or []):
        print(n)' 2>/dev/null || true
}

# ------------------------------------------------------------- preflight --
say "Preflight"

for tool in kubectl helm python3; do
    if command -v "$tool" >/dev/null 2>&1; then
        ok "$tool on PATH"
    else
        bad "$tool not on PATH"
        case "$tool" in kubectl|helm)
            warn "the cluster stage installs it on the control-plane node; for a"
            warn "cluster built before it did: RKE2Backend().prepare_control_plane(<name>)" ;;
        esac
    fi
done

if [ -r "$KUBECONFIG_PATH" ]; then
    ok "kubeconfig readable at $KUBECONFIG_PATH"
else
    bad "kubeconfig not readable at $KUBECONFIG_PATH (set MULTISTACK_KUBECONFIG)"
fi

if [ "$FAILURES" -eq 0 ] && kc get nodes --request-timeout=10s >/dev/null 2>&1; then
    ok "cluster reachable ($(kc get nodes --no-headers --request-timeout=10s 2>/dev/null | wc -l | tr -d ' ') nodes)"
else
    bad "cluster not reachable with this kubeconfig"
fi

# Everything above goes through MULTISTACK_KUBECONFIG. The commands in
# docs/mop-api-deploy.md §4, §6 and §7 are a bare kubectl/helm, which read
# $KUBECONFIG or ~/.kube/config instead -- so without this a deploy could
# land on one cluster and be "verified" or rolled back on another, with
# nothing to say so. Compared by kube-system's UID rather than the server
# URL: the same cluster is 127.0.0.1:6443 in one file and the node's IP in
# another, and two rebuilt clusters can share a URL.
if [ "$FAILURES" -eq 0 ]; then
    cluster_uid() { "$@" get namespace kube-system -o jsonpath='{.metadata.uid}' --request-timeout=10s 2>/dev/null || true; }
    sdk_uid="$(cluster_uid kc)"
    bare_uid="$(cluster_uid kubectl)"
    if [ -z "$bare_uid" ]; then
        warn "a bare kubectl reaches no cluster, so the MOP's manual commands won't"
        warn "work. Align it: ln -sfn \"$KUBECONFIG_PATH\" ~/.kube/config"
    elif [ "$bare_uid" = "$sdk_uid" ]; then
        ok "a bare kubectl/helm targets the same cluster"
    else
        bad "a bare kubectl reaches a DIFFERENT cluster than $KUBECONFIG_PATH"
        warn "verify and rollback commands would act on the wrong cluster. Align it:"
        warn "  ln -sfn \"$KUBECONFIG_PATH\" ~/.kube/config   (and unset KUBECONFIG)"
    fi
fi

# The registry stage prints "assuming the packages are public" and
# returns when MULTISTACK_GHCR_USER/_TOKEN are unset -- it does not fail.
# So an unset token surfaces later as ImagePullBackOff rather than here,
# unless the secrets already exist from a previous run.
if [ "$VERIFY_ONLY" = false ]; then
    missing_secret=""
    for ns in $PULL_SECRET_NAMESPACES; do
        kc get secret "$PULL_SECRET" -n "$ns" --request-timeout=10s >/dev/null 2>&1 \
            || missing_secret="$missing_secret $ns"
    done
    if [ -z "$missing_secret" ]; then
        ok "pull secret $PULL_SECRET present in:$(printf ' %s' $PULL_SECRET_NAMESPACES)"
    elif [ -n "${MULTISTACK_GHCR_USER:-}" ] && [ -n "${MULTISTACK_GHCR_TOKEN:-}" ]; then
        ok "pull secret absent in$missing_secret — the registry stage will create it"
    else
        bad "pull secret $PULL_SECRET missing in$missing_secret, and MULTISTACK_GHCR_USER/_TOKEN are unset"
        warn "the registry stage skips silently without them; pods would land ImagePullBackOff"
    fi
fi

# An unreachable registry is the other way pods land ImagePullBackOff, and
# like a missing pull secret it shows up minutes into a deploy rather than
# here. Reported always; only failed on when it is set and wrong.
if [ -n "$IMAGE_REGISTRY" ]; then
    ok "images from $IMAGE_REGISTRY"
else
    warn "no image registry derived (not a git checkout?) — each chart's own"
    warn "values.yaml default will be used, which may not be where your"
    warn "images actually are. Set MULTISTACK_IMAGE_REGISTRY to be explicit."
fi

# Two stage combinations no-op or hang instead of failing, so they are
# caught here rather than discovered halfway through a deploy.
case ",$STAGES," in
  *,billing,*)
    case ",$STAGES," in
      *,database,*) ;;
      *) bad "billing is in STAGES but database is not — full_stack.py skips"
         warn "billing entirely when the database stage did not run (it needs"
         warn "an authenticated DATABASE_URL_BILLING only that stage produces), and"
         warn "still exits 0. Add database to MULTISTACK_STAGES, or drop billing." ;;
    esac ;;
esac

case ",$STAGES," in
  *,policy,*|*,controlplane,*)
    case ",$STAGES," in
      *,cache,*) ;;
      *) if [ -z "${MULTISTACK_CACHE_PASSWORD:-}" ]; then
             bad "policy/controlplane without the cache stage rebuilds the cache URL"
             warn "from MULTISTACK_CACHE_PASSWORD, and prompts on stdin when unset —"
             warn "which hangs a non-interactive run. Export it, or add cache to STAGES."
         fi ;;
    esac ;;
esac

if [ "$FAILURES" -gt 0 ]; then
    echo; echo "Preflight failed. Fix the above before deploying." >&2
    exit 1
fi

if [ "$DRY_RUN" = true ]; then
    echo; echo "Dry run: would deploy stages [$STAGES] from ${IMAGE_REGISTRY:-chart defaults}, then verify. Nothing changed."
    exit 0
fi

# ---------------------------------------------------------------- deploy --
if [ "$VERIFY_ONLY" = false ]; then
    say "Deploy ($STAGES)"
    # Each stage is idempotent and adopts whatever is already live, so a
    # re-run after a partial failure resumes rather than duplicating.
    MULTISTACK_STAGES="$STAGES" \
    MULTISTACK_KUBECONFIG="$KUBECONFIG_PATH" \
    MULTISTACK_IMAGE_REGISTRY="$IMAGE_REGISTRY" \
        python3 examples/full_stack.py
fi

# ---------------------------------------------------------------- verify --
say "Verify: NATS backbone"

# Discovered, not assumed. Two different nats-box pods can exist: the
# NATS chart's own Deployment-managed one, and the bare `nats-box` Pod
# multistack.queue.drivers.nats.deployment.client creates. The latter runs `sleep 86400`
# with restartPolicy: Never, so it Completes after 24h and `kubectl exec`
# into it fails -- while `kubectl get pod` still finds it. Phase is what
# matters here, not existence.
NATS_BOX="$(kc get pods -n "$NATS_NAMESPACE" \
    -o jsonpath='{range .items[?(@.status.phase=="Running")]}{.metadata.name}{"\n"}{end}' \
    2>/dev/null | grep '^nats-box' | head -1 || true)"

if [ -n "$NATS_BOX" ]; then
    ok "admin pod $NATS_BOX Running in $NATS_NAMESPACE"
else
    bad "no Running nats-box pod in $NATS_NAMESPACE — cannot inspect JetStream"
    stale="$(kc get pods -n "$NATS_NAMESPACE" -o name 2>/dev/null | grep 'nats-box' || true)"
    [ -n "$stale" ] && warn "found but not Running: $stale (delete it so the SDK recreates it)"
    echo; echo "Verification cannot continue without a Running admin pod." >&2
    exit 1
fi

STREAMS="$(nats_cli stream ls -j | json_names || true)"
for stream in "$RAW_STREAM" "$ENRICHED_STREAM"; do
    if printf '%s\n' "$STREAMS" | grep -qx "$stream"; then
        ok "stream $stream exists"
    else
        bad "stream $stream missing — the queue stage did not complete"
    fi
done

say "Verify: durable consumers bound"

check_consumers() {
    local stream="$1"; shift
    local found
    found="$(nats_cli consumer ls "$stream" -j | json_names || true)"
    for durable in $@; do
        if printf '%s\n' "$found" | grep -qx "$durable"; then
            ok "$stream -> $durable bound"
        else
            bad "$stream -> $durable NOT bound (service is up but consuming nothing)"
        fi
    done
}

check_consumers "$RAW_STREAM"      $RAW_CONSUMERS
check_consumers "$ENRICHED_STREAM" $ENRICHED_CONSUMERS

say "Verify: single-replica consumers"

# A durable allows one subscription, so a second replica binds nothing.
# It will not crash and it will not log an obvious error -- it just never
# processes a message, which shows up later as usage that was never metered.
for entry in $SINGLETON_CONSUMERS; do
    app="${entry%%:*}"
    ns="${entry##*:}"
    replicas="$(kc get deploy -n "$ns" -l "app.kubernetes.io/name=$app" \
                -o jsonpath='{.items[*].spec.replicas}' 2>/dev/null || true)"
    if [ -z "$replicas" ]; then
        warn "$app not found in $ns (stage skipped?)"
    elif [ "$replicas" = "1" ]; then
        ok "$app replicas=1"
    else
        bad "$app replicas=$replicas — only one pod can hold its durable; scale to 1"
    fi
done

say "Verify: consumer backlog"

# num_pending climbing across runs means a consumer is bound but not
# keeping up, or is bound and failing to ack. Reported, never failed on:
# a healthy pipeline under load has a non-zero backlog too.
for stream in "$RAW_STREAM" "$ENRICHED_STREAM"; do
    for durable in $(nats_cli consumer ls "$stream" -j | json_names || true); do
        pending="$(nats_cli consumer info "$stream" "$durable" -j 2>/dev/null \
                   | python3 -c 'import json,sys; print(json.load(sys.stdin).get("num_pending","?"))' 2>/dev/null || echo "?")"
        printf '  %-14s %-24s num_pending=%s\n' "$stream" "$durable" "$pending"
    done
done

say "Verify: image pulls"

for ns in $PULL_SECRET_NAMESPACES; do
    if kc get secret "$PULL_SECRET" -n "$ns" >/dev/null 2>&1; then
        ok "$ns has $PULL_SECRET"
    else
        bad "$ns missing $PULL_SECRET — first-party pods there cannot pull"
    fi
done

# The secret existing is not the same as it working: a wrong or expired
# token produces the identical `unauthorized` backoff as a missing one.
stuck="$(kc get pods -A \
    -o jsonpath='{range .items[*]}{.metadata.namespace}{"/"}{.metadata.name}{" "}{.status.containerStatuses[*].state.waiting.reason}{"\n"}{end}' \
    2>/dev/null | grep -E 'ImagePullBackOff|ErrImagePull' || true)"
if [ -z "$stuck" ]; then
    ok "no pod stuck on an image pull"
else
    bad "pods stuck pulling images:"
    printf '%s\n' "$stuck" | sed 's/^/          /'
fi

# ----------------------------------------------------------------- result --
echo
if [ "$FAILURES" -eq 0 ]; then
    printf '\033[32mAPI layer deployed and NATS wiring verified.\033[0m\n'
    exit 0
fi
printf '\033[31m%d check(s) failed — see docs/mop-api-deploy.md for each one.\033[0m\n' "$FAILURES"
exit 1
