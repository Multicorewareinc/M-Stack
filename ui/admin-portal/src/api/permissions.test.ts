import { describe, expect, it } from 'vitest';
import { createMasterPermission, listPermissions } from './permissions';

describe('permissions api', () => {
  it('backend rejects unauthorized mutation independent of UI', async () => {
    // The UI may have hidden the Create button, but if the call is made anyway
    // (e.g. via devtools, or a UI bug), the mock backend must still say no.
    await expect(createMasterPermission({ resource: 'users', action: 'delete' }, [])).rejects.toMatchObject({
      code: 'FORBIDDEN',
    });
  });

  it('allows the mutation when the mock identity carries the permission', async () => {
    const result = await createMasterPermission({ resource: 'users', action: 'delete' }, ['permissions.create']);
    expect(result.id).toBeDefined();
  });

  it('lists permissions from MSW', async () => {
    const perms = await listPermissions();
    expect(perms.length).toBeGreaterThan(0);
  });
});
