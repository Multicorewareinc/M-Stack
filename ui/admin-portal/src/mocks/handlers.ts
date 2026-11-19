import { http, HttpResponse } from 'msw';
import type { Organization, Permission, Plan, UserDirectoryEntry } from '../api/types';
import {
  mockAuditLogs,
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

// Users created via POST during a test/dev session, mirroring the createdOrganizations pattern.
let createdUsers: UserDirectoryEntry[] = [];
let nextUserSeq = 1;

// Pending invitations: a couple of seeded rows, plus whatever POST /v1/invitations adds during a session.
interface MockInvitation {
  id: string;
  email: string;
  name: string | null;
  organization_id: string;
  role_id: string;
  role_name: string;
  invited_by: string | null;
  created_at: string;
  expires_at: string;
}
const DAY_MS = 24 * 60 * 60 * 1000;
function seedInvitations(): MockInvitation[] {
  const now = Date.now();
  return [
    {
      id: 'inv_seed_2',
      email: 'priya@globex.com',
      name: 'Priya',
      organization_id: 'org_002',
      role_id: 'role_org_user',
      role_name: 'org_user',
      invited_by: 'root@platform.local',
      created_at: new Date(now - 2 * DAY_MS).toISOString(),
      expires_at: new Date(now + 5.5 * DAY_MS).toISOString(),
    },
    {
      id: 'inv_seed_1',
      email: 'sam@acme.com',
      name: 'Sam',
      organization_id: 'org_001',
      role_id: 'role_org_admin',
      role_name: 'org_admin',
      invited_by: 'root@platform.local',
      created_at: new Date(now - 4 * DAY_MS).toISOString(),
      expires_at: new Date(now + 3.5 * DAY_MS).toISOString(),
    },
  ];
}
let invitations: MockInvitation[] = seedInvitations();
let nextInvitationSeq = 1;

/** Test-only: clears mutable mock state between tests (wired in test/setup.ts). */
export function resetAdminMockState() {
  createdOrganizations = [];
  provisioningPollCounts.clear();
  createdPlans = [];
  createdPermissions = [];
  createdUsers = [];
  nextUserSeq = 1;
  invitations = seedInvitations();
  nextInvitationSeq = 1;
}

function findOrganization(id: string): Organization | undefined {
  return mockOrganizations.find((o) => o.id === id) ?? createdOrganizations.find((o) => o.id === id);
}

function userEmailOrUsernameTaken(orgId: string, email: string, username: string): 'email' | 'username' | null {
  const all = [...mockUserDirectory, ...createdUsers].filter((u) => u.organization_id === orgId);
  if (all.some((u) => u.email === email)) return 'email';
  if (all.some((u) => u.username === username)) return 'username';
  return null;
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

  // Mirrors Admin CP POST /v1/invitations: invite into an existing org, or create a new one first. An
  // email of taken@example.com stands in for "already has an account" (409 on the email field).
  http.post('/v1/invitations', async ({ request }) => {
    const body = (await request.json()) as {
      email: string;
      name?: string;
      role: string;
      organization_id?: string;
      new_organization?: { name: string; plan_id: string };
    };
    if (body.email === 'taken@example.com') {
      return HttpResponse.json(
        { error: { message: 'A user with this email already exists', type: 'conflict', field: 'email' } },
        { status: 409 },
      );
    }
    const existing = [...mockOrganizations, ...createdOrganizations].find((o) => o.id === body.organization_id);
    if (!body.new_organization && !existing) {
      return HttpResponse.json({ error: { message: 'Organization not found', type: 'not_found' } }, { status: 404 });
    }
    const orgName = body.new_organization?.name ?? existing!.name;
    const row: MockInvitation = {
      id: `inv_${nextInvitationSeq++}`,
      email: body.email,
      name: body.name ?? null,
      organization_id: existing?.id ?? `org_${slugify(orgName)}`,
      role_id: `role_${body.role}`,
      role_name: body.role,
      invited_by: 'root@platform.local',
      created_at: new Date().toISOString(),
      expires_at: new Date(Date.now() + 7 * DAY_MS).toISOString(),
    };
    invitations = [row, ...invitations.filter((i) => i.email !== row.email)]; // a new link replaces the old one
    return HttpResponse.json(
      {
        ...row,
        organization_name: orgName,
        invite_path: '/accept-invite?token=mock-invite-token-0123456789',
        invite_url: null,
        email_status: 'not_configured', // no mail server in mock mode
        email_detail: 'SMTP is not configured (SMTP_HOST and SMTP_FROM_ADDRESS).',
        organization_created: Boolean(body.new_organization),
      },
      { status: 201 },
    );
  }),

  // Mirrors Admin CP GET /v1/invitations: pending invitations across all orgs, newest first.
  http.get('/v1/invitations', ({ request }) => {
    const q = new URL(request.url).searchParams;
    const orgId = q.get('organization_id');
    const search = q.get('search')?.toLowerCase();
    const role = q.get('role');
    const limit = Number(q.get('limit') ?? 25);
    const offset = Number(q.get('offset') ?? 0);
    const matches = invitations.filter(
      (i) =>
        (!orgId || i.organization_id === orgId) &&
        (!role || i.role_name === role) &&
        (!search || i.email.toLowerCase().includes(search)),
    );
    return HttpResponse.json({ items: matches.slice(offset, offset + limit), total: matches.length, limit, offset });
  }),

  http.delete('/v1/invitations/:id', ({ params }) => {
    if (!invitations.some((i) => i.id === params.id)) {
      return HttpResponse.json({ error: { message: 'Invitation not found', type: 'not_found' } }, { status: 404 });
    }
    invitations = invitations.filter((i) => i.id !== params.id);
    return new HttpResponse(null, { status: 204 });
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

  http.post('/v1/organizations/:id/users', async ({ params, request }) => {
    const orgId = params.id as string;
    if (!findOrganization(orgId)) {
      return HttpResponse.json({ error: { message: 'Organization not found', type: 'not_found' } }, { status: 404 });
    }
    const body = (await request.json()) as {
      username: string;
      email: string;
      password: string;
      display_name?: string;
    };
    const conflictField = userEmailOrUsernameTaken(orgId, body.email, body.username);
    if (conflictField) {
      const label = conflictField === 'email' ? `Email '${body.email}'` : `Username '${body.username}'`;
      return HttpResponse.json(
        { error: { message: `${label} already exists in this organization`, type: 'conflict', field: conflictField } },
        { status: 409 },
      );
    }
    const now = new Date().toISOString();
    const created: UserDirectoryEntry = {
      id: `usr_${nextUserSeq++}`,
      organization_id: orgId,
      username: body.username,
      display_name: body.display_name ?? null,
      email: body.email,
      status: 'active',
      created_at: now,
      updated_at: now,
    };
    createdUsers.push(created);
    return HttpResponse.json(created, { status: 201 });
  }),

  http.get('/v1/users', () => HttpResponse.json([...mockUserDirectory, ...createdUsers])),
  http.get('/v1/users/:id', ({ params }) =>
    params.id === mockUserDetail.id
      ? HttpResponse.json(mockUserDetail)
      : HttpResponse.json({ error: { message: 'User not found', type: 'not_found' } }, { status: 404 }),
  ),

  http.get('/v1/audit-logs', ({ request }) => {
    const q = new URL(request.url).searchParams;
    const orgId = q.get('organization_id');
    const action = q.get('action');
    const since = q.get('since');
    const until = q.get('until');
    const search = q.get('search')?.toLowerCase();
    const limit = Number(q.get('limit') ?? 50);
    const offset = Number(q.get('offset') ?? 0);
    const matches = mockAuditLogs.filter(
      (l) =>
        (!orgId || l.organization_id === orgId) &&
        (!action || l.action === action) &&
        (!since || new Date(l.created_at) >= new Date(since)) &&
        (!until || new Date(l.created_at) <= new Date(until)) &&
        (!search ||
          [l.actor_email, l.resource_name, l.action].some((v) => v?.toLowerCase().includes(search))),
    );
    return HttpResponse.json({ items: matches.slice(offset, offset + limit), total: matches.length, limit, offset });
  }),

  http.get('/v1/platform-summary', () => HttpResponse.json(mockPlatformSummary)),
];
