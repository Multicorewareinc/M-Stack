#!/usr/bin/env bash
#
# postgres.sh — Install PostgreSQL 16 on RKE2 (via CloudNativePG) and create
#               application databases for E2E testing.
#
# Usage:
#   ./scripts/e2e/postgres.sh up      # install CNPG + cluster + databases
#   ./scripts/e2e/postgres.sh down    # remove everything
#   ./scripts/e2e/postgres.sh status  # show current state + connection strings
#   ./scripts/e2e/postgres.sh env     # print env vars for export
#
# Required env:
#   KUBECONFIG    Path to a kubeconfig with cluster-admin on the target cluster.
#
# Optional env:
#   NAMESPACE           Namespace for the Postgres cluster. Default: postgres
#   CNPG_OPERATOR_NS    Namespace for the CNPG operator.    Default: cnpg-system
#   CLUSTER_NAME        CNPG Cluster name.                  Default: e2e-pg
#   PG_USER             Application user.                   Default: postgres
#   PG_PASSWORD         Application password.               Default: postgres
#   PG_VERSION          Postgres major version.             Default: 16
#   PG_INSTANCES        Number of instances.                Default: 1
#   PG_STORAGE_SIZE     PVC size.                           Default: 1Gi
#   STORAGE_CLASS       StorageClass.                       Default: longhorn
#   CNPG_VERSION        CNPG release tag.                   Default: v1.25.1
#
set -euo pipefail

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

NAMESPACE="${NAMESPACE:-postgres}"
CNPG_OPERATOR_NS="${CNPG_OPERATOR_NS:-cnpg-system}"
CLUSTER_NAME="${CLUSTER_NAME:-e2e-pg}"
PG_USER="${PG_USER:-postgres}"
PG_PASSWORD="${PG_PASSWORD:-postgres}"
PG_VERSION="${PG_VERSION:-16}"
PG_INSTANCES="${PG_INSTANCES:-1}"
PG_STORAGE_SIZE="${PG_STORAGE_SIZE:-1Gi}"
STORAGE_CLASS="${STORAGE_CLASS:-longhorn}"
CNPG_VERSION="${CNPG_VERSION:-v1.25.1}"

