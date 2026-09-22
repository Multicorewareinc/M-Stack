import { describe, expect, it } from 'vitest';
import { mockOrganizations } from '../mocks/fixtures';
import type { Organization } from './types';
import { listOrganizations } from './organizations';

describe('organizations api', () => {
  it('mock fixture matches Organization type', () => {
    const org: Organization = mockOrganizations[0];
    // Must fail if a required field is absent from the fixture.
    for (const key of ['id', 'name', 'slug', 'plan_id', 'status', 'user_count', 'created_at', 'updated_at'] as const) {
      expect(org[key], `missing field: ${key}`).toBeDefined();
    }
  });

  it('MSW returns seeded organizations', async () => {
    const orgs = await listOrganizations();
    expect(orgs).toHaveLength(mockOrganizations.length);
    expect(orgs[0].name).toBe('Acme Corporation');
  });
});
