import { describe, expect, it } from 'vitest';
import type { Organization } from '../../api/types';
import { getAvailableActions } from './actions';

function org(overrides: Partial<Organization>): Organization {
  return {
    id: 'org_x',
    name: 'X',
    slug: 'x',
    plan_id: 'plan_free',
    status: 'active',
    user_count: 0,
    retryable: false,
    created_at: '',
    updated_at: '',
    ...overrides,
  };
}

describe('getAvailableActions', () => {
  it('offers Change Plan and Suspend for active', () => {
    const keys = getAvailableActions(org({ status: 'active' })).map((a) => a.key);
    expect(keys).toEqual(['view', 'change-plan', 'suspend', 'delete']);
  });

  it('offers only View for provisioning, and never Delete mid-provisioning', () => {
    const keys = getAvailableActions(org({ status: 'provisioning' })).map((a) => a.key);
    expect(keys).toEqual(['view']);
  });

  it('offers Retry for failed + retryable, not for failed + non-retryable', () => {
    expect(getAvailableActions(org({ status: 'failed', retryable: true })).map((a) => a.key)).toEqual([
      'view',
      'retry',
      'delete',
    ]);
    // Must fail if Retry appears regardless of the backend flag.
    expect(getAvailableActions(org({ status: 'failed', retryable: false })).map((a) => a.key)).toEqual([
      'view',
      'delete',
    ]);
  });

  it('offers Delete for every status except provisioning', () => {
    for (const status of ['active', 'failed', 'suspended'] as const) {
      expect(getAvailableActions(org({ status })).map((a) => a.key)).toContain('delete');
    }
    expect(getAvailableActions(org({ status: 'provisioning' })).map((a) => a.key)).not.toContain('delete');
  });

  it('never offers Suspend for a non-active organization', () => {
    for (const status of ['provisioning', 'failed', 'suspended'] as const) {
      const keys = getAvailableActions(org({ status })).map((a) => a.key);
      // Must fail if Suspend appears for a non-active organization.
      expect(keys).not.toContain('suspend');
    }
  });
});
