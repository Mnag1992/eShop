import { test, expect } from '@playwright/test';
import { addProductToCart, emptyCart } from '../cart-helpers';

test('TC_CART_008 Checkout with MaxQuantityPerItem for a product', async ({ page }) => {
  // Ensure the shopping bag is empty.
  await emptyCart(page);

  // Navigate to the Orders page to get the initial count before placing a new order.
  await page.goto('/user/orders');
  await expect(page.getByRole('heading', { name: 'Orders', exact: true })).toBeVisible();
  const existingOrderCount = await page.locator('.order-number').count();

  // Add 'Adventurer GPS Watch' to the bag.
  await addProductToCart(page, 'Adventurer GPS Watch');

  // Navigate to the shopping bag (/cart).
  await page.goto('/cart');
  await expect(page.getByRole('heading', { name: 'Shopping bag', exact: true })).toBeVisible();
  await expect(page.getByText('Adventurer GPS Watch')).toBeVisible();

  // Increase the quantity of 'Adventurer GPS Watch' to 10.
  const quantityInput = page.locator('[data-cart-quantity-input]');
  await expect(quantityInput).toHaveValue('1'); // Verify initial quantity is 1

  const updateRequest = page.waitForRequest(request =>
    request.method() === 'POST' && new URL(request.url()).pathname === '/cart');

  await quantityInput.fill('10');
  // After filling the input, trigger a blur event to ensure the form submission is initiated.
  // The "Apply" button in the markup is aria-hidden and not meant for direct interaction.
  await quantityInput.blur();

  await updateRequest; // Wait for the cart update to complete

  await expect(quantityInput).toHaveValue('10', { timeout: 15_000 });
  // Verify the total price for 10 Adventurer GPS Watches ($199.99 * 10 = $1999.90)
  await expect(page.locator('.cart-summary-total')).toContainText('$1,999.90', { timeout: 15_000 });

  // Proceed to checkout.
  await page.getByRole('link', { name: 'Check out' }).click();

  // Verify we are on the checkout page.
  await expect(page.getByRole('heading', { name: 'Checkout' })).toBeVisible();
  // Verify pre-filled address details (from seeded user's storage state)
  await expect(page.getByLabel('Address')).toHaveValue('15703 NE 61st Ct');
  await expect(page.getByLabel('City')).toHaveValue('Redmond');

  // Place the order.
  await page.getByRole('button', { name: 'Place order' }).click();

  // Expected: The order should be placed successfully.
  // The checkout process redirects to the orders page.
  await expect(page).toHaveURL(/\/user\/orders$/, { timeout: 15_000 });
  await expect(page.getByRole('heading', { name: 'Orders', exact: true })).toBeVisible();
  // Verify a new order has been added.
  await expect(page.locator('.order-number')).toHaveCount(existingOrderCount + 1, { timeout: 15_000 });

  // Note: The provided markup for the Orders page does not expose individual product names or quantities within an order.
  // Therefore, the expectation "The new order on the Orders page should show 'Adventurer GPS Watch' with a quantity of 10"
  // cannot be fully automated with the current UI. We verify the order count and successful navigation.

  // Clean up the cart for subsequent tests.
  await emptyCart(page);
});
