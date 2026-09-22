import { describe, expect, it } from 'vitest';
import { queryKeys } from './queryKeys';

describe('tenant-aware query keys', () => {
  it('includes organization id in scoped keys', () => {
    // Must fail if the key omits org context.
    expect(queryKeys.users('org_001')).toEqual(['users', 'org_001']);
    expect(queryKeys.roles('org_001')).toEqual(['roles', 'org_001']);
    expect(queryKeys.rolePermissions('org_001', 'role_admin')).toEqual(['role-permissions', 'org_001', 'role_admin']);
  });

  it('differs between organizations', () => {
    expect(queryKeys.users('org_001')).not.toEqual(queryKeys.users('org_002'));
  });
});
