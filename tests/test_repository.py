"""SQLite 持久化层测试。"""

from decimal import Decimal

from models import Bet, MatchResult, money, utcnow


def test_event_upsert_and_read(repo, make_event):
    event = make_event()
    assert repo.upsert_events([event]) == 1

    stored = repo.get_event(event.id)
    assert stored is not None
    assert stored.title == event.title
    assert stored.markets[0].outcomes[0].odds == event.markets[0].outcomes[0].odds
    assert [item.id for item in repo.list_events()] == [event.id]
    assert [item.id for item in repo.list_events(only_open=False)] == [event.id]


def test_odds_snapshot_only_records_changes(repo, make_event):
    event = make_event()
    assert repo.record_odds_snapshot([event]) == 7  # 3 个 1X2 + 2 个大小球 + 2 个双方进球
    assert repo.record_odds_snapshot([event]) == 0

    changed = make_event(odds=("1.90", "3.60", "4.20"))
    assert repo.record_odds_snapshot([changed]) == 1

    history = repo.odds_history(event.id, "1x2", "home")
    assert [value for value, _ in history] == [Decimal("1.85"), Decimal("1.90")]


def test_result_upsert(repo):
    recorded = utcnow()
    repo.upsert_result(MatchResult("e1", 2, 1, recorded))
    repo.upsert_result(MatchResult("e1", 3, 1, recorded))  # 覆盖

    stored = repo.get_result("e1")
    assert stored is not None
    assert (stored.home_score, stored.away_score) == (3, 1)
    assert repo.get_result("nope") is None


def test_bet_roundtrip(repo):
    bet = Bet(
        id="B1",
        event_id="e1",
        event_label="Arsenal vs Chelsea",
        market_key="1x2",
        market_name="全场独赢",
        outcome_key="home",
        outcome_label="主胜",
        odds=Decimal("1.85"),
        stake=money("100"),
        placed_at=utcnow(),
    )
    repo.add_bet(bet)
    loaded = repo.list_bets()[0]
    assert loaded.id == bet.id
    assert loaded.stake == Decimal("100.00")
    assert loaded.status == "pending"

    settled = loaded.with_status("won", Decimal("185.00"))
    repo.update_bet(settled)
    assert repo.list_bets("won")[0].payout == Decimal("185.00")
    assert repo.pending_bets() == []


def test_upsert_keeps_ended_status_when_result_exists(repo, make_event):
    repo.upsert_events([make_event(status="upcoming")])
    repo.upsert_result(MatchResult("e1", 1, 0, utcnow()))

    # 重新抓取时数据源说「进行中」，也不应该覆盖掉已录入的赛果
    repo.upsert_events([make_event(status="live")])
    stored = repo.get_event("e1")
    assert stored.status == "ended"
    assert stored.live is False
    assert stored.score == (1, 0)


def test_source_column_roundtrip_and_filter(repo, make_event):
    repo.upsert_events([
        make_event("mock-1", source="mock"),
        make_event("real-1", source="stake"),
    ])
    assert repo.sources() == {"mock": 1, "stake": 1}
    assert [event.id for event in repo.list_events(source="mock")] == ["mock-1"]
    assert [event.id for event in repo.list_events(source="stake")] == ["real-1"]
    assert repo.get_event("mock-1").source == "mock"


def test_purge_source_removes_related_rows(repo, make_event):
    mock_event = make_event("mock-a", source="mock")
    real_event = make_event("real-a", source="stake")
    repo.upsert_events([mock_event, real_event])
    repo.record_odds_snapshot([mock_event, real_event])
    repo.upsert_result(MatchResult("mock-a", 1, 0, utcnow()))
    repo.add_bet(
        Bet(
            id="B-MOCK",
            event_id="mock-a",
            event_label=mock_event.title,
            market_key="1x2",
            market_name="全场独赢",
            outcome_key="home",
            outcome_label="主胜",
            odds=Decimal("1.85"),
            stake=money("10"),
            placed_at=utcnow(),
            source="mock",
        )
    )

    removed = repo.purge_source("mock")

    assert removed["events"] == 1
    assert removed["bets"] == 1
    assert removed["results"] == 1
    assert removed["snapshots"] == 7
    assert repo.get_event("mock-a") is None
    assert repo.get_result("mock-a") is None
    assert repo.list_bets() == []
    assert repo.sources() == {"stake": 1}
    assert repo.get_event("real-a") is not None


def test_legacy_database_gets_source_column(tmp_path):
    """老库没有 source 列，打开时应自动补列并把 mock- 前缀的数据标成离线。"""
    import sqlite3

    path = tmp_path / "legacy.db"
    con = sqlite3.connect(str(path))
    con.executescript(
        """
        CREATE TABLE events (
            id TEXT PRIMARY KEY, sport TEXT, league TEXT, home TEXT, away TEXT,
            start_time TEXT, status TEXT, live INTEGER, payload TEXT, updated_at TEXT
        );
        CREATE TABLE bets (
            id TEXT PRIMARY KEY, event_id TEXT, event_label TEXT, market_key TEXT,
            market_name TEXT, outcome_key TEXT, outcome_label TEXT, odds TEXT,
            stake TEXT, status TEXT, payout TEXT, placed_at TEXT, settled_at TEXT,
            note TEXT
        );
        """
    )
    con.execute(
        "INSERT INTO events VALUES ('mock-x','football','L','A','B','2026-01-01T00:00:00+00:00','upcoming',0,'{}','x')"
    )
    con.execute(
        "INSERT INTO events VALUES ('real-y','football','L','C','D','2026-01-01T00:00:00+00:00','upcoming',0,'{}','x')"
    )
    con.commit()
    con.close()

    from storage.repository import Repository

    repo = Repository(path)
    try:
        assert repo.sources() == {"mock": 1, "stake": 1}
    finally:
        repo.close()


def test_meta_and_reset(repo, make_event):
    repo.set_meta("initial_balance", "1000")
    assert repo.get_meta("initial_balance") == "1000"

    repo.upsert_events([make_event()])
    repo.add_bet(
        Bet(
            id="B2",
            event_id="e1",
            event_label="A vs B",
            market_key="1x2",
            market_name="全场独赢",
            outcome_key="home",
            outcome_label="主胜",
            odds=Decimal("2.00"),
            stake=money("10"),
            placed_at=utcnow(),
        )
    )
    repo.upsert_result(MatchResult("e1", 1, 0, utcnow()))

    repo.reset(drop_bets=True)
    assert repo.list_bets() == []
    assert repo.get_meta("initial_balance") is None
    assert repo.list_events()  # 比赛缓存保留
    assert repo.get_result("e1") is not None

    repo.reset(drop_bets=False, drop_results=True, drop_meta=False)
    assert repo.get_result("e1") is None
