"""把 Stake 返回的 JSON 解析成内部模型。

Stake 的体育接口没有公开文档，字段名会随前端版本变化，所以这里不做强绑定：

* 先在任意嵌套结构里找出「像赛事节点」的 dict（edges/nodes/list 都能处理）；
* 再用「大小写 + 下划线不敏感」的别名表取字段；
* 解析不出来时返回空列表，由上层给出可操作的提示。

如果哪天字段全变了，把浏览器 DevTools 里抓到的响应存成 JSON，
用 ``python cli.py capture 文件.json`` 导入即可，不需要改代码。
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any, Iterable, Iterator, Mapping, Sequence

from models import (
    KIND_1X2,
    KIND_BTTS,
    KIND_OTHER,
    KIND_TOTALS,
    STATUS_ENDED,
    STATUS_LIVE,
    STATUS_UNKNOWN,
    STATUS_UPCOMING,
    Event,
    Market,
    MatchResult,
    Outcome,
    odds_value,
    parse_datetime,
    to_decimal,
    utcnow,
)

_ID_KEYS = ("id", "eventId", "event_id", "slug", "key", "fixtureId")
_COMP_KEYS = ("competitors", "teams", "participants", "opponents", "runners")
_HOME_KEYS = ("homeTeam", "home_team", "home", "team1", "localTeam")
_AWAY_KEYS = ("awayTeam", "away_team", "away", "team2", "visitorTeam")
_START_KEYS = (
    "startTime",
    "start_time",
    "commenceTime",
    "commence_time",
    "startsAt",
    "startDate",
    "scheduled",
    "kickoff",
    "date",
)
_MARKET_KEYS = ("markets", "odds", "bettingMarkets", "prices", "marketGroups")

_ONE_X_TWO = re.compile(
    r"1\s*[x×]\s*2|match\s*(result|winner)|full[\s-]?time\s*result|"
    r"moneyline|winner|胜平负|独赢|主胜平|胜负",
    re.IGNORECASE,
)
_TOTALS = re.compile(
    r"total|over[\s/&-]?under|o/u|大小球|总进球|进球数|goals?\s*(over|under)", re.IGNORECASE
)
_BTTS = re.compile(r"both\s+teams?\s+to\s+score|btts|双方进球|两队均得分", re.IGNORECASE)
_LINE = re.compile(r"(\d+(?:\.\d+)?)")

_DRAW_WORDS = ("draw", "tie", "x", "平")


def _norm_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def _norm(mapping: Mapping[str, Any]) -> dict[str, Any]:
    return {_norm_key(key): value for key, value in mapping.items()}


def _pick(mapping: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        value = mapping.get(_norm_key(name))
        if value not in (None, "", [], {}):
            return value
    return default


def _text(value: Any) -> str:
    """从一个可能是 str / dict / list 的字段里取出可读文本。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, Mapping):
        inner = _norm(value)
        for key in ("name", "fullName", "shortName", "title", "label", "webName", "text", "code"):
            found = _pick(inner, key)
            if isinstance(found, str) and found.strip():
                return found.strip()
        return ""
    if isinstance(value, Sequence):
        for item in value:
            text = _text(item)
            if text:
                return text
    return ""


def _slug(label: str, fallback: str = "outcome") -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_")
    return slug or fallback


# --------------------------------------------------------------------------- #
# 节点发现
# --------------------------------------------------------------------------- #
def _looks_like_event(node: Mapping[str, Any]) -> bool:
    data = _norm(node)
    has_id = bool(_pick(data, *_ID_KEYS))
    has_teams = bool(_pick(data, *_COMP_KEYS)) or (
        bool(_pick(data, *_HOME_KEYS)) and bool(_pick(data, *_AWAY_KEYS))
    )
    has_time = bool(_pick(data, *_START_KEYS))
    return has_id and has_teams and has_time


def iter_event_nodes(payload: Any, max_depth: int = 12) -> Iterator[Mapping[str, Any]]:
    """深度优先遍历任意 JSON，产出所有「像赛事」的节点。"""

    def walk(obj: Any, depth: int) -> Iterator[Mapping[str, Any]]:
        if depth > max_depth:
            return
        if isinstance(obj, Mapping):
            if _looks_like_event(obj):
                yield obj
                return
            for value in obj.values():
                yield from walk(value, depth + 1)
        elif isinstance(obj, Sequence) and not isinstance(obj, (str, bytes)):
            for item in obj:
                yield from walk(item, depth + 1)

    seen: set[str] = set()
    for node in walk(payload, 0):
        data = _norm(node)
        identifier = str(_pick(data, *_ID_KEYS) or "")
        if identifier in seen:
            continue
        seen.add(identifier)
        yield node


