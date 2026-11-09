"""命令行入口：不启动图形界面也能抓取、下注、结算、统计。

用法示例::

    python cli.py probe                 # 诊断凭据 / IP 是否可用
    python cli.py fetch                 # 抓取足球赛事与赔率
    python cli.py events                # 查看已缓存的比赛
    python cli.py bet --event mock-epl-001 --outcome home --stake 50
    python cli.py settle --event mock-epl-001 --score 2-1
    python cli.py stats                 # 模拟账户盈亏
    python cli.py demo                  # 离线端到端演示（不联网）
"""

from __future__ import annotations

import argparse
import json
import logging
import unicodedata
from dataclasses import replace
from pathlib import Path
from typing import Sequence

from betting.engine import BetError, SimulationEngine
from config import DATA_SOURCES, Settings, load_settings
from data.providers import MockProvider, build_provider
from data.stake_client import StakeClient, StakeError
from data.stake_parse import load_capture
from models import BET_PENDING, Event, money
from storage.repository import Repository

log = logging.getLogger("groundbet.cli")


# --------------------------------------------------------------------------- #
# 公共辅助
# --------------------------------------------------------------------------- #
def _open(args) -> tuple[Settings, Repository, SimulationEngine]:
    settings = load_settings()
    overrides: dict = {}
    source = getattr(args, "source", None)
    if source:
        overrides["data_source"] = source
    db = getattr(args, "db", None)
    if db:
        overrides["db_path"] = Path(db)
    if overrides:
        settings = replace(settings, **overrides)
    repo = Repository(settings.db_path)
    engine = SimulationEngine(repo, settings.initial_balance)
    return settings, repo, engine


def _fmt_odds(odds) -> str:
    return "-" if odds is None else f"{odds:.2f}"


