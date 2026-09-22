import { expect, test } from '@playwright/test';

// org_002 (Example Inc) is seeded provisioning and the MSW handler flips it
// to active deterministically after a fixed poll count (handlers.ts), not a
// wall-clock delay — the detail page's live polling (useProvisioningStatus,
// refetchInterval every 2s) picks up the transition without a reload.
test('organization detail reflects provisioning to active transition live', async ({ page }) => {
  await page.goto('/organizations/org_002');
  await expect(page.getByRole('heading', { name: 'Example Inc' })).toBeVisible();
  // Status renders twice (header badge + Overview tab) — scope to one.
  await expect(page.getByRole('status', { name: 'Status: Provisioning' }).first()).toBeVisible();

  // Must fail if the UI keeps showing provisioning after the backend reports active.
  await expect(page.getByRole('status', { name: 'Status: Active' }).first()).toBeVisible({ timeout: 15_000 });
});

// org_003 is failed + retryable:true; org_004 is failed + retryable:false.
// The list's row actions must offer Retry only for the former.
test('Retry Provisioning is offered only when the backend marks the organization retryable', async ({ page }) => {
  await page.goto('/organizations');
  await expect(page.getByText('Startup X')).toBeVisible();
  await expect(page.getByText('Broken Co')).toBeVisible();

  const rows = page.getByRole('row');
  const retryableRow = rows.filter({ hasText: 'Startup X' });
  const notRetryableRow = rows.filter({ hasText: 'Broken Co' });

  await retryableRow.getByRole('button', { name: 'Row actions' }).click();
  await expect(page.getByRole('menuitem', { name: 'Retry Provisioning' })).toBeVisible();
  await page.keyboard.press('Escape');

  await notRetryableRow.getByRole('button', { name: 'Row actions' }).click();
  // Must fail if Retry appears regardless of the backend flag.
  await expect(page.getByRole('menuitem', { name: 'Retry Provisioning' })).toHaveCount(0);
});
