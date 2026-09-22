import type {
  Organization,
  PlatformSummary,
  Plan,
  Permission,
  UserDetail,
  UserDirectoryEntry,
} from '../api/types';

export const mockPlans: Plan[] = [
  {
    id: 'plan_free',
    name: 'Free',
    tpm: 10_000,
    rpm: 60,
    quota_monthly_tokens: 100_000,
    is_default: true,
    is_active: true,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
  },
  {
    id: 'plan_pro',
    name: 'Pro',
    tpm: 100_000,
    rpm: 600,
    quota_monthly_tokens: 1_000_000,
    is_default: false,
    is_active: true,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
  },
  {
    id: 'plan_legacy',
    name: 'Legacy',
    tpm: 50_000,
    rpm: 300,
    quota_monthly_tokens: 500_000,
    is_default: false,
    is_active: false,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-08-01T00:00:00Z',
  },
];

export const mockOrganizations: Organization[] = [
  {
    id: 'org_001',
    name: 'Acme Corporation',
    slug: 'acme',
    plan_id: 'plan_pro',
    status: 'active',
    user_count: 124,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-06-01T00:00:00Z',
    retryable: false,
  },
  {
    id: 'org_002',
    name: 'Example Inc',
    slug: 'example',
    plan_id: 'plan_pro',
    status: 'provisioning',
    user_count: 0,
    created_at: '2026-09-01T00:00:00Z',
    updated_at: '2026-09-01T00:00:00Z',
    retryable: false,
  },
  {
    id: 'org_003',
    name: 'Startup X',
    slug: 'startup-x',
    plan_id: 'plan_free',
    status: 'failed',
    user_count: 0,
    created_at: '2026-09-05T00:00:00Z',
    updated_at: '2026-09-05T00:00:00Z',
    retryable: true,
  },
  {
    id: 'org_004',
    name: 'Broken Co',
    slug: 'broken-co',
    plan_id: 'plan_free',
    status: 'failed',
    user_count: 0,
    created_at: '2026-09-06T00:00:00Z',
    updated_at: '2026-09-06T00:00:00Z',
    // TODO: confirm — a permanently-unretryable failure has no real distinct backend
    // representation yet (every `failed` org is retryable per create_org_workflow's only
    // failure path today); kept as a mock-only edge case for the UI to render correctly if/when
    // the backend adds one.
    retryable: false,
  },
];

export const mockPermissions: Permission[] = [
  {
    id: 'perm_users_read',
    resource: 'users',
    action: 'read',
    slug: 'users.read',
    description: 'View users',
    is_active: true,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
  },
  {
    id: 'perm_users_create',
    resource: 'users',
    action: 'create',
    slug: 'users.create',
    description: 'Create users',
    is_active: true,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
  },
];

export const mockUserDirectory: UserDirectoryEntry[] = [
  {
    id: 'usr_001',
    organization_id: 'org_001',
    username: 'john',
    display_name: 'John Smith',
    email: 'john@acme.com',
    status: 'active',
    created_at: '2026-01-05T00:00:00Z',
    updated_at: '2026-06-01T00:00:00Z',
  },
  {
    id: 'usr_002',
    organization_id: 'org_002',
    username: 'jane',
    display_name: 'Jane Doe',
    email: 'jane@example.com',
    status: 'active',
    created_at: '2026-02-01T00:00:00Z',
    updated_at: '2026-07-01T00:00:00Z',
  },
];

export const mockUserDetail: UserDetail = {
  id: 'usr_001',
  organization_id: 'org_001',
  username: 'john',
  email: 'john@acme.com',
  first_name: 'John',
  last_name: 'Smith',
  display_name: 'John Smith',
  status: 'active',
  metadata: {},
  roles: ['Admin', 'Developer'],
  effective_permissions: ['users.read', 'users.create', 'roles.read'],
  created_at: '2026-01-05T00:00:00Z',
  updated_at: '2026-06-01T00:00:00Z',
  last_login_at: '2026-09-09T12:30:00Z',
};

// TODO: confirm — platform summary endpoint is now implemented server-side
// (GET /v1/platform-summary) but this app still mock-first develops against MSW (ADR-022).
export const mockPlatformSummary: PlatformSummary = {
  organizations: mockOrganizations.length,
  active_users: 2841,
  plans: mockPlans.length,
  provisioning: mockOrganizations.filter((o) => o.status === 'provisioning').length,
  active: mockOrganizations.filter((o) => o.status === 'active').length,
  failed: mockOrganizations.filter((o) => o.status === 'failed').length,
};
