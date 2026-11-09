"""pytest 公共设置与夹具。"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from models import (  # noqa: E402
    KIND_1X2,
    KIND_BTTS,
    KIND_TOTALS,
    STATUS_UPCOMING,
    Event,
    Market,
    Outcome,
    odds_value,
)
from storage.repository import Repository  # noqa: E402


@pytest.fixture
def make_event():
    """构造一场带 1X2 / 大小球 / 双方进球盘口的比赛。"""

    def _make(
        event_id: str = "e1",
        home: str = "Arsenal",
        away: str = "Chelsea",
        *,
        status: str = STATUS_UPCOMING,
        score: tuple[int, int] | None = None,
        odds: tuple[str, str, str] | None = ("1.85", "3.60", "4.20"),
        totals: tuple[str, str, str] | None = ("2.5", "1.95", "1.85"),
        btts: bool = True,
        start_offset: int = 60,
        source: str = "stake",
    ) -> Event:
        markets: list[Market] = []
        if odds:
            markets.append(
                Market(
                    key=KIND_1X2,
                    name="全场独赢",
                    kind=KIND_1X2,
                    outcomes=tuple(
                        Outcome(key=key, label=label, odds=odds_value(value))
                        for key, label, value in zip(("home", "draw", "away"), (home, "平局", away), odds)
                    ),
                )
            )
        if totals:
            line = Decimal(totals[0])
            markets.append(
                Market(
                    key=f"totals_{line}",
                    name=f"总进球 {line}",
                    kind=KIND_TOTALS,
                    line=line,
                    outcomes=(
                        Outcome(key="over", label=f"大 {line}", odds=odds_value(totals[1])),
                        Outcome(key="under", label=f"小 {line}", odds=odds_value(totals[2])),
                    ),
                )
            )
        if btts:
            markets.append(
                Market(
                    key=KIND_BTTS,
                    name="双方进球",
                    kind=KIND_BTTS,
                    outcomes=(
                        Outcome(key="yes", label="是", odds=odds_value("1.72")),
                        Outcome(key="no", label="否", odds=odds_value("2.05")),
                    ),
                )
            )
        return Event(
            id=event_id,
            sport="football",
            league="Premier League",
            home=home,
            away=away,
            start_time=datetime.now(timezone.utc) + timedelta(minutes=start_offset),
            status=status,
            live=status == "live",
            markets=tuple(markets),
            score=score,
            source=source,
        )

    return _make


@pytest.fixture
def repo(tmp_path):
    repository = Repository(tmp_path / "groundbet-test.db")
    yield repository
    repository.close()


@pytest.fixture
def engine(repo):
    from betting.engine import SimulationEngine

    return SimulationEngine(repo, Decimal("1000"))
