import { readdirSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const componentsDir = resolve(process.cwd(), 'src/components');

function componentSources(): { file: string; text: string }[] {
  return readdirSync(componentsDir)
    .filter((f) => f.endsWith('.tsx') && !f.endsWith('.test.tsx'))
    .map((f) => ({ file: f, text: readFileSync(resolve(componentsDir, f), 'utf8') }));
}

describe('components use tokens only', () => {
  it('contain no raw hex color literals', () => {
    // Must fail if a component introduces a raw #rrggbb / #rgb color.
    const hex = /#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b/;
    for (const { file, text } of componentSources()) {
      expect(hex.test(text), `raw hex color found in ${file}`).toBe(false);
    }
  });

  it('contain no arbitrary z-index literals', () => {
    // Must fail if a component sets an inline z-index instead of a token class.
    const arbitraryZ = /z-\[\d+\]|zIndex:\s*\d+/;
    for (const { file, text } of componentSources()) {
      expect(arbitraryZ.test(text), `arbitrary z-index found in ${file}`).toBe(false);
    }
  });
});
