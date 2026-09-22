"""Tests for the database spec, and what it publishes.

A Postgres cluster is the thing the control planes cannot start
without, so the value that matters is the connection URL a consumer
gets from it -- and the scheduling, because CloudNativePG emits none of
its own and rke2-cp01 carries no taint.
"""
from __future__ import annotations

import pytest

from multistack.database import CNPGOptions, Database, DatabaseConfig

KUBECONFIG = "/tmp/kc.yaml"


def database(**kwargs) -> DatabaseConfig:
    base = dict(name="admin_control_plane", owner="admin", password="s3cret")
    base.update(kwargs)
    return DatabaseConfig(**base)


def cnpg(**kwargs) -> Database:
    base = dict(kubeconfig_path=KUBECONFIG, name="admin-pg",
                namespace="platform-db", database=database())
    base.update(kwargs)
    return Database(**base)


# -- what a consumer gets ------------------------------------------------
def test_the_endpoint_is_the_primary_service():
    """-rw, not -ro or -r. CloudNativePG names three Services per
    cluster and only the primary accepts writes; a consumer handed the
    read-only one fails on its first INSERT, not at connect time."""
    assert cnpg().endpoint == (
        "postgresql://admin@admin-pg-rw.platform-db.svc.cluster.local:5432"
        "/admin_control_plane"
    )


def test_the_endpoint_carries_no_password():
    """It goes into non-secret configuration -- the control-plane charts
    put DATABASE_URL in a Secret precisely because a URL with a password
    in it must not reach a ConfigMap. CloudNativePG already writes the
    credentials to <cluster>-app-secret."""
    spec = cnpg(database=database(password="hunter2"))
    assert "hunter2" not in spec.endpoint
    assert "@admin-pg-rw" in spec.endpoint, "the owner is not a credential"


def test_there_is_no_endpoint_for_an_operator_only_spec():
    """The same model drives operator-only operations, where there is no
    database to point at. record() skips a None rather than publishing
    half a URL."""
    assert Database(kubeconfig_path=KUBECONFIG).endpoint is None
    assert cnpg(name="admin-pg", database=None).endpoint is None


def test_it_publishes_the_key_a_consumer_reads():
    from multistack.stack import CAPABILITY_OUTPUT

    assert Database.PROVIDES == {"database_url": "endpoint"}
    assert CAPABILITY_OUTPUT["database"] == "database_url"


def test_the_secret_name_follows_the_cluster():
    assert cnpg().secret_name == "admin-pg-app-secret"
    with pytest.raises(ValueError, match="name is required"):
        _ = Database(kubeconfig_path=KUBECONFIG).secret_name


