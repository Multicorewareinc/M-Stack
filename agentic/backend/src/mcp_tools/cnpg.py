"""CNPG tool -- exposes multistack.Database as an MCP tool.

The database capability, whose one implementation today is `cnpg`
(CloudNativePG), managing a real PostgreSQL cluster on Kubernetes. The
tool keeps the implementation's name because that is what a user asks
for by name; the spec it builds is `Database(type="cnpg")`, the same
way build_valkey_plan builds a `Cache(type="valkey")`.

Unlike every other capability here, it has TWO independent lifecycles
that the SDK deliberately keeps separate: installing the operator
(`DatabaseBackend.create()`, a Helm release) and creating an actual
database cluster on top of it (`DatabaseBackend.create_cluster()`,
which requires the operator already installed). This tool's
`cnpg.database` being set or not decides which the rendered script
does: unset means operator-only, set means operator install followed by
a real cluster and database.

SECURITY NOTE: `DatabaseConfig.password` is currently a required, real
plaintext value -- the SDK has no Secret-reference alternative yet
(confirmed directly in code: the cnpg driver's `_apply_secret()` always
writes the password straight from this field, there is no path around
it). Storing it in plain text is a known, explicit, team-approved
interim state, not an oversight -- other components have the same
limitation and it's slated for a future migration to Secret-based
handling. Until then, this tool must NEVER invent a password -- always
require the real one from the user, same as any other required field
it can't guess.

Like Storage/MinIO/Gateway/Policy/Inference/IngressGateway/Valkey, a
database deploys into an EXISTING cluster -- it needs a real
kubeconfig_path."""

from __future__ import annotations

from pydantic import validate_call

from multistack import Database

from mcp_server import mcp
from mcp_tools._script import check_script, dump


def _render_script(cnpg: Database) -> str:
    """Renders a validated database plan as the single readable Python
    script that installs it -- same reasoning as
    mcp_tools.gateway._render_script: built from the validated object's
    own fields, not a hand-typed template that could silently drop one.

    `database` is the spec's own nested DatabaseConfig, rendered as its
    own constructor call like every other nested model here. When it's
    None, the script only installs the operator -- create_cluster()
    requires the operator already installed, so there's nothing further
    a script can correctly do without a database to create.

    `options` (CNPGOptions today, carrying the operator's Helm release
    and its CRD) renders as its own constructor call rather than a raw
    dict, same as mcp_tools.valkey._render_script. It gained an options
    model when the capability moved out of the `core/` + `backends/`
    split."""
    field_lines = [
        f"    {k}={v!r}," for k, v in dump(cnpg).items()
        if k not in ("database", "options")
    ]
    if cnpg.options is not None:
        opt_args = ", ".join(
            f"{k}={v!r}" for k, v in dump(cnpg.options).items())
        field_lines.append(
            f"    options={type(cnpg.options).__name__}({opt_args}),")
    if cnpg.database is not None:
        db_fields = dump(cnpg.database)
        db_args = ", ".join(f"{k}={v!r}" for k, v in db_fields.items())
        field_lines.append(f"    database=DatabaseConfig({db_args}),")

    imports = ["from multistack import Database, DatabaseConfig" if cnpg.database is not None else "from multistack import Database"]
    imports.append("from multistack.database import CNPGOptions, DatabaseBackend")

    body = [
        "database = Database(",
        "\n".join(field_lines),
        ")",
        "backend = DatabaseBackend()",
        "backend.create(database)",
    ]
    if cnpg.database is not None:
        body.append("backend.create_cluster(database)")

    script = "\n".join(imports) + "\n\n" + "\n".join(body) + "\n"
    check_script(script)
    return script


@mcp.tool()
@validate_call
def build_cnpg_plan(cnpg: Database) -> dict:
    """
    Validate a CNPG (CloudNativePG/PostgreSQL) plan against the real
    SDK and render it as a single readable Python script that installs
    it onto an EXISTING cluster. Does NOT provision anything --
    read-only, safe to call freely.

    `cnpg` is the real Database type directly (requires
    `kubeconfig_path` for the cluster this installs onto, and `type`
    defaults to the only implementation, `cnpg`). Leave `database` unset to
    install just the CloudNativePG operator (no database created);
    set it to also create a real PostgreSQL cluster and database on
    top of that operator -- `database` requires `name`, `owner`, and
    `password` all be real values, and `cnpg.name` (the Cluster's own
    name) must also be set for a database to be created.

    SECURITY: `database.password` is a REAL, PLAINTEXT password --
    there is currently no way to reference an existing Secret instead
    (a known, team-approved interim limitation, not a gap in this
    tool). NEVER invent, guess, or suggest a password yourself -- if
    the user wants a database created but hasn't given a real
    password, ask for it explicitly, the same as any other required
    value you can't make up.

    Once deployed, this database's `database_url` is the connection string
    a consumer would use -- it carries no credential (the password
    stays in the Secret CloudNativePG writes, named `{cnpg.name}-app-secret`).

    Returns {"valid": True, ...plan..., "script": <the deployable script>}
    on success, or {"valid": False, "error": <reason>} if the SDK's own
    validation rejected it (e.g. a missing kubeconfig_path, or database
    fields given without cnpg.name set).
    """
    if cnpg.database is not None:
        # Constructing a Database validates the operator half only --
        # the cluster half is optional, because the same spec drives an
        # operator-only install. So construction does NOT check that a
        # database requires cnpg.name to be set (confirmed directly:
        # `Database(database=...)` with no `name` constructs without
        # error). Only validate_cluster() catches it,
        # and only the backend calls that, inside the generated script,
        # too late to matter here. Call it explicitly so this is a real
        # {"valid": False} instead of a script that looks fine and fails
        # once actually run.
        try:
            cnpg.validate_cluster()
        except ValueError as e:
            return {"valid": False, "error": str(e)}

    try:
        script = _render_script(cnpg)
    except AssertionError as e:
        return {"valid": False, "error": str(e)}

    result = {
        "valid": True,
        "kubeconfig_path": cnpg.kubeconfig_path,
        "operator_release_name": cnpg.operator_release_name,
        "script": script,
    }
    if cnpg.database is not None:
        result["name"] = cnpg.name
        result["database_url"] = cnpg.endpoint
    return result
