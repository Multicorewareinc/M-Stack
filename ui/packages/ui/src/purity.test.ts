import { readdirSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const componentsDir = resolve(process.cwd(), 'src/components');

function componentSources(): { file: string; text: string }[] {
  return readdirSync(componentsDir)
    .filter((f) => f.endsWith('.tsx') && !f.endsWith('.test.tsx'))
    .map((f) => ({ file: f, text: readFileSync(resolve(componentsDir, f), 'utf8') }));
}

describe('package has no network, routing, or auth', () => {
  const banned: { name: string; re: RegExp }[] = [
    { name: 'fetch()', re: /\bfetch\s*\(/ },
    { name: 'axios', re: /from ['"]axios['"]/ },
    { name: 'react-router', re: /react-router/ },
    { name: 'XMLHttpRequest', re: /XMLHttpRequest/ },
    { name: 'WebSocket', re: /new\s+WebSocket/ },
    { name: 'auth seam', re: /useAuth|AuthProvider|getAccessToken/ },
  ];

  it('components import no network, routing, or auth', () => {
    for (const { file, text } of componentSources()) {
      for (const b of banned) {
        expect(b.re.test(text), `${b.name} found in ${file}`).toBe(false);
      }
    }
  });
});
