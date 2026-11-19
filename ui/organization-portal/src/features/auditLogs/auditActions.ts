import type { BadgeProps } from '@multistack/ui';

/** The event names an organization's trail can contain — drives the "Event" filter. */
export const AUDIT_ACTIONS = [
  'user.created',
  'user.updated',
  'user.deleted',
  'user.password_reset',
  'user.roles_updated',
  'role.created',
  'role.updated',
  'role.deleted',
  'role.permissions_updated',
  'api_key.created',
  'api_key.rotated',
  'api_key.updated',
  'api_key.revoked',
  'auth.login',
  'auth.login_failed',
  'auth.password_changed',
  'invitation.created',
  'invitation.accepted',
  'invitation.revoked',
] as const;

const SUBJECTS: Record<string, string> = { api_key: 'API key', auth: 'Auth' };

/** 'api_key.created' -> 'API key created'; 'role.permissions_updated' -> 'Role permissions updated'. */
export function actionLabel(action: string): string {
  const [subject = '', verb = ''] = action.split('.');
  const words = (s: string) => s.replace(/_/g, ' ');
  const label = `${SUBJECTS[subject] ?? words(subject)} ${words(verb)}`.trim();
  return label.charAt(0).toUpperCase() + label.slice(1);
}

export function actionVariant(action: string): NonNullable<BadgeProps['variant']> {
  if (action.endsWith('deleted') || action.endsWith('revoked') || action.endsWith('login_failed')) return 'danger';
  if (action.endsWith('created')) return 'success';
  if (action.startsWith('auth.')) return 'info';
  if (action.endsWith('password_reset')) return 'warning';
  return 'primary';
}

/** An 'admin' actor is a platform super-admin acting on this organization's behalf — say so, since
 * they are not a member the org's own admins will recognise. */
export function actorLabel(log: { actor_type: string; actor_email: string | null }): string {
  const who = log.actor_email ?? (log.actor_type === 'service' ? 'System' : 'Unknown');
  return log.actor_type === 'admin' ? `${who} (platform admin)` : who;
}

/** Who performed an event, for the detail view. */
const ACTOR_TYPE_LABELS: Record<string, string> = {
  user: 'Organization member',
  admin: 'Platform admin',
  service: 'Service key',
  anonymous: 'Anonymous',
};

export function actorTypeLabel(actorType: string): string {
  return ACTOR_TYPE_LABELS[actorType] ?? actorType;
}