# --------------------------------------------------------------------------- #
# 字段提取
# --------------------------------------------------------------------------- #
def _team_name(value: Any) -> str:
    if isinstance(value, Mapping):
        data = _norm(value)
        for key in ("name", "fullName", "shortName", "webName", "title"):
            found = _pick(data, key)
            if isinstance(found, str) and found.strip():
                return found.strip()
        return ""
    return _text(value)


def _split_teams(data: Mapping[str, Any]) -> tuple[str, str]:
    competitors = _pick(data, *_COMP_KEYS)
    if isinstance(competitors, Sequence) and not isinstance(competitors, (str, bytes)):
        names = [_team_name(item) for item in competitors]
        qualifiers = []
        for item in competitors:
            if isinstance(item, Mapping):
                inner = _norm(item)
                qualifiers.append(
                    str(_pick(inner, "qualifier", "homeAway", "side", "type") or "").lower()
                )
            else:
                qualifiers.append("")
        home_index = next((i for i, q in enumerate(qualifiers) if "home" in q), None)
        away_index = next((i for i, q in enumerate(qualifiers) if "away" in q), None)
        if home_index is not None and away_index is not None:
            return names[home_index], names[away_index]
        if len(names) >= 2:
            return names[0], names[1]
    home = _team_name(_pick(data, *_HOME_KEYS))
    away = _team_name(_pick(data, *_AWAY_KEYS))
    return home, away


def _league_name(data: Mapping[str, Any]) -> str:
    for key in ("league", "tournament", "competition", "category", "group", "series"):
        value = _pick(data, key)
        if isinstance(value, Mapping):
            text = _text(value)
            if text:
                return text
        elif isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _sport_name(data: Mapping[str, Any]) -> str:
    value = _pick(data, "sport", "sportSlug", "discipline")
    if isinstance(value, Mapping):
        return _text(value) or "football"
    return (str(value).strip() if value else "football")


def _parse_score_text(text: str) -> tuple[int, int] | None:
    match = re.match(r"^\s*(\d{1,3})\s*[:\-–]\s*(\d{1,3})\s*$", text)
    if match:
        return int(match.group(1)), int(match.group(2))
    return None


def _split_score(data: Mapping[str, Any]) -> tuple[int, int] | None:
    for key in ("score", "scores", "result", "fullTimeScore", "finalScore", "scoreString"):
        raw = _pick(data, key)
        if raw is None:
            continue
        if isinstance(raw, str):
            parsed = _parse_score_text(raw)
            if parsed:
                return parsed
        if isinstance(raw, Mapping):
            inner = _norm(raw)
            home = to_decimal(_pick(inner, "home", "homeScore", "homeGoals", "local", "team1"))
            away = to_decimal(_pick(inner, "away", "awayScore", "awayGoals", "visitor", "team2"))
            if home is not None and away is not None:
                return int(home), int(away)
            # {"1": 2, "2": 1} / {"home": {"score": 2}}
            nested_home = _pick(inner, "homeTeam", "team1")
            if isinstance(nested_home, Mapping):
                home = to_decimal(_pick(_norm(nested_home), "score", "value", "goals"))
            nested_away = _pick(inner, "awayTeam", "team2")
            if isinstance(nested_away, Mapping):
                away = to_decimal(_pick(_norm(nested_away), "score", "value", "goals"))
            if home is not None and away is not None:
                return int(home), int(away)
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
            values = []
            for item in raw:
                if isinstance(item, Mapping):
                    inner = _norm(item)
                    value = to_decimal(_pick(inner, "score", "value", "goals", "points"))
                    if value is None:
                        continue
                    qualifier = str(_pick(inner, "qualifier", "side", "type") or "").lower()
                    values.append((qualifier, value))
                else:
                    value = to_decimal(item)
                    if value is not None:
                        values.append(("", value))
            if len(values) >= 2:
                homes = [v for q, v in values if "home" in q]
                aways = [v for q, v in values if "away" in q]
                if homes and aways:
                    return int(homes[0]), int(aways[0])
                return int(values[0][1]), int(values[1][1])
    home = to_decimal(_pick(data, "homeScore", "home_score", "homeGoals"))
    away = to_decimal(_pick(data, "awayScore", "away_score", "awayGoals"))
    if home is not None and away is not None:
        return int(home), int(away)
    return None


_ENDED_WORDS = ("finish", "ended", "final", "closed", "settled", "complete", "resulted", "ended")
_LIVE_WORDS = ("live", "inplay", "in_play", "running", "progress", "started", "playing")
_UPCOMING_WORDS = ("scheduled", "upcoming", "not_started", "notstarted", "prematch", "pre_match", "open", "pending")


