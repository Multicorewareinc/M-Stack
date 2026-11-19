"""
Wraps each agentic/backend/src/mcp_tools/<resource>.py tool function as a
LangChain tool, for use inside our own LangGraph orchestrator.

Deliberately NOT going through the MCP client/subprocess machinery here
(see the module docstring reasoning in graph.py) -- these are the exact
same functions, same validation, same docstring-becomes-schema, just
called in-process. An external MCP client (Claude Desktop, or anything
else) still reaches the same underlying logic by running
`python -m mcp_server` (from `agentic/backend/src/`) separately.

One tool here is not a deployment capability at all: ask_user(), below --
it belongs in this file rather than mcp_tools/ because it builds nothing
and has no SDK type to validate against; it only ever pauses the turn.
"""

from __future__ import annotations

from typing import Optional

from langchain_core.tools import tool
from langgraph.types import interrupt

from mcp_tools.accelerator import build_accelerator_plan
from mcp_tools.billing import build_billing_plan
from mcp_tools.cnpg import build_cnpg_plan
from mcp_tools.controlplane import build_controlplane_plan
from mcp_tools.enricher import build_enricher_plan
from mcp_tools.full_stack import build_full_stack_plan
from mcp_tools.gateway import build_gateway_plan
from mcp_tools.inference import build_inference_plan
from mcp_tools.ingress_gateway import build_ingress_gateway_plan
from mcp_tools.observability import build_observability_plan
from mcp_tools.policy import build_policy_plan
from mcp_tools.portal import build_portal_plan
from mcp_tools.queue import build_queue_plan
from mcp_tools.rke2 import build_rke2_cluster_plan
from mcp_tools.route import build_route_plan
from mcp_tools.seaweedfs import build_seaweedfs_plan
from mcp_tools.storage import build_storage_plan
from mcp_tools.tokenizer import build_tokenizer_plan
from mcp_tools.valkey import build_valkey_plan
from mcp_tools.vault import build_vault_plan

# tool() reads each function's type hints and docstring to build the
# schema the model sees -- the same docstrings written for the MCP tool
# layer, reused as-is rather than duplicated.
build_rke2_cluster_plan_tool = tool(build_rke2_cluster_plan)
build_storage_plan_tool = tool(build_storage_plan)
build_seaweedfs_plan_tool = tool(build_seaweedfs_plan)
build_gateway_plan_tool = tool(build_gateway_plan)
build_policy_plan_tool = tool(build_policy_plan)
build_ingress_gateway_plan_tool = tool(build_ingress_gateway_plan)
build_inference_plan_tool = tool(build_inference_plan)
build_valkey_plan_tool = tool(build_valkey_plan)
build_cnpg_plan_tool = tool(build_cnpg_plan)
build_observability_plan_tool = tool(build_observability_plan)
build_controlplane_plan_tool = tool(build_controlplane_plan)
build_portal_plan_tool = tool(build_portal_plan)
build_tokenizer_plan_tool = tool(build_tokenizer_plan)
build_enricher_plan_tool = tool(build_enricher_plan)
build_billing_plan_tool = tool(build_billing_plan)
build_route_plan_tool = tool(build_route_plan)
build_accelerator_plan_tool = tool(build_accelerator_plan)
build_queue_plan_tool = tool(build_queue_plan)
build_vault_plan_tool = tool(build_vault_plan)
build_full_stack_plan_tool = tool(build_full_stack_plan)


@tool
def ask_user(question: str, options: Optional[list[str]] = None) -> str:
    """Offer the user a choice between two or more valid ways to build
    what they asked for, when nothing they said settles it and no build
    tool can know which they want.

    Call the build tool first. What it reports as missing is asked for
    you: values the user has to give, whether to build on a new or an
    existing cluster, a required choice between types. Never use this
    for any of those, or for a value that is simply missing. Use it for
    a real fork the tool would accept either way, at most once per turn,
    in your own words -- a real question, not a status report.

    `options` is the short list of real choices. Pauses the turn and
    returns the user's exact answer as plain text; nothing here fails or
    needs a retry."""
    return interrupt({"question": question, "options": options or []})


ALL_TOOLS = [
    build_rke2_cluster_plan_tool,
    build_storage_plan_tool,
    build_seaweedfs_plan_tool,
    build_gateway_plan_tool,
    build_policy_plan_tool,
    build_ingress_gateway_plan_tool,
    build_inference_plan_tool,
    build_valkey_plan_tool,
    build_cnpg_plan_tool,
    build_observability_plan_tool,
    build_controlplane_plan_tool,
    build_portal_plan_tool,
    build_tokenizer_plan_tool,
    build_enricher_plan_tool,
    build_billing_plan_tool,
    build_route_plan_tool,
    build_accelerator_plan_tool,
    build_queue_plan_tool,
    build_vault_plan_tool,
    build_full_stack_plan_tool,
    ask_user,
]
