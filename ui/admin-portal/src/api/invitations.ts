import { request } from './client';

export type InviteRole = 'org_admin' | 'org_user';

export interface InviteInput {
  email: string;
  name?: string;
  role: InviteRole;
  /** Invite into an existing organization... */
  organization_id?: string;
  /** ...or create this organization first. Exactly one of the two. */
  new_organization?: { name: string; plan_id: string };
}

/** POST /v1/invitations response (Admin CP proxying Org CP). The raw token only ever appears inside
 * `invite_path` / `invite_url` of THIS response — it cannot be fetched again. */
export interface Invitation {
  id: string;
  email: string;
  name: string | null;
  organization_id: string;
  organization_name: string;
  role_id: string;
  role_name: string;
  expires_at: string;
  /** Path the invitee opens in the Organization Portal, e.g. /accept-invite?token=... */
  /** Only present when the email was NOT sent: otherwise the invitee has the link and the API omits it. */
  invite_path: string | null;
  /** Absolute link, present only when the Org CP knows the Organization Portal's public URL. */
  invite_url: string | null;
  /** Whether the invitation was also emailed. The link above always works either way. */
  email_status: 'sent' | 'not_configured' | 'failed' | 'rejected';
  /** Why nothing was sent, when `email_status` is not 'sent'. */
  email_detail: string | null;
  organization_created: boolean;
}

export function createInvitation(input: InviteInput, accessToken?: string | null): Promise<Invitation> {
  return request<Invitation>('/invitations', { method: 'POST', body: input, accessToken });
}

/** One row of the pending-invitations list. Never carries the token (only its hash is stored). */
export interface InvitationListItem {
  id: string;
  email: string;
  name: string | null;
  organization_id: string;
  role_id: string;
  role_name: string;
  invited_by: string | null;
  created_at: string;
  expires_at: string;
}

export interface InvitationPage {
  items: InvitationListItem[];
  total: number;
  limit: number;
  offset: number;
}

export interface InvitationFilters {
  organizationId?: string;
  /** Case-insensitive substring of the invitee's email. */
  search?: string;
  /** Exact role name. */
  role?: string;
}

/** GET /v1/invitations — pending invitations across every organization, newest first. */
export function listInvitations(
  filters: InvitationFilters,
  limit: number,
  offset: number,
  accessToken?: string | null,
): Promise<InvitationPage> {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (filters.organizationId) params.set('organization_id', filters.organizationId);
  if (filters.search) params.set('search', filters.search);
  if (filters.role) params.set('role', filters.role);
  return request<InvitationPage>(`/invitations?${params.toString()}`, { accessToken });
}

/** DELETE /v1/invitations/{id} — cancels a pending invitation so its link stops working. */
export function revokeInvitation(id: string, accessToken?: string | null): Promise<void> {
  return request<void>(`/invitations/${id}`, { method: 'DELETE', accessToken });
}

/** The link to hand to the invitee. The invitation is accepted in the Organization Portal, which is a
 * different origin from this portal, so prefer the server's absolute URL, then VITE_ORG_PORTAL_URL;
 * only as a last resort fall back to this portal's own origin (right for single-origin deployments). */
export function inviteLink(invitation: Pick<Invitation, 'invite_path' | 'invite_url'>): string {
  if (invitation.invite_url) return invitation.invite_url;
  if (!invitation.invite_path) return ''; // emailed: the API sends no link back
  const base = (import.meta.env.VITE_ORG_PORTAL_URL || window.location.origin).replace(/\/+$/, '');
  return `${base}${invitation.invite_path}`;
}
