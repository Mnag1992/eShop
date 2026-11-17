import { test, expect } from '@playwright/test';
import { addProductToCart, emptyCart, removeFirstCartItem } from '../cart-helpers';

test('TC_ORDER_SIZE_007 Order size is not displayed when the cart is empty', async ({ page }) => {
  await emptyCart(page); // Ensures cart is empty and navigates to /cart

  await expect(page.getByRole('heading', { name: 'Shopping bag', exact: true })).toBeVisible();
  await expect(page.getByText('Your shopping bag is empty')).toBeVisible();
  await expect(page.locator('[data-cart-order-size]')).not.toBeVisible();
});

