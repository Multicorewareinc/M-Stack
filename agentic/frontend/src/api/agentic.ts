/**
 * Client for the agentic layer's HTTP API (agentic/backend/src/router.py).
 *
 * Three endpoints, one response shape. `status` is the whole protocol:
 * "needs_input" means the model asked something and is waiting, "resolved"
 * means a script was produced and lives in `script`.
 *
 * No API key here on purpose -- the dev proxy attaches X-API-Key
 * server-side (see vite.config.ts), so the shared service key never
 * reaches the browser.
 */

export type SessionStatus = 'needs_input' | 'resolved';

export interface SessionResponse {
  session_id: string;
  status: SessionStatus;
  /** The model's question, or its final reply text. */
  message: string | null;
  /** The generated script, present once status === "resolved". */
  script: string | null;
}

const BASE = '/agentic';

export class AgenticError extends Error {
  constructor(
    message: string,
    readonly status?: number,
  ) {
    super(message);
    this.name = 'AgenticError';
  }
}

async function parse(response: Response): Promise<SessionResponse> {
  if (!response.ok) {
    // The service returns a JSON error body (agentic/backend/src/errors.py); fall
    // back to the status line when it doesn't, so a proxy failure or an
    // unreachable backend still surfaces something readable.
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = (await response.json()) as { detail?: string; message?: string };
      detail = body.detail ?? body.message ?? detail;
    } catch {
      /* non-JSON body -- keep the status line */
    }
    throw new AgenticError(detail, response.status);
  }
  return (await response.json()) as SessionResponse;
}

/** Starts a new conversation. The returned session_id drives every later call. */
export async function startSession(message: string): Promise<SessionResponse> {
  const response = await fetch(`${BASE}/v1/sessions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message }),
  });
  return parse(response);
}

/** Sends another message on an existing conversation. */
export async function continueSession(sessionId: string, message: string): Promise<SessionResponse> {
  const response = await fetch(`${BASE}/v1/sessions/${encodeURIComponent(sessionId)}/messages`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message }),
  });
  return parse(response);
}

/** Read-only status check -- what state a session is in, without sending anything. */
export async function getSession(sessionId: string): Promise<SessionResponse> {
  const response = await fetch(`${BASE}/v1/sessions/${encodeURIComponent(sessionId)}`);
  return parse(response);
}

/* ------------------------------------------------------------ streaming */

/** One progress event from a streamed turn (see the backend's
 * orchestration/graph.py stream_events). `final` is always last and
 * carries the same fields the blocking endpoints return. */
export type StreamEvent =
  | { type: 'tool_call'; tool: string; args: Record<string, unknown> }
  | { type: 'reasoning'; text: string }
  | { type: 'tool_result'; tool: string; valid: boolean | null; error: string | null }
  | ({ type: 'final' } & SessionResponse);

/** Everything a turn emits before its `final` event -- i.e. the work the
 * model did to get to its answer, which the UI keeps alongside the reply. */
export type TraceEvent = Exclude<StreamEvent, { type: 'final' }>;

/**
 * Sends a message and yields progress events as the turn actually runs,
 * so the UI can show which tool is being called while the model is still
 * working rather than sitting on a spinner for the whole turn.
 *
 * NDJSON over a plain fetch body rather than EventSource: these are POSTs
 * and the dev proxy attaches an auth header, neither of which EventSource
 * can do. A partial line at the end of a chunk is held back until the
 * rest of it arrives -- chunk boundaries and line boundaries are
 * unrelated, and parsing half a JSON object throws.
 */
export async function* streamMessage(
  sessionId: string | null,
  message: string,
): AsyncGenerator<StreamEvent> {
  const url = sessionId
    ? `${BASE}/v1/sessions/${encodeURIComponent(sessionId)}/messages/stream`
    : `${BASE}/v1/sessions/stream`;

  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message }),
  });

  if (!response.ok || !response.body) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = (await response.json()) as { detail?: string; message?: string };
      detail = body.detail ?? body.message ?? detail;
    } catch {
      /* non-JSON body -- keep the status line */
    }
    throw new AgenticError(detail, response.status);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    const lines = buffer.split('\n');
    buffer = lines.pop() ?? '';
    for (const line of lines) {
      if (line.trim()) yield JSON.parse(line) as StreamEvent;
    }
  }
  if (buffer.trim()) yield JSON.parse(buffer) as StreamEvent;
}
