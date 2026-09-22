import { describe, expect, it } from 'vitest';
import { shouldUseMockAuth } from './env';

describe('shouldUseMockAuth', () => {
  it('is true under the test runner', () => {
    // Vitest sets MODE to 'test'.
    expect(shouldUseMockAuth()).toBe(true);
  });
});
