import { expect, test } from '@playwright/test';

// Proves the tenant-cache-eviction path (useTenantCacheEviction / MockAuthProvider,
// SP-13 §174-175) end to end in a real browser: org_001's data must never remain
// visible after a live switch to org_002, and org_002's own distinct data must render.
// window.__setMockOrg is a test-only seam (never present outside dev/test, see
// auth/MockAuthProvider.tsx) that changes the org context without a full page reload,
// so this exercises the same code path a real org switch would go through.
test('switching org context evicts the previous tenant\'s data and loads the new tenant\'s', async ({ page }) => {
  await page.goto('/roles');

  await expect(page.getByRole('heading', { name: 'Roles' })).toBeVisible();
  await expect(page.getByText('Acme Corporation')).toBeVisible();
  await expect(page.getByRole('cell', { name: 'Admin' })).toBeVisible();
  await expect(page.getByRole('cell', { name: 'Developer' })).toBeVisible();

  await page.evaluate(() => {
    (window as unknown as { __setMockOrg: (ctx: { id: string; name: string }) => void }).__setMockOrg({
      id: 'org_002',
      name: 'Globex Corporation',
    });
  });

  await expect(page.getByText('Globex Corporation')).toBeVisible();
  await expect(page.getByRole('cell', { name: 'Billing Manager' })).toBeVisible();

  // Must fail if org_001's roles remain rendered (or servable from cache) after the switch.
  await expect(page.getByRole('cell', { name: 'Admin' })).not.toBeVisible();
  await expect(page.getByRole('cell', { name: 'Developer' })).not.toBeVisible();
  await expect(page.getByText('Acme Corporation')).not.toBeVisible();
});
