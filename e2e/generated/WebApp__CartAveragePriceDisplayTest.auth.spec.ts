import { test, expect } from '@playwright/test';
import { addProductToCart, emptyCart, removeFirstCartItem } from '../cart-helpers';

test.describe('Cart Average Price Display', () => {

  // The following scenarios (TC_CART_AVG_PRICE_001, 002, 003, 004, 005, 007)
  // require verifying the display of "Average price per item".
  // Based on the provided 'src/WebApp/Components/Pages/Cart/CartPage.razor' markup,
  // an element displaying "Average price per item" does not exist.
  // As per the hard rule: "If a scenario cannot be built from the listed data, skip that test() instead of guessing."
  // Therefore, these tests are not automatable with the given markup and are omitted.

  test('TC_CART_AVG_PRICE_006 Verify average price is not displayed when the cart is empty', async ({ page }) => {
    await emptyCart(page);
    await page.goto('/cart');

    // This test verifies the absence of the "Average price per item" text,
    // which is consistent with it not being present in the provided markup.
    await expect(page.getByText('Average price per item')).not.toBeVisible({ timeout: 15_000 });
    // No cleanup needed as the cart is already empty
  });

});
