"""领域模型与赛果判定的单元测试。"""

from datetime import timezone
from decimal import Decimal

from models import (
    BET_LOST,
    BET_VOID,
    BET_WON,
    KIND_BTTS,
    KIND_1X2,
    KIND_TOTALS,
    Event,
    Market,
    Outcome,
    evaluate_market,
    odds_value,
    settle_bet,
)


def test_1x2_home_win(make_event):
    market = make_event().market(KIND_1X2)
    assert evaluate_market(market, 2, 1) == {"home": BET_WON, "draw": BET_LOST, "away": BET_LOST}


def test_1x2_draw(make_event):
    market = make_event().market(KIND_1X2)
    assert evaluate_market(market, 1, 1)["draw"] == BET_WON


def test_1x2_two_way_market(make_event):
    market = Market(
        key="1x2",
        name="Match Winner",
        kind=KIND_1X2,
        outcomes=(
            Outcome("home", "Brazil", odds_value("1.80")),
            Outcome("away", "Argentina", odds_value("2.00")),
        ),
    )
    assert evaluate_market(market, 0, 1) == {"home": BET_LOST, "away": BET_WON}


def test_totals_over_under(make_event):
    market = make_event().market("totals_2.5")
    assert evaluate_market(market, 2, 1) == {"over": BET_WON, "under": BET_LOST}
    assert evaluate_market(market, 1, 0) == {"over": BET_LOST, "under": BET_WON}


def test_totals_integer_line_push_is_void():
    market = Market(
        key="totals_3",
        name="总进球 3",
        kind=KIND_TOTALS,
        line=Decimal("3"),
        outcomes=(Outcome("over", "大 3", odds_value("1.9")), Outcome("under", "小 3", odds_value("1.9"))),
    )
    assert evaluate_market(market, 2, 1) == {"over": BET_VOID, "under": BET_VOID}


def test_btts(make_event):
    market = make_event().market(KIND_BTTS)
    assert evaluate_market(market, 1, 2) == {"yes": BET_WON, "no": BET_LOST}
    assert evaluate_market(market, 3, 0) == {"yes": BET_LOST, "no": BET_WON}


def test_unknown_market_is_void(make_event):
    market = Market(key="corners", name="角球数", kind="other", outcomes=(Outcome("over", "大 9.5", odds_value("1.9")),))
    assert evaluate_market(market, 1, 0) == {"over": BET_VOID}


def test_settle_bet_payouts(make_event):
    event = make_event()
    market = event.market(KIND_1X2)
    outcome = market.outcome("home")
    from models import Bet, utcnow

    bet = Bet(
        id="B1",
        event_id=event.id,
        event_label=event.title,
        market_key=market.key,
        market_name=market.name,
        outcome_key="home",
        outcome_label="Arsenal",
        odds=outcome.odds,
        stake=Decimal("100"),
        placed_at=utcnow(),
    )
    won = settle_bet(bet, event, 2, 0)
    assert won.status == BET_WON
    assert won.payout == Decimal("185.00")
    assert won.profit == Decimal("85.00")

    lost = settle_bet(bet, event, 0, 2)
    assert lost.status == BET_LOST
    assert lost.payout == Decimal("0.00")
    assert lost.profit == Decimal("-100.00")


def test_void_refunds_stake(make_event):
    event = make_event(totals=("3", "1.90", "1.90"))
    market = event.market("totals_3")
    from decimal import Decimal as D
    from models import Bet, utcnow

    bet = Bet(
        id="B2",
        event_id=event.id,
        event_label=event.title,
        market_key=market.key,
        market_name=market.name,
        outcome_key="over",
        outcome_label="大 3",
        odds=D("1.90"),
        stake=D("50"),
        placed_at=utcnow(),
    )
    settled = settle_bet(bet, event, 2, 1)
    assert settled.status == BET_VOID
    assert settled.payout == D("50.00")
    assert settled.profit == D("0.00")


def test_event_json_roundtrip(make_event):
    event = make_event(score=(2, 1))
    clone = Event.from_dict(event.to_dict())
    assert clone.id == event.id
    assert clone.start_time == event.start_time
    assert clone.start_time.tzinfo == timezone.utc
    assert clone.score == (2, 1)
    assert clone.markets[0].outcomes[0].odds == event.markets[0].outcomes[0].odds
