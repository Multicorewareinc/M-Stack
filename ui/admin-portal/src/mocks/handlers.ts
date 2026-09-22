import { http, HttpResponse } from 'msw';
import type { Organization, Permission, Plan } from '../api/types';
import {
  mockOrganizations,
  mockPermissions,
  mockPlans,
  mockPlatformSummary,
  mockUserDetail,
  mockUserDirectory,
} from './fixtures';

// Mirrors the real Admin CP /v1 routes (api/microservices/admin-control-plane/src).
// Error bodies match the real envelope exactly: { error: { message, type, field? } } (ADR-003).

// Organizations created via POST during a test/dev session, kept separate
// from the static seed so the seed itself stays immutable and shareable.
let createdOrganizations: Organization[] = [];
// Per-id GET call count, used to progress a provisioning org to active after
// a fixed number of polls — deterministic (call-count based), never
// wall-clock based, so both unit tests (fake timers) and Playwright get a
// reproducible transition (see design.md D2, tasks.md 1.4).
const provisioningPollCounts = new Map<string, number>();
const ACTIVATE_AFTER_POLLS = 2;

// Plans created via POST during a test/dev session, mirroring the
// createdOrganizations pattern above.
let createdPlans: Plan[] = [];

// Permissions created via POST during a test/dev session.
let createdPermissions: Permission[] = [];
// Permission ids the mock backend treats as "assigned to one or more roles" —
// deactivating one of these must surface that specific conflict (§52).
const ROLE_REFERENCED_PERMISSION_IDS = new Set(['perm_users_read']);

/** Test-only: clears mutable mock state between tests (wired in test/setup.ts). */
export function resetAdminMockState() {
  createdOrganizations = [];
  provisioningPollCounts.clear();
  createdPlans = [];
  createdPermissions = [];
}

function findOrganization(id: string): Organization | undefined {
  return mockOrganizations.find((o) => o.id === id) ?? createdOrganizations.find((o) => o.id === id);
}

function findPlan(id: string): Plan | undefined {
  return mockPlans.find((p) => p.id === id) ?? createdPlans.find((p) => p.id === id);
}

function planNameTaken(name: string): boolean {
  return mockPlans.some((p) => p.name === name) || createdPlans.some((p) => p.name === name);
}

function withProvisioningProgress(org: Organization): Organization {
  if (org.status !== 'provisioning') return org;
  const polls = (provisioningPollCounts.get(org.id) ?? 0) + 1;
  provisioningPollCounts.set(org.id, polls);
  return polls > ACTIVATE_AFTER_POLLS ? { ...org, status: 'active' } : org;
}

/** lowercase, non-alphanumeric -> '-', collapse repeats, strip ends — mirrors the real
 * backend's stdlib slugify (organizations/schemas.py), server-computed, never a client input. */
function slugify(name: string): string {
  return name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
}

function orgNameOrSlugTaken(name: string, slug: string): boolean {
  const all = [...mockOrganizations, ...createdOrganizations];
  return all.some((o) => o.name === name || o.slug === slug);
}

function findPermission(id: string): Permission | undefined {
  return mockPermissions.find((p) => p.id === id) ?? createdPermissions.find((p) => p.id === id);
}

/** Server-computed as `{resource}.{action}` — mirrors permissions/schemas.py's slug_for. */
function slugFor(resource: string, action: string): string {
  return `${resource}.${action}`;
}

function permissionSlugTaken(slug: string): boolean {
  return mockPermissions.some((p) => p.slug === slug) || createdPermissions.some((p) => p.slug === slug);
}

