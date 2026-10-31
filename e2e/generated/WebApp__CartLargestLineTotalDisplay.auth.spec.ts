import { test, expect } from '@playwright/test';
import { addProductToCart, emptyCart, removeFirstCartItem } from '../cart-helpers';

test.afterEach(async ({ page }) => {
  // Ensure cart is empty after each test to maintain a clean state for subsequent tests.
  // This is crucial because tests run serially and share the seeded user's basket.
  await emptyCart(page);
});

test('TC_CART_LARGEST_LINE_001 Display largest line total with a single product in cart', async ({ page }) => {
  await emptyCart(page);
  await addProductToCart(page, 'Adventurer GPS Watch');

  await page.goto('/cart');
  await expect(page.getByRole('heading', { name: 'Shopping bag', exact: true })).toBeVisible();
  await expect(page.locator('[data-cart-largest-line]')).toContainText('$199.99', { timeout: 15_000 });
});

test('TC_CART_LARGEST_LINE_002 Display largest line total with multiple products of different line totals', async ({ page }) => {
  await emptyCart(page);
  await addProductToCart(page, 'Adventurer GPS Watch'); // $199.99
  await addProductToCart(page, 'Alpine Peak Down Jacket'); // $249.99

  await page.goto('/cart');
  await expect(page.getByRole('heading', { name: 'Shopping bag', exact: true })).toBeVisible();
  await expect(page.locator('[data-cart-largest-line]')).toContainText('$249.99', { timeout: 15_000 });
});

test('TC_CART_LARGEST_LINE_003 Display largest line total when quantity makes a cheaper item\'s line total higher', async ({ page }) => {
  await emptyCart(page);
  await addProductToCart(page, 'Alpine Peak Down Jacket'); // 1 unit = $249.99
  await addProductToCart(page, 'Adventurer GPS Watch'); // 1 unit = $199.99

  await page.goto('/cart');
  await expect(page.getByRole('heading', { name: 'Shopping bag', exact: true })).toBeVisible();

  // Increase 'Adventurer GPS Watch' to 2 units (2 * $199.99 = $399.98)
  const updateRequest = page.waitForRequest(request =>
    request.method() === 'POST' && new URL(request.url()).pathname === '/cart');
  await Promise.all([
    updateRequest,
    page.getByRole('button', { name: 'Increase quantity of Adventurer GPS Watch' }).click(),
  ]);

  await expect(page.locator('[data-cart-largest-line]')).toContainText('$399.98', { timeout: 15_000 });
});

test('TC_CART_LARGEST_LINE_004 Largest line total updates when increasing quantity of an item', async ({ page }) => {
  await emptyCart(page);
  await addProductToCart(page, 'Adventurer GPS Watch'); // 1 unit = $199.99
  await addProductToCart(page, 'Alpine Peak Down Jacket'); // 1 unit = $249.99

  await page.goto('/cart');
  await expect(page.getByRole('heading', { name: 'Shopping bag', exact: true })).toBeVisible();
  await expect(page.locator('[data-cart-largest-line]')).toContainText('$249.99', { timeout: 15_000 }); // Initial largest

  // Increase the quantity of 'Adventurer GPS Watch' to 2 (2 * $199.99 = $399.98)
  const updateRequest = page.waitForRequest(request =>
    request.method() === 'POST' && new URL(request.url()).pathname === '/cart');
  await Promise.all([
    updateRequest,
    page.getByRole('button', { name: 'Increase quantity of Adventurer GPS Watch' }).click(),
  ]);

  await expect(page.locator('[data-cart-largest-line]')).toContainText('$399.98', { timeout: 15_000 });
});

