"""Stake 体育接口 v1（``/_api/v1/graphql``）的查询与解析。

真实 schema（2026-11 实测，GraphQL Introspection 被关闭，字段由线上响应反推）::

    Query.sport(sportId: ID!)                  -> Sport
    Query.sportFixture(fixtureId: ID!)         -> SportFixture
    Sport.fixtureList(limit: Int, type: SportSearchEnum) -> [SportFixture]

    SportFixture.data 是 union: ... on SportFixtureDataMatch { startTime teams { name qualifier } }
    SportFixture.eventStatus 是 union: ... on SportFixtureEventStatusData { homeScore awayScore matchStatus }
    SportFixture.groups -> SportGroup -> templates -> markets -> outcomes { name odds active }

只取 ``main`` 盘口组里的三个市场模板（extId）：

* ``1``  1x2
* ``29`` Both Teams to Score
* ``18`` Asian Total（只接受 .0 / .5 盘口，避开四分之一球）

其它盘口（正确比分、半全场、让球等）不解析，避免产生无法自动结算的注单。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from models import (
    KIND_1X2,
    KIND_BTTS,
    KIND_TOTALS,
    STATUS_ENDED,
    STATUS_LIVE,
    STATUS_UPCOMING,
    STATUS_UNKNOWN,
    Event,
    Market,
    MatchResult,
    Outcome,
    odds_value,
    parse_datetime,
    to_decimal,
    utcnow,
)

#: 足球的 sportId（stake.com 内部 UUID，slug 不能代替，只能填 UUID）
SOCCER_SPORT_ID = "5b4b60b9-ed95-41e7-97e3-f33aa172cf12"

SPORT_IDS = {
    "football": SOCCER_SPORT_ID,
    "soccer": SOCCER_SPORT_ID,
}

TEMPLATE_1X2 = "1"
TEMPLATE_BTTS = "29"
TEMPLATE_TOTALS = "18"

MAIN_GROUP = "main"

_ACCEPTED_LINES = (0.0, 0.5)

_ENDED_WORDS = ("finished", "ended", "full time", "after penalties", "awarded", "settled", "ft")
_LIVE_WORDS = (
    "live",
    "in play",
    "inplay",
    "1st half",
    "2nd half",
    "half time",
    "halftime",
    "extra time",
    "penalties",
    "in progress",
)
_UPCOMING_WORDS = ("not started", "scheduled", "upcoming", "prematch", "pre-match")


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ("" if value is None else str(value))


def parse_start_time(value: Any) -> datetime | None:
    """Stake 的 startTime 形如 ``Mon, 09 Nov 2026 12:30:00 GMT``。"""
    text = _text(value)
    if not text:
        return None
    for pattern in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S"):
        try:
            parsed = datetime.strptime(text, pattern)
        except ValueError:
            continue
        return parsed.replace(tzinfo=timezone.utc)
    return parse_datetime(text)


def fixture_status(node: Mapping[str, Any]) -> tuple[str, bool]:
    """返回 (内部状态, 是否 live)。"""
    event = node.get("eventStatus") or {}
    match_status = _text(event.get("matchStatus")).lower()
    raw_status = _text(node.get("status")).lower()

    if any(word in match_status for word in _ENDED_WORDS) or raw_status in ("ended", "finished", "settled"):
        return STATUS_ENDED, False
    if any(word in match_status for word in _LIVE_WORDS) or raw_status == "live":
        return STATUS_LIVE, True
    if any(word in match_status for word in _UPCOMING_WORDS):
        return STATUS_UPCOMING, False
    if match_status:
        return STATUS_UNKNOWN, False
    return STATUS_UPCOMING, False


def fixture_score(node: Mapping[str, Any]) -> tuple[int, int] | None:
    event = node.get("eventStatus") or {}
    home = event.get("homeScore")
    away = event.get("awayScore")
    if isinstance(home, (int, float)) and isinstance(away, (int, float)):
        return int(home), int(away)
    return None


def _teams(fixture: Mapping[str, Any]) -> tuple[str, str]:
    data = fixture.get("data") or {}
    teams = data.get("teams") or []
    home = away = ""
    for team in teams:
        qualifier = _text(team.get("qualifier")).lower()
        name = _text(team.get("name"))
        if qualifier == "home":
            home = name
        elif qualifier == "away":
            away = name
    if home and away:
        return home, away
    # 兜底：name 形如 "Home - Away"
    parts = [part.strip() for part in _text(fixture.get("name")).split(" - ")]
    if len(parts) == 2:
        return parts[0], parts[1]
    return home, away


def _line_from_labels(labels: Sequence[str]) -> float | None:
    for label in labels:
        match = re.search(r"(\d+(?:\.\d+)?)", label or "")
        if match:
            return float(match.group(1))
    return None


def _one_x_two(outs: Sequence[Mapping[str, Any]], home: str, away: str) -> Market | None:
    outcomes: list[Outcome] = []
    for index, item in enumerate(outs):
        if item.get("active") is False:
            continue
        name = _text(item.get("name"))
        odds = to_decimal(item.get("odds"))
        if odds is None or odds <= 1:
            continue
        lowered = name.lower()
        if lowered in ("draw", "tie", "x"):
            key = "draw"
        elif home and (home.lower() == lowered or home.lower() in lowered):
            key = "home"
        elif away and (away.lower() == lowered or away.lower() in lowered):
            key = "away"
        else:
            key = ("home", "draw", "away")[index] if len(outs) == 3 else ("home", "away")[index]
        outcomes.append(Outcome(key=key, label=name or key, odds=odds_value(odds)))
    keys = {item.key for item in outcomes}
    if not outcomes or not keys <= {"home", "draw", "away"}:
        return None
    return Market(key=KIND_1X2, name="1X2", kind=KIND_1X2, outcomes=tuple(outcomes))


def _btts(outs: Sequence[Mapping[str, Any]]) -> Market | None:
    outcomes: list[Outcome] = []
    for item in outs:
        if item.get("active") is False:
            continue
        name = _text(item.get("name"))
        odds = to_decimal(item.get("odds"))
        if odds is None or odds <= 1:
            continue
        lowered = name.lower()
        key = "yes" if lowered.startswith("y") else "no" if lowered.startswith("n") else ""
        if key:
            outcomes.append(Outcome(key=key, label=name, odds=odds_value(odds)))
    if len(outcomes) != 2:
        return None
    return Market(key=KIND_BTTS, name="Both Teams to Score", kind=KIND_BTTS, outcomes=tuple(outcomes))


def _totals(outs: Sequence[Mapping[str, Any]]) -> Market | None:
    labels = [_text(item.get("name")) for item in outs]
    line = _line_from_labels(labels)
    if line is None:
        return None
    line_decimal = to_decimal(line)
    if line_decimal is None:
        return None
    # 只接受 2 / 2.5 这类整半球盘；2.25 / 2.75 需要拆盘，暂不支持
    if line_decimal != line_decimal.to_integral() and (line_decimal * 2) != (line_decimal * 2).to_integral():
        return None

    outcomes: list[Outcome] = []
    for item in outs:
        if item.get("active") is False:
            continue
        name = _text(item.get("name"))
        odds = to_decimal(item.get("odds"))
        if odds is None or odds <= 1:
            continue
        lowered = name.lower()
        if lowered.startswith("over"):
            key = "over"
        elif lowered.startswith("under"):
            key = "under"
        else:
            continue
        outcomes.append(Outcome(key=key, label=name, odds=odds_value(odds)))
    if {item.key for item in outcomes} != {"over", "under"}:
        return None
    return Market(
        key=f"totals_{line_decimal.normalize()}",
        name=f"Total {line_decimal.normalize()}",
        kind=KIND_TOTALS,
        outcomes=tuple(outcomes),
        line=line_decimal,
    )


def _markets(fixture: Mapping[str, Any], home: str, away: str) -> tuple[Market, ...]:
    markets: list[Market] = []
    for group in fixture.get("groups") or []:
        if _text(group.get("name")).lower() != MAIN_GROUP:
            continue
        for template in group.get("templates") or []:
            ext_id = _text(template.get("extId"))
            for market in template.get("markets") or []:
                outcomes = market.get("outcomes") or []
                built: Market | None = None
                if ext_id == TEMPLATE_1X2:
                    built = _one_x_two(outcomes, home, away)
                elif ext_id == TEMPLATE_BTTS:
                    built = _btts(outcomes)
                elif ext_id == TEMPLATE_TOTALS:
                    built = _totals(outcomes)
                if built is not None:
                    markets.append(built)
    deduped: dict[str, Market] = {}
    for market in markets:
        deduped.setdefault(market.key, market)
    return tuple(deduped.values())


def fixture_to_event(fixture: Mapping[str, Any]) -> Event | None:
    """把一条 SportFixture 转成内部 Event；串关/冠军盘（无球队）返回 None。"""
    data = fixture.get("data") or {}
    if data.get("__typename") != "SportFixtureDataMatch":
        return None
    if data.get("isOutright"):
        return None
    fixture_id = _text(fixture.get("id"))
    home, away = _teams(fixture)
    start_time = parse_start_time(data.get("startTime"))
    if not fixture_id or not home or not away or start_time is None:
        return None

    status, live = fixture_status(fixture)
    score = fixture_score(fixture)
    if score is not None and status == STATUS_UNKNOWN:
        # matchStatus 缺失但有比分：按已结束处理
        status = STATUS_ENDED
        live = False

    tournament = fixture.get("tournament") or {}
    category = tournament.get("category") or {}

    return Event(
        id=fixture_id,
        sport="football",
        league=_text(tournament.get("name")) or _text(category.get("name")),
        home=home,
        away=away,
        start_time=start_time,
        status=status,
        live=live,
        markets=_markets(fixture, home, away),
        score=score,
    )


def fixture_to_result(fixture: Mapping[str, Any]) -> MatchResult | None:
    if (fixture.get("data") or {}).get("__typename") != "SportFixtureDataMatch":
        return None
    status, _ = fixture_status(fixture)
    score = fixture_score(fixture)
    if status != STATUS_ENDED or score is None:
        return None
    return MatchResult(
        event_id=_text(fixture.get("id")),
        home_score=score[0],
        away_score=score[1],
        recorded_at=utcnow(),
    )


def parse_fixtures(payload: Any) -> list[Event]:
    """``{"sport": {"upcoming": [...], "live": [...]}}`` 或直接是 fixture 列表。

    没有可下注盘口的比赛会被丢掉（例如只有冠军盘的场次）。
    """
    nodes = _fixture_nodes(payload)
    events = [fixture_to_event(node) for node in nodes]
    return [event for event in events if event is not None and event.markets]


def parse_results(payload: Any) -> list[MatchResult]:
    nodes = _fixture_nodes(payload)
    results = [fixture_to_result(node) for node in nodes]
    return [result for result in results if result is not None]


def _fixture_nodes(payload: Any) -> list[Mapping[str, Any]]:
    if isinstance(payload, list):
        return [node for node in payload if isinstance(node, Mapping)]
    if not isinstance(payload, Mapping):
        return []

    nodes: list[Mapping[str, Any]] = []
    sport = payload.get("sport")
    if isinstance(sport, Mapping):
        # fixtureList / upcoming / live 都可能是列表
        for value in sport.values():
            if isinstance(value, list):
                nodes.extend(node for node in value if isinstance(node, Mapping) and "eventStatus" in node)
    # 批量别名查询：{"f0": {...}, "f1": {...}}
    for value in payload.values():
        if isinstance(value, Mapping) and "eventStatus" in value:
            nodes.append(value)

    deduped: dict[str, Mapping[str, Any]] = {}
    for node in nodes:
        identifier = _text(node.get("id"))
        if identifier:
            deduped.setdefault(identifier, node)
    return list(deduped.values())


def chunked(items: Iterable[str], size: int) -> list[list[str]]:
    items = list(items)
    return [items[index:index + size] for index in range(0, len(items), size)]


_RESULT_FIELDS = """
    id
    status
    data {
      __typename
      ... on SportFixtureDataMatch { startTime isOutright teams { name qualifier } }
    }
    eventStatus {
      __typename
      ... on SportFixtureEventStatusData { homeScore awayScore matchStatus }
    }
"""

_FIXTURE_ID = re.compile(r"^[0-9a-fA-F-]{36}$")


def build_result_query(fixture_ids: Sequence[str]) -> str:
    """用别名一次查多场比赛的比分（GraphQL 单请求）。"""
    safe_ids = [item for item in fixture_ids if _FIXTURE_ID.match(item)]
    if not safe_ids:
        raise ValueError("没有合法的 fixtureId")
    parts = [
        f'  f{index}: sportFixture(fixtureId: "{item}") {{ {_RESULT_FIELDS} }}'
        for index, item in enumerate(safe_ids)
    ]
    return "query SportResults {\n" + "\n".join(parts) + "\n}"
