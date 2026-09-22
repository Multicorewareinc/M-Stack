"""
Exercises the full agentic pipeline end to end: NL request -> knowledge
+ model + MCP tools (LangGraph orchestration) -> validated script handed
back. Needs GROQ_API_KEY in .env.

    cd agentic/backend/src && python -m examples.agentic_orchestration
"""

from orchestration.graph import send_message

print("=" * 70)
print("1) Underspecified request -- should ask a clarifying question, not guess")
print("=" * 70)
result = send_message("demo-1", "Set up an RKE2 cluster called demo-cluster.")
print("reply:", result.get("reply"))
assert result.get("reply") and not result.get("script"), "expected a clarifying question, got a script"

print()
print("=" * 70)
print("2) Fully-specified request -- should produce a validated script")
print("=" * 70)
result = send_message(
    "demo-2",
    "Set up an RKE2 cluster called ai-cluster on 10.0.0.11 (server) and "
    "10.0.0.12 (agent), using cilium with kube-proxy replacement disabled.",
)
print("reply:", result.get("reply"))
print("script:", result.get("script"))
assert result.get("script"), "expected a generated script, got none"
