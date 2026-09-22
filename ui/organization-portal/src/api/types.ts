// Typed resource models — the Organization Portal's view of Org CP data.
// Architecture reference: rbac_four_container_architecture.md §14-16, §25-27.
// Field-for-field matches the real backend's Pydantic schemas
// (api/microservices/organization-control-plane/src/modules/*/schemas.py) — reconciled after
// the mock-first build; see docs/api-contracts.md for anything still mocked.

export interface User {
  id: string;
  organization_id: string;
  username: string;
  email: string;
  first_name?: string | null;
  last_name?: string | null;
  display_name?: string | null;
  status: string;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  last_login_at?: string | null;
}

export interface Role {
  id: string;
  organization_id: string;
  name: string;
  description?: string | null;
  is_system_role: boolean;
  created_at: string;
  updated_at: string;
  // TODO: confirm — backend-derived counts for the list view (§61, §109),
  // mirroring how Organization.user_count already works (Architecture §9).
  // Not yet part of the real Org CP contract; the UI never computes these
  // by paging through users/permissions client-side.
  user_count?: number;
  permission_count?: number;
}

/** Read-only reference/projection of the Admin CP master permission list (§26). */
export interface Permission {
  id: string;
  resource: string;
  action: string;
  slug: string;
  description?: string;
  is_active: boolean;
}

/** Real response is `{ permission_ids }` only (Org CP's PermissionSet) — no `role_id`. */
export interface RolePermission {
  permission_ids: string[];
}

export interface OrgSummary {
  organization: { name: string; slug: string; status: string };
  user_count: number;
  role_count: number;
  plan: {
    name: string;
    tpm: number;
    rpm: number;
    quota_monthly_tokens: number;
  };
}
