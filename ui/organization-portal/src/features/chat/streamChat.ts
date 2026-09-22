// Model Gateway streaming inference client (chat playground). The chat UI streams assistant
// tokens directly from the Model Gateway over the same-origin /mg/v1/chat/completions proxy,
// authenticated with a dedicated per-user chat API key (see chatKey.ts). This is a DEDICATED
// client — api/client.ts's request() buffers the whole body (await response.json()) and
// structurally cannot stream, so it is not reused here. EventSource is also unusable (GET-only,
// can't set an Authorization header or POST a body); fetch + ReadableStream is the correct
// primitive for an authenticated streaming POST.

import { ApiError } from '../../api/client';

export interface ChatMessageInput {
  role: string;
  content: string;
}

/** One rate-limit scope's current state, read from the gateway's X-RateLimit-{scope}-* response
 * headers (see model-gateway/src/dependencies.py's _stash_ratelimit_scopes). */
export interface RateLimitScopeUsage {
  limit: number;
  remaining: number;
  /** Unix seconds. */
  reset: number;
}

export interface RateLimitUsage {
  rpm?: RateLimitScopeUsage;
  tpm?: RateLimitScopeUsage;
}

export interface StreamChatParams {
  /** Raw chat key (sk-...), held in memory only — never persisted. */
  apiKey: string;
  model: string;
  /** Full prior history + the new user message, in order. */
  messages: ChatMessageInput[];
  /** Cancels the in-flight stream (unmount / New chat). */
  signal?: AbortSignal;
  /** Called once response headers arrive (both success and a 429 denial carry them — the
   * gateway stamps rate-limit headers before raising). Omitted scopes mean that limiter is
   * unconfigured (unlimited) for this org, not zero. */
  onRateLimitUsage?: (usage: RateLimitUsage) => void;
}

function readRateLimitUsage(headers: Headers): RateLimitUsage {
  const scope = (name: string): RateLimitScopeUsage | undefined => {
    const limit = headers.get(`X-RateLimit-${name}-Limit`);
    const remaining = headers.get(`X-RateLimit-${name}-Remaining`);
    const reset = headers.get(`X-RateLimit-${name}-Reset`);
    if (limit === null || remaining === null || reset === null) return undefined;
    return { limit: Number(limit), remaining: Number(remaining), reset: Number(reset) };
  };
  const usage: RateLimitUsage = {};
  const rpm = scope('Rpm');
  const tpm = scope('Tpm');
  if (rpm) usage.rpm = rpm;
  if (tpm) usage.tpm = tpm;
  return usage;
}

/**
 * Async generator yielding assistant content deltas until the stream ends (data: [DONE]).
 * Throws an ApiError on a pre-flight non-2xx response (with Retry-After/X-RateLimit-* from the
 * response HEADERS attached, since those never appear in the JSON body), and on a mid-stream
 * error frame from the gateway (e.g. an upstream truncation).
 */
export async function* streamChat(params: StreamChatParams): AsyncGenerator<string, void, unknown> {
  const { apiKey, model, messages, signal, onRateLimitUsage } = params;

  const response = await fetch('/mg/v1/chat/completions', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'text/event-stream',
      Authorization: `Bearer ${apiKey}`,
    },
    body: JSON.stringify({ model, messages, stream: true }),
    signal,
  });

  onRateLimitUsage?.(readRateLimitUsage(response.headers));

  if (!response.ok) {
    const text = await response.text();
    let parsed: { error?: { message?: string; type?: string } } | undefined;
    try {
      parsed = text ? JSON.parse(text) : undefined;
    } catch {
      parsed = undefined;
    }
    const message = parsed?.error?.message ?? `Request failed with status ${response.status}`;
    // Retry-After lives in the response HEADERS, not the JSON body — a body-only error parser
    // can never see it, so it's read explicitly here (same reasoning as the reference client
    // this was built from: rate-limit hints are header-only on model-gateway's 429).
    const retryAfterHeader = response.headers.get('Retry-After');
    const retryAfter = retryAfterHeader ? Number(retryAfterHeader) : undefined;
    throw new ApiError(
      response.status === 401 ? 'UNAUTHORIZED' : response.status === 429 ? 'RATE_LIMITED' : 'INTERNAL_ERROR',
      message,
      response.status,
      undefined,
      Number.isFinite(retryAfter) ? retryAfter : undefined,
    );
  }

  const body = response.body;
  if (!body) throw new ApiError('INTERNAL_ERROR', 'Stream returned no body');

  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // SSE frames are separated by a blank line.
      let sep: number;
      while ((sep = buffer.indexOf('\n\n')) !== -1) {
        const frame = buffer.slice(0, sep);
        buffer = buffer.slice(sep + 2);

        for (const line of frame.split('\n')) {
          const trimmed = line.trim();
          if (!trimmed.startsWith('data:')) continue; // ignore comments/keepalives
          const data = trimmed.slice('data:'.length).trim();
          if (data === '[DONE]') return;
          let chunk: unknown;
          try {
            chunk = JSON.parse(data);
          } catch {
            continue; // non-JSON keepalive
          }
          const obj = chunk as {
            error?: { type?: string; message?: string };
            choices?: Array<{ delta?: { content?: string } }>;
          };
          if (obj.error) {
            throw new ApiError('INTERNAL_ERROR', obj.error.message ?? 'Upstream stream terminated');
          }
          const delta = obj.choices?.[0]?.delta?.content;
          if (delta) yield delta;
        }
      }
    }
  } finally {
    try {
      await reader.cancel();
    } catch {
      /* no-op */
    }
  }
}
