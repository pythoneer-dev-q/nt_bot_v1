from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class TariffOption:
    period_id: str
    title: str
    months: int
    days: int
    discount_multiplier: int
    discount_percent: float
    raw_price: float
    total_price: int
    price_per_month: float
    savings: int

    @property
    def button_text(self) -> str:
        if self.discount_percent > 0:
            return f"⭐ {self.title} — {self.total_price} ₽ (-{int(self.discount_percent)}%)"
        return f"⭐ {self.title} — {self.total_price} ₽"


def calculate_tariffs(base_price: float, discount_percent_base: float) -> list[TariffOption]:
    """
    Вычисляет тарифные планы с прогрессирующей скидкой.
    Правило: скидка с большим периодом удваивается (0% -> D% -> 2*D% -> 4*D%).
    Чем больше срок, тем дешевле стоимость месяца.
    """
    tiers = [
        ("1m", "1 месяц", 1, 30, 0),
        ("3m", "3 месяца", 3, 90, 1),
        ("6m", "6 месяцев", 6, 180, 2),
        ("12m", "12 месяцев", 12, 365, 4),
    ]

    options: list[TariffOption] = []
    for period_id, title, months, days, mult in tiers:
        discount = 0.0 if mult == 0 else min(float(discount_percent_base) * mult, 80.0)
        raw_price = base_price * months
        discounted = raw_price * (1.0 - (discount / 100.0))
        total_price = max(int(round(discounted)), 1)
        savings = max(int(round(raw_price - total_price)), 0)
        price_per_month = round(total_price / months, 1)

        options.append(
            TariffOption(
                period_id=period_id,
                title=title,
                months=months,
                days=days,
                discount_multiplier=mult,
                discount_percent=discount,
                raw_price=round(raw_price, 2),
                total_price=total_price,
                price_per_month=price_per_month,
                savings=savings,
            )
        )

    return options


def get_tariff_by_period(period_id: str, base_price: float, discount_percent_base: float) -> TariffOption | None:
    for option in calculate_tariffs(base_price, discount_percent_base):
        if option.period_id == period_id or str(option.months) == period_id:
            return option
    return None

