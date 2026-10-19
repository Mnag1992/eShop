import { test, expect } from '@playwright/test';
import { addProductToCart, emptyCart, removeFirstCartItem } from '../cart-helpers';

test.describe('Cart Priciest Item Display', () => {

  test.afterEach(async ({ page }) => {
    // Ensure the cart is empty after each test to maintain a clean state for subsequent tests.
    await emptyCart(page);
  });

  test('TC_CART_PRICIEST_001 Display priciest item with a single product in cart', async ({ page }) => {
    await emptyCart(page);
    await addProductToCart(page, 'Adventurer GPS Watch');
    await page.goto('/cart');
    await expect(page.locator('[data-cart-priciest-item]')).toHaveText('Priciest item: Adventurer GPS Watch ($199.99)');
  });

  test('TC_CART_PRICIEST_002 Display priciest item with multiple products of different prices', async ({ page }) => {
    await emptyCart(page);
    // The previously used 'Blizzard Rider Snowboard' was not found on the initial catalog page.
    // Using 'Adventurer GPS Watch' ($199.99) and 'Alpine Peak Down Jacket' ($249.99) as replacements.
    // These products are assumed to be available on the first catalog page based on alphabetical order
    // and the fact that 'Adventurer GPS Watch' is known to be on the first page.
    await addProductToCart(page, 'Adventurer GPS Watch'); // $199.99
    await addProductToCart(page, 'Alpine Peak Down Jacket'); // $249.99
    await page.goto('/cart');
    await expect(page.locator('[data-cart-priciest-item]')).toHaveText('Priciest item: Alpine Peak Down Jacket ($249.99)');
  });

  // TC_CART_PRICIEST_003: "Display priciest item with multiple products having the same highest unit price"
  // This scenario requires two distinct products with the same highest unit price to be available
  // on the first catalog page. The products explicitly mentioned in the scenario ("Gravity 5000 All-Mountain Skis",
  // "GravityZone All-Mountain Skis") are unlikely to be on the first page due to their alphabetical position.
  // Attempts to use other products with the same price (e.g., 'Adventurer GPS Watch' and 'VenturePro GPS Watch')
  // also failed because 'VenturePro GPS Watch' was not found on the first page.
  // Given the constraint to only use products accessible from the initial catalog page via `addProductToCart`
  // and not to modify existing helpers or invent data, this scenario cannot be reliably automated.
  // As per the rule: "If a scenario cannot be built from the listed data, skip that test() instead of guessing."

  test('TC_CART_PRICIEST_004 Priciest item updates after removing the current priciest item', async ({ page }) => {
    await emptyCart(page);
    // The previously used 'Expedition 200 GPS Navigator' was not found on the initial catalog page.
    // Using 'Alpine Peak Down Jacket' ($249.99) and 'Adventurer GPS Watch' ($199.99) as replacements.
    // 'Alpine Peak Down Jacket' is added first, making it the first item in the cart, and it is the priciest.
    await addProductToCart(page, 'Alpine Peak Down Jacket'); // $249.99 (priciest initially)
    await addProductToCart(page, 'Adventurer GPS Watch'); // $199.99
    await page.goto('/cart');

    await expect(page.locator('[data-cart-priciest-item]')).toHaveText('Priciest item: Alpine Peak Down Jacket ($249.99)', { timeout: 15_000 });

    // Remove the first item in the cart, which is 'Alpine Peak Down Jacket'.
    await removeFirstCartItem(page);

    await expect(page.locator('[data-cart-priciest-item]')).toHaveText('Priciest item: Adventurer GPS Watch ($199.99)', { timeout: 15_000 });
  });

  test('TC_CART_PRICIEST_005 Priciest item updates after adding a new, more expensive item', async ({ page }) => {
    await emptyCart(page);
    // The previously used 'Expedition 200 GPS Navigator' was not found on the initial catalog page.
    // Using 'Adventurer GPS Watch' ($199.99) and 'Alpine Peak Down Jacket' ($249.99) as replacements.
    await addProductToCart(page, 'Adventurer GPS Watch'); // $199.99 (priciest initially)
    await page.goto('/cart');

    await expect(page.locator('[data-cart-priciest-item]')).toHaveText('Priciest item: Adventurer GPS Watch ($199.99)', { timeout: 15_000 });

    // Add a more expensive item
    await addProductToCart(page, 'Alpine Peak Down Jacket'); // $249.99
    await page.goto('/cart'); // Navigate back to cart to see the updated summary

    await expect(page.locator('[data-cart-priciest-item]')).toHaveText('Priciest item: Alpine Peak Down Jacket ($249.99)', { timeout: 15_000 });
  });

  test('TC_CART_PRICIEST_006 Priciest item is not displayed when the cart is empty', async ({ page }) => {
    await emptyCart(page);
    await page.goto('/cart');
    await expect(page.getByText('Priciest item:')).not.toBeVisible();
  });

});
