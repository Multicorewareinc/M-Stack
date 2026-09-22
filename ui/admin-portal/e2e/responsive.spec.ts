import { expect, test } from '@playwright/test';

test('no horizontal overflow at mobile width', async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 667 });
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Overview' })).toBeVisible();

  const { scrollWidth, clientWidth } = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  }));

  // Must fail if the page's scrollWidth exceeds its clientWidth.
  expect(scrollWidth).toBeLessThanOrEqual(clientWidth);
});
