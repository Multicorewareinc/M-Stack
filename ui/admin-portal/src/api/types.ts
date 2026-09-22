// Typed resource models — the Admin Portal's view of Admin CP data.
// Architecture reference: rbac_four_container_architecture.md §7-9, §17, §26.
// Field-for-field matches the real backend's Pydantic schemas
// (api/microservices/admin-control-plane/src/modules/*/schemas.py) — reconciled after the
// mock-first build; see docs/api-contracts.md for the endpoints that are still mocked.

// Real backend statuses are lowercase (OrgStatus/UserUpdate.status Literals) — 'suspended' is
// the only one a client may ever set; 'provisioning'/'failed' are workflow-only transitions.
export type OrganizationStatus = 'active' | 'provisioning' | 'failed' | 'suspended';

export interface Organization {
  id: string;
  name: string;
  slug: string;
  plan_id: string;
  status: OrganizationStatus;
  created_at: string;
  updated_at: string;
  /** Org CP user count, bulk-fetched server-side (never per-row, §184). */
  user_count: number;
  /** True only when `status === 'failed'` — the only case Org CP init can be retried. */
  retryable: boolean;
}

export interface Plan {
  id: string;
  name: string;
  tpm: number;
  rpm: number;
  quota_monthly_tokens: number;
  /** Server-controlled (seed-only); never accepted from client input. */
  is_default: boolean;
  /** Soft-deactivate — mirrors Permission.is_active. */
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

export interface Permission {
  id: string;
  resource: string;
  action: string;
  /** Server-computed as `{resource}.{action}` — never a client input, on create or update. */
  slug: string;
  description?: string;
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

/** Org CP's own user record, unscoped by org (Admin CP's platform-wide directory, §184). */
export interface UserDirectoryEntry {
  id: string;
  organization_id: string;
  username: string;
  display_name: string | null;
  email: string;
  status: string;
  created_at: string;
  updated_at: string;
}

/** Composed by Admin CP from the authoritative Org CP record + roles + the local permission
 * catalog (three calls, ADR-018) — not a single real endpoint's raw shape. */
export interface UserDetail {
  id: string;
  organization_id: string;
  username: string;
  email: string;
  first_name?: string | null;
  last_name?: string | null;
  display_name: string | null;
  status: string;
  metadata: Record<string, unknown>;
  roles: string[];
  /** Computed by Org CP through role composition (§108) — never client-computed. */
  effective_permissions: string[];
  created_at: string;
  updated_at: string;
  last_login_at?: string | null;
}

export interface PlatformSummary {
  organizations: number;
  active_users: number;
  plans: number;
  provisioning: number;
  active: number;
  failed: number;
}
