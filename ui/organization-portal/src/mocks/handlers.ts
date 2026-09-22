import { http, HttpResponse } from 'msw';
import type { Role, User } from '../api/types';
import {
  mockOrgBRoles,
  mockOrgBSummary,
  mockOrgSummary,
  mockPermissions,
  mockRolePermissions,
  mockRoles,
  mockUsageSummary,
  mockUsageSummaryB,
  mockUsageTimeseries,
  mockUsageTimeseriesB,
  mockUserRoles,
  mockUsers,
} from './fixtures';
import { getCurrentMockOrgId } from './mockOrgContext';

// Mirrors the real Org CP /v1 routes (api/microservices/organization-control-plane/src).
// Error bodies match the real envelope exactly: { error: { message, type, field? } } (ADR-003).

// Users created via POST during a test/dev session, kept separate from the
// static seed so the seed itself stays immutable and shareable.
let createdUsers: User[] = [];
// Per-user role assignment, mutable copy of the seed so PUT can update it
// without mutating the shared fixture object.
let userRoles: Record<string, string[]> = { ...mockUserRoles };
// Roles created via POST during a test/dev session.
let createdRoles: Role[] = [];
// Per-role permission composition, mutable copy of the seed.
let rolePermissions: Record<string, string[]> = Object.fromEntries(
  mockRolePermissions.map((rp) => [rp.role_id, [...rp.permission_ids]]),
);

/** Test-only: clears mutable mock state between tests (wired in test/setup.ts). */
export function resetOrgMockState() {
  createdUsers = [];
  userRoles = { ...mockUserRoles };
  createdRoles = [];
  rolePermissions = Object.fromEntries(mockRolePermissions.map((rp) => [rp.role_id, [...rp.permission_ids]]));
}

function findUser(id: string): User | undefined {
  return mockUsers.find((u) => u.id === id) ?? createdUsers.find((u) => u.id === id);
}

function findRole(id: string): Role | undefined {
  return mockRoles.find((r) => r.id === id) ?? createdRoles.find((r) => r.id === id);
}

function roleNameTaken(name: string): boolean {
  return (
    mockRoles.some((r) => r.name.toLowerCase() === name.toLowerCase()) ||
    createdRoles.some((r) => r.name.toLowerCase() === name.toLowerCase())
  );
}

function roleHasUsers(id: string): boolean {
  return Object.values(userRoles).some((ids) => ids.includes(id));
}

