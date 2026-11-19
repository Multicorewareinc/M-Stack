// Invitations — modules/invitations on organization-control-plane. Like /api/audit-logs and
// /api/api-keys these live at /api/invitations, NOT under getBaseUrl()'s /v1 prefix:
//  - POST/GET /api/invitations and DELETE /api/invitations/{id} are an end-user JWT surface (org taken
//    from the verified token server-side, so a signed-in admin only ever sees their own),
//  - POST /api/invitations/inspect and /accept are PUBLIC — the invitee has no account yet; the
//    unguessable one-time token in the body is their credential.
import { ApiError, type ApiErrorCode } from './client';
import { getAccessToken } from '../auth/tokenStore';

const BASE = '/api/invitations';

export interface InviteInput {
  email: string;
  name?: string;
  /** Picked from this organization's own roles (the server rejects another tenant's role id). */
  role_id?: string;
}

/** The raw token only ever appears inside `invite_path` / `invite_url` of this response. */
export interface Invitation {
  id: string;
  email: string;
  name: string | null;
  organization_id: string;
  organization_name: string;
  role_id: string;
  role_name: string;
  expires_at: string;
  /** Path the invitee opens, e.g. /accept-invite?token=... */
  /** Only present when the email was NOT sent: otherwise the invitee has the link and the API omits it. */
  invite_path: string | null;
  /** Absolute link, present only when the Org CP knows this portal's public URL. */
  invite_url: string | null;
  /** Whether the invitation was also emailed. The link above always works either way. */
  email_status: 'sent' | 'not_configured' | 'failed' | 'rejected';
  /** Why nothing was sent, when `email_status` is not 'sent'. */
  email_detail: string | null;
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
  /** Case-insensitive substring of the invitee's email. */
  search?: string;
  /** Exact role name. */
  role?: string;
}

export interface InvitationDetails {
  email: string;
  name: string | null;
  organization_name: string;
  role_name: string;
  expires_at: string;
}

const STATUS_TO_CODE: Record<number, ApiErrorCode> = {
  401: 'UNAUTHORIZED',
  403: 'FORBIDDEN',
  404: 'NOT_FOUND',
  409: 'CONFLICT',
  410: 'NOT_FOUND', // gone: an expired/used link — callers tell it apart by ApiError.status
  422: 'VALIDATION_ERROR',
};

async function call<T>(
  method: 'GET' | 'POST' | 'DELETE',
  path: string,
  body: unknown,
  authenticated: boolean,
): Promise<T | undefined> {
  const headers: Record<string, string> = {};
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (authenticated) headers.Authorization = `Bearer ${getAccessToken() ?? ''}`;

  let res: Response;
  try {
    res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  } catch {
    throw new ApiError('INTERNAL_ERROR', 'Unable to reach the server.');
  }
  if (res.status === 204) return undefined;

  const isJson = res.headers.get('content-type')?.includes('application/json');
  const payload = isJson ? await res.json().catch(() => undefined) : undefined;
  if (!res.ok) {
    throw new ApiError(
      STATUS_TO_CODE[res.status] ?? 'INTERNAL_ERROR',
      payload?.error?.message ?? 'Request failed.',
      res.status,
      payload?.error?.field,
    );
  }
  return payload as T;
}

export async function createInvitation(input: InviteInput): Promise<Invitation> {
  return (await call<Invitation>('POST', BASE, input, true)) as Invitation;
}

export async function listInvitations(
  filters: InvitationFilters,
  limit: number,
  offset: number,
): Promise<InvitationPage> {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (filters.search) params.set('search', filters.search);
  if (filters.role) params.set('role', filters.role);
  return (await call<InvitationPage>('GET', `${BASE}?${params.toString()}`, undefined, true)) as InvitationPage;
}

/** Cancels a pending invitation so its link stops working. */
export async function revokeInvitation(id: string): Promise<void> {
  await call<void>('DELETE', `${BASE}/${id}`, undefined, true);
}

export async function inspectInvitation(token: string): Promise<InvitationDetails> {
  return (await call<InvitationDetails>('POST', `${BASE}/inspect`, { token }, false)) as InvitationDetails;
}

export async function acceptInvitation(token: string, password: string): Promise<void> {
  await call<void>('POST', `${BASE}/accept`, { token, password }, false);
}

/** The link to hand to the invitee: the server's absolute URL when it knows one, else this portal's own
 * origin (the invitation is accepted here, so that is the right base). */
export function inviteLink(invitation: Pick<Invitation, 'invite_path' | 'invite_url'>): string {
  if (invitation.invite_url) return invitation.invite_url;
  if (!invitation.invite_path) return ''; // emailed: the API sends no link back
  return `${window.location.origin}${invitation.invite_path}`;
}
