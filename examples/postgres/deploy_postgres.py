import os
import subprocess

from multistack.database import (
    CNPGOptions,
    Database,
    DatabaseBackend,
    DatabaseConfig,
)
from multistack.state import default_state_manager

KUBECONFIG = os.path.expanduser(
    os.environ.get(
        "MULTISTACK_KUBECONFIG",
        "~/.multistack/kubeconfig",
    )
)

ENVIRONMENT = os.environ.get("ENVIRONMENT", "dev").strip().lower()
if ENVIRONMENT == "main":
    ENVIRONMENT = "prod"
elif ENVIRONMENT in {"e2e", "e2e-test"}:
    ENVIRONMENT = "e2e-test"
elif ENVIRONMENT not in {"dev", "qa", "staging", "prod"}:
    raise ValueError("ENVIRONMENT must be dev, qa, staging, prod, or e2e-test")

NAMESPACE = os.environ.get("NAMESPACE") or f"{ENVIRONMENT}-postgres"
CLUSTER_NAME = os.environ.get("CLUSTER_NAME") or f"{ENVIRONMENT}-pg"
PG_USER = os.environ.get("PG_USER", "postgres")
PG_PASSWORD = os.environ.get("PG_PASSWORD", "postgres")
PG_INSTANCES = int(os.environ.get("PG_INSTANCES", "1"))
CNPG_OPERATOR_NS = os.environ.get("CNPG_OPERATOR_NS") or "cnpg-system"

DATABASES = [
    "admin_control_plane",
    "org_control_plane",
    "billing_service",
]

# ---------------------------------------------------------------------------
# 1. Register existing Kubernetes / RKE2 cluster into Multistack State Store
# ---------------------------------------------------------------------------
state = default_state_manager()
cluster_dep_name = "existing-rke2"

if not state.get(cluster_dep_name):
    state.start(cluster_dep_name, component_type="cluster")
    state.mark_healthy(cluster_dep_name, kubeconfig_path=KUBECONFIG)
    print(f"Registered external cluster '{cluster_dep_name}' in multistack state.")

# ---------------------------------------------------------------------------
# 2. Operator & Database Cluster Deployment
# ---------------------------------------------------------------------------
CNPG_OPTIONS = CNPGOptions(
    operator_release_name="cnpg",
    operator_namespace=CNPG_OPERATOR_NS,
    operator_chart_version="0.23.2",
)


def kubectl(*args):
    return subprocess.run(
        ["kubectl", "--kubeconfig", KUBECONFIG, *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def operator_is_ready():
    try:
        kubectl("get", "crd", "clusters.postgresql.cnpg.io")
        ready = kubectl(
            "-n",
            CNPG_OPERATOR_NS,
            "get",
            "deployment",
            "cnpg-controller-manager",
            "-o",
            "jsonpath={.status.readyReplicas}",
        )
        return ready == "1"
    except subprocess.CalledProcessError:
        return False


backend = DatabaseBackend()

if operator_is_ready():
    print("Reusing existing CloudNativePG operator.")
else:
    operator = Database(
        kubeconfig_path=KUBECONFIG,
        options=CNPG_OPTIONS,
    )
    release = backend.create(operator)
    print(f"CloudNativePG operator installed: {release}")

cluster_config = Database(
    kubeconfig_path=KUBECONFIG,
    name=CLUSTER_NAME,
    namespace=NAMESPACE,
    instances=PG_INSTANCES,
    image="ghcr.io/cloudnative-pg/postgresql:16",
    storage_class="longhorn",
    storage_size="1Gi",
    options=CNPG_OPTIONS,
    database=None,
)

cluster_config.database = DatabaseConfig(
    name="postgres",
    owner=PG_USER,
    password=PG_PASSWORD,
)

cluster = backend.create_cluster(cluster_config)
print(f"PostgreSQL cluster created: {cluster.name}")


# ---------------------------------------------------------------------------
# 3. Align Password, Create Databases & Output Endpoints
# ---------------------------------------------------------------------------
def init_databases():
    primary_pod = f"{CLUSTER_NAME}-1"

    print(f"Waiting for primary pod '{primary_pod}' to become ready...")
    kubectl(
        "-n",
        NAMESPACE,
        "wait",
        "--for=condition=Ready",
        f"pod/{primary_pod}",
        "--timeout=5m",
    )

    # 1. Enforce password explicitly on the postgres role
    align_pw_sql = f"ALTER USER {PG_USER} WITH PASSWORD '{PG_PASSWORD}';"
    subprocess.run(
        [
            "kubectl",
            "--kubeconfig",
            KUBECONFIG,
            "-n",
            NAMESPACE,
            "exec",
            "-i",
            primary_pod,
            "-c",
            "postgres",
            "--",
            "psql",
            "-U",
            PG_USER,
            "-d",
            "postgres",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            align_pw_sql,
        ],
        check=True,
        capture_output=True,
    )
    print(f"Password aligned for role '{PG_USER}'.")

    # 2. Idempotently create each application database
    for db in DATABASES:
        check_sql = f"SELECT 1 FROM pg_database WHERE datname = '{db}';"
        check_proc = subprocess.run(
            [
                "kubectl",
                "--kubeconfig",
                KUBECONFIG,
                "-n",
                NAMESPACE,
                "exec",
                "-i",
                primary_pod,
                "-c",
                "postgres",
                "--",
                "psql",
                "-U",
                PG_USER,
                "-d",
                "postgres",
                "-tAc",
                check_sql,
            ],
            capture_output=True,
            text=True,
            check=True,
        )

        if "1" in check_proc.stdout.strip():
            print(f"Database '{db}' already exists.")
            continue

        create_sql = f"CREATE DATABASE {db} OWNER {PG_USER};"
        subprocess.run(
            [
                "kubectl",
                "--kubeconfig",
                KUBECONFIG,
                "-n",
                NAMESPACE,
                "exec",
                "-i",
                primary_pod,
                "-c",
                "postgres",
                "--",
                "psql",
                "-U",
                PG_USER,
                "-d",
                "postgres",
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                create_sql,
            ],
            check=True,
            capture_output=True,
        )
        print(f"Database '{db}' created.")

    # 3. Connection endpoints summary
    svc_rw = f"{CLUSTER_NAME}-rw.{NAMESPACE}.svc.cluster.local:5432"
    print("\nApplication Endpoints:")
    for db in DATABASES:
        print(f"  {db}: postgresql://{PG_USER}:{PG_PASSWORD}@{svc_rw}/{db}")


init_databases()
