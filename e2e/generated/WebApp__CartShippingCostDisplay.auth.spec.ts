import { test, expect } from '@playwright/test';
import { addProductToCart, emptyCart, removeFirstCartItem } from '../cart-helpers';

// Helper to get the shipping cost element, scoped to the cart summary
const getShippingCostElement = (page) => page.locator('.cart-summary [data-cart-shipping-cost]');

test('TC_SHIPPING_003 Shipping cost updates from paid to free when adding items to cross the threshold', async ({ page }) => {
  await emptyCart(page);
  // Ensure the homepage is fully loaded before attempting to add products
  await page.goto('/', { waitUntil: 'networkidle' });
  await addProductToCart(page, 'Adventurer GPS Watch'); // $199.99

  await page.goto('/cart');
  await expect(page.getByRole('heading', { name: 'Shopping bag', exact: true })).toBeVisible();
  await expect(getShippingCostElement(page)).toContainText('Shipping: $9.99', { timeout: 15_000 });

  // Add second item to cross the threshold
  await page.goto('/', { waitUntil: 'networkidle' }); // Re-ensure homepage is loaded for the second add
  await addProductToCart(page, 'Alpine Peak Down Jacket'); // $249.99
  // Total: $199.99 + $249.99 = $449.98 (above $300 threshold)

  await page.goto('/cart'); // Navigate back to cart to see updated shipping
  await expect(page.getByRole('heading', { name: 'Shopping bag', exact: true })).toBeVisible();
  await expect(getShippingCostElement(page)).toContainText('Shipping: Free', { timeout: 15_000 });

  await emptyCart(page); // Clean up
});

test('TC_SHIPPING_005 Shipping cost is not displayed when the cart is empty', async ({ page }) => {
  await emptyCart(page); // Ensure cart is empty

  await page.goto('/cart');
  await expect(page.getByRole('heading', { name: 'Shopping bag', exact: true })).toBeVisible();
  await expect(page.getByText('Your shopping bag is empty')).toBeVisible();

  // Assert that the shipping cost element is not visible
  await expect(getShippingCostElement(page)).not.toBeVisible();

  // No cleanup needed as cart is already empty
});

