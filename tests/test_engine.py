"""模拟下注引擎的单元测试。"""

from decimal import Decimal

import pytest

from betting.engine import BetError
from models import BET_PENDING, BET_WON, STATUS_ENDED, Event, Market, Outcome, odds_value


def test_place_bet_deducts_available_balance(engine, make_event):
    event = make_event()
    engine.repo.upsert_events([event])
    bet = engine.place_bet(event, "1x2", "home", "100")

    assert bet.status == BET_PENDING
    assert engine.balance == Decimal("900.00")
    stats = engine.stats()
    assert stats.pending_stake == Decimal("100.00")
    assert stats.equity == Decimal("1000.00")


def test_settle_win_credits_payout(engine, make_event):
    event = make_event()
    engine.repo.upsert_events([event])
    engine.place_bet(event, "1x2", "home", "100")

    settled = engine.settle_event(event.id, 2, 0)
    assert len(settled) == 1
    assert settled[0].status == BET_WON
    assert engine.balance == Decimal("1085.00")

    stats = engine.stats()
    assert stats.realized_profit == Decimal("85.00")
    assert stats.roi == pytest.approx(0.85)
    assert stats.win_rate == pytest.approx(1.0)


def test_settle_loss_keeps_stake(engine, make_event):
    event = make_event()
    engine.repo.upsert_events([event])
    engine.place_bet(event, "1x2", "home", "100")
    engine.settle_event(event.id, 0, 2)

    assert engine.balance == Decimal("900.00")
    stats = engine.stats()
    assert stats.realized_profit == Decimal("-100.00")
    assert stats.lost == 1


def test_reject_insufficient_balance(engine, make_event):
    event = make_event()
    engine.repo.upsert_events([event])
    with pytest.raises(BetError, match="余额不足"):
        engine.place_bet(event, "1x2", "home", "5000")


def test_reject_duplicate_open_bet(engine, make_event):
    event = make_event()
    engine.repo.upsert_events([event])
    engine.place_bet(event, "1x2", "home", "10")
    with pytest.raises(BetError, match="已有一张待结算注单"):
        engine.place_bet(event, "1x2", "home", "10")


def test_reject_closed_event(engine, make_event):
    event = make_event(status=STATUS_ENDED, score=(1, 0))
    engine.repo.upsert_events([event])
    with pytest.raises(BetError, match="无法下注"):
        engine.place_bet(event, "1x2", "home", "10")


def test_reject_unsupported_market(engine, make_event):
    event = make_event(btts=False)
    exotic = Market(
        key="corners",
        name="角球数",
        kind="other",
        outcomes=(Outcome("over", "大 9.5", odds_value("1.9")),),
    )
    event = Event(
        id=event.id,
        sport=event.sport,
        league=event.league,
        home=event.home,
        away=event.away,
        start_time=event.start_time,
        status=event.status,
        markets=event.markets + (exotic,),
    )
    engine.repo.upsert_events([event])
    with pytest.raises(BetError, match="不支持"):
        engine.place_bet(event, "corners", "over", "10")


def test_reject_bad_arguments(engine, make_event):
    event = make_event()
    engine.repo.upsert_events([event])
    with pytest.raises(BetError):
        engine.place_bet(event, "1x2", "nope", "10")
    with pytest.raises(BetError):
        engine.place_bet(event, "nope", "home", "10")
    with pytest.raises(BetError):
        engine.place_bet(event, "1x2", "home", "0")


def test_settle_is_idempotent(engine, make_event):
    event = make_event()
    engine.repo.upsert_events([event])
    engine.place_bet(event, "1x2", "home", "100")
    engine.settle_event(event.id, 2, 0)
    balance = engine.balance

    again = engine.settle_event(event.id, 2, 0)
    assert again == []
    assert engine.balance == balance


def test_settle_pending_uses_stored_results(engine, make_event):
    from models import MatchResult, utcnow

    event = make_event()
    engine.repo.upsert_events([event])
    engine.place_bet(event, "totals_2.5", "over", "20")
    engine.repo.upsert_result(MatchResult(event.id, 2, 1, utcnow()))

    settled = engine.settle_pending()
    assert len(settled) == 1
    assert settled[0].status == BET_WON
    assert engine.balance == Decimal("1019.00")  # 1000 - 20 + 20*1.95


def test_settle_marks_event_ended_and_blocks_new_bets(engine, make_event):
    event = make_event()
    engine.repo.upsert_events([event])
    engine.place_bet(event, "1x2", "home", "10")

    engine.settle_event(event.id, 2, 0)
    stored = engine.repo.get_event(event.id)
    assert stored.status == STATUS_ENDED
    assert stored.score == (2, 0)
    assert stored.live is False

    with pytest.raises(BetError, match="已录入赛果"):
        engine.place_bet(stored, "1x2", "away", "10")


def test_max_drawdown(engine, make_event):
    first = make_event("e1")
    second = make_event("e2")
    engine.repo.upsert_events([first, second])
    engine.place_bet(first, "1x2", "home", "100")
    engine.place_bet(second, "1x2", "home", "100")

    engine.settle_event(first.id, 0, 1)  # 输
    assert engine.stats().max_drawdown == Decimal("100.00")
    engine.settle_event(second.id, 2, 0)  # 赢 @1.85
    assert engine.balance == Decimal("985.00")  # 1000 - 100 + 185 - 100
    assert engine.stats().max_drawdown == Decimal("100.00")


def test_clean_demo_data_keeps_real_account(engine, make_event):
    demo = make_event("mock-1", source="mock")
    real = make_event("real-1", source="stake")
    engine.repo.upsert_events([demo, real])
    demo_bet = engine.place_bet(demo, "1x2", "home", "10")
    engine.place_bet(real, "1x2", "home", "20")
    assert demo_bet.source == "mock"

    removed = engine.clean_demo_data()

    assert removed["events"] == 1
    assert removed["bets"] == 1
    assert [event.id for event in engine.repo.list_events()] == ["real-1"]
    assert engine.stats().pending_stake == Decimal("20.00")
    assert engine.balance == Decimal("980.00")
    assert engine.clean_demo_data()["events"] == 0  # 幂等


def test_reset_restores_initial_balance(engine, make_event):
    event = make_event()
    engine.repo.upsert_events([event])
    engine.place_bet(event, "1x2", "home", "100")
    engine.reset()
    assert engine.balance == Decimal("1000.00")
    assert engine.repo.list_bets() == []


def test_balance_formula_matches_stats(engine, make_event):
    event = make_event()
    engine.repo.upsert_events([event])
    engine.place_bet(event, "1x2", "home", "30")
    engine.place_bet(event, "btts", "no", "20")
    stats = engine.stats()
    assert stats.balance + stats.pending_stake == stats.initial_balance
    assert stats.bet_count == 2
    assert stats.pending_count == 2
