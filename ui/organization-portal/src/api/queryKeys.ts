// Org query keys (§177). Organization-scoped data includes the organization id
// so cached queries never leak across tenants when the mocked identity/org
// context switches (§174-175).
export function scopedKey(base: string, orgId: string): readonly [string, string] {
  return [base, orgId] as const;
}

export const queryKeys = {
  organization: (orgId: string) => scopedKey('organization', orgId),
  users: (orgId: string) => scopedKey('users', orgId),
  user: (orgId: string, userId: string) => ['user', orgId, userId] as const,
  userRoles: (orgId: string, userId: string) => ['user-roles', orgId, userId] as const,
  roles: (orgId: string) => scopedKey('roles', orgId),
  role: (orgId: string, roleId: string) => ['role', orgId, roleId] as const,
  rolePermissions: (orgId: string, roleId: string) => ['role-permissions', orgId, roleId] as const,
  permissions: (orgId: string) => scopedKey('permissions', orgId),
  apiKeys: (orgId: string) => scopedKey('api-keys', orgId),
  usageSummary: (orgId: string) => scopedKey('usage-summary', orgId),
  usageTimeseries: (orgId: string) => scopedKey('usage-timeseries', orgId),
  usageByUser: (orgId: string) => scopedKey('usage-by-user', orgId),
  usageByKey: (orgId: string) => scopedKey('usage-by-key', orgId),
};
