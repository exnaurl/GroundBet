"""容错解析的单元测试：覆盖几种常见返回结构。"""

from datetime import datetime, timezone
from decimal import Decimal

from data.stake_parse import parse_events, parse_results

GRAPHQL_PAYLOAD = {
    "data": {
        "sportEvents": {
            "edges": [
                {
                    "node": {
                        "id": "evt-1",
                        "startTime": "2026-11-10T20:00:00.000Z",
                        "status": "scheduled",
                        "sport": {"name": "Football", "slug": "football"},
                        "league": {"name": "Premier League", "slug": "epl"},
                        "competitors": [
                            {"name": "Arsenal", "qualifier": "home"},
                            {"name": "Chelsea", "qualifier": "away"},
                        ],
                        "markets": [
                            {
                                "name": "1X2",
                                "outcomes": [
                                    {"name": "Arsenal", "odds": 1.85},
                                    {"name": "Draw", "odds": 3.6},
                                    {"name": "Chelsea", "odds": 4.2},
                                ],
                            },
                            {
                                "name": "Total Goals 2.5",
                                "outcomes": [
                                    {"name": "Over 2.5", "odds": 1.95},
                                    {"name": "Under 2.5", "odds": 1.85},
                                ],
                            },
                        ],
                    }
                }
            ]
        }
    }
}


def test_parse_graphql_edges():
    events = parse_events(GRAPHQL_PAYLOAD)
    assert len(events) == 1
    event = events[0]
    assert event.id == "evt-1"
    assert (event.home, event.away) == ("Arsenal", "Chelsea")
    assert event.league == "Premier League"
    assert event.start_time == datetime(2026, 11, 10, 20, 0, tzinfo=timezone.utc)
    assert event.status == "upcoming"

    main = event.market("1x2")
    assert main is not None
    assert main.outcome("home").odds == Decimal("1.85")
    assert main.outcome("draw").odds == Decimal("3.60")
    assert main.outcome("away").odds == Decimal("4.20")

    totals = event.market("totals_2.5")
    assert totals is not None
    assert totals.line == Decimal("2.5")
    assert totals.outcome("over").odds == Decimal("1.95")


def test_parse_flat_odds_dict():
    payload = {
        "events": [
            {
                "id": "evt-2",
                "home_team": "Alpha",
                "away_team": "Beta",
                "start_time": "2026-11-11T12:00:00Z",
                "status": "live",
                "odds": {"home": 1.9, "draw": 3.2, "away": 4.0},
            }
        ]
    }
    events = parse_events(payload)
    assert len(events) == 1
    event = events[0]
    assert event.live is True
    assert event.status == "live"
    main = event.market("1x2")
    assert {outcome.key for outcome in main.outcomes} == {"home", "draw", "away"}


def test_parse_chinese_labels():
    payload = {
        "events": [
            {
                "id": "cn-1",
                "home": "上海海港",
                "away": "北京国安",
                "start_time": "2026-11-12T11:35:00+08:00",
                "status": "not_started",
                "markets": [
                    {
                        "name": "胜平负",
                        "outcomes": [
                            {"name": "主胜", "odds": 2.3},
                            {"name": "平局", "odds": 3.1},
                            {"name": "客胜", "odds": 3.05},
                        ],
                    },
                    {
                        "name": "大小球",
                        "line": 2.5,
                        "outcomes": [
                            {"name": "大", "odds": 1.9},
                            {"name": "小", "odds": 1.9},
                        ],
                    },
                ],
            }
        ]
    }
    events = parse_events(payload)
    assert len(events) == 1
    event = events[0]
    assert event.market("1x2").outcome("home").odds == Decimal("2.30")
    assert event.market("totals_2.5").outcome("under").odds == Decimal("1.90")


def test_parse_two_way_market():
    payload = {
        "events": [
            {
                "id": "ko-1",
                "home": "France",
                "away": "Brazil",
                "start_time": "2026-11-13T18:00:00Z",
                "status": "scheduled",
                "markets": [
                    {
                        "name": "Match Winner",
                        "outcomes": [
                            {"name": "France", "odds": 2.4},
                            {"name": "Brazil", "odds": 1.6},
                        ],
                    }
                ],
            }
        ]
    }
    market = parse_events(payload)[0].market("1x2")
    assert {outcome.key for outcome in market.outcomes} == {"home", "away"}


def test_parse_results_from_ended_nodes():
    payload = {
        "data": {
            "sportEvents": {
                "edges": [
                    {
                        "node": {
                            "id": "evt-3",
                            "startTime": "2026-11-01T20:00:00Z",
                            "status": "finished",
                            "sport": {"slug": "football"},
                            "league": {"name": "Serie A"},
                            "competitors": [
                                {"name": "Inter", "qualifier": "home"},
                                {"name": "Milan", "qualifier": "away"},
                            ],
                            "score": {"home": 2, "away": 1},
                        }
                    }
                ]
            }
        }
    }
    results = parse_results(payload)
    assert len(results) == 1
    assert results[0].event_id == "evt-3"
    assert (results[0].home_score, results[0].away_score) == (2, 1)


def test_parse_manual_results_list():
    payload = [{"event_id": "evt-9", "home_score": 0, "away_score": 0}]
    results = parse_results(payload)
    assert len(results) == 1
    assert results[0].event_id == "evt-9"


def test_ignore_unrelated_json():
    assert parse_events({"hello": "world"}) == []
    assert parse_results({"hello": "world"}) == []
