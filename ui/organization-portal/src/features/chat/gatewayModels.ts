// GET /v1/models on the Model Gateway — the same per-request-authenticated /v1 surface as
// chat/completions, so it needs the chat key too (see chatKey.ts). A separate tiny client rather
// than api/client.ts's request(), which defaults to organization-control-plane's /v1 prefix —
// this hits the gateway's own /mg/v1 proxy path instead.
import { ApiError } from '../../api/client';

export interface GatewayModel {
  id: string;
}

export async function listGatewayModels(apiKey: string): Promise<GatewayModel[]> {
  let res: Response;
  try {
    res = await fetch('/mg/v1/models', { headers: { Authorization: `Bearer ${apiKey}` } });
  } catch {
    throw new ApiError('INTERNAL_ERROR', 'Unable to reach the model gateway.');
  }
  if (!res.ok) {
    throw new ApiError(res.status === 401 ? 'UNAUTHORIZED' : 'INTERNAL_ERROR', 'Unable to load models.', res.status);
  }
  const payload = (await res.json()) as { data?: Array<{ id: string }> };
  return (payload.data ?? []).map((m) => ({ id: m.id }));
}
