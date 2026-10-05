// Example acceptance test, written by bootstrap (verify-enable). Replace it with tests derived from a
// task's acceptance criteria. Tag each test with its task and criterion, for example '@T-4-AC2', so
// `just verify <url> @T-4-AC2` runs just that criterion.
import { test, expect } from '@playwright/test';
import { expectNoA11yViolations } from './axe';

test('home page shows a main heading', { tag: '@EXAMPLE-AC1' }, async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
});

test('home page has no accessibility violations', { tag: '@EXAMPLE-AC2' }, async ({ page }, testInfo) => {
  await page.goto('/');
  await expectNoA11yViolations(page, testInfo);
});

// Real conditions are created with network interception the test controls, for example a failing backend:
//   await page.route('**/api/items', (route) => route.fulfill({ status: 500 }));
