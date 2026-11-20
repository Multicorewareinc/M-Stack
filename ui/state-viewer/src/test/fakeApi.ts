import { vi } from 'vitest';
import type { Deployment, DeploymentDetail, FeedEntry, StateInfo, StateSummary } from '../api/client';

export const DB = '/home/me/.multistack/state.db';

export const deployments: Deployment[] = [
  {
    name: 'edge-cluster',
    component_type: 'cluster',
    status: 'provisioned',
    kubeconfig_path: '/tmp/kubeconfig',
    error: null,
    created_at: '2026-10-08T05:00:00+00:00',
    updated_at: '2026-10-08T05:10:00+00:00',
    latest_health_check: { status: 'healthy', detail: 'deployment succeeded', checked_at: '2026-10-08T05:10:00+00:00' },
  },
  {
    name: 'longhorn',
    component_type: 'storage',
    status: 'failed',
    kubeconfig_path: null,
    error: 'helm timed out after 600s',
    created_at: '2026-10-08T05:11:00+00:00',
    updated_at: '2026-10-08T05:21:00+00:00',
    latest_health_check: { status: 'unhealthy', detail: 'helm timed out after 600s', checked_at: '2026-10-08T05:21:00+00:00' },
  },
  {
    name: 'default/chat-route',
    component_type: 'route',
    status: 'degraded',
    kubeconfig_path: null,
    error: null,
    created_at: '2026-10-08T05:22:00+00:00',
    updated_at: '2026-10-08T05:23:00+00:00',
    latest_health_check: { status: 'degraded', detail: 'backend slow', checked_at: '2026-10-08T05:23:00+00:00' },
  },
];

const feed: FeedEntry[] = [
  { id: 3, deployment_name: 'default/chat-route', component_type: 'route', status: 'degraded', detail: 'backend slow', checked_at: '2026-10-08T05:23:00+00:00' },
  { id: 2, deployment_name: 'longhorn', component_type: 'storage', status: 'unhealthy', detail: 'helm timed out after 600s', checked_at: '2026-10-08T05:21:00+00:00' },
  { id: 1, deployment_name: 'edge-cluster', component_type: 'cluster', status: 'healthy', detail: 'deployment succeeded', checked_at: '2026-10-08T05:10:00+00:00' },
];

export function detailFor(name: string): DeploymentDetail | undefined {
  const d = deployments.find((x) => x.name === name);
  if (!d) return undefined;
  const { latest_health_check: _latest, ...rest } = d;
  return {
    ...rest,
    health_checks: feed.filter((f) => f.deployment_name === name).map(({ id, status, detail, checked_at }) => ({ id, status, detail, checked_at })),
  };
}

export interface FakeOptions {
  exists?: boolean;
  /** Make every request fail the way an unreachable server does. */
  down?: boolean;
  /** Override the deployments returned (e.g. an empty db). */
  rows?: Deployment[];
}

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

/** Installs a fetch fake for the viewer API and returns the list of URLs requested. */
export function installFakeApi(options: FakeOptions = {}) {
  const requests: URL[] = [];
  const rows = options.rows ?? deployments;

  const fake = vi.fn(async (input: RequestInfo | URL) => {
    if (options.down) throw new TypeError('Failed to fetch');
    const url = new URL(String(input), 'http://viewer.test');
    requests.push(url);
    const q = url.searchParams;

    if (url.pathname === '/api/info') {
      const info: StateInfo = {
        db_path: DB,
        exists: options.exists ?? true,
        deployments: rows.length,
        health_checks: feed.length,
        generated_at: '2026-10-08T05:30:00+00:00',
      };
      return json(info);
    }
    if (url.pathname === '/api/summary') {
      const count = (key: 'status' | 'component_type') => {
        const m = new Map<string, number>();
        rows.forEach((r) => m.set(r[key], (m.get(r[key]) ?? 0) + 1));
        return [...m.entries()];
      };
      const summary: StateSummary = {
        total: rows.length,
        by_status: count('status').map(([status, c]) => ({ status: status as never, count: c })),
        by_component_type: count('component_type').map(([component_type, c]) => ({ component_type, count: c })),
        last_updated: '2026-10-08T05:23:00+00:00',
        generated_at: '2026-10-08T05:30:00+00:00',
      };
      return json(summary);
    }
    if (url.pathname === '/api/deployments') {
      const search = q.get('search')?.toLowerCase();
      return json(
        rows.filter(
          (r) =>
            (!q.get('status') || r.status === q.get('status')) &&
            (!q.get('component_type') || r.component_type === q.get('component_type')) &&
            (!search || r.name.toLowerCase().includes(search) || (r.error ?? '').toLowerCase().includes(search)),
        ),
      );
    }
    if (url.pathname === '/api/deployment') {
      const found = detailFor(q.get('name') ?? '');
      return found ? json(found) : json({ error: { message: `Deployment '${q.get('name')}' not found.` } }, 404);
    }
    if (url.pathname === '/api/health-checks') {
      return json(feed.filter((f) => !q.get('status') || f.status === q.get('status')));
    }
    return json({ error: { message: 'no such endpoint' } }, 404);
  });

  vi.stubGlobal('fetch', fake);
  return requests;
}
