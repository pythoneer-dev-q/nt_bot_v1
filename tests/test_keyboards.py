from __future__ import annotations

import unittest
from core.tariffs import calculate_tariffs
from utils import kbs


class TestKeyboards(unittest.IsolatedAsyncioTestCase):
    async def test_main_menu_markup(self):
        markup = await kbs.main_menu()
        self.assertIsNotNone(markup)
        all_callbacks = [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]
        self.assertIn("user:connect", all_callbacks)
        self.assertNotIn("user:tariffs", all_callbacks)
        self.assertNotIn("user:sub", all_callbacks)  # "кнопка подписка не нужна, если есть подключиться"
        self.assertNotIn("user:devices", all_callbacks)  # "устройства и сброс ссылки должны быть в настройках подписки"
        self.assertIn("user:profile", all_callbacks)
        self.assertIn("user:faq", all_callbacks)
        self.assertIn("user:support", all_callbacks)
        self.assertIn("user:stats", all_callbacks)
        # Ensure no donate callback
        for cb in all_callbacks:
            self.assertNotIn("donate", cb.lower())

    async def test_topup_keyboard(self):
        markup = await kbs.topup_keyboard()
        all_callbacks = [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]
        self.assertIn("user:topup_amount:100", all_callbacks)
        self.assertIn("user:topup_amount:300", all_callbacks)
        self.assertIn("user:topup_amount:500", all_callbacks)
        self.assertIn("user:topup_amount:1000", all_callbacks)
        self.assertIn("user:topup_custom", all_callbacks)
        self.assertIn("user:menu", all_callbacks)
        # Tariffs should NOT be in topup keyboard
        self.assertNotIn("user:tariffs", all_callbacks)

    async def test_subscription_keyboard_auto_renew_only_on_premium(self):
        # Free tariff: NO auto-renewal ("автопродления на фри тарифе не существует")
        markup_free = await kbs.subscription_keyboard("https://sub.url/test", is_premium=False)
        all_callbacks_free = [b.callback_data for row in markup_free.inline_keyboard for b in row if b.callback_data]
        self.assertNotIn("user:toggle_autorenew", all_callbacks_free)

        # Premium tariff: auto-renewal toggle is present
        markup_prem = await kbs.subscription_keyboard("https://sub.url/test", is_premium=True, auto_renew=True)
        all_callbacks_prem = [b.callback_data for row in markup_prem.inline_keyboard for b in row if b.callback_data]
        all_texts_prem = [b.text for row in markup_prem.inline_keyboard for b in row]
        self.assertIn("user:toggle_autorenew", all_callbacks_prem)
        self.assertTrue(any("ВКЛ" in t for t in all_texts_prem))

        markup_off = await kbs.subscription_keyboard("https://sub.url/test", is_premium=True, auto_renew=False)
        all_texts_off = [b.text for row in markup_off.inline_keyboard for b in row]
        self.assertTrue(any("ВЫКЛ" in t for t in all_texts_off))

    async def test_ad_interstitial_keyboard(self):
        markup = await kbs.ad_interstitial_keyboard("https://ad-sponsor.com", "Лучший хостинг")
        all_urls = [b.url for row in markup.inline_keyboard for b in row if b.url]
        self.assertIn("https://ad-sponsor.com", all_urls)

    async def test_premium_connect_no_ad(self):
        markup = await kbs.premium_connect_keyboard("https://sub.url/premium123")
        all_urls = [b.url for row in markup.inline_keyboard for b in row if b.url]
        all_texts = [b.text for row in markup.inline_keyboard for b in row]

        self.assertIn("https://sub.url/premium123", all_urls)
        # No ad banner button
        self.assertFalse(any("реклам" in t.lower() or "спонсор" in t.lower() for t in all_texts))

    async def test_connect_choice_keyboard(self):
        markup = await kbs.connect_choice_keyboard()
        all_callbacks = [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]
        self.assertIn("user:connect:free", all_callbacks)
        self.assertIn("user:connect:premium", all_callbacks)
        self.assertIn("user:menu", all_callbacks)

    async def test_tariffs_keyboard(self):
        tariffs = calculate_tariffs(199, 10)
        markup = await kbs.tariffs_keyboard(tariffs, can_overdraft=True)
        all_callbacks = [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]

        self.assertIn("pay:select:1m", all_callbacks)
        self.assertIn("pay:select:3m", all_callbacks)
        self.assertIn("pay:select:6m", all_callbacks)
        self.assertIn("pay:select:12m", all_callbacks)
        self.assertIn("user:menu", all_callbacks)

        prof_kb = await kbs.profile_keyboard(can_overdraft=True)
        prof_cbs = [b.callback_data for row in prof_kb.inline_keyboard for b in row if b.callback_data]
        self.assertIn("user:overdraft", prof_cbs)
        self.assertIn("user:topup", prof_cbs)
        self.assertIn("user:history", prof_cbs)


if __name__ == "__main__":
    unittest.main()