# -- scheduling ----------------------------------------------------------
def test_pods_are_kept_off_the_control_plane_by_default():
    """CloudNativePG emits no scheduling constraints of its own, so
    without this Postgres is eligible for rke2-cp01 -- next to etcd,
    competing for the same disk. scratch/rebuild/postgres-clusters.yaml
    carries the same block by hand for exactly this reason."""
    terms = cnpg().affinity["nodeAffinity"][
        "requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]
    expressions = [e for t in terms for e in t["matchExpressions"]]
    assert {"key": "node-role.kubernetes.io/control-plane",
            "operator": "DoesNotExist"} in expressions


def test_the_default_affinity_is_not_shared_between_specs():
    """A mutable default returned by reference would let one spec's edit
    reach every other spec built afterwards."""
    first, second = cnpg(), cnpg()
    first.affinity["nodeAffinity"]["extra"] = True
    assert "extra" not in second.affinity["nodeAffinity"]


def test_scheduling_can_be_given_up_deliberately():
    assert cnpg(affinity={}).affinity == {}


# -- validation ----------------------------------------------------------
def test_naming_a_cluster_does_not_require_its_password():
    """validate_cluster_identity exists for the read and delete paths.
    Folded into validate_cluster, asking whether a cluster existed --
    or deleting one -- meant typing its password into a spec first."""
    spec = Database(kubeconfig_path=KUBECONFIG, name="admin-pg",
                namespace="platform-db")
    spec.validate_cluster_identity()

    with pytest.raises(ValueError, match="database configuration is required"):
        spec.validate_cluster()


def test_creating_one_requires_the_whole_bootstrap():
    with pytest.raises(ValueError, match="cluster name is required"):
        Database(kubeconfig_path=KUBECONFIG, database=database()).validate_cluster()


def test_a_database_needs_a_name_an_owner_and_a_password():
    for field in ("name", "owner", "password"):
        with pytest.raises(ValueError):
            database(**{field: ""})


def test_an_operator_spec_needs_no_cluster_fields():
    Database(kubeconfig_path=KUBECONFIG).validate()


def test_a_poll_interval_longer_than_the_timeout_is_refused():
    """It would poll once and time out, reporting a timeout for a
    cluster that was merely never looked at twice."""
    with pytest.raises(ValueError, match="ready_poll_interval"):
        cnpg(ready_timeout=10, ready_poll_interval=30)


def test_there_is_no_ambient_kubeconfig():
    with pytest.raises(ValueError, match="kubeconfig_path is required"):
        Database(kubeconfig_path="")


def test_a_misspelled_field_is_refused():
    with pytest.raises(Exception):
        Database(kubeconfig_path=KUBECONFIG, storagesize="10Gi")


def test_it_declares_the_cluster_dependency():
    assert Database.REQUIRES == ("cluster",)
    assert Database.FROM_STACK == {"kubeconfig_path": "kubeconfig_path"}


# -- the shape the migration put it in ------------------------------------
def test_it_is_a_capability_with_an_implementation_named_by_type():
    """`Database(type="cnpg")`, not `CNPG()`. The spec names the
    capability a dependent asks for; CloudNativePG is one way to serve
    it, the same way Longhorn serves storage and Valkey serves cache."""
    assert Database.CAPABILITY == "database"
    assert Database.SUPPORTED_TYPES == ("cnpg",)
    assert cnpg().type == "cnpg"

    with pytest.raises(ValueError, match="Unknown database type"):
        Database(kubeconfig_path=KUBECONFIG, type="rds")


def test_the_operator_settings_live_in_the_implementation_s_options():
    """They are CloudNativePG-specific to the last field -- a different
    implementation installs a different chart and registers a different
    CRD -- which is the rule for what goes in options rather than on the
    spec. Reading them off the spec still works, so no call site moved.
    """
    spec = cnpg()
    assert isinstance(spec.options, CNPGOptions)
    assert spec.operator_release_name == "cnpg"
    assert spec.operator_namespace == "cnpg-system"
    assert spec.operator_chart == "cloudnative-pg"
    assert spec.operator_chart_version == "0.29.0"
    assert spec.crd_name == "clusters.postgresql.cnpg.io"

    chosen = cnpg(options=CNPGOptions(operator_release_name="pg-operator",
                                      values={"replicaCount": 2}))
    assert chosen.operator_release_name == "pg-operator"
    assert chosen.values == {"replicaCount": 2}


def test_the_namespace_defaults_through_the_capability():
    """`namespace=None` means "the implementation's default", which is
    what every migrated capability means by it -- so the default lives in
    DEFAULT_NAMESPACES and is read back through resolved_namespace."""
    assert Database(kubeconfig_path=KUBECONFIG).resolved_namespace == "postgres"
    assert cnpg().resolved_namespace == "platform-db"

    with pytest.raises(ValueError, match="namespace must not be empty"):
        cnpg(namespace="   ")


def test_an_operator_release_name_is_still_required():
    """It moved into options; the check moved with it, so an empty one
    is refused at construction rather than at install time."""
    with pytest.raises(ValueError, match="operator_release_name is required"):
        cnpg(options=CNPGOptions(operator_release_name=""))
