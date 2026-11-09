"""领域模型与赛果判定。

这个模块是纯 Python 的（不依赖 Qt、网络、数据库），抓取层、下注引擎、
存储层和 UI 都只依赖这里的类型，因此同一份逻辑既能被 GUI 调用，
也能被 CLI 与单元测试调用。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Mapping

CENT = Decimal("0.01")
ODDS_STEP = Decimal("0.0001")

STATUS_UPCOMING = "upcoming"
STATUS_LIVE = "live"
STATUS_ENDED = "ended"
STATUS_UNKNOWN = "unknown"

KIND_1X2 = "1x2"
KIND_TOTALS = "totals"
KIND_BTTS = "btts"
KIND_OTHER = "other"

#: 引擎能自动结算的盘口类型；其它类型不允许下注，避免产生永远无法结算的单子。
SUPPORTED_KINDS = (KIND_1X2, KIND_TOTALS, KIND_BTTS)

STATUS_LABELS = {
    STATUS_UPCOMING: "未开始",
    STATUS_LIVE: "进行中",
    STATUS_ENDED: "已结束",
    STATUS_UNKNOWN: "未知",
}

#: 下注单状态
BET_PENDING = "pending"
BET_WON = "won"
BET_LOST = "lost"
BET_VOID = "void"

BET_STATUS_LABELS = {
    BET_PENDING: "待结算",
    BET_WON: "赢",
    BET_LOST: "输",
    BET_VOID: "退款",
}


# --------------------------------------------------------------------------- #
# 数值 / 时间工具
# --------------------------------------------------------------------------- #
def to_decimal(value: Any, default: Decimal | None = None) -> Decimal | None:
    """把任意输入安全地转成 Decimal，失败时返回 default。"""
    if value is None or value == "":
        return default
    if isinstance(value, Decimal):
        return value if value.is_finite() else default
    try:
        text = str(value).strip().replace(",", "")
        number = Decimal(text)
    except (InvalidOperation, ValueError, ArithmeticError):
        return default
    return number if number.is_finite() else default


def money(value: Any) -> Decimal:
    """金额：两位小数，四舍五入。"""
    return to_decimal(value, Decimal("0")).quantize(CENT, rounding=ROUND_HALF_UP)


def odds_value(value: Any) -> Decimal:
    """赔率：保留 4 位小数（显示时再格式化成两位）。"""
    return to_decimal(value, Decimal("0")).quantize(ODDS_STEP, rounding=ROUND_HALF_UP)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    """统一成带时区的 UTC 时间；naive 时间按 UTC 处理。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_datetime(value: Any) -> datetime | None:
    """解析 ISO8601 / Unix 时间戳（秒或毫秒）。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return as_utc(value)
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if number > 1e11:  # 毫秒
            number /= 1000.0
        try:
            return datetime.fromtimestamp(number, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return as_utc(datetime.fromisoformat(text))
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# 盘口
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Outcome:
    """一个可下注的选项，例如「主胜 1.87」。"""

    key: str
    label: str
    odds: Decimal

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "label": self.label, "odds": str(self.odds)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Outcome":
        return cls(
            key=str(data.get("key") or ""),
            label=str(data.get("label") or data.get("key") or ""),
            odds=odds_value(data.get("odds")),
        )


@dataclass(frozen=True, slots=True)
class Market:
    """一个盘口，例如「全场独赢」「总进球 2.5」。"""

    key: str
    name: str
    kind: str
    outcomes: tuple[Outcome, ...]
    line: Decimal | None = None

    def outcome(self, key: str) -> Outcome | None:
        for item in self.outcomes:
            if item.key == key:
                return item
        return None

    @property
    def tradable(self) -> bool:
        return self.kind in SUPPORTED_KINDS and bool(self.outcomes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "kind": self.kind,
            "line": None if self.line is None else str(self.line),
            "outcomes": [item.to_dict() for item in self.outcomes],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Market":
        return cls(
            key=str(data.get("key") or ""),
            name=str(data.get("name") or data.get("key") or ""),
            kind=str(data.get("kind") or KIND_OTHER),
            line=to_decimal(data.get("line")),
            outcomes=tuple(Outcome.from_dict(item) for item in data.get("outcomes") or ()),
        )


@dataclass(frozen=True, slots=True)
class Event:
    """一场比赛，附带抓取到的赔率。"""

    id: str
    sport: str
    league: str
    home: str
    away: str
    start_time: datetime
    status: str = STATUS_UNKNOWN
    live: bool = False
    markets: tuple[Market, ...] = ()
    score: tuple[int, int] | None = None
    #: 数据来源标记：「stake」= 线上真实数据，「mock」= 离线演示数据
    source: str = "stake"

    @property
    def title(self) -> str:
        return f"{self.home} vs {self.away}"

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status or STATUS_UNKNOWN)

    @property
    def settled(self) -> bool:
        return self.status == STATUS_ENDED and self.score is not None

    def market(self, key: str) -> Market | None:
        for item in self.markets:
            if item.key == key:
                return item
        return None

    def main_market(self) -> Market | None:
        """优先取 1X2，其次取任意可交易的盘口。"""
        for kind in (KIND_1X2, KIND_TOTALS, KIND_BTTS):
            for item in self.markets:
                if item.kind == kind:
                    return item
        return None

    def tradable_markets(self) -> tuple[Market, ...]:
        return tuple(m for m in self.markets if m.tradable)

    def is_open(self) -> bool:
        return self.status in (STATUS_UPCOMING, STATUS_LIVE)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "sport": self.sport,
            "league": self.league,
            "home": self.home,
            "away": self.away,
            "start_time": as_utc(self.start_time).isoformat(),
            "status": self.status,
            "live": self.live,
            "score": list(self.score) if self.score else None,
            "markets": [item.to_dict() for item in self.markets],
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Event":
        score = data.get("score")
        return cls(
            id=str(data.get("id") or ""),
            sport=str(data.get("sport") or ""),
            league=str(data.get("league") or ""),
            home=str(data.get("home") or ""),
            away=str(data.get("away") or ""),
            start_time=parse_datetime(data.get("start_time")) or utcnow(),
            status=str(data.get("status") or STATUS_UNKNOWN),
            live=bool(data.get("live")),
            markets=tuple(Market.from_dict(item) for item in data.get("markets") or ()),
            score=(int(score[0]), int(score[1])) if score else None,
            source=str(data.get("source") or "stake"),
        )


@dataclass(frozen=True, slots=True)
class MatchResult:
    """一场比赛的最终比分。"""

    event_id: str
    home_score: int
    away_score: int
    recorded_at: datetime
    status: str = STATUS_ENDED

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "home_score": self.home_score,
            "away_score": self.away_score,
            "recorded_at": as_utc(self.recorded_at).isoformat(),
            "status": self.status,
        }


# --------------------------------------------------------------------------- #
# 下注单
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Bet:
    """一张模拟下注单（只记录在本地数据库，不涉及真实资金）。"""

    id: str
    event_id: str
    event_label: str
    market_key: str
    market_name: str
    outcome_key: str
    outcome_label: str
    odds: Decimal
    stake: Decimal
    placed_at: datetime
    status: str = BET_PENDING
    payout: Decimal = Decimal("0")
    settled_at: datetime | None = None
    note: str = ""
    #: 与 Event.source 一致，用于区分「演示注单」和「真实赛事注单」
    source: str = "stake"

    @property
    def potential_return(self) -> Decimal:
        return money(self.stake * self.odds)

    @property
    def profit(self) -> Decimal:
        if self.status == BET_PENDING:
            return Decimal("0")
        return money(self.payout - self.stake)

    @property
    def status_label(self) -> str:
        return BET_STATUS_LABELS.get(self.status, self.status)

    def with_status(
        self,
        status: str,
        payout: Decimal,
        settled_at: datetime | None = None,
        note: str | None = None,
    ) -> "Bet":
        return Bet(
            id=self.id,
            event_id=self.event_id,
            event_label=self.event_label,
            market_key=self.market_key,
            market_name=self.market_name,
            outcome_key=self.outcome_key,
            outcome_label=self.outcome_label,
            odds=self.odds,
            stake=self.stake,
            placed_at=self.placed_at,
            status=status,
            payout=money(payout),
            settled_at=as_utc(settled_at) if settled_at else utcnow(),
            note=self.note if note is None else note,
            source=self.source,
        )


# --------------------------------------------------------------------------- #
# 赛果判定
# --------------------------------------------------------------------------- #
def evaluate_market(
    market: Market, home_score: int, away_score: int
) -> dict[str, str]:
    """根据比分判定该盘口每个选项的结果：won / lost / void。

    无法判定的盘口一律返回 void（退回本金），避免出现永远挂着的注单。
    """
    total = home_score + away_score
    result: dict[str, str] = {}

    if market.kind == KIND_1X2:
        keys = {item.key for item in market.outcomes}
        # 允许「主胜 / 平局 / 客胜」以及没有平局的二选一（例如淘汰赛）
        if keys not in ({"home", "draw", "away"}, {"home", "away"}):
            return {item.key: BET_VOID for item in market.outcomes}
        winner = "home" if home_score > away_score else "away" if away_score > home_score else "draw"
        for item in market.outcomes:
            result[item.key] = BET_WON if item.key == winner else BET_LOST
        return result

    if market.kind == KIND_TOTALS:
        line = market.line
        if line is None:
            return {item.key: BET_VOID for item in market.outcomes}
        if Decimal(total) == line:
            return {item.key: BET_VOID for item in market.outcomes}
        over = Decimal(total) > line
        for item in market.outcomes:
            if item.key == "over":
                result[item.key] = BET_WON if over else BET_LOST
            elif item.key == "under":
                result[item.key] = BET_WON if not over else BET_LOST
            else:
                result[item.key] = BET_VOID
        return result

    if market.kind == KIND_BTTS:
        both = home_score > 0 and away_score > 0
        for item in market.outcomes:
            if item.key == "yes":
                result[item.key] = BET_WON if both else BET_LOST
            elif item.key == "no":
                result[item.key] = BET_WON if not both else BET_LOST
            else:
                result[item.key] = BET_VOID
        return result

    return {item.key: BET_VOID for item in market.outcomes}


def settle_bet(bet: Bet, event: Event, home_score: int, away_score: int) -> Bet:
    """按比分结算一张注单；盘口/选项缺失时按退款处理。"""
    market = event.market(bet.market_key)
    if market is None:
        return bet.with_status(BET_VOID, bet.stake, note="盘口已不存在，自动退款")
    evaluation = evaluate_market(market, home_score, away_score)
    outcome = evaluation.get(bet.outcome_key, BET_VOID)
    if outcome == BET_WON:
        return bet.with_status(BET_WON, bet.stake * bet.odds, note=f"比分 {home_score}-{away_score}")
    if outcome == BET_LOST:
        return bet.with_status(BET_LOST, Decimal("0"), note=f"比分 {home_score}-{away_score}")
    return bet.with_status(BET_VOID, bet.stake, note=f"比分 {home_score}-{away_score}，退款")
