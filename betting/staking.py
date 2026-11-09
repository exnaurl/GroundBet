"""注码策略与赔率换算工具。"""

from __future__ import annotations

from decimal import Decimal
from typing import Iterable

from models import money, to_decimal

STRATEGIES = {
    "flat": "固定金额",
    "percent": "账户比例",
    "kelly": "凯利公式",
}


def implied_probability(odds: Decimal | float | str) -> float:
    """赔率对应的隐含概率（含水位）。"""
    value = float(to_decimal(odds, Decimal("0")) or 0)
    return 1.0 / value if value > 0 else 0.0


def bookmaker_margin(odds: Iterable[Decimal | float | str]) -> float:
    """欧赔水位：所有结果隐含概率之和 - 1。"""
    total = sum(implied_probability(item) for item in odds)
    return total - 1.0 if total > 0 else 0.0


def fair_odds(odds: Iterable[Decimal | float | str]) -> list[float]:
    """去掉水位后的「公平赔率」，用于粗略判断价值。"""
    probabilities = [implied_probability(item) for item in odds]
    total = sum(probabilities)
    if total <= 0:
        return []
    return [round(1.0 / (p / total), 3) for p in probabilities]


def percent_stake(balance: Decimal, percent: float) -> Decimal:
    if percent <= 0:
        return Decimal("0.00")
    return money(balance * Decimal(str(percent)) / Decimal("100"))


def kelly_stake(
    balance: Decimal,
    probability: float,
    odds: Decimal | float | str,
    *,
    fraction: float = 0.25,
    max_percent: float = 10.0,
) -> Decimal:
    """分数凯利：f* = (p·o − 1) / (o − 1)，再乘以 fraction 并限制上限。

    概率需要用户自己估（例如用另一家博彩公司的去水概率），
    这里不替你假设模型，避免给出虚假的「必胜」建议。
    """
    decimal_odds = float(to_decimal(odds, Decimal("0")) or 0)
    if decimal_odds <= 1.0 or not 0 < probability < 1:
        return Decimal("0.00")
    edge = (probability * decimal_odds - 1.0) / (decimal_odds - 1.0)
    if edge <= 0:
        return Decimal("0.00")
    share = min(edge * fraction, max_percent / 100.0)
    return money(balance * Decimal(str(share)))


def suggested_stake(
    strategy: str,
    balance: Decimal,
    *,
    odds: Decimal | float | str = Decimal("2.00"),
    probability: float | None = None,
    amount: Decimal | float | str = Decimal("10"),
    percent: float = 2.0,
) -> Decimal:
    """按策略算建议注码；算不出来时退回固定金额。"""
    if strategy == "percent":
        stake = percent_stake(balance, percent)
    elif strategy == "kelly":
        stake = kelly_stake(balance, probability or 0.0, odds)
    else:
        stake = money(amount)
    return min(stake, money(balance))
