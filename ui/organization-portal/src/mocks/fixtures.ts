import type { UsageBucket, UsageSummary, UsageTimeseries } from '../api/usage';
import type { OrgSummary, Permission, Role, User } from '../api/types';

// Identity fields for the single seeded organization (org_001 / "Acme Corporation") — used to
// build mockOrgSummary below. Org CP's own tenant record has no stored slug (see
// organizations/schemas.py's slugify helper — a real org-summary response derives one from the
// name, same as here).
const ORG_NAME = 'Acme Corporation';
const ORG_SLUG = 'acme';
const ORG_STATUS = 'active';

export const mockUsers: User[] = [
  {
    id: 'usr_001',
    organization_id: 'org_001',
    username: 'john',
    email: 'john@acme.com',
    first_name: 'John',
    last_name: 'Smith',
    display_name: 'John Smith',
    status: 'active',
    metadata: {},
    created_at: '2026-01-05T00:00:00Z',
    updated_at: '2026-06-01T00:00:00Z',
    last_login_at: '2026-09-09T12:30:00Z',
  },
  {
    id: 'usr_002',
    organization_id: 'org_001',
    username: 'mark',
    email: 'mark@acme.com',
    first_name: 'Mark',
    last_name: 'Lee',
    display_name: 'Mark Lee',
    status: 'suspended',
    metadata: {},
    created_at: '2026-02-01T00:00:00Z',
    updated_at: '2026-08-01T00:00:00Z',
  },
];

// Per-user role assignment for the mock backend (§60) — usr_001 is Admin,
// usr_002 has no roles yet.
export const mockUserRoles: Record<string, string[]> = {
  usr_001: ['role_admin'],
  usr_002: [],
};

export const mockRoles: Role[] = [
  {
    id: 'role_admin',
    organization_id: 'org_001',
    name: 'Admin',
    description: 'Organization administrator',
    is_system_role: true,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
  },
  {
    id: 'role_developer',
    organization_id: 'org_001',
    name: 'Developer',
    description: 'Development team access',
    is_system_role: false,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
  },
];

// TODO: confirm — Org CP has no GET /v1/permissions catalog endpoint yet (AD-08).
// This mirrors the Admin CP master list (architecture §26) as a read-only
// reference/projection, which is what Org CP is expected to serve.
export const mockPermissions: Permission[] = [
  { id: 'perm_users_read', resource: 'users', action: 'read', slug: 'users.read', description: 'View users', is_active: true },
  { id: 'perm_users_create', resource: 'users', action: 'create', slug: 'users.create', description: 'Create users', is_active: true },
  { id: 'perm_roles_read', resource: 'roles', action: 'read', slug: 'roles.read', description: 'View roles', is_active: true },
];

// Seed shape for the mock's internal role->permissions lookup (mocks/handlers.ts) — an
// internal bookkeeping structure, not the wire response shape (which is `{ permission_ids }`
// only, no `role_id`; see api/types.ts's RolePermission).
export const mockRolePermissions: { role_id: string; permission_ids: string[] }[] = [
  { role_id: 'role_admin', permission_ids: ['perm_users_read', 'perm_users_create', 'perm_roles_read'] },
  { role_id: 'role_developer', permission_ids: ['perm_users_read'] },
];

export const mockOrgSummary: OrgSummary = {
  organization: { name: ORG_NAME, slug: ORG_SLUG, status: ORG_STATUS },
  user_count: mockUsers.length,
  role_count: mockRoles.length,
  plan: { name: 'Pro', tpm: 100_000, rpm: 600, quota_monthly_tokens: 1_000_000 },
};

// A second seeded organization — deliberately thin (only what the tenant-isolation e2e needs to
// prove one org's data never renders under another's context, SP-13): its own name and its own,
// entirely distinct roles list. Everything else in this mock stays single-tenant (org_001).
export const mockOrgBRoles: Role[] = [
  {
    id: 'role_billing_manager',
    organization_id: 'org_002',
    name: 'Billing Manager',
    description: 'Manages billing for Globex Corporation',
    is_system_role: false,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
  },
];

