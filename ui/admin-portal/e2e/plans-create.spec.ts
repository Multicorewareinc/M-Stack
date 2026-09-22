import { expect, test } from '@playwright/test';

test('creating a plan makes it appear in the list', async ({ page }) => {
  await page.goto('/plans');
  await expect(page.getByRole('heading', { name: 'Plans' })).toBeVisible();
  await expect(page.getByText('Pro')).toBeVisible();

  await page.getByRole('button', { name: '+ Create Plan' }).click();
  await expect(page.getByRole('heading', { name: 'Create Plan' })).toBeVisible();

  await page.getByLabel('Name').fill('Enterprise Plus');
  await page.getByLabel('TPM Limit').fill('2000000');
  await page.getByLabel('RPM Limit').fill('12000');
  await page.getByLabel('Quota').fill('20000000');
  // exact: true — "+ Create Plan" (header) also contains this substring.
  await page.getByRole('button', { name: 'Create Plan', exact: true }).click();

  // Must fail if the new plan does not appear without a manual refresh.
  await expect(page.getByText('Enterprise Plus')).toBeVisible();
});
