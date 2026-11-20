// Typed client for the SDK's `python -m multistack.state.viewer` API (multistack/state/viewer/server.py).
// Field-for-field the shapes that server returns. Everything is a GET: the viewer is read-only.

export type DeploymentStatus =
  | 'pending'
  | 'validating'
  | 'provisioning'
  | 'provisioned'
  | 'degraded'
  | 'failed'
  | 'deleting';

export type HealthStatus = 'unknown' | 'healthy' | 'degraded' | 'unhealthy';

export interface HealthCheck {
  id: number;
  status: HealthStatus;
  detail: string;
  checked_at: string;
}

export interface Deployment {
  name: string;
  /** Free string chosen by each backend: 'cluster', 'storage', 'objectstore', 'gateway', ... */
  component_type: string;
  status: DeploymentStatus;
  kubeconfig_path: string | null;
  error: string | null;
  created_at: string;
  updated_at: string;
  latest_health_check: Omit<HealthCheck, 'id'> | null;
}

export interface DeploymentDetail extends Omit<Deployment, 'latest_health_check'> {
  /** Newest first. */
  health_checks: HealthCheck[];
}

export interface FeedEntry extends HealthCheck {
  deployment_name: string;
  /** null when the deployment has since been deleted. */
  component_type: string | null;
}

export interface StateInfo {
  db_path: string;
  exists: boolean;
  deployments: number;
  health_checks: number;
  generated_at: string;
}

export interface StateSummary {
  total: number;
  by_status: { status: DeploymentStatus; count: number }[];
  by_component_type: { component_type: string; count: number }[];
  last_updated: string | null;
  generated_at: string;
}

export class ApiError extends Error {
  constructor(
    message: string,
    public status?: number,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

async function get<T>(path: string, params: Record<string, string | number | undefined> = {}): Promise<T> {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== '') query.set(key, String(value));
  }
  const qs = query.toString();

  let res: Response;
  try {
    res = await fetch(`/api${path}${qs ? `?${qs}` : ''}`, { headers: { Accept: 'application/json' } });
  } catch {
    throw new ApiError(
      'Cannot reach the state viewer API. Start it with: python -m multistack.state.viewer',
    );
  }
  const isJson = res.headers.get('content-type')?.includes('application/json');
  const payload = isJson ? await res.json().catch(() => undefined) : undefined;
  if (!res.ok) {
    throw new ApiError(payload?.error?.message ?? `Request failed with status ${res.status}`, res.status);
  }
  return payload as T;
}

export const api = {
  info: () => get<StateInfo>('/info'),
  summary: () => get<StateSummary>('/summary'),
  deployments: (filters: { status?: string; componentType?: string; search?: string } = {}) =>
    get<Deployment[]>('/deployments', {
      status: filters.status,
      component_type: filters.componentType,
      search: filters.search,
    }),
  deployment: (name: string) => get<DeploymentDetail>('/deployment', { name }),
  healthChecks: (filters: { status?: string; deployment?: string; limit?: number } = {}) =>
    get<FeedEntry[]>('/health-checks', filters),
};