def _status_of(data: Mapping[str, Any]) -> tuple[str, bool]:
    raw = _pick(data, "status", "state", "eventStatus", "matchStatus", "phase")
    text = (_text(raw) or str(raw or "")).lower().replace("-", "_")
    if any(word in text for word in _ENDED_WORDS):
        return STATUS_ENDED, False
    if any(word in text for word in _LIVE_WORDS):
        return STATUS_LIVE, True
    if any(word in text for word in _UPCOMING_WORDS):
        return STATUS_UPCOMING, False
    if _pick(data, "live", "isLive", "inPlay"):
        return STATUS_LIVE, True
    return STATUS_UNKNOWN, bool(_pick(data, "live", "isLive", "inPlay"))


def _detect_kind(name: str, labels: Sequence[str]) -> str:
    text = f"{name} {' '.join(labels)}"
    if _BTTS.search(text):
        return KIND_BTTS
    if _TOTALS.search(text):
        return KIND_TOTALS
    if _ONE_X_TWO.search(text):
        return KIND_1X2
    return KIND_OTHER


def _contains_name(label: str, team: str) -> bool:
    team = team.strip().lower()
    if len(team) < 3:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(team)}(?![a-z0-9])", label) is not None


def _map_1x2_key(label: str, index: int, total: int, home: str, away: str) -> str:
    lowered = label.strip().lower()
    if not lowered:
        return ("home", "draw", "away")[index] if total == 3 else ("home", "away")[index]
    # 先判「平局」和中文主客，避免队名里恰好含 home/away 之类的字母
    if re.search(r"\bdraw\b|\btie\b|^x$|平", lowered):
        return "draw"
    if re.search(r"主\s*胜|主队", lowered):
        return "home"
    if re.search(r"客\s*胜|客队", lowered):
        return "away"
    if lowered in ("home", "1"):
        return "home"
    if lowered in ("away", "2"):
        return "away"
    if _contains_name(lowered, home):
        return "home"
    if _contains_name(lowered, away):
        return "away"
    if total == 3:
        return ("home", "draw", "away")[index]
    if total == 2:
        return ("home", "away")[index]
    return _slug(label)


def _outcome_pairs(raw: Any) -> list[tuple[str, Decimal]]:
    """把 outcomes 的几种写法统一成 [(label, odds)]。"""
    pairs: list[tuple[str, Decimal]] = []
    if isinstance(raw, Mapping):
        for label, value in raw.items():
            odds = to_decimal(value)
            if odds is None and isinstance(value, Mapping):
                odds = to_decimal(_pick(_norm(value), "odds", "price", "decimal", "value"))
            if odds is not None:
                pairs.append((str(label), odds))
        return pairs
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        for index, item in enumerate(raw):
            if isinstance(item, Mapping):
                inner = _norm(item)
                label = _text(_pick(inner, "name", "label", "title", "selection", "outcome")) or f"选项{index + 1}"
                odds = to_decimal(_pick(inner, "odds", "price", "decimal", "value", "odd"))
            else:
                label = f"选项{index + 1}"
                odds = to_decimal(item)
            if odds is not None:
                pairs.append((label, odds))
    return pairs


def _build_market(
    name: str,
    line: Decimal | None,
    pairs: Sequence[tuple[str, Decimal]],
    home: str,
    away: str,
) -> Market | None:
    if not pairs:
        return None
    labels = [label for label, _ in pairs]
    kind = _detect_kind(name, labels)
    if kind == KIND_OTHER and not name and len(pairs) == 3:
        kind = KIND_1X2
    if kind == KIND_1X2:
        outcomes = tuple(
            Outcome(
                key=_map_1x2_key(label, index, len(pairs), home, away),
                label=label,
                odds=odds_value(odds),
            )
            for index, (label, odds) in enumerate(pairs)
        )
        return Market(key=KIND_1X2, name=name or "全场独赢", kind=KIND_1X2, outcomes=outcomes)
    if kind == KIND_TOTALS:
        if line is None:
            match = _LINE.search(name)
            line = to_decimal(match.group(1)) if match else None
        outcomes = tuple(
            Outcome(
                key=(
                    "over"
                    if re.search(r"over|大|以上|高于", label, re.IGNORECASE)
                    else "under"
                    if re.search(r"under|小|以下|低于", label, re.IGNORECASE)
                    else _slug(label)
                ),
                label=label,
                odds=odds_value(odds),
            )
            for label, odds in pairs
        )
        key = f"totals_{line}" if line is not None else "totals"
        return Market(key=key, name=name or "总进球", kind=KIND_TOTALS, outcomes=outcomes, line=line)
    if kind == KIND_BTTS:
        outcomes = tuple(
            Outcome(
                key=(
                    "yes"
                    if re.search(r"yes|是|进|both", label, re.IGNORECASE)
                    else "no"
                    if re.search(r"no|否|不", label, re.IGNORECASE)
                    else _slug(label)
                ),
                label=label,
                odds=odds_value(odds),
            )
            for label, odds in pairs
        )
        return Market(key=KIND_BTTS, name=name or "双方进球", kind=KIND_BTTS, outcomes=outcomes)
    outcomes = tuple(
        Outcome(key=_slug(label, f"outcome_{index}"), label=label, odds=odds_value(odds))
        for index, (label, odds) in enumerate(pairs)
    )
    return Market(key=_slug(name, KIND_OTHER), name=name or "其它盘口", kind=KIND_OTHER, outcomes=outcomes)


