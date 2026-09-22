import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

// MSW must not load in a production build. Enforced structurally: the mocks
// module is imported dynamically (so bundlers code-split it into its own
// chunk, per MSW's documented pattern) and the import is gated behind
// shouldUseMockAuth(), which folds to `false` when Vite inlines
// import.meta.env.DEV/MODE for a production build (see auth/env.ts).
describe('production build excludes MSW', () => {
  it('imports mocks/browser dynamically, gated behind shouldUseMockAuth', () => {
    const main = readFileSync(resolve(process.cwd(), 'src/main.tsx'), 'utf8');
    // Must fail if MSW ships to production: a static top-level import of
    // mocks/browser would always be bundled into the main entry chunk.
    expect(main).not.toMatch(/^import .* from ['"]\.\/mocks\/browser['"]/m);
    expect(main).toMatch(/await import\(['"]\.\/mocks\/browser['"]\)/);
    expect(main).toMatch(/if \(!shouldUseMockAuth\(\)\) return;/);
  });
});
