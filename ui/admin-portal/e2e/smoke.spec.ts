import { expect, test } from '@playwright/test';

test('admin shell boots and navigates against MSW', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('link', { name: 'Overview' })).toBeVisible();

  await page.getByRole('link', { name: 'Organizations' }).click();
  await expect(page.getByRole('heading', { name: 'Organizations' })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Organizations' })).toHaveAttribute('aria-current', 'page');
});