def _display_width(text: str) -> int:
    """中文字符占两个终端列，按显示宽度计算。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in str(text))


def _pad(text, width: int, align: str = "<") -> str:
    text = str(text)
    space = max(0, width - _display_width(text))
    return " " * space + text if align == ">" else text + " " * space


def _truncate(text: str, width: int) -> str:
    text = str(text)
    if _display_width(text) <= width:
        return text
    kept: list[str] = []
    used = 0
    for char in text:
        char_width = 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        if used + char_width > width - 1:
            break
        kept.append(char)
        used += char_width
    return "".join(kept) + "…"


def _print_rows(spec: Sequence[tuple[str, int, str]], rows: Sequence[Sequence[str]]) -> None:
    header = " ".join(_pad(name, width, align) for name, width, align in spec)
    print(header)
    print("-" * _display_width(header))
    for row in rows:
        print(" ".join(_pad(_truncate(value, width), width, align) for value, (_, width, align) in zip(row, spec)))


def _print_events(events: Sequence[Event], limit: int = 40) -> None:
    if not events:
        print("（没有比赛数据）")
        return
    spec = [
        ("ID", 15, "<"),
        ("开赛", 12, "<"),
        ("联赛", 22, "<"),
        ("比赛", 34, "<"),
        ("状态", 8, "<"),
        ("主胜", 7, ">"),
        ("平局", 7, ">"),
        ("客胜", 7, ">"),
        ("比分", 8, ">"),
    ]
    rows = []
    for event in events[:limit]:
        main = event.market("1x2")
        home = main.outcome("home") if main else None
        draw = main.outcome("draw") if main else None
        away = main.outcome("away") if main else None
        rows.append(
            [
                event.id,
                event.start_time.astimezone().strftime("%m-%d %H:%M"),
                event.league,
                event.title,
                event.status_label,
                _fmt_odds(home.odds if home else None),
                _fmt_odds(draw.odds if draw else None),
                _fmt_odds(away.odds if away else None),
                f"{event.score[0]}-{event.score[1]}" if event.score else "-",
            ]
        )
    _print_rows(spec, rows)
    if len(events) > limit:
        print(f"…… 共 {len(events)} 场，只显示前 {limit} 场")


def _find_event(repo: Repository, token: str) -> Event:
    events = repo.list_events()
    for event in events:
        if event.id == token:
            return event
    lowered = token.lower()
    matches = [event for event in events if lowered in event.id.lower() or lowered in event.title.lower()]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise SystemExit(f"找不到比赛：{token}（先运行 fetch 或 events 看看有哪些）")
    print(f"「{token}」匹配到多场比赛，请用更精确的 ID：")
    for event in matches[:10]:
        print(f"  {event.id}  {event.title}")
    raise SystemExit(1)


def _parse_score(text: str) -> tuple[int, int]:
    parts = text.replace(":", "-").split("-")
    if len(parts) != 2:
        raise SystemExit("比分格式应为 2-1 或 2:1")
    try:
        return int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise SystemExit("比分必须是整数") from exc


def _provider_label(provider) -> str:
    return str(getattr(provider, "label", getattr(provider, "name", "未知")))


def _warn_if_degraded(provider) -> None:
    warning = getattr(provider, "last_warning", "")
    if warning:
        print(f"⚠ 真实数据源不可用，已使用「{_provider_label(provider)}」：{warning}")
        print()


# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #
def cmd_probe(args) -> int:
    settings = load_settings()
    source = getattr(args, "source", None) or settings.data_source
    if source == "mock":
        print("当前配置为离线演示数据源，无需联网。")
        return 0
    client = StakeClient(replace(settings, data_source="stake"))
    print(f"目标：{settings.api_base}/_api/graphql")
    print(f"UA：{settings.user_agent[:60]}…")
    print(f"代理：{settings.proxy or '（未配置）'}")
    report = client.probe()
    print(f"结果：{'✅ 可用' if report.ok else '❌ 失败'}（HTTP {report.status}）")
    print(f"说明：{report.detail}")
    if report.sample:
        print(f"响应片段：{report.sample}")
    if report.ok and args.introspect:
        fields = client.introspect_sports()
        if not fields:
            print("Stake 已关闭 GraphQL Introspection，无法列出字段；")
            print("需要时请把浏览器抓到的查询写入 queries/events.graphql 覆盖。")
        else:
            sports = [
                name
                for name in fields
                if any(word in name.lower() for word in ("sport", "event", "match", "fixture"))
            ]
            print("schema 中与体育相关的查询字段：")
            for name in sports or fields:
                print(f"  - {name}")
    return 0 if report.ok else 2


def cmd_fetch(args) -> int:
    settings, repo, engine = _open(args)
    provider = build_provider(settings)
    try:
        events = provider.fetch_events()
    except StakeError as exc:
        print(f"抓取失败：{exc}")
        return 2
    _warn_if_degraded(provider)
    repo.upsert_events(events)
    changed = repo.record_odds_snapshot(events)
    print(f"抓取 {len(events)} 场比赛（数据源：{_provider_label(provider)}），赔率变化 {changed} 条")
    used_source = getattr(provider, "active", provider.name)
    if used_source == "stake":
        cleaned = engine.clean_demo_data()
        if cleaned["events"] or cleaned["bets"]:
            print(
                f"已清理离线演示数据：{cleaned['events']} 场比赛、"
                f"{cleaned['bets']} 张注单、{cleaned['snapshots']} 条赔率快照"
            )
    _print_events(events)
    if args.dump:
        Path(args.dump).write_text(
            json.dumps([event.to_dict() for event in events], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"原始解析结果已写入 {args.dump}")
    return 0


def cmd_events(args) -> int:
    _settings, repo, _engine = _open(args)
    events = repo.list_events(only_open=not args.all)
    _print_events(events)
    return 0


def cmd_results(args) -> int:
    settings, repo, engine = _open(args)
    event_ids = sorted({bet.event_id for bet in repo.pending_bets()})
    if not event_ids:
        print("没有待结算的注单，无需拉取赛果")
        return 0
    provider = build_provider(settings)
    try:
        results = provider.fetch_results(event_ids)
    except StakeError as exc:
        print(f"拉取赛果失败：{exc}")
        return 2
    _warn_if_degraded(provider)
    for result in results:
        repo.upsert_result(result)
    settled = engine.settle_pending()
    print(f"更新 {len(results)} 场赛果，结算 {len(settled)} 张注单")
    for bet in settled:
        print(f"  {bet.event_label}  {bet.outcome_label}  本金 {bet.stake} → {bet.status_label}  盈亏 {bet.profit:+.2f}")
    print(f"当前余额：{engine.balance}")
    return 0


def cmd_bet(args) -> int:
    _settings, repo, engine = _open(args)
    event = _find_event(repo, args.event)
    market = event.market(args.market) if args.market else event.main_market()
    if market is None:
        print(f"比赛没有可下注的盘口：{args.market or '（默认 1X2）'}")
        print("可用盘口：" + "、".join(f"{m.key}({m.name})" for m in event.tradable_markets()))
        return 2
    outcome = market.outcome(args.outcome)
    if outcome is None:
        lowered = args.outcome.lower()
        outcome = next(
            (item for item in market.outcomes if lowered in item.label.lower() or lowered in item.key.lower()),
            None,
        )
    if outcome is None:
        print("选项不存在，可选：" + "、".join(f"{o.key}({o.label} @ {o.odds})" for o in market.outcomes))
        return 2
    try:
        bet = engine.place_bet(event, market.key, outcome.key, money(args.stake))
    except BetError as exc:
        print(f"下注被拒绝：{exc}")
        return 2
    print(f"已下注 {bet.id}")
    print(f"  {bet.event_label}")
    print(f"  {bet.market_name} / {bet.outcome_label} @ {bet.odds}")
    print(f"  本金 {bet.stake}　潜在回报 {bet.potential_return}")
    print(f"  余额 {engine.balance}")
    return 0


def cmd_settle(args) -> int:
    _settings, _repo, engine = _open(args)
    if args.event and args.score:
        event_id = _find_event(engine.repo, args.event).id
        home, away = _parse_score(args.score)
        settled = engine.settle_event(event_id, home, away)
        print(f"已录入比分 {home}-{away}，结算 {len(settled)} 张注单")
    else:
        settled = engine.settle_pending()
        print(f"按已有赛果结算 {len(settled)} 张注单")
    for bet in settled:
        print(f"  {bet.event_label}  {bet.outcome_label}  {bet.stake} → {bet.status_label}  {bet.profit:+.2f}")
    print(f"当前余额：{engine.balance}")
    return 0


def cmd_bets(args) -> int:
    _settings, repo, engine = _open(args)
    bets = repo.list_bets(status=None if args.all else BET_PENDING, limit=args.limit)
    if not bets:
        print("（没有注单）")
        return 0
    spec = [
        ("ID", 12, "<"),
        ("比赛", 34, "<"),
        ("选项", 20, "<"),
        ("赔率", 6, ">"),
        ("本金", 8, ">"),
        ("状态", 8, "<"),
        ("盈亏", 9, ">"),
    ]
    rows = [
        [
            bet.id,
            bet.event_label,
            bet.outcome_label,
            f"{bet.odds:.2f}",
            f"{bet.stake:.2f}",
            bet.status_label,
            f"{bet.profit:+.2f}" if bet.status != BET_PENDING else "-",
        ]
        for bet in bets
    ]
    _print_rows(spec, rows)
    print()
    print(f"余额 {engine.balance}　待结算本金 {engine.stats().pending_stake}")
    return 0


def cmd_stats(args) -> int:
    _settings, repo, engine = _open(args)
    print("\n".join(engine.summary_lines()))
    print(f"数据库：{repo.path}")
    return 0


def cmd_capture(args) -> int:
    _settings, repo, _engine = _open(args)
    try:
        events = load_capture(args.file)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"读取失败：{exc}")
        return 2
    if not events:
        print("这个 JSON 里没有解析出赛事，可能不是 GraphQL 响应。")
        return 2
    repo.upsert_events(events)
    repo.record_odds_snapshot(events)
    print(f"从 {args.file} 导入 {len(events)} 场比赛")
    _print_events(events)
    return 0


def cmd_reset(args) -> int:
    _settings, repo, engine = _open(args)
    engine.reset()
    if args.results:
        repo.reset(drop_bets=False, drop_results=True, drop_meta=False)
        print("已清空赛果缓存")
    print("模拟账户已重置为初始资金", engine.initial_balance)
    return 0


def cmd_clean_demo(args) -> int:
    _settings, repo, engine = _open(args)
    before = repo.sources()
    if "mock" not in before:
        print("数据库里没有离线演示数据，无需清理")
        return 0
    removed = engine.clean_demo_data()
    print(
        f"已删除离线演示数据：{removed['events']} 场比赛、{removed['bets']} 张注单、"
        f"{removed['snapshots']} 条赔率快照、{removed['results']} 条赛果"
    )
    print(f"剩余来源：{repo.sources() or '（空）'}")
    return 0


def cmd_demo(args) -> int:
    """离线端到端演示：抓取 → 下注 → 结算 → 统计，全程不联网。"""
    settings = replace(
        load_settings(),
        data_source="mock",
        db_path=Path(args.db or "data/demo.db"),
    )
    repo = Repository(settings.db_path)
    engine = SimulationEngine(repo, settings.initial_balance)
    engine.reset()

    provider = MockProvider(settings.mock_dir / "mock_events.json")
    events = provider.fetch_events()
    repo.upsert_events(events)
    repo.record_odds_snapshot(events)

    print("=" * 70)
    print("离线演示（所有赔率来自 fixtures/mock_events.json，不涉及真实资金）")
    print("=" * 70)
    print(f"1. 加载 {len(events)} 场比赛，数据库：{repo.path}")

    open_events = [event for event in events if event.is_open()]
    preferences = [("1x2", "home", 50), ("totals", "over", 30), ("btts", "yes", 20)]
    placed = 0
    chosen = []
    for event, (kind, outcome_key, stake) in zip(open_events, preferences):
        market = next(
            (
                item
                for item in event.markets
                if (item.kind == kind or (kind == "totals" and item.key.startswith("totals")))
                and item.outcome(outcome_key) is not None
            ),
            None,
        )
        if market is None:
            continue
        try:
            bet = engine.place_bet(event, market.key, outcome_key, stake)
        except BetError as exc:
            print(f"   跳过 {event.title}：{exc}")
            continue
        placed += 1
        chosen.append(event)
        print(
            f"2.{placed} 下注 {event.title} · {market.name} · {bet.outcome_label} "
            f"@{bet.odds} 本金 {bet.stake}"
        )
    print(f"   共下注 {placed} 张，余额 {engine.balance}")

    scores = [(2, 1), (0, 0), (3, 2)]
    settled_total = 0
    for event, score in zip(chosen, scores):
        settled = engine.settle_event(event.id, score[0], score[1])
        settled_total += len(settled)
        print(f"3. 录入赛果 {event.title} {score[0]}-{score[1]}，结算 {len(settled)} 张")
    print(f"   共结算 {settled_total} 张")
    print()
    print("\n".join(engine.summary_lines()))
    print()
    print("提示：python main.py 打开图形界面，可继续在界面上下注 / 录入赛果。")
    repo.close()
    return 0


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="GroundBet · Stake 足球赔率抓取与模拟下注（CLI）")
    parser.add_argument("--log-level", default="WARNING")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, func, help_text: str) -> argparse.ArgumentParser:
        item = sub.add_parser(name, help=help_text)
        item.set_defaults(func=func)
        item.add_argument("--source", choices=DATA_SOURCES, help="覆盖数据源配置")
        item.add_argument("--db", help="覆盖数据库路径")
        return item

    probe = add("probe", cmd_probe, "诊断 access token / cf_clearance / IP 是否可用")
    probe.add_argument("--introspect", action="store_true", help="顺便列出 schema 里的体育查询字段")

    fetch = add("fetch", cmd_fetch, "抓取足球赛事与赔率")
    fetch.add_argument("--dump", help="把解析后的赛事写入 JSON 文件")

    events = add("events", cmd_events, "查看已缓存的比赛")
    events.add_argument("--all", action="store_true", help="包含已结束的比赛")

    add("results", cmd_results, "拉取赛果并结算注单")

    bet = add("bet", cmd_bet, "模拟下注")
    bet.add_argument("--event", required=True, help="比赛 ID 或标题关键字")
    bet.add_argument("--market", help="盘口 key，默认取主盘口")
    bet.add_argument("--outcome", required=True, help="选项 key 或名称，例如 home / over")
    bet.add_argument("--stake", type=float, required=True, help="下注金额")

    settle = add("settle", cmd_settle, "录入比分或按已有赛果结算")
    settle.add_argument("--event", help="比赛 ID 或标题关键字")
    settle.add_argument("--score", help="比分，例如 2-1")

    bets = add("bets", cmd_bets, "查看注单")
    bets.add_argument("--all", action="store_true", help="包含已结算的注单")
    bets.add_argument("--limit", type=int, default=50)

    add("stats", cmd_stats, "模拟账户统计")

    capture = add("capture", cmd_capture, "导入浏览器 DevTools 保存的响应 JSON")
    capture.add_argument("file", help="JSON 文件路径")

    reset = add("reset", cmd_reset, "重置模拟账户")
    reset.add_argument("--results", action="store_true", help="同时清空赛果缓存")

    add("demo", cmd_demo, "离线端到端演示")
    add("clean-demo", cmd_clean_demo, "删除数据库里的离线演示数据")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.WARNING),
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\n已中断")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
