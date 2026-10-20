const { test, expect } = require('@playwright/test');

test('local page uses the allocated port', async ({ page, baseURL }) => {
  expect(new URL(baseURL).port).toBe(process.env.PORT);
  await page.goto('/');
  await expect(page.getByRole('heading')).toHaveText('Gate browser fixture');
  if (process.env.GATE_BROWSER_FIXTURE_FAIL) expect(true).toBe(false);
});