DATABASES=(
  "admin_control_plane"
  "org_control_plane"
  "billing_service"
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

log()  { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\n\033[1;33m[!]\033[0m %s\n' "$*" >&2; }
die()  { printf '\n\033[1;31m[✗]\033[0m %s\n' "$*" >&2; exit 1; }

need() {
  command -v "$1" >/dev/null 2>&1 || die "required tool not found: $1"
}

preflight() {
  need kubectl
  [ -n "${KUBECONFIG:-}" ] || die "KUBECONFIG is not set"
  [ -f "$KUBECONFIG" ] || die "KUBECONFIG does not point to a file: $KUBECONFIG"
  kubectl get nodes >/dev/null || die "cannot reach cluster with KUBECONFIG=$KUBECONFIG"
}

# ---------------------------------------------------------------------------
# up
# ---------------------------------------------------------------------------

install_operator() {
  log "Checking CloudNativePG operator in ${CNPG_OPERATOR_NS}"

  # Check BOTH the CRDs and the deployment; either one missing means
  # the install is incomplete and must be re-applied.
  if kubectl get crd clusters.postgresql.cnpg.io >/dev/null 2>&1 \
     && kubectl get deploy -n "$CNPG_OPERATOR_NS" -l app.kubernetes.io/name=cloudnative-pg >/dev/null 2>&1; then
    echo "CNPG operator already installed (CRDs + deployment present)."
    return 0
  fi

  log "Installing CloudNativePG operator ${CNPG_VERSION}"
  echo "  (CRD present: $(kubectl get crd clusters.postgresql.cnpg.io >/dev/null 2>&1 && echo yes || echo no))"
  echo "  (deploy present: $(kubectl get deploy -n "$CNPG_OPERATOR_NS" -l app.kubernetes.io/name=cloudnative-pg >/dev/null 2>&1 && echo yes || echo no))"

  kubectl apply --server-side -f \
    "https://raw.githubusercontent.com/cloudnative-pg/cloudnative-pg/release-1.25/releases/cnpg-1.25.1.yaml" \
    || die "failed to apply CNPG operator manifest"

  log "Waiting for CRDs to be Established"
  kubectl wait --for=condition=Established crd/clusters.postgresql.cnpg.io --timeout=2m \
    || die "CRD clusters.postgresql.cnpg.io did not become Established"

  log "Waiting for CNPG operator to become Ready"
  kubectl -n "$CNPG_OPERATOR_NS" rollout status deploy/cnpg-controller-manager --timeout=5m \
    || die "CNPG controller did not become Ready"

  kubectl -n "$CNPG_OPERATOR_NS" get deploy
}

ensure_namespace() {
  log "Ensuring namespace ${NAMESPACE}"
  if kubectl get namespace "$NAMESPACE" >/dev/null 2>&1; then
    echo "Namespace ${NAMESPACE} already exists."
  else
    kubectl create namespace "$NAMESPACE"
  fi
  kubectl label namespace "$NAMESPACE" \
    "app.kubernetes.io/part-of=multistack" \
    "multistack/component=postgres" \
    --overwrite >/dev/null
}

create_cluster() {
  log "Creating CloudNativePG Cluster ${CLUSTER_NAME} (PostgreSQL ${PG_VERSION})"

  kubectl apply -n "$NAMESPACE" -f - <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: ${CLUSTER_NAME}-app-user
type: kubernetes.io/basic-auth
stringData:
  username: ${PG_USER}
  password: ${PG_PASSWORD}
---
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: ${CLUSTER_NAME}
  labels:
    app.kubernetes.io/part-of: multistack
    multistack/component: postgres
spec:
  instances: ${PG_INSTANCES}
  imageName: ghcr.io/cloudnative-pg/postgresql:${PG_VERSION}
  storage:
    size: ${PG_STORAGE_SIZE}
    storageClass: ${STORAGE_CLASS}
  bootstrap:
    initdb:
      database: postgres
      owner: ${PG_USER}
      secret:
        name: ${CLUSTER_NAME}-app-user
  enableSuperuserAccess: true
  superuserSecret:
    name: ${CLUSTER_NAME}-app-user
  postgresql:
    parameters:
      max_connections: "200"
      shared_buffers: "256MB"
  resources:
    requests:
      cpu: 100m
      memory: 256Mi
    limits:
      memory: 512Mi
EOF

  log "Waiting for PostgreSQL to be Ready (up to 10m)"
  if ! kubectl wait --for=condition=Ready "cluster/${CLUSTER_NAME}" -n "$NAMESPACE" --timeout=10m; then
    kubectl -n "$NAMESPACE" describe "cluster/${CLUSTER_NAME}" || true
    kubectl -n "$NAMESPACE" get pods || true
    kubectl -n "$NAMESPACE" logs -l cnpg.io/cluster="$CLUSTER_NAME" --tail=200 || true
    die "PostgreSQL cluster did not become Ready"
  fi

  kubectl -n "$NAMESPACE" get cluster "$CLUSTER_NAME"
  kubectl -n "$NAMESPACE" get pods
}

create_databases() {
  log "Creating application databases"

  local primary_pod="${CLUSTER_NAME}-1"
  # Wait for the primary pod to accept connections.
  kubectl -n "$NAMESPACE" wait --for=condition=Ready "pod/${primary_pod}" --timeout=5m

  for db in "${DATABASES[@]}"; do
    echo "  -> ${db}"
    # The `postgres` database is the maintenance DB; create each app DB from it.
    # Use a heredoc through kubectl exec so we don't depend on a local psql.
    if kubectl -n "$NAMESPACE" exec -i "$primary_pod" -c postgres -- \
       psql -U "$PG_USER" -d postgres -v ON_ERROR_STOP=1 -c \
         "SELECT 1 FROM pg_database WHERE datname = '${db}'" \
       | grep -q 1; then
      echo "     already exists"
      continue
    fi

    kubectl -n "$NAMESPACE" exec -i "$primary_pod" -c postgres -- \
      psql -U "$PG_USER" -d postgres -v ON_ERROR_STOP=1 <<SQL
CREATE DATABASE ${db} OWNER ${PG_USER};
ALTER DATABASE ${db} OWNER TO ${PG_USER};
SQL
  done

  log "Databases present:"
  kubectl -n "$NAMESPACE" exec -i "$primary_pod" -c postgres -- \
    psql -U "$PG_USER" -d postgres -c '\l'
}

print_endpoints() {
  log "Connection endpoints"

  local svc_rw="${CLUSTER_NAME}-rw"
  local svc_ro="${CLUSTER_NAME}-ro"
  local svc_r="${CLUSTER_NAME}-r"

  echo "In-cluster hostnames (use these inside the same cluster):"
  echo "  read-write: ${svc_rw}.${NAMESPACE}.svc.cluster.local:5432"
  echo "  read-only : ${svc_ro}.${NAMESPACE}.svc.cluster.local:5432"
  echo "  any       : ${svc_r}.${NAMESPACE}.svc.cluster.local:5432"
  echo ""
  echo "Short forms (same namespace):"
  echo "  ${svc_rw}:5432"
  echo ""
  echo "Credentials:"
  echo "  user: ${PG_USER}"
  echo "  password: ${PG_PASSWORD}"
  echo ""
  echo "Connection URLs for each database:"
  for db in "${DATABASES[@]}"; do
    echo "  ${db}:"
    echo "    postgresql://${PG_USER}:${PG_PASSWORD}@${svc_rw}.${NAMESPACE}.svc.cluster.local:5432/${db}"
  done
  echo ""
  echo "Superuser URL (postgres db):"
  echo "  postgresql://${PG_USER}:${PG_PASSWORD}@${svc_rw}.${NAMESPACE}.svc.cluster.local:5432/postgres"
}

write_env_file() {
  local out="${1:-/tmp/postgres.env}"
  local svc_rw="${CLUSTER_NAME}-rw"
  local host="${svc_rw}.${NAMESPACE}.svc.cluster.local"

  cat > "$out" <<EOF
export PGHOST="${host}"
export PGPORT="5432"
export PGUSER="${PG_USER}"
export PGPASSWORD="${PG_PASSWORD}"

export DATABASE_URL_ADMIN="postgresql://${PG_USER}:${PG_PASSWORD}@${host}:5432/admin_control_plane"
export DATABASE_URL_ORG="postgresql://${PG_USER}:${PG_PASSWORD}@${host}:5432/org_control_plane"
export DATABASE_URL_BILLING="postgresql://${PG_USER}:${PG_PASSWORD}@${host}:5432/billing_service"
EOF

  echo "Wrote ${out}"
}

# ---------------------------------------------------------------------------
# down
# ---------------------------------------------------------------------------

down() {
  log "Removing PostgreSQL cluster ${CLUSTER_NAME} from ${NAMESPACE}"

  kubectl -n "$NAMESPACE" delete cluster "$CLUSTER_NAME" --ignore-not-found --wait=false || true
  kubectl -n "$NAMESPACE" delete secret "${CLUSTER_NAME}-app-user" --ignore-not-found || true

  log "Deleting namespace ${NAMESPACE}"
  kubectl delete namespace "$NAMESPACE" --ignore-not-found --wait=true --timeout=5m || true

  # Optional: remove the operator. Uncomment if you want a full teardown.
  # log "Removing CNPG operator"
  # kubectl delete -f https://raw.githubusercontent.com/cloudnative-pg/cloudnative-pg/release-1.25/releases/cnpg-1.25.1.yaml --ignore-not-found || true

  log "PostgreSQL removed."
}

# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

status() {
  log "Cluster status"
  kubectl -n "$NAMESPACE" get cluster "$CLUSTER_NAME" -o wide 2>/dev/null || echo "  (no cluster)"

  log "Pods"
  kubectl -n "$NAMESPACE" get pods 2>/dev/null || true

  log "Services"
  kubectl -n "$NAMESPACE" get svc 2>/dev/null || true

  log "Secrets"
  kubectl -n "$NAMESPACE" get secrets 2>/dev/null | grep -i "${CLUSTER_NAME}" || true
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

main() {
  local cmd="${1:-}"

  case "$cmd" in
    up)
      preflight
      install_operator
      ensure_namespace
      create_cluster
      create_databases
      print_endpoints
      write_env_file "/tmp/postgres.env"
      echo ""
      echo "Source it: source /tmp/postgres.env"
      ;;
    down)
      preflight
      down
      ;;
    status)
      preflight
      status
      ;;
    env)
      print_endpoints
      write_env_file "/tmp/postgres.env"
      echo ""
      echo "Source it: source /tmp/postgres.env"
      ;;
    *)
      cat <<USAGE
Usage: $0 {up|down|status|env}

  up      Install CNPG operator (if missing), create cluster + databases
  down    Delete the cluster and namespace
  status  Show cluster/pods/services
  env     Print connection strings and write /tmp/postgres.env

Environment overrides:
  NAMESPACE=$NAMESPACE
  CNPG_OPERATOR_NS=$CNPG_OPERATOR_NS
  CLUSTER_NAME=$CLUSTER_NAME
  PG_USER=$PG_USER
  PG_PASSWORD=$PG_PASSWORD
  PG_VERSION=$PG_VERSION
  PG_INSTANCES=$PG_INSTANCES
  PG_STORAGE_SIZE=$PG_STORAGE_SIZE
  STORAGE_CLASS=$STORAGE_CLASS
  CNPG_VERSION=$CNPG_VERSION
USAGE
      exit 1
      ;;
  esac
}

main "$@"