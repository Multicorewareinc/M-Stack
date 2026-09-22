import AxeBuilder from '@axe-core/playwright';
import { expect, test } from '@playwright/test';

test('overview page has no serious accessibility violations', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Overview' })).toBeVisible();

  const results = await new AxeBuilder({ page }).analyze();
  const serious = results.violations.filter((v) => v.impact === 'serious' || v.impact === 'critical');

  // Must fail if a serious/critical violation is introduced.
  expect(serious).toEqual([]);
});
