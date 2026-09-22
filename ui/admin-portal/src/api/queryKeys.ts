// Admin query keys (§176). Admin CP data is platform-scoped, not tenant-scoped,
// so no organization id is threaded through these — see organization-portal's
// queryKeys.ts for the tenant-aware convention.
export const queryKeys = {
  // `search` is part of the key so each search term caches independently;
  // invalidation uses the bare ['organizations'] prefix (TanStack Query
  // matches by prefix by default) so every search-scoped variant is covered
  // by one invalidateQueries call — see features/organizations hooks.
  organizations: (search = '') => ['organizations', search] as const,
  organization: (id: string) => ['organization', id] as const,
  organizationUsers: (id: string) => ['organization-users', id] as const,
  plans: () => ['plans'] as const,
  permissions: () => ['permissions'] as const,
  users: () => ['users'] as const,
  user: (id: string) => ['user', id] as const,
  platformSummary: () => ['platform-summary'] as const,
};