def _extract_markets(data: Mapping[str, Any], home: str, away: str) -> tuple[Market, ...]:
    raw = _pick(data, *_MARKET_KEYS)
    markets: list[Market] = []
    if isinstance(raw, Mapping):
        inner = _norm(raw)
        # stakeapi 风格的扁平赔率：{"home": 1.9, "draw": 3.4, "away": 3.9}
        flat = _outcome_pairs(
            {key: value for key, value in inner.items() if to_decimal(value) is not None}
        )
        if flat:
            market = _build_market("全场独赢", None, flat, home, away)
            if market:
                markets.append(market)
        else:
            for name, value in raw.items():
                pairs = _outcome_pairs(value)
                market = _build_market(str(name), None, pairs, home, away)
                if market:
                    markets.append(market)
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            inner = _norm(item)
            name = _text(_pick(inner, "name", "title", "label", "type", "marketName", "key")) or "盘口"
            line = to_decimal(_pick(inner, "line", "handicap", "total", "points", "param"))
            pairs = _outcome_pairs(
                _pick(inner, "outcomes", "selections", "prices", "runners", "options", "odds")
            )
            market = _build_market(name, line, pairs, home, away)
            if market:
                markets.append(market)

    deduped: dict[str, Market] = {}
    for market in markets:
        deduped.setdefault(market.key, market)
    return tuple(deduped.values())


def parse_event(node: Mapping[str, Any]) -> Event | None:
    data = _norm(node)
    event_id = _pick(data, *_ID_KEYS)
    home, away = _split_teams(data)
    start_time = parse_datetime(_pick(data, *_START_KEYS))
    if not event_id or not home or not away or start_time is None:
        return None
    status, live = _status_of(data)
    score = _split_score(data)
    if score and status == STATUS_UNKNOWN:
        status = STATUS_ENDED
    return Event(
        id=str(event_id),
        sport=_sport_name(data),
        league=_league_name(data),
        home=home,
        away=away,
        start_time=start_time,
        status=status,
        live=live,
        markets=_extract_markets(data, home, away),
        score=score,
    )


def parse_events(payload: Any) -> list[Event]:
    events = []
    for node in iter_event_nodes(payload):
        event = parse_event(node)
        if event:
            events.append(event)
    return events


# --------------------------------------------------------------------------- #
# 赛果
# --------------------------------------------------------------------------- #
def _manual_result(item: Mapping[str, Any]) -> MatchResult | None:
    data = _norm(item)
    event_id = _pick(data, "event_id", "eventId", "id")
    home = to_decimal(_pick(data, "home_score", "homeScore", "home"))
    away = to_decimal(_pick(data, "away_score", "awayScore", "away"))
    if not event_id or home is None or away is None:
        return None
    return MatchResult(
        event_id=str(event_id),
        home_score=int(home),
        away_score=int(away),
        recorded_at=parse_datetime(_pick(data, "recorded_at")) or utcnow(),
    )


def parse_results(payload: Any) -> list[MatchResult]:
    """支持两种输入：赛事节点（取已结束 + 比分），或手工整理的赛果列表。"""
    if isinstance(payload, Mapping) and "results" in payload:
        payload = payload["results"]
    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)):
        manual = [_manual_result(item) for item in payload if isinstance(item, Mapping)]
        if manual and all(manual):
            return [item for item in manual if item]

    results: list[MatchResult] = []
    for node in iter_event_nodes(payload):
        event = parse_event(node)
        if event and event.score is not None and event.status == STATUS_ENDED:
            results.append(
                MatchResult(
                    event_id=event.id,
                    home_score=event.score[0],
                    away_score=event.score[1],
                    recorded_at=utcnow(),
                )
            )
    return results


def events_to_results(events: Iterable[Event]) -> list[MatchResult]:
    return [
        MatchResult(
            event_id=event.id,
            home_score=event.score[0],
            away_score=event.score[1],
            recorded_at=utcnow(),
        )
        for event in events
        if event.score is not None and event.status == STATUS_ENDED
    ]


def load_capture(path: str) -> list[Event]:
    """从 DevTools 保存的响应 JSON 里导入赛事（抓不到接口时的兜底）。"""
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    return parse_events(payload)
