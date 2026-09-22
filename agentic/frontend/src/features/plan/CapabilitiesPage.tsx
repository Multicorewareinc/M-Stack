import { Badge, PageHeader } from '@multistack/ui';

/**
 * A static roster of what the agentic layer can build, so a demo can answer
 * "what does it cover?" without typing a prompt. Mirrors the tools registered
 * in agentic/backend/src/orchestration/tools.py.
 */
const CAPABILITIES: { name: string; tool: string; note: string }[] = [
  { name: 'RKE2 cluster', tool: 'build_rke2_cluster_plan', note: 'nodes, CNI, kube-proxy' },
  { name: 'Block storage', tool: 'build_storage_plan', note: 'Longhorn' },
  { name: 'Object store', tool: 'build_minio_plan', note: 'MinIO tenant' },
  { name: 'Model serving', tool: 'build_inference_plan', note: 'vLLM' },
  { name: 'Model gateway', tool: 'build_gateway_plan', note: 'OpenAI-compatible proxy' },
  { name: 'Rate limiting', tool: 'build_policy_plan', note: 'rpm and tpm' },
  { name: 'Ingress gateway', tool: 'build_ingress_gateway_plan', note: 'MetalLB + Istio' },
  { name: 'Route', tool: 'build_route_plan', note: 'one service through the front door' },
  { name: 'Cache', tool: 'build_valkey_plan', note: 'Valkey' },
  { name: 'Database', tool: 'build_cnpg_plan', note: 'CloudNativePG' },
  { name: 'Observability', tool: 'build_observability_plan', note: 'Prometheus + Grafana' },
  { name: 'Tokenizer', tool: 'build_tokenizer_plan', note: 'token counting' },
  { name: 'Enricher', tool: 'build_enricher_plan', note: 'guarantees a token count' },
  { name: 'Billing', tool: 'build_billing_plan', note: 'metering + Stripe' },
  { name: 'Control plane', tool: 'build_controlplane_plan', note: 'admin + organization' },
  { name: 'Portal', tool: 'build_portal_plan', note: 'admin + organization UI' },
  { name: 'GPU accelerator', tool: 'build_accelerator_plan', note: 'nvidia device plugin' },
  { name: 'Whole platform', tool: 'build_full_stack_plan', note: 'composes all of the above' },
];

export function CapabilitiesPage() {
  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-y-auto px-6 py-6">
      <PageHeader
        title="Capabilities"
        description="Every capability the SDK ships has a tool here. None of them can deploy."
      />
      <div className="mt-4 grid gap-2 sm:grid-cols-2">
        {CAPABILITIES.map((c) => (
          <div key={c.tool} className="rounded-lg border border-subtle bg-neutral-0 p-3">
            <div className="flex items-center justify-between gap-2">
              <span className="text-body font-semibold text-neutral-900">{c.name}</span>
              <Badge variant="neutral">read-only</Badge>
            </div>
            <p className="mt-0.5 font-mono text-caption text-primary-600">{c.tool}</p>
            <p className="text-caption text-neutral-500">{c.note}</p>
          </div>
        ))}
      </div>
    </div>
  );
}
