"""数据源抽象：真实 Stake、离线演示、以及「真实失败自动降级」的组合。

上层（UI/CLI）只依赖 ``fetch_events()`` / ``fetch_results()`` 两个方法，
换成别的数据源（例如 Odds API、本地回放）只要实现同样的接口即可。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol, Sequence, runtime_checkable

from config import Settings
from data.stake_client import StakeClient, StakeError
from models import (
    KIND_1X2,
    KIND_BTTS,
    KIND_TOTALS,
    STATUS_ENDED,
    STATUS_LIVE,
    STATUS_UPCOMING,
    Event,
    Market,
    MatchResult,
    Outcome,
    as_utc,
    odds_value,
    utcnow,
)

log = logging.getLogger(__name__)


@runtime_checkable
class DataProvider(Protocol):
    name: str

    def fetch_events(self) -> list[Event]: ...

    def fetch_results(self, event_ids: Sequence[str] | None = None) -> list[MatchResult]: ...


# --------------------------------------------------------------------------- #
# 离线演示数据
# --------------------------------------------------------------------------- #
class MockProvider:
    """读取 fixtures/mock_events.json 的离线数据源。

    时间按「相对现在」的偏移生成，所以无论哪天运行，列表里都有
    未开始 / 进行中 / 已结束（带比分）三种比赛，方便演示完整流程。
    """

    name = "mock"
    label = "离线演示"

    def __init__(self, fixture_path: Path | str, now: datetime | None = None) -> None:
        self.fixture_path = Path(fixture_path)
        self._now = as_utc(now) if now else None

    # -- 读取 ------------------------------------------------------------ #
    def _load(self) -> dict[str, Any]:
        try:
            with self.fixture_path.open(encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError as exc:
            raise StakeError(f"找不到离线数据文件：{self.fixture_path}") from exc
        except json.JSONDecodeError as exc:
            raise StakeError(f"离线数据格式错误：{exc}") from exc

    def _events(self) -> list[Event]:
        payload = self._load()
        now = self._now or utcnow()
        events: list[Event] = []
        for item in payload.get("events", []):
            events.append(self._build_event(item, now))
        events.sort(key=lambda event: event.start_time)
        return events

    def _build_event(self, item: dict[str, Any], now: datetime) -> Event:
        offset = int(item.get("offset_minutes", 0))
        start = now + timedelta(minutes=offset)
        score = item.get("score")
        score_pair = (int(score[0]), int(score[1])) if score else None

        if score_pair is not None:
            status = STATUS_ENDED
        elif offset <= 0:
            status = STATUS_LIVE
        else:
            status = STATUS_UPCOMING

        home = str(item.get("home", "主队"))
        away = str(item.get("away", "客队"))
        markets: list[Market] = []

        odds = item.get("odds") or {}
        if odds:
            outcomes = []
            for key, label in (("home", "主胜"), ("draw", "平局"), ("away", "客胜")):
                if key in odds:
                    outcomes.append(Outcome(key=key, label=f"{label} {home if key == 'home' else away if key == 'away' else ''}".strip(), odds=odds_value(odds[key])))
            if outcomes:
                markets.append(Market(key=KIND_1X2, name="全场独赢", kind=KIND_1X2, outcomes=tuple(outcomes)))

        for totals in item.get("totals", []) or []:
            line = Decimal(str(totals.get("line", 2.5)))
            outcomes = tuple(
                Outcome(key=side, label=f"{'大' if side == 'over' else '小'} {line}", odds=odds_value(totals[side]))
                for side in ("over", "under")
                if totals.get(side) is not None
            )
            if outcomes:
                markets.append(
                    Market(
                        key=f"totals_{line}",
                        name=f"总进球 {line}",
                        kind=KIND_TOTALS,
                        outcomes=outcomes,
                        line=line,
                    )
                )

        btts = item.get("btts")
        if btts:
            pairs = [("yes", "双方进球-是"), ("no", "双方进球-否")]
            outcomes = tuple(
                Outcome(key=key, label=label, odds=odds_value(btts[index]))
                for index, (key, label) in enumerate(pairs)
                if index < len(btts)
            )
            if outcomes:
                markets.append(Market(key=KIND_BTTS, name="双方进球", kind=KIND_BTTS, outcomes=outcomes))

        return Event(
            id=str(item["id"]),
            sport=str(item.get("sport", "football")),
            league=str(item.get("league", "")),
            home=home,
            away=away,
            start_time=start,
            status=status,
            live=status == STATUS_LIVE,
            markets=tuple(markets),
            score=score_pair,
            source="mock",
        )

    # -- 接口 ------------------------------------------------------------ #
    def fetch_events(self) -> list[Event]:
        return self._events()

    def fetch_results(self, event_ids: Sequence[str] | None = None) -> list[MatchResult]:
        return [
            MatchResult(
                event_id=event.id,
                home_score=event.score[0],
                away_score=event.score[1],
                recorded_at=utcnow(),
            )
            for event in self._events()
            if event.status == STATUS_ENDED and event.score is not None
        ]


# --------------------------------------------------------------------------- #
# 真实数据源
# --------------------------------------------------------------------------- #
class StakeProvider:
    name = "stake"
    label = "Stake 线上"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def fetch_events(self) -> list[Event]:
        with StakeClient(self.settings) as client:
            return client.fetch_events()

    def fetch_results(self, event_ids: Sequence[str] | None = None) -> list[MatchResult]:
        with StakeClient(self.settings) as client:
            return client.fetch_results(list(event_ids or []))


# --------------------------------------------------------------------------- #
# 自动降级
# --------------------------------------------------------------------------- #
class FallbackProvider:
    """先试真实数据源，失败则用离线演示数据，并把原因记录下来给 UI 展示。"""

    def __init__(self, primary: DataProvider, fallback: DataProvider) -> None:
        self.primary = primary
        self.fallback = fallback
        self.name = f"{primary.name}+{fallback.name}"
        self.active = primary.name
        self.last_warning = ""

    @property
    def label(self) -> str:
        return getattr(self.primary if self.active == self.primary.name else self.fallback, "label", self.active)

    def _run(self, method: str, *args):
        try:
            result = getattr(self.primary, method)(*args)
            self.active = self.primary.name
            self.last_warning = ""
            return result
        except StakeError as exc:
            self.last_warning = str(exc)
            self.active = self.fallback.name
            log.warning("真实数据源不可用，改用离线演示数据：%s", exc)
            return getattr(self.fallback, method)(*args)

    def fetch_events(self) -> list[Event]:
        return self._run("fetch_events")

    def fetch_results(self, event_ids: Sequence[str] | None = None) -> list[MatchResult]:
        return self._run("fetch_results", event_ids)


def build_provider(settings: Settings) -> DataProvider:
    """按配置返回数据源；auto 时真实失败自动降级。"""
    mock_path = settings.mock_dir / "mock_events.json"
    mock = MockProvider(mock_path)
    if settings.data_source == "mock":
        return mock
    stake = StakeProvider(settings)
    if settings.data_source == "stake":
        return stake
    return FallbackProvider(stake, mock)
