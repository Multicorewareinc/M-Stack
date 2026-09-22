import { expect, test } from '@playwright/test';

test('backend rejects a forced request with no permission header', async ({ page }) => {
  await page.goto('/users');
  await expect(page.getByRole('heading', { name: 'Users' })).toBeVisible();

  // A raw fetch from inside the page's own origin so MSW's registered
  // Service Worker intercepts it (a Node-side Playwright request would
  // instead hit the dev server's /v1 proxy and miss MSW entirely). This
  // bypasses the app's own usePermission-gated createUser() wrapper, which
  // is what "forced" means here — no permission header is attached.
  const status = await page.evaluate(async () => {
    const res = await fetch('/v1/users', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: 'forced', email: 'forced@example.com' }),
    });
    return res.status;
  });

  // Must fail if the forced request succeeds.
  expect(status).toBe(403);
});
