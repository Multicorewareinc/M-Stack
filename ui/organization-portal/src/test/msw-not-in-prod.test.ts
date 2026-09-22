import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

describe('production build excludes MSW', () => {
  it('imports mocks/browser dynamically, gated behind shouldUseMockAuth', () => {
    const main = readFileSync(resolve(process.cwd(), 'src/main.tsx'), 'utf8');
    expect(main).not.toMatch(/^import .* from ['"]\.\/mocks\/browser['"]/m);
    expect(main).toMatch(/await import\(['"]\.\/mocks\/browser['"]\)/);
    expect(main).toMatch(/if \(!shouldUseMockAuth\(\)\) return;/);
  });
});
