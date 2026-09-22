import type { Organization } from '../../api/types';

export type OrgActionKey = 'view' | 'change-plan' | 'suspend' | 'retry' | 'delete';

export interface OrgRowAction {
  label: string;
  key: OrgActionKey;
  destructive?: boolean;
}

/**
 * Status -> available row actions. Retry is offered only for a `failed`
 * organization the backend explicitly marks retryable (§92, §114) — never
 * inferred from status alone. Change Plan / Suspend only apply once active;
 * provisioning/suspended rows get View only.
 *
 * Editing organization fields is not a separate action here — this spec
 * builds the Detail page's Configuration tab (§105) as the place field
 * changes will surface; a dedicated inline-edit form is a future addition,
 * not modeled by this spec's requirements.
 */
export function getAvailableActions(org: Organization): OrgRowAction[] {
  const actions: OrgRowAction[] = [{ label: 'View', key: 'view' }];

  if (org.status === 'active') {
    actions.push(
      { label: 'Change Plan', key: 'change-plan' },
      { label: 'Suspend', key: 'suspend', destructive: true },
    );
  } else if (org.status === 'failed' && org.retryable) {
    actions.push({ label: 'Retry Provisioning', key: 'retry' });
  }

  // Not offered while provisioning is in flight — nothing to hard-delete mid-workflow.
  if (org.status !== 'provisioning') {
    actions.push({ label: 'Delete', key: 'delete', destructive: true });
  }

  return actions;
}
