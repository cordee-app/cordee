import { test, expect } from '@playwright/test';

/**
 * Verify the global hover-help tooltip system (data-tip + TooltipProvider):
 * 1. No tooltip before the 500ms hover delay.
 * 2. Tooltip appears with the correct text once hovered steadily.
 * 3. Tooltip disappears when the pointer leaves.
 * 4. Tooltip also appears on keyboard focus.
 */

test('shows delayed help tooltip on hover and hides on mouse-out', async ({ page }) => {
  await page.goto('/?loggedout');
  await page.waitForSelector('.app-container, [data-tip]', { timeout: 15000 });

  await page.evaluate(() => {
    const btn = document.createElement('button');
    btn.id = 'tip-probe';
    btn.setAttribute('data-tip', 'Probe help text');
    btn.textContent = 'Probe';
    btn.style.cssText = 'position:fixed;top:120px;left:120px;z-index:1;padding:8px;';
    document.body.appendChild(btn);
  });

  const probe = page.locator('#tip-probe');
  const tooltip = page.locator('[role="tooltip"]');

  await probe.hover();

  await page.waitForTimeout(250);
  await expect(tooltip).toHaveCount(0);

  await expect(tooltip).toBeVisible({ timeout: 2000 });
  await expect(tooltip).toHaveText('Probe help text');

  await page.mouse.move(600, 400);
  await expect(tooltip).toHaveCount(0);
});

test('shows tooltip on keyboard focus', async ({ page }) => {
  await page.goto('/?loggedout');
  await page.waitForSelector('.app-container, [data-tip]', { timeout: 15000 });

  await page.evaluate(() => {
    const btn = document.createElement('button');
    btn.id = 'focus-probe';
    btn.setAttribute('data-tip', 'Focus help text');
    btn.textContent = 'Focus probe';
    btn.style.cssText = 'position:fixed;top:160px;left:120px;z-index:1;padding:8px;';
    document.body.appendChild(btn);
  });

  const tooltip = page.locator('[role="tooltip"]');
  await page.locator('#focus-probe').focus();

  await expect(tooltip).toBeVisible({ timeout: 2000 });
  await expect(tooltip).toHaveText('Focus help text');
});

test('real controls expose non-empty help text', async ({ page }) => {
  await page.goto('/?loggedout');
  await page.waitForSelector('.app-container, [data-tip]', { timeout: 15000 });

  const empty = await page.evaluate(() =>
    Array.from(document.querySelectorAll('[data-tip]')).filter((el) => !(el.getAttribute('data-tip') || '').trim()).length,
  );
  expect(empty).toBe(0);
});
