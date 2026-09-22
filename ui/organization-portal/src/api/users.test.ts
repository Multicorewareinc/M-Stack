import { describe, expect, it } from 'vitest';
import { mockUsers } from '../mocks/fixtures';
import type { User } from './types';
import { createUser, listUsers } from './users';

describe('users api', () => {
  it('mock fixture matches User type', () => {
    const user: User = mockUsers[0];
    for (const key of ['id', 'organization_id', 'username', 'email', 'display_name', 'status'] as const) {
      expect(user[key], `missing field: ${key}`).toBeDefined();
    }
  });

  it('MSW returns seeded users', async () => {
    const users = await listUsers();
    expect(users).toHaveLength(mockUsers.length);
    expect(users[0].display_name).toBe('John Smith');
  });

  it('backend rejects unauthorized create independent of UI', async () => {
    await expect(createUser({ username: 'x', email: 'x@acme.com' }, [])).rejects.toMatchObject({ code: 'FORBIDDEN' });
  });
});