test('TC_CART_LARGEST_LINE_005 Largest line total updates when decreasing quantity of the current largest item, and another item becomes largest', async ({ page }) => {
  await emptyCart(page);
  // Add Adventurer GPS Watch (1 unit initially)
  await addProductToCart(page, 'Adventurer GPS Watch');

  await page.goto('/cart');
  await expect(page.getByRole('heading', { name: 'Shopping bag', exact: true })).toBeVisible();

  // Increase Adventurer GPS Watch to 3 units (3 * $199.99 = $599.97)
  const adventurerWatchQuantityInput = page.locator('.cart-item', { hasText: 'Adventurer GPS Watch' }).locator('[data-cart-quantity-input]');
  await adventurerWatchQuantityInput.fill('3');
  const updateRequest1 = page.waitForRequest(request =>
    request.method() === 'POST' && new URL(request.url()).pathname === '/cart');
  await Promise.all([
    updateRequest1,
    page.keyboard.press('Enter'), // Submit the form by pressing Enter after filling
  ]);
  await expect(adventurerWatchQuantityInput).toHaveValue('3', { timeout: 15_000 });

  // Add Alpine Peak Down Jacket (1 unit = $249.99)
  await addProductToCart(page, 'Alpine Peak Down Jacket');

  await page.goto('/cart'); // Navigate back to cart to ensure all items are loaded
  await expect(page.getByRole('heading', { name: 'Shopping bag', exact: true })).toBeVisible();
  await expect(page.locator('[data-cart-largest-line]')).toContainText('$599.97', { timeout: 15_000 }); // Initial largest

  // Decrease the quantity of 'Adventurer GPS Watch' to 1 (1 * $199.99 = $199.99)
  await adventurerWatchQuantityInput.fill('1');
  const updateRequest2 = page.waitForRequest(request =>
    request.method() === 'POST' && new URL(request.url()).pathname === '/cart');
  await Promise.all([
    updateRequest2,
    page.keyboard.press('Enter'), // Submit the form by pressing Enter after filling
  ]);
  await expect(adventurerWatchQuantityInput).toHaveValue('1', { timeout: 15_000 });

  await expect(page.locator('[data-cart-largest-line]')).toContainText('$249.99', { timeout: 15_000 });
});

test('TC_CART_LARGEST_LINE_006 Largest line total updates after removing the current largest line item', async ({ page }) => {
  await emptyCart(page);
  await addProductToCart(page, 'Adventurer GPS Watch'); // $199.99
  await addProductToCart(page, 'Alpine Peak Down Jacket'); // $249.99

  await page.goto('/cart');
  await expect(page.getByRole('heading', { name: 'Shopping bag', exact: true })).toBeVisible();
  await expect(page.locator('[data-cart-largest-line]')).toContainText('$249.99', { timeout: 15_000 }); // Initial largest

  // Remove 'Alpine Peak Down Jacket'
  const removeRequest = page.waitForRequest(request =>
    request.method() === 'POST' && new URL(request.url()).pathname === '/cart');
  await Promise.all([
    removeRequest,
    page.locator('.cart-item', { hasText: 'Alpine Peak Down Jacket' }).getByRole('button', { name: /^Remove/ }).click(),
  ]);

  await expect(page.locator('[data-cart-largest-line]')).toContainText('$199.99', { timeout: 15_000 });
});

// TC_CART_LARGEST_LINE_007: This test is skipped because the '[data-cart-largest-line]' element
// is not rendered in the DOM when the cart is empty, as per the provided markup.
// The markup explicitly renders the cart summary section (which contains this element)
// only when 'basketItems.Count > 0'.
// Therefore, it's not possible to assert its text content when the cart is empty.
// The scenario is not automatable under the given constraints.

test('TC_CART_LARGEST_LINE_008 Display largest line total when an item is at maximum quantity', async ({ page }) => {
  await emptyCart(page);
  // Add 'Adventurer GPS Watch' (1 unit initially)
  await addProductToCart(page, 'Adventurer GPS Watch');
  await page.goto('/cart');
  await expect(page.getByRole('heading', { name: 'Shopping bag', exact: true })).toBeVisible();

  // Increase quantity to 10
  const quantityInput = page.locator('.cart-item', { hasText: 'Adventurer GPS Watch' }).locator('[data-cart-quantity-input]');
  await quantityInput.fill('10');
  const updateRequest = page.waitForRequest(request =>
    request.method() === 'POST' && new URL(request.url()).pathname === '/cart');
  await Promise.all([
    updateRequest,
    page.keyboard.press('Enter'), // Submit the form by pressing Enter after filling
  ]);

  await expect(quantityInput).toHaveValue('10', { timeout: 15_000 });
  // Fix: The displayed value includes a thousands separator.
  await expect(page.locator('[data-cart-largest-line]')).toContainText('$1,999.90', { timeout: 15_000 });
});

