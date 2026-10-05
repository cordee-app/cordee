import { test, expect } from '@playwright/test';

/**
 * Verify the new scaffolding flow:
 * 1. Create project → ScaffoldProgress modal shows logs
 * 2. After draft completes, modal shows editable draft in a textarea
 * 3. Clicking "Apply Guide" imports tasks and switches to the Board tab
 */

test('creates project, shows editable draft, applies guide, switches to board', async ({ page }) => {
  test.setTimeout(120000);
  await page.goto('/');

  await page.waitForSelector('.dash-project-card', { timeout: 10000 });

  await page.click('.new-project-card');

  await page.fill('input[placeholder="e.g. My SaaS App"]', 'Playwright Test Project');
  await page.fill('textarea[placeholder="Describe what you want to build…"]', 'A test pitch for scaffolding chat.');

  // Select a working scaffolding model.
  const scaffoldSelect = page.locator('label:has-text("Scaffolding model") + select');
  await scaffoldSelect.selectOption('mistral-medium-latest');

  await page.click('button:has-text("Create Project")');

  // The ScaffoldProgress modal should appear.
  await page.waitForSelector('h3:has-text("Scaffolding")', { timeout: 15000 });

  // Verify that log messages appear.
  await expect(page.locator('.modal-content')).toContainText('Generating phase roadmap', { timeout: 30000 });

  // Wait for the draft to appear in the textarea (not auto-applied anymore).
  await page.waitForSelector('textarea', { timeout: 90000 });
  const draftValue = await page.locator('textarea').inputValue();
  expect(draftValue.length).toBeGreaterThan(100);
  expect(draftValue).toContain('Phase 1');

  // Click "Apply Guide".
  await page.click('button:has-text("Apply Guide")');

  // After apply, the modal closes and the Board tab becomes active.
  await expect(page.locator('.main-tabs .active')).toContainText('Board', { timeout: 15000 });
});