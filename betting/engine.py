"""模拟下注引擎。

只操作本地数据库：下单只扣减「可用余额」，结算时才产生盈亏，
不会向 stake.com 发送任何真实下注请求。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Sequence

from models import (
    BET_LOST,
    BET_PENDING,
    BET_VOID,
    BET_WON,
    STATUS_ENDED,
    Bet,
    Event,
    MatchResult,
    money,
    odds_value,
    settle_bet,
    utcnow,
)
from storage.repository import Repository

ZERO = Decimal("0.00")
MIN_STAKE = Decimal("0.01")
MIN_ODDS = Decimal("1.01")


class BetError(ValueError):
    """下单被拒绝：参数非法、余额不足、重复下单等。"""


@dataclass(frozen=True, slots=True)
class AccountStats:
    initial_balance: Decimal
    balance: Decimal
    pending_stake: Decimal
    realized_profit: Decimal
    total_staked: Decimal
    total_returned: Decimal
    bet_count: int
    settled_count: int
    won: int
    lost: int
    voided: int
    pending_count: int
    max_drawdown: Decimal

    @property
    def equity(self) -> Decimal:
        """总资产 = 可用余额 + 未结算本金。"""
        return money(self.balance + self.pending_stake)

    @property
    def roi(self) -> float:
        if self.total_staked <= 0:
            return 0.0
        return float(self.realized_profit / self.total_staked)

    @property
    def win_rate(self) -> float:
        decided = self.won + self.lost
        if decided <= 0:
            return 0.0
        return self.won / decided


class SimulationEngine:
    """模拟投注账户。"""

    def __init__(self, repo: Repository, initial_balance: Decimal | str | float = Decimal("1000")) -> None:
        self.repo = repo
        self._initial_balance = money(initial_balance)
        # 首次运行时把初始资金写进 meta，之后即使改了配置也不会凭空多钱
        if repo.get_meta("initial_balance") is None:
            repo.set_meta("initial_balance", str(self._initial_balance))

    # -- 账户 ------------------------------------------------------------ #
    @property
    def initial_balance(self) -> Decimal:
        raw = self.repo.get_meta("initial_balance")
        return money(raw) if raw is not None else self._initial_balance

    def pending_stake(self) -> Decimal:
        return money(sum((bet.stake for bet in self.repo.list_bets(status=BET_PENDING)), ZERO))

    def realized_profit(self) -> Decimal:
        return money(
            sum(
                (bet.payout - bet.stake for bet in self.repo.list_bets() if bet.status != BET_PENDING),
                ZERO,
            )
        )

    @property
    def balance(self) -> Decimal:
        """可用余额 = 初始资金 + 已实现盈亏 - 未结算本金。"""
        return money(self.initial_balance + self.realized_profit() - self.pending_stake())

    def stats(self) -> AccountStats:
        bets = self.repo.list_bets()
        settled = [bet for bet in bets if bet.status != BET_PENDING]
        pending = [bet for bet in bets if bet.status == BET_PENDING]
        total_staked = money(sum((bet.stake for bet in settled), ZERO))
        total_returned = money(sum((bet.payout for bet in settled), ZERO))
        realized = money(total_returned - total_staked)
        return AccountStats(
            initial_balance=self.initial_balance,
            balance=self.balance,
            pending_stake=money(sum((bet.stake for bet in pending), ZERO)),
            realized_profit=realized,
            total_staked=total_staked,
            total_returned=total_returned,
            bet_count=len(bets),
            settled_count=len(settled),
            won=sum(1 for bet in settled if bet.status == BET_WON),
            lost=sum(1 for bet in settled if bet.status == BET_LOST),
            voided=sum(1 for bet in settled if bet.status == BET_VOID),
            pending_count=len(pending),
            max_drawdown=self._max_drawdown(settled),
        )

    def equity_curve(self) -> list[tuple[datetime, Decimal]]:
        """已结算注单按时间排序后的权益曲线（用于统计回撤）。"""
        settled = [bet for bet in self.repo.list_bets() if bet.status != BET_PENDING and bet.settled_at]
        settled.sort(key=lambda bet: bet.settled_at)
        balance = self.initial_balance
        if not settled:
            return [(utcnow(), balance)]
        points: list[tuple[datetime, Decimal]] = []
        for bet in settled:
            balance = money(balance + bet.payout - bet.stake)
            points.append((bet.settled_at, balance))
        return points

    def _max_drawdown(self, settled: Sequence[Bet]) -> Decimal:
        ordered = sorted(
            (bet for bet in settled if bet.settled_at),
            key=lambda bet: bet.settled_at,
        )
        if not ordered:
            return ZERO
        balance = self.initial_balance
        peak = balance
        worst = ZERO
        for bet in ordered:
            balance = balance + bet.payout - bet.stake
            peak = max(peak, balance)
            drawdown = peak - balance
            worst = max(worst, drawdown)
        return money(worst)

    # -- 下单 ------------------------------------------------------------ #
    def place_bet(
        self,
        event: Event,
        market_key: str,
        outcome_key: str,
        stake: Decimal | str | float,
    ) -> Bet:
        stake_amount = money(stake)
        market = event.market(market_key)
        if market is None:
            raise BetError(f"盘口不存在：{market_key}")
        if not market.tradable:
            raise BetError(f"暂不支持自动结算的盘口：{market.name}（{market.kind}）")
        outcome = market.outcome(outcome_key)
        if outcome is None:
            raise BetError(f"选项不存在：{outcome_key}")
        if self.repo.get_result(event.id) is not None:
            raise BetError("该比赛已录入赛果，不能下注")
        if not event.is_open():
            raise BetError("比赛已结束，无法下注")
        if stake_amount < MIN_STAKE:
            raise BetError(f"下注金额不能小于 {MIN_STAKE}")
        if outcome.odds < MIN_ODDS:
            raise BetError(f"赔率异常（{outcome.odds}），已拒绝下单")
        if self.repo.has_open_bet(event.id, market.key, outcome.key):
            raise BetError("同一盘口选项已有一张待结算注单，请先结算或改选其它选项")

        balance = self.balance
        if stake_amount > balance:
            raise BetError(f"余额不足：可用 {balance}，需要 {stake_amount}")

        bet = Bet(
            id=f"B{uuid.uuid4().hex[:10].upper()}",
            event_id=event.id,
            event_label=event.title,
            market_key=market.key,
            market_name=market.name,
            outcome_key=outcome.key,
            outcome_label=outcome.label,
            odds=odds_value(outcome.odds),
            stake=stake_amount,
            placed_at=utcnow(),
            source=event.source or "stake",
        )
        self.repo.add_bet(bet)
        return bet

    # -- 结算 ------------------------------------------------------------ #
    def settle_event(self, event_id: str, home_score: int, away_score: int) -> list[Bet]:
        """记录比分并结算该场所有待结算注单（可重复调用，已结算的不动）。"""
        if home_score < 0 or away_score < 0:
            raise BetError("比分不能为负数")
        event = self.repo.get_event(event_id)
        if event is None:
            raise BetError(f"未找到比赛：{event_id}")

        self.repo.upsert_result(
            MatchResult(
                event_id=event_id,
                home_score=int(home_score),
                away_score=int(away_score),
                recorded_at=utcnow(),
            )
        )
        # 同步把比赛标成已结束，避免界面还显示「未开始」而被再次下注
        self.repo.upsert_events(
            [
                replace(
                    event,
                    status=STATUS_ENDED,
                    live=False,
                    score=(int(home_score), int(away_score)),
                )
            ]
        )
        settled: list[Bet] = []
        for bet in self.repo.bets_for_event(event_id):
            if bet.status != BET_PENDING:
                continue
            updated = settle_bet(bet, event, int(home_score), int(away_score))
            self.repo.update_bet(updated)
            settled.append(updated)
        return settled

    def settle_pending(self) -> list[Bet]:
        """把所有「已有赛果」的比赛上的待结算注单结掉。"""
        settled: list[Bet] = []
        for bet in self.repo.pending_bets():
            result = self.repo.get_result(bet.event_id)
            if result is None:
                continue
            event = self.repo.get_event(bet.event_id)
            if event is None:
                continue
            updated = settle_bet(bet, event, result.home_score, result.away_score)
            self.repo.update_bet(updated)
            settled.append(updated)
        return settled

    # -- 维护 ------------------------------------------------------------ #
    def clean_demo_data(self) -> dict[str, int]:
        """删除全部离线演示数据（演示比赛 + 演示注单 + 快照 + 赛果）。

        切到线上抓取成功后调用：演示账户的盈亏会一起消失，不会污染真实统计。
        """
        return self.repo.purge_source("mock")

    def reset(self) -> None:
        """清空注单，余额回到初始资金。"""
        self.repo.reset(drop_bets=True)
        self.repo.set_meta("initial_balance", str(self._initial_balance))

    def summary_lines(self) -> list[str]:
        stats = self.stats()
        return [
            f"初始资金：{stats.initial_balance}",
            f"可用余额：{stats.balance}",
            f"未结算本金：{stats.pending_stake}",
            f"总资产：{stats.equity}",
            f"已实现盈亏：{stats.realized_profit}（ROI {stats.roi * 100:.2f}%）",
            f"注单：{stats.bet_count} 张（待结算 {stats.pending_count}，赢 {stats.won}，输 {stats.lost}，退款 {stats.voided}）",
            f"胜率：{stats.win_rate * 100:.1f}%　最大回撤：{stats.max_drawdown}",
        ]