export const handlers = [
  http.get('/v1/users', () => HttpResponse.json([...mockUsers, ...createdUsers])),
  http.get('/v1/users/:id', ({ params }) => {
    const user = findUser(params.id as string);
    return user
      ? HttpResponse.json(user)
      : HttpResponse.json({ error: { message: 'User not found', type: 'not_found' } }, { status: 404 });
  }),
  http.post('/v1/users', async ({ request }) => {
    if (!request.headers.get('x-mock-permission')?.includes('users.create')) {
      return HttpResponse.json({ error: { message: 'Forbidden', type: 'forbidden' } }, { status: 403 });
    }
    const body = (await request.json()) as Partial<User> & { username: string; email: string };
    const created: User = {
      id: `usr_${body.username}`,
      organization_id: 'org_001',
      username: body.username,
      email: body.email,
      first_name: body.first_name,
      last_name: body.last_name,
      display_name: body.display_name ?? body.username,
      status: body.status ?? 'active',
      metadata: {},
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    };
    createdUsers.push(created);
    userRoles[created.id] = [];
    return HttpResponse.json(created, { status: 201 });
  }),
  http.patch('/v1/users/:id', async ({ params, request }) => {
    const user = findUser(params.id as string);
    if (!user) {
      return HttpResponse.json({ error: { message: 'User not found', type: 'not_found' } }, { status: 404 });
    }
    const body = (await request.json()) as Partial<User>;
    Object.assign(user, body, { updated_at: new Date().toISOString() });
    return HttpResponse.json(user);
  }),

  http.get('/v1/users/:id/roles', ({ params }) => {
    const id = params.id as string;
    if (!findUser(id)) {
      return HttpResponse.json({ error: { message: 'User not found', type: 'not_found' } }, { status: 404 });
    }
    return HttpResponse.json({ role_ids: userRoles[id] ?? [] });
  }),
  http.put('/v1/users/:id/roles', async ({ params, request }) => {
    const id = params.id as string;
    if (!findUser(id)) {
      return HttpResponse.json({ error: { message: 'User not found', type: 'not_found' } }, { status: 404 });
    }
    const body = (await request.json()) as { role_ids: string[] };
    userRoles[id] = body.role_ids;
    return HttpResponse.json({ role_ids: userRoles[id] });
  }),
  http.get('/v1/users/:id/permissions', ({ params }) => {
    const id = params.id as string;
    const roleIds = userRoles[id] ?? [];
    const permissionIds = new Set<string>();
    for (const roleId of roleIds) {
      (rolePermissions[roleId] ?? []).forEach((p) => permissionIds.add(p));
    }
    return HttpResponse.json({ permission_ids: [...permissionIds] });
  }),

  http.get('/v1/roles', () => {
    // Tenant isolation seam (SP-13): a second mock org (org_002) has its own, entirely separate
    // roles list with no shared state — proves the UI never leaks org_001 data after switching.
    if (getCurrentMockOrgId() === 'org_002') {
      const withCounts = mockOrgBRoles.map((r) => ({ ...r, user_count: 0, permission_count: 0 }));
      return HttpResponse.json(withCounts);
    }
    // Counts are attached here exactly as a real backend would derive them
    // server-side — the UI never computes these by paging per-role (§109, §186).
    const withCounts = [...mockRoles, ...createdRoles].map((r) => ({
      ...r,
      user_count: Object.values(userRoles).filter((ids) => ids.includes(r.id)).length,
      permission_count: (rolePermissions[r.id] ?? []).length,
    }));
    return HttpResponse.json(withCounts);
  }),
  http.get('/v1/roles/:id', ({ params }) => {
    const role = findRole(params.id as string);
    if (!role) {
      return HttpResponse.json({ error: { message: 'Role not found', type: 'not_found' } }, { status: 404 });
    }
    return HttpResponse.json({
      ...role,
      user_count: Object.values(userRoles).filter((ids) => ids.includes(role.id)).length,
      permission_count: (rolePermissions[role.id] ?? []).length,
    });
  }),
  http.post('/v1/roles', async ({ request }) => {
    const body = (await request.json()) as { name: string; description?: string };
    if (roleNameTaken(body.name)) {
      return HttpResponse.json(
        { error: { message: `Role name '${body.name}' already exists in this organization`, type: 'conflict', field: 'name' } },
        { status: 409 },
      );
    }
    const created: Role = {
      id: `role_${body.name.toLowerCase().replace(/\s+/g, '_')}`,
      organization_id: 'org_001',
      name: body.name,
      description: body.description,
      is_system_role: false,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    };
    createdRoles.push(created);
    rolePermissions[created.id] = [];
    return HttpResponse.json(created, { status: 201 });
  }),
  http.patch('/v1/roles/:id', async ({ params, request }) => {
    const role = findRole(params.id as string);
    if (!role) {
      return HttpResponse.json({ error: { message: 'Role not found', type: 'not_found' } }, { status: 404 });
    }
    const body = (await request.json()) as Partial<Role>;
    if (body.name && body.name !== role.name && roleNameTaken(body.name)) {
      return HttpResponse.json(
        { error: { message: `Role name '${body.name}' already exists in this organization`, type: 'conflict', field: 'name' } },
        { status: 409 },
      );
    }
    Object.assign(role, body, { updated_at: new Date().toISOString() });
    return HttpResponse.json(role);
  }),
  http.delete('/v1/roles/:id', ({ params }) => {
    const id = params.id as string;
    const role = findRole(id);
    if (!role) {
      return HttpResponse.json({ error: { message: 'Role not found', type: 'not_found' } }, { status: 404 });
    }
    if (roleHasUsers(id)) {
      return HttpResponse.json(
        { error: { message: 'This role cannot be deleted while it is assigned to users.', type: 'conflict' } },
        { status: 409 },
      );
    }
    createdRoles = createdRoles.filter((r) => r.id !== id);
    return new HttpResponse(null, { status: 204 });
  }),

  http.get('/v1/roles/:id/permissions', ({ params }) => {
    const id = params.id as string;
    if (!findRole(id)) {
      return HttpResponse.json({ error: { message: 'Role not found', type: 'not_found' } }, { status: 404 });
    }
    return HttpResponse.json({ permission_ids: rolePermissions[id] ?? [] });
  }),
  http.put('/v1/roles/:id/permissions', async ({ params, request }) => {
    const id = params.id as string;
    if (!findRole(id)) {
      return HttpResponse.json({ error: { message: 'Role not found', type: 'not_found' } }, { status: 404 });
    }
    const body = (await request.json()) as { permission_ids: string[] };
    rolePermissions[id] = body.permission_ids;
    return HttpResponse.json({ permission_ids: rolePermissions[id] });
  }),

  http.get('/v1/permissions', () => HttpResponse.json(mockPermissions)),

  http.get('/v1/organization/summary', () =>
    HttpResponse.json(getCurrentMockOrgId() === 'org_002' ? mockOrgBSummary : mockOrgSummary),
  ),

  http.get('/v1/usage/summary', () =>
    HttpResponse.json(getCurrentMockOrgId() === 'org_002' ? mockUsageSummaryB : mockUsageSummary),
  ),

  http.get('/v1/usage/timeseries', () =>
    HttpResponse.json(getCurrentMockOrgId() === 'org_002' ? mockUsageTimeseriesB : mockUsageTimeseries),
  ),
];
