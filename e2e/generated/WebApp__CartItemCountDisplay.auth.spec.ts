import { test, expect } from '@playwright/test';
import { addProductToCart, emptyCart, removeFirstCartItem } from '../cart-helpers';

test.beforeEach(async ({ page }) => {
  // Ensure the cart is empty before each test to maintain isolation.
  // This also satisfies the requirement to "start from emptyCart(page)".
  await emptyCart(page);
});

test('TC_CART_ITEM_COUNT_001 Verify \'1 item in your bag\' label when one product is added to cart', async ({ page }) => {
  await addProductToCart(page, 'Adventurer GPS Watch');
  await page.goto('/cart');
  await expect(page.getByText('1 item in your bag')).toBeVisible({ timeout: 15_000 });
  // The cart will be emptied by the beforeEach hook of the next test.
});

test('TC_CART_ITEM_COUNT_002 Verify \'X items in your bag\' label when quantity of a single product is increased', async ({ page }) => {
  await addProductToCart(page, 'Adventurer GPS Watch');
  await page.goto('/cart');

  // Increase the quantity of 'Adventurer GPS Watch' to 2
  const updateRequest = page.waitForRequest(request =>
    request.method() === 'POST' && new URL(request.url()).pathname === '/cart');

  await Promise.all([
    updateRequest,
    page.getByRole('button', { name: /^Increase quantity of Adventurer GPS Watch/ }).click(),
  ]);

  await expect(page.getByText('2 items in your bag')).toBeVisible({ timeout: 15_000 });
  // The cart will be emptied by the beforeEach hook of the next test.
});

test('TC_CART_ITEM_COUNT_003 Verify \'X items in your bag\' label when multiple distinct products are added', async ({ page }) => {
  await addProductToCart(page, 'Adventurer GPS Watch');
  await addProductToCart(page, 'Expedition Backpack');
  await page.goto('/cart');
  await expect(page.getByText('2 items in your bag')).toBeVisible({ timeout: 15_000 });
  // The cart will be emptied by the beforeEach hook of the next test.
});

test('TC_CART_ITEM_COUNT_004 Verify \'X items in your bag\' label updates after removing an item', async ({ page }) => {
  await addProductToCart(page, 'Adventurer GPS Watch');
  await addProductToCart(page, 'Expedition Backpack');
  await page.goto('/cart');

  // Remove 'Expedition Backpack' from the cart
  const updateRequest = page.waitForRequest(request =>
    request.method() === 'POST' && new URL(request.url()).pathname === '/cart');

  await Promise.all([
    updateRequest,
    page.getByRole('button', { name: 'Remove Expedition Backpack from your bag' }).click(),
  ]);

  await expect(page.getByText('1 item in your bag')).toBeVisible({ timeout: 15_000 });
  // The cart will be emptied by the beforeEach hook of the next test.
});

test('TC_CART_ITEM_COUNT_005 Verify item count label is not displayed when the cart is empty', async ({ page }) => {
  // The beforeEach hook ensures the cart is empty and navigates to /cart.
  // The emptyCart helper already asserts 'Your shopping bag is empty'.
  await expect(page.getByText('Your shopping bag is empty')).toBeVisible();

  // Verify the element with 'data-cart-item-count' is not present.
  await expect(page.locator('[data-cart-item-count]')).not.toBeVisible({ timeout: 15_000 });
  // The cart is already empty, satisfying the "leave the cart empty" requirement.
});