test('TC_CART_LARGEST_LINE_009 Display largest line total with multiple products, including one at max quantity, and another product has a higher line total', async ({ page }) => {
  await emptyCart(page);
  // Add 'Adventurer GPS Watch' (1 unit initially)
  await addProductToCart(page, 'Adventurer GPS Watch');
  await page.goto('/cart');
  await expect(page.getByRole('heading', { name: 'Shopping bag', exact: true })).toBeVisible();

  // Set 'Adventurer GPS Watch' to 10 units = $1999.90
  const adventurerWatchQuantityInput = page.locator('.cart-item', { hasText: 'Adventurer GPS Watch' }).locator('[data-cart-quantity-input]');
  await adventurerWatchQuantityInput.fill('10');
  const updateRequest1 = page.waitForRequest(request =>
    request.method() === 'POST' && new URL(request.url()).pathname === '/cart');
  await Promise.all([
    updateRequest1,
    page.keyboard.press('Enter'),
  ]);
  await expect(adventurerWatchQuantityInput).toHaveValue('10', { timeout: 15_000 });

  // Add 'Alpine Peak Down Jacket' (1 unit initially)
  await addProductToCart(page, 'Alpine Peak Down Jacket');
  await page.goto('/cart');
  await expect(page.getByRole('heading', { name: 'Shopping bag', exact: true })).toBeVisible();

  // Set 'Alpine Peak Down Jacket' to 10 units = $2499.90
  const alpineJacketQuantityInput = page.locator('.cart-item', { hasText: 'Alpine Peak Down Jacket' }).locator('[data-cart-quantity-input]');
  await alpineJacketQuantityInput.fill('10');
  const updateRequest2 = page.waitForRequest(request =>
    request.method() === 'POST' && new URL(request.url()).pathname === '/cart');
  await Promise.all([
    updateRequest2,
    page.keyboard.press('Enter'),
  ]);
  await expect(alpineJacketQuantityInput).toHaveValue('10', { timeout: 15_000 });

  // Fix: The displayed value includes a thousands separator.
  await expect(page.locator('[data-cart-largest-line]')).toContainText('$2,499.90', { timeout: 15_000 });
});

test('TC_CART_LARGEST_LINE_010 Display largest line total with multiple products, including one at max quantity, and it is the largest', async ({ page }) => {
  await emptyCart(page);
  await addProductToCart(page, 'Alpine Peak Down Jacket'); // 1 unit = $249.99

  // Add 'Adventurer GPS Watch' (1 unit initially)
  await addProductToCart(page, 'Adventurer GPS Watch');
  await page.goto('/cart');
  await expect(page.getByRole('heading', { name: 'Shopping bag', exact: true })).toBeVisible();

  // Set 'Adventurer GPS Watch' to 10 units = $1999.90
  const adventurerWatchQuantityInput = page.locator('.cart-item', { hasText: 'Adventurer GPS Watch' }).locator('[data-cart-quantity-input]');
  await adventurerWatchQuantityInput.fill('10');
  const updateRequest = page.waitForRequest(request =>
    request.method() === 'POST' && new URL(request.url()).pathname === '/cart');
  await Promise.all([
    updateRequest,
    page.keyboard.press('Enter'),
  ]);
  await expect(adventurerWatchQuantityInput).toHaveValue('10', { timeout: 15_000 });

  // Fix: The displayed value includes a thousands separator.
  await expect(page.locator('[data-cart-largest-line]')).toContainText('$1,999.90', { timeout: 15_000 });
});
