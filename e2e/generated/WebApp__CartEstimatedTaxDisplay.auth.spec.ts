import { test, expect } from '@playwright/test';
import { addProductToCart, emptyCart, removeFirstCartItem } from '../cart-helpers';

test('TC_CART_TAX_001 Display estimated tax for a single product in the cart', async ({ page }) => {
  await emptyCart(page);
  await addProductToCart(page, 'Adventurer GPS Watch');
  await page.goto('/cart');
  await expect(page.getByRole('heading', { name: 'Shopping bag', exact: true })).toBeVisible();
  await expect(page.locator('[data-cart-estimated-tax]')).toContainText('Estimated tax (8%): $16.00', { timeout: 15_000 });
});

test('TC_CART_TAX_003 Estimated tax updates when product quantity is increased', async ({ page }) => {
  await emptyCart(page);
  await addProductToCart(page, 'Adventurer GPS Watch');
  await page.goto('/cart');
  await expect(page.getByRole('heading', { name: 'Shopping bag', exact: true })).toBeVisible();

  // Increase the quantity of 'Adventurer GPS Watch' to 2
  const updateRequest = page.waitForRequest(request =>
    request.method() === 'POST' && new URL(request.url()).pathname === '/cart');
  await Promise.all([
    updateRequest,
    page.getByRole('button', { name: 'Increase quantity of Adventurer GPS Watch' }).click(),
  ]);

  await expect(page.locator('[data-cart-quantity-input]').first()).toHaveValue('2', { timeout: 15_000 });
  await expect(page.locator('[data-cart-estimated-tax]')).toContainText('Estimated tax (8%): $32.00', { timeout: 15_000 });
});

test('TC_CART_TAX_005 Estimated tax is not displayed when the cart is empty', async ({ page }) => {
  await emptyCart(page); // This helper navigates to /cart and ensures it's empty.
  await expect(page.getByText('Your shopping bag is empty')).toBeVisible();
  await expect(page.locator('[data-cart-estimated-tax]')).not.toBeVisible();
});

test('TC_CART_TAX_006 Verify the displayed tax rate percentage on the cart page', async ({ page }) => {
  await emptyCart(page);
  await addProductToCart(page, 'Adventurer GPS Watch');
  await page.goto('/cart');
  await expect(page.getByRole('heading', { name: 'Shopping bag', exact: true })).toBeVisible();
  await expect(page.locator('[data-cart-estimated-tax]')).toContainText('Estimated tax (8%):', { timeout: 15_000 });
});
