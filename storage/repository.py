"""SQLite 持久化层。

表结构刻意保持简单：事件整条以 JSON 存 payload（便于兼容不同数据源的字段），
同时把常用字段冗余成列，用于排序和过滤。金额/赔率一律以 TEXT 存字符串，
读取时再转回 Decimal，避免浮点误差。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from decimal import Decimal
from pathlib import Path
from typing import Iterable

from models import (
    STATUS_ENDED,
    Bet,
    Event,
    MatchResult,
    as_utc,
    money,
    odds_value,
    parse_datetime,
    utcnow,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id         TEXT PRIMARY KEY,
    sport      TEXT NOT NULL DEFAULT '',
    league     TEXT NOT NULL DEFAULT '',
    home       TEXT NOT NULL DEFAULT '',
    away       TEXT NOT NULL DEFAULT '',
    start_time TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'unknown',
    live       INTEGER NOT NULL DEFAULT 0,
    source     TEXT NOT NULL DEFAULT 'stake',
    payload    TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS odds_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    TEXT NOT NULL,
    market_key  TEXT NOT NULL,
    outcome_key TEXT NOT NULL,
    odds        TEXT NOT NULL,
    captured_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bets (
    id            TEXT PRIMARY KEY,
    event_id      TEXT NOT NULL,
    event_label   TEXT NOT NULL DEFAULT '',
    market_key    TEXT NOT NULL DEFAULT '',
    market_name   TEXT NOT NULL DEFAULT '',
    outcome_key   TEXT NOT NULL DEFAULT '',
    outcome_label TEXT NOT NULL DEFAULT '',
    odds          TEXT NOT NULL,
    stake         TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',
    payout        TEXT NOT NULL DEFAULT '0',
    placed_at     TEXT NOT NULL,
    settled_at    TEXT,
    note          TEXT NOT NULL DEFAULT '',
    source        TEXT NOT NULL DEFAULT 'stake'
);

CREATE TABLE IF NOT EXISTS results (
    event_id    TEXT PRIMARY KEY,
    home_score  INTEGER NOT NULL,
    away_score  INTEGER NOT NULL,
    status      TEXT NOT NULL DEFAULT 'ended',
    recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

#: 索引要等 source 列补齐后再建（老库可能没有该列）
SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_events_start ON events (start_time);
CREATE INDEX IF NOT EXISTS idx_events_source ON events (source);
CREATE INDEX IF NOT EXISTS idx_odds_lookup
    ON odds_snapshots (event_id, market_key, outcome_key);
CREATE INDEX IF NOT EXISTS idx_bets_status ON bets (status);
CREATE INDEX IF NOT EXISTS idx_bets_source ON bets (source);
"""


