"""
Wraps each agentic/backend/src/mcp_tools/<resource>.py tool function as a
LangChain tool, for use inside our own LangGraph orchestrator.

Deliberately NOT going through the MCP client/subprocess machinery here
(see the module docstring reasoning in graph.py) -- these are the exact
same functions, same validation, same docstring-becomes-schema, just
called in-process. An external MCP client (Claude Desktop, or anything
else) still reaches the same underlying logic by running
`python -m mcp_server` (from `agentic/backend/src/`) separately.
"""

from __future__ import annotations

from langchain_core.tools import tool

from mcp_tools.accelerator import build_accelerator_plan
from mcp_tools.billing import build_billing_plan
from mcp_tools.cnpg import build_cnpg_plan
from mcp_tools.controlplane import build_controlplane_plan
from mcp_tools.enricher import build_enricher_plan
from mcp_tools.full_stack import build_full_stack_plan
from mcp_tools.gateway import build_gateway_plan
from mcp_tools.inference import build_inference_plan
from mcp_tools.ingress_gateway import build_ingress_gateway_plan
from mcp_tools.minio import build_minio_plan
from mcp_tools.observability import build_observability_plan
from mcp_tools.policy import build_policy_plan
from mcp_tools.portal import build_portal_plan
from mcp_tools.queue import build_queue_plan
from mcp_tools.rke2 import build_rke2_cluster_plan
from mcp_tools.route import build_route_plan
from mcp_tools.storage import build_storage_plan
from mcp_tools.tokenizer import build_tokenizer_plan
from mcp_tools.valkey import build_valkey_plan

# tool() reads each function's type hints and docstring to build the
# schema the model sees -- the same docstrings written for the MCP tool
# layer, reused as-is rather than duplicated.
build_rke2_cluster_plan_tool = tool(build_rke2_cluster_plan)
build_storage_plan_tool = tool(build_storage_plan)
build_minio_plan_tool = tool(build_minio_plan)
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
build_full_stack_plan_tool = tool(build_full_stack_plan)

ALL_TOOLS = [
    build_rke2_cluster_plan_tool,
    build_storage_plan_tool,
    build_minio_plan_tool,
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
    build_full_stack_plan_tool,
]
