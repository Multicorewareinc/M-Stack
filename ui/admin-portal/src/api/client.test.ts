import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError, getBaseUrl, request } from './client';

describe('api client', () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.restoreAllMocks();
  });

  it('defaults base path to /v1', () => {
    vi.stubEnv('VITE_API_BASE_URL', '');
    // Must fail if it hard-codes /api/v1.
    expect(getBaseUrl()).toBe('/v1');
  });

  it('maps HTTP statuses to error taxonomy', async () => {
    const cases: [number, string][] = [
      [401, 'UNAUTHORIZED'],
      [403, 'FORBIDDEN'],
      [404, 'NOT_FOUND'],
      [409, 'CONFLICT'],
      [422, 'VALIDATION_ERROR'],
      [429, 'RATE_LIMITED'],
      [500, 'INTERNAL_ERROR'],
    ];
    for (const [status, code] of cases) {
      vi.stubGlobal(
        'fetch',
        vi.fn().mockResolvedValue(
          // The real backend's envelope (ADR-003) — never a flat `{ message }`.
          new Response(JSON.stringify({ error: { message: 'x', type: 'whatever' } }), {
            status,
            headers: { 'content-type': 'application/json' },
          }),
        ),
      );
      await expect(request('/whatever')).rejects.toMatchObject({ code });
    }
  });

  it('unwraps the real error envelope for message and field', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ error: { message: 'Plan name already exists', type: 'conflict', field: 'name' } }), {
          status: 409,
          headers: { 'content-type': 'application/json' },
        }),
      ),
    );
    await expect(request('/whatever')).rejects.toMatchObject({
      code: 'CONFLICT',
      message: 'Plan name already exists',
      field: 'name',
    });
  });

  it('falls back to a generic message when the body has no error envelope', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('', { status: 500 })));
    await expect(request('/whatever')).rejects.toMatchObject({
      code: 'INTERNAL_ERROR',
      message: 'Request failed with status 500',
      field: undefined,
    });
  });

  it('maps a network failure to INTERNAL_ERROR', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));
    // Must fail if a rejected fetch propagates unmapped.
    await expect(request('/whatever')).rejects.toBeInstanceOf(ApiError);
    await expect(request('/whatever')).rejects.toMatchObject({ code: 'INTERNAL_ERROR' });
  });

  it('contains no static credential', () => {
    const source = readFileSync(resolve(process.cwd(), 'src/api/client.ts'), 'utf8');
    // Must fail if a hard-coded key/token string is introduced.
    expect(source).not.toMatch(/Bearer\s+[A-Za-z0-9._-]{10,}/);
    expect(source).not.toMatch(/service[_-]?key\s*=\s*['"][^'"]+['"]/i);
  });
});
