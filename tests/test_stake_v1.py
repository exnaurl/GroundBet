"""Stake v1 真实结构的解析测试（字段对照线上响应）。"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from data.stake_v1 import build_result_query, fixture_to_event, fixture_to_result, parse_fixtures


def outcome(name: str, odds: float, active: bool = True) -> dict:
    return {"__typename": "SportMarketOutcome", "name": name, "odds": odds, "active": active}


def template(ext_id: str, name: str, markets: list[dict]) -> dict:
    return {"extId": ext_id, "name": name, "markets": markets}


def make_fixture(
    *,
    fixture_id: str = "6225dc5b-a32d-49ac-979a-47fbde2e5997",
    home: str = "Wadi Degla SC",
    away: str = "Kahrabaa Ismailia",
    match_status: str = "Not started",
    start: str = "Mon, 09 Nov 2026 12:30:00 GMT",
    home_score=None,
    away_score=None,
    groups: list[dict] | None = None,
    data_type: str = "SportFixtureDataMatch",
) -> dict:
    if groups is None:
        groups = [
            {
                "name": "main",
                "templates": [
                    template("1", "1x2", [
                        {"extId": "1", "name": "1x2", "specifiers": "",
                         "outcomes": [outcome(home, 1.91), outcome("Draw", 3.2), outcome(away, 3.75)]},
                    ]),
                    template("29", "Both Teams to Score", [
                        {"extId": "29", "name": "Both Teams to Score", "specifiers": "",
                         "outcomes": [outcome("Yes", 1.99), outcome("No", 1.67)]},
                    ]),
                    template("18", "Asian Total", [
                        {"extId": "18", "name": "Asian Total", "specifiers": "",
                         "outcomes": [outcome("Over 2.5", 2.18), outcome("Under 2.5", 1.58)]},
                    ]),
                    template("16", "Asian Handicap", [
                        {"extId": "16", "name": "Asian Handicap", "specifiers": "",
                         "outcomes": [outcome(f"{home} (-0.5)", 1.87), outcome(f"{away} (0.5)", 1.8)]},
                    ]),
                ],
            }
        ]
    return {
        "id": fixture_id,
        "stakeFixtureId": "eacfc025-f8c1-4267-b16e-09c6ed09233b",
        "name": f"{home} - {away}",
        "status": "active",
        "marketCount": 40,
        "data": {
            "__typename": data_type,
            "startTime": start,
            "isOutright": False,
            "teams": [
                {"name": home, "qualifier": "home"},
                {"name": away, "qualifier": "away"},
            ],
        },
        "tournament": {
            "name": "Copa Argentina 2026",
            "slug": "copa-argentina",
            "category": {"name": "Argentina", "slug": "argentina"},
        },
        "eventStatus": {
            "__typename": "SportFixtureEventStatusData",
            "homeScore": home_score,
            "awayScore": away_score,
            "matchStatus": match_status,
        },
        "groups": groups,
    }


def test_parse_upcoming_fixture():
    event = fixture_to_event(make_fixture())

    assert event is not None
    assert event.home == "Wadi Degla SC"
    assert event.away == "Kahrabaa Ismailia"
    assert event.league == "Copa Argentina 2026"
    assert event.start_time == datetime(2026, 11, 9, 12, 30, tzinfo=timezone.utc)
    assert event.status == "upcoming"
    assert event.live is False
    assert event.source == "stake"

    main = event.market("1x2")
    assert main is not None
    assert main.outcome("home").odds == Decimal("1.91")
    assert main.outcome("draw").odds == Decimal("3.20")
    assert main.outcome("away").odds == Decimal("3.75")

    btts = event.market("btts")
    assert btts.outcome("yes").odds == Decimal("1.99")

    totals = event.market("totals_2.5")
    assert totals is not None
    assert totals.line == Decimal("2.5")
    assert totals.outcome("over").odds == Decimal("2.18")

    # 让球盘不应被解析成可下注盘口
    assert len(event.markets) == 3


def test_skip_outright_fixture():
    node = make_fixture(data_type="SportFixtureDataOutright")
    assert fixture_to_event(node) is None


def test_skip_quarter_line_totals():
    groups = [{
        "name": "main",
        "templates": [
            template("1", "1x2", [
                {"extId": "1", "name": "1x2", "specifiers": "",
                 "outcomes": [outcome("A", 2.0), outcome("Draw", 3.0), outcome("B", 3.5)]},
            ]),
            template("18", "Asian Total", [
                {"extId": "18", "name": "Asian Total", "specifiers": "",
                 "outcomes": [outcome("Over 2.25", 1.9), outcome("Under 2.25", 1.9)]},
            ]),
        ],
    }]
    event = fixture_to_event(make_fixture(home="A", away="B", groups=groups))
    assert event.market("totals_2.25") is None
    assert event.market("totals_2.5") is None


def test_only_main_group_is_used():
    groups = [
        {"name": "main", "templates": [template("1", "1x2", [
            {"extId": "1", "name": "1x2", "specifiers": "",
             "outcomes": [outcome("A", 2.0), outcome("Draw", 3.0), outcome("B", 3.5)]},
        ])]},
        {"name": "goals", "templates": [template("18", "Asian Total", [
            {"extId": "18", "name": "Asian Total", "specifiers": "",
             "outcomes": [outcome("Over 1.5", 1.4), outcome("Under 1.5", 2.8)]},
        ])]},
    ]
    event = fixture_to_event(make_fixture(home="A", away="B", groups=groups))
    assert event.market("totals_1.5") is None
    assert len(event.markets) == 1


def test_finished_fixture_has_score():
    node = make_fixture(match_status="Finished", home_score=2, away_score=1)
    event = fixture_to_event(node)
    assert event.status == "ended"
    assert event.score == (2, 1)

    result = fixture_to_result(node)
    assert result is not None
    assert (result.home_score, result.away_score) == (2, 1)


def test_live_fixture_keeps_live_status():
    node = make_fixture(match_status="Live", home_score=1, away_score=0)
    event = fixture_to_event(node)
    assert event.status == "live"
    assert event.live is True
    assert event.score == (1, 0)


def test_unfinished_fixture_has_no_result():
    assert fixture_to_result(make_fixture()) is None


def test_parse_payload_wrapper():
    payload = {"sport": {"fixtureList": [make_fixture(), make_fixture(fixture_id="c0ffee00-0000-0000-0000-000000000000")]}}
    events = parse_fixtures(payload)
    assert len(events) == 2


def test_build_result_query_uses_aliases_and_rejects_bad_ids():
    query = build_result_query([
        "6225dc5b-a32d-49ac-979a-47fbde2e5997",
        "not-an-id",
        "c0ffee00-0000-0000-0000-000000000000",
    ])
    assert 'f0: sportFixture(fixtureId: "6225dc5b-a32d-49ac-979a-47fbde2e5997")' in query
    assert 'f1: sportFixture(fixtureId: "c0ffee00-0000-0000-0000-000000000000")' in query
    assert "not-an-id" not in query

    with pytest.raises(ValueError):
        build_result_query(["bad"])
