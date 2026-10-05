// axe-core wiring, written by bootstrap (verify-enable). Call it from any test:
//   await expectNoA11yViolations(page, testInfo);
import AxeBuilder from '@axe-core/playwright';
import { expect, type Page, type TestInfo } from '@playwright/test';

export async function expectNoA11yViolations(page: Page, testInfo: TestInfo) {
  const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']).analyze();
  // The full result is kept as evidence next to the trace and screenshots.
  await testInfo.attach('axe-results', { body: JSON.stringify(results, null, 2), contentType: 'application/json' });
  expect(
    results.violations.map((v) => `${v.id}: ${v.help} (${v.nodes.length} nodes)`),
    'accessibility violations',
  ).toEqual([]);
}