export const mockOrgBSummary: OrgSummary = {
  organization: { name: 'Globex Corporation', slug: 'globex', status: 'active' },
  user_count: 0,
  role_count: mockOrgBRoles.length,
  plan: { name: 'Free', tpm: 10_000, rpm: 60, quota_monthly_tokens: 100_000 },
};

// Org-scoped usage (add-org-cp-usage-proxy) — org-wide only, real shape (requests + token split +
// a zero-filled daily bucket list), matching organization-control-plane's /v1/usage/* proxy to
// billing. `requests` here is a plausible per-day request count consistent with each day's token
// total, not derived from mockData.ts's separate per-key mock (that mock stays untouched/unused
// by these two endpoints, per its own now-partial mockup status).
const mockUsageBuckets: UsageBucket[] = [
  { date: '2026-09-03', requests: 8_950, total_tokens: 610_000, prompt_tokens: 410_000, completion_tokens: 200_000 },
  { date: '2026-09-04', requests: 10_320, total_tokens: 720_000, prompt_tokens: 480_000, completion_tokens: 240_000 },
  { date: '2026-09-05', requests: 8_140, total_tokens: 580_000, prompt_tokens: 390_000, completion_tokens: 190_000 },
  { date: '2026-09-06', requests: 4_600, total_tokens: 310_000, prompt_tokens: 210_000, completion_tokens: 100_000 },
  { date: '2026-09-07', requests: 4_320, total_tokens: 295_000, prompt_tokens: 200_000, completion_tokens: 95_000 },
  { date: '2026-09-08', requests: 12_010, total_tokens: 840_000, prompt_tokens: 560_000, completion_tokens: 280_000 },
  { date: '2026-09-09', requests: 13_040, total_tokens: 910_000, prompt_tokens: 610_000, completion_tokens: 300_000 },
  { date: '2026-09-10', requests: 16_120, total_tokens: 1_120_000, prompt_tokens: 750_000, completion_tokens: 370_000 },
  { date: '2026-09-11', requests: 14_050, total_tokens: 980_000, prompt_tokens: 650_000, completion_tokens: 330_000 },
  { date: '2026-09-12', requests: 15_070, total_tokens: 1_050_000, prompt_tokens: 700_000, completion_tokens: 350_000 },
  { date: '2026-09-13', requests: 9_180, total_tokens: 640_000, prompt_tokens: 430_000, completion_tokens: 210_000 },
  { date: '2026-09-14', requests: 6_010, total_tokens: 420_000, prompt_tokens: 280_000, completion_tokens: 140_000 },
  { date: '2026-09-15', requests: 2_580, total_tokens: 180_000, prompt_tokens: 120_000, completion_tokens: 60_000 },
  { date: '2026-09-16', requests: 4_150, total_tokens: 290_000, prompt_tokens: 195_000, completion_tokens: 95_000 },
];

export const mockUsageTimeseries: UsageTimeseries = {
  period: { start: mockUsageBuckets[0].date, end: mockUsageBuckets[mockUsageBuckets.length - 1].date },
  buckets: mockUsageBuckets,
};

export const mockUsageSummary: UsageSummary = {
  requests: mockUsageBuckets.reduce((sum, b) => sum + b.requests, 0),
  total_tokens: mockUsageBuckets.reduce((sum, b) => sum + b.total_tokens, 0),
  prompt_tokens: mockUsageBuckets.reduce((sum, b) => sum + b.prompt_tokens, 0),
  completion_tokens: mockUsageBuckets.reduce((sum, b) => sum + b.completion_tokens, 0),
  period: mockUsageTimeseries.period,
};

// org_002 (Globex) is deliberately thin elsewhere (tenant-isolation seam, SP-13) — zero usage
// keeps that consistent rather than reusing org_001's numbers under a different org context.
export const mockUsageTimeseriesB: UsageTimeseries = {
  period: mockUsageTimeseries.period,
  buckets: [],
};

export const mockUsageSummaryB: UsageSummary = {
  requests: 0,
  total_tokens: 0,
  prompt_tokens: 0,
  completion_tokens: 0,
  period: mockUsageTimeseries.period,
};
