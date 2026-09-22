import { expect, test } from '@playwright/test';

test('org shell boots and navigates against MSW', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByText('Acme Corporation')).toBeVisible();

  await page.getByRole('link', { name: 'Roles' }).click();
  await expect(page.getByRole('heading', { name: 'Roles' })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Roles' })).toHaveAttribute('aria-current', 'page');
});