class Repository:
    """线程安全的 SQLite 封装（抓取线程与 UI 线程会共用同一个实例）。"""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if self.path.parent and str(self.path.parent) not in ("", "."):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._migrate()
            self._conn.executescript(SCHEMA_INDEXES)
            self._conn.commit()

    def _migrate(self) -> None:
        """给老数据库补 source 列，并按 id 前缀回填历史演示数据。"""
        for table in ("events", "bets"):
            columns = {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")}
            if "source" not in columns:
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN source TEXT NOT NULL DEFAULT 'stake'"
                )
        # 老数据没有来源标记：演示数据的 id / event_id 都是 mock- 前缀
        self._conn.execute(
            "UPDATE events SET source='mock' WHERE source='stake' AND id LIKE 'mock-%'"
        )
        self._conn.execute(
            "UPDATE bets SET source='mock' WHERE source='stake' AND event_id LIKE 'mock-%'"
        )
        self._conn.commit()

    # -- 生命周期 -------------------------------------------------------- #
    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "Repository":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- 比赛 ------------------------------------------------------------ #
    def upsert_events(self, events: Iterable[Event]) -> int:
        events = list(events)
        if not events:
            return 0
        # 已有赛果的比赛，不允许被新抓取的「进行中 / 未开始」状态覆盖回去
        with self._lock:
            result_rows = self._conn.execute(
                "SELECT event_id, home_score, away_score FROM results"
            ).fetchall()
        results = {
            row["event_id"]: (int(row["home_score"]), int(row["away_score"]))
            for row in result_rows
        }
        rows = []
        now = utcnow().isoformat()
        for event in events:
            payload = event.to_dict()
            if event.id in results:
                payload["status"] = STATUS_ENDED
                payload["live"] = False
                payload["score"] = list(results[event.id])
            rows.append(
                (
                    event.id,
                    event.sport,
                    event.league,
                    event.home,
                    event.away,
                    as_utc(event.start_time).isoformat(),
                    payload["status"],
                    1 if payload["live"] else 0,
                    event.source or "stake",
                    json.dumps(payload, ensure_ascii=False),
                    now,
                )
            )
        if not rows:
            return 0
        with self._lock:
            self._conn.executemany(
                """
                INSERT INTO events
                    (id, sport, league, home, away, start_time, status, live, source, payload, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    sport=excluded.sport,
                    league=excluded.league,
                    home=excluded.home,
                    away=excluded.away,
                    start_time=excluded.start_time,
                    status=excluded.status,
                    live=excluded.live,
                    source=excluded.source,
                    payload=excluded.payload,
                    updated_at=excluded.updated_at
                """,
                rows,
            )
            self._conn.commit()
        return len(rows)

    def get_event(self, event_id: str) -> Event | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM events WHERE id = ?", (event_id,)
            ).fetchone()
        return Event.from_dict(json.loads(row["payload"])) if row else None

    def list_events(self, only_open: bool = False, source: str | None = None) -> list[Event]:
        sql = "SELECT payload FROM events"
        clauses = []
        params: list = []
        if only_open:
            clauses.append("status IN ('upcoming', 'live')")
        if source:
            clauses.append("source = ?")
            params.append(source)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY start_time ASC"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [Event.from_dict(json.loads(row["payload"])) for row in rows]

    # -- 赔率快照 -------------------------------------------------------- #
    def record_odds_snapshot(self, events: Iterable[Event]) -> int:
        """只为「发生变化」的赔率写快照，避免每次刷新都塞长表。"""
        events = list(events)
        if not events:
            return 0
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT s.event_id, s.market_key, s.outcome_key, s.odds
                FROM odds_snapshots s
                JOIN (
                    SELECT event_id, market_key, outcome_key, MAX(id) AS max_id
                    FROM odds_snapshots
                    GROUP BY event_id, market_key, outcome_key
                ) latest ON latest.max_id = s.id
                """
            ).fetchall()
        latest = {
            (row["event_id"], row["market_key"], row["outcome_key"]): row["odds"]
            for row in rows
        }
        now = utcnow().isoformat()
        inserts: list[tuple] = []
        for event in events:
            for market in event.markets:
                for outcome in market.outcomes:
                    key = (event.id, market.key, outcome.key)
                    value = str(odds_value(outcome.odds))
                    if latest.get(key) == value:
                        continue
                    inserts.append((event.id, market.key, outcome.key, value, now))
        if not inserts:
            return 0
        with self._lock:
            self._conn.executemany(
                """
                INSERT INTO odds_snapshots (event_id, market_key, outcome_key, odds, captured_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                inserts,
            )
            self._conn.commit()
        return len(inserts)

    def odds_history(self, event_id: str, market_key: str, outcome_key: str) -> list[tuple]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT odds, captured_at FROM odds_snapshots
                WHERE event_id = ? AND market_key = ? AND outcome_key = ?
                ORDER BY id ASC
                """,
                (event_id, market_key, outcome_key),
            ).fetchall()
        return [(Decimal(row["odds"]), row["captured_at"]) for row in rows]

    # -- 赛果 ------------------------------------------------------------ #
    def upsert_result(self, result: MatchResult) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO results (event_id, home_score, away_score, status, recorded_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    home_score=excluded.home_score,
                    away_score=excluded.away_score,
                    status=excluded.status,
                    recorded_at=excluded.recorded_at
                """,
                (
                    result.event_id,
                    int(result.home_score),
                    int(result.away_score),
                    result.status,
                    as_utc(result.recorded_at).isoformat(),
                ),
            )
            self._conn.commit()

    def get_result(self, event_id: str) -> MatchResult | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM results WHERE event_id = ?", (event_id,)
            ).fetchone()
        return self._row_to_result(row) if row else None

    def list_results(self) -> list[MatchResult]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM results ORDER BY recorded_at").fetchall()
        return [self._row_to_result(row) for row in rows]

    @staticmethod
    def _row_to_result(row: sqlite3.Row) -> MatchResult:
        return MatchResult(
            event_id=row["event_id"],
            home_score=row["home_score"],
            away_score=row["away_score"],
            status=row["status"],
            recorded_at=parse_datetime(row["recorded_at"]) or utcnow(),
        )

    # -- 注单 ------------------------------------------------------------ #
    def add_bet(self, bet: Bet) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO bets
                    (id, event_id, event_label, market_key, market_name, outcome_key,
                     outcome_label, odds, stake, status, payout, placed_at, settled_at, note, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._bet_params(bet),
            )
            self._conn.commit()

    def update_bet(self, bet: Bet) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE bets SET
                    event_id=?, event_label=?, market_key=?, market_name=?,
                    outcome_key=?, outcome_label=?, odds=?, stake=?, status=?,
                    payout=?, placed_at=?, settled_at=?, note=?, source=?
                WHERE id=?
                """,
                (
                    bet.event_id,
                    bet.event_label,
                    bet.market_key,
                    bet.market_name,
                    bet.outcome_key,
                    bet.outcome_label,
                    str(bet.odds),
                    str(bet.stake),
                    bet.status,
                    str(bet.payout),
                    as_utc(bet.placed_at).isoformat(),
                    as_utc(bet.settled_at).isoformat() if bet.settled_at else None,
                    bet.note,
                    bet.source or "stake",
                    bet.id,
                ),
            )
            self._conn.commit()

    @staticmethod
    def _bet_params(bet: Bet) -> tuple:
        return (
            bet.id,
            bet.event_id,
            bet.event_label,
            bet.market_key,
            bet.market_name,
            bet.outcome_key,
            bet.outcome_label,
            str(bet.odds),
            str(bet.stake),
            bet.status,
            str(bet.payout),
            as_utc(bet.placed_at).isoformat(),
            as_utc(bet.settled_at).isoformat() if bet.settled_at else None,
            bet.note,
            bet.source or "stake",
        )

    def list_bets(
        self,
        status: str | None = None,
        limit: int | None = None,
        source: str | None = None,
    ) -> list[Bet]:
        sql = "SELECT * FROM bets"
        clauses = []
        params: list = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if source:
            clauses.append("source = ?")
            params.append(source)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY placed_at DESC, rowid DESC"
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_bet(row) for row in rows]

    def pending_bets(self) -> list[Bet]:
        return self.list_bets(status="pending")

    def bets_for_event(self, event_id: str) -> list[Bet]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM bets WHERE event_id = ? ORDER BY placed_at", (event_id,)
            ).fetchall()
        return [self._row_to_bet(row) for row in rows]

    def has_open_bet(self, event_id: str, market_key: str, outcome_key: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT 1 FROM bets
                WHERE event_id=? AND market_key=? AND outcome_key=? AND status='pending'
                LIMIT 1
                """,
                (event_id, market_key, outcome_key),
            ).fetchone()
        return row is not None

    @staticmethod
    def _row_to_bet(row: sqlite3.Row) -> Bet:
        settled_at = parse_datetime(row["settled_at"]) if row["settled_at"] else None
        return Bet(
            id=row["id"],
            event_id=row["event_id"],
            event_label=row["event_label"],
            market_key=row["market_key"],
            market_name=row["market_name"],
            outcome_key=row["outcome_key"],
            outcome_label=row["outcome_label"],
            odds=odds_value(row["odds"]),
            stake=money(row["stake"]),
            placed_at=parse_datetime(row["placed_at"]) or utcnow(),
            status=row["status"],
            payout=money(row["payout"]),
            settled_at=settled_at,
            note=row["note"],
            source=row["source"] if "source" in row.keys() else "stake",
        )

    # -- 元数据 ---------------------------------------------------------- #
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )
            self._conn.commit()

    def reset(
        self,
        *,
        drop_bets: bool = True,
        drop_results: bool = False,
        drop_meta: bool = True,
    ) -> None:
        """重置模拟账户（默认保留比赛与赛果缓存）。"""
        with self._lock:
            if drop_bets:
                self._conn.execute("DELETE FROM bets")
            if drop_results:
                self._conn.execute("DELETE FROM results")
            if drop_meta:
                self._conn.execute("DELETE FROM meta")
            self._conn.commit()

    def counts(self) -> dict[str, int]:
        with self._lock:
            result = {}
            for table in ("events", "odds_snapshots", "bets", "results"):
                row = self._conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
                result[table] = int(row["n"])
        return result

    def sources(self) -> dict[str, int]:
        """按来源统计比赛数，例如 {'stake': 47, 'mock': 10}。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT source, COUNT(*) AS n FROM events GROUP BY source"
            ).fetchall()
        return {str(row["source"]): int(row["n"]) for row in rows}

    def purge_source(self, source: str) -> dict[str, int]:
        """删掉某个来源的全部数据：比赛、赔率快照、赛果、注单。

        用于「切到线上后清理离线演示数据」。删除注单时余额会按公式重算，
        演示账户的盈亏不会残留到真实数据里。
        """
        removed = {"events": 0, "bets": 0, "snapshots": 0, "results": 0}
        with self._lock:
            removed["snapshots"] = self._conn.execute(
                "DELETE FROM odds_snapshots WHERE event_id IN "
                "(SELECT id FROM events WHERE source = ?)",
                (source,),
            ).rowcount
            removed["results"] = self._conn.execute(
                "DELETE FROM results WHERE event_id IN (SELECT id FROM events WHERE source = ?)",
                (source,),
            ).rowcount
            removed["bets"] = self._conn.execute(
                "DELETE FROM bets WHERE source = ?", (source,)
            ).rowcount
            removed["events"] = self._conn.execute(
                "DELETE FROM events WHERE source = ?", (source,)
            ).rowcount
            self._conn.commit()
        return removed