export const handlers = [
  http.get('/v1/organizations', ({ request }) => {
    const search = new URL(request.url).searchParams.get('search')?.toLowerCase();
    const all = [...mockOrganizations, ...createdOrganizations];
    const result = search ? all.filter((o) => o.name.toLowerCase().includes(search)) : all;
    return HttpResponse.json(result);
  }),

  http.get('/v1/organizations/:id', ({ params }) => {
    const org = findOrganization(params.id as string);
    if (!org) {
      return HttpResponse.json({ error: { message: 'Organization not found', type: 'not_found' } }, { status: 404 });
    }
    return HttpResponse.json(withProvisioningProgress(org));
  }),

  http.post('/v1/organizations', async ({ request }) => {
    const body = (await request.json()) as { name: string; plan_id: string };
    const slug = slugify(body.name);
    if (orgNameOrSlugTaken(body.name, slug)) {
      return HttpResponse.json(
        { error: { message: `Organization name '${body.name}' (slug '${slug}') already exists`, type: 'conflict', field: 'name' } },
        { status: 409 },
      );
    }
    const created: Organization = {
      id: `org_${slug}`,
      name: body.name,
      slug,
      plan_id: body.plan_id,
      status: 'provisioning',
      user_count: 0,
      retryable: false,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    };
    createdOrganizations.push(created);
    return HttpResponse.json(created, { status: 201 });
  }),

  http.patch('/v1/organizations/:id', async ({ params, request }) => {
    const org = findOrganization(params.id as string);
    if (!org) {
      return HttpResponse.json({ error: { message: 'Organization not found', type: 'not_found' } }, { status: 404 });
    }
    const body = (await request.json()) as Partial<Pick<Organization, 'name' | 'plan_id' | 'status'>>;
    if (body.name) {
      org.slug = slugify(body.name);
    }
    Object.assign(org, body, { updated_at: new Date().toISOString() });
    return HttpResponse.json(org);
  }),

  http.post('/v1/organizations/:id/retry', ({ params }) => {
    const org = findOrganization(params.id as string);
    if (!org) {
      return HttpResponse.json({ error: { message: 'Organization not found', type: 'not_found' } }, { status: 404 });
    }
    if (org.status !== 'failed') {
      return HttpResponse.json(
        { error: { message: `Organization ${org.id} is not in a failed state`, type: 'conflict' } },
        { status: 409 },
      );
    }
    org.status = 'active';
    org.retryable = false;
    provisioningPollCounts.delete(org.id);
    return HttpResponse.json(org);
  }),

  http.get('/v1/plans', () => HttpResponse.json([...mockPlans, ...createdPlans])),

  http.post('/v1/plans', async ({ request }) => {
    const body = (await request.json()) as { name: string; tpm?: number; rpm?: number; quota_monthly_tokens?: number };
    if (planNameTaken(body.name)) {
      return HttpResponse.json(
        { error: { message: `Plan name '${body.name}' already exists`, type: 'conflict', field: 'name' } },
        { status: 409 },
      );
    }
    const created: Plan = {
      id: `plan_${slugify(body.name)}`,
      name: body.name,
      tpm: body.tpm ?? 0,
      rpm: body.rpm ?? 0,
      quota_monthly_tokens: body.quota_monthly_tokens ?? 0,
      is_default: false,
      is_active: true,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    };
    createdPlans.push(created);
    return HttpResponse.json(created, { status: 201 });
  }),

  http.patch('/v1/plans/:id', async ({ params, request }) => {
    const plan = findPlan(params.id as string);
    if (!plan) {
      return HttpResponse.json({ error: { message: 'Plan not found', type: 'not_found' } }, { status: 404 });
    }
    const body = (await request.json()) as Partial<Plan>;
    if (body.name && body.name !== plan.name && planNameTaken(body.name)) {
      return HttpResponse.json(
        { error: { message: `Plan name '${body.name}' already exists`, type: 'conflict', field: 'name' } },
        { status: 409 },
      );
    }
    Object.assign(plan, body, { updated_at: new Date().toISOString() });
    return HttpResponse.json(plan);
  }),

  http.get('/v1/permissions', () => HttpResponse.json([...mockPermissions, ...createdPermissions])),
  http.post('/v1/permissions', async ({ request }) => {
    // A permission mutation is Super-Admin-only (§74, §230). createMasterPermission always
    // sends this header based on the current mock identity's permissions — a forced call that
    // bypasses the app entirely sends none, and must be denied just the same (absence is not a
    // free pass; matches org-portal's createUser/POST /v1/users convention).
    if (!request.headers.get('x-mock-permission')?.includes('permissions.create')) {
      return HttpResponse.json({ error: { message: 'Forbidden', type: 'forbidden' } }, { status: 403 });
    }
    const body = (await request.json()) as { resource: string; action: string; description?: string };
    const slug = slugFor(body.resource, body.action);
    if (permissionSlugTaken(slug)) {
      return HttpResponse.json(
        { error: { message: `Permission '${slug}' already exists`, type: 'conflict', field: 'action' } },
        { status: 409 },
      );
    }
    const created: Permission = {
      id: `perm_${slug.replace(/\./g, '_')}`,
      resource: body.resource,
      action: body.action,
      slug,
      description: body.description,
      is_active: true,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    };
    createdPermissions.push(created);
    return HttpResponse.json(created, { status: 201 });
  }),

  http.patch('/v1/permissions/:id', async ({ params, request }) => {
    const permission = findPermission(params.id as string);
    if (!permission) {
      return HttpResponse.json({ error: { message: 'Permission not found', type: 'not_found' } }, { status: 404 });
    }
    const body = (await request.json()) as Partial<Pick<Permission, 'resource' | 'action' | 'description' | 'is_active'>>;

    if (body.is_active === false && ROLE_REFERENCED_PERMISSION_IDS.has(permission.id)) {
      return HttpResponse.json(
        {
          error: {
            message: 'This permission cannot be removed because it is currently assigned to one or more roles.',
            type: 'conflict',
          },
        },
        { status: 409 },
      );
    }
    const nextResource = body.resource ?? permission.resource;
    const nextAction = body.action ?? permission.action;
    const nextSlug = slugFor(nextResource, nextAction);
    if (nextSlug !== permission.slug && permissionSlugTaken(nextSlug)) {
      return HttpResponse.json(
        { error: { message: `Permission '${nextSlug}' already exists`, type: 'conflict', field: 'action' } },
        { status: 409 },
      );
    }
    Object.assign(permission, body, { slug: nextSlug, updated_at: new Date().toISOString() });
    return HttpResponse.json(permission);
  }),

  http.get('/v1/organizations/:id/users', ({ params }) =>
    HttpResponse.json(mockUserDirectory.filter((u) => u.organization_id === params.id)),
  ),
  http.get('/v1/users', () => HttpResponse.json(mockUserDirectory)),
  http.get('/v1/users/:id', ({ params }) =>
    params.id === mockUserDetail.id
      ? HttpResponse.json(mockUserDetail)
      : HttpResponse.json({ error: { message: 'User not found', type: 'not_found' } }, { status: 404 }),
  ),

  http.get('/v1/platform-summary', () => HttpResponse.json(mockPlatformSummary)),
];
