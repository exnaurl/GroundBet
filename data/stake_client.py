"""Stake.com 体育数据客户端（真实网络抓取层）。

只做「读取」：拉取赛事、赔率、比分，不会调用任何下注接口。

真实接口（2026-11 实测）：

* ``POST https://stake.com/_api/v1/graphql``
* 赛事：``sport(sportId: <UUID>) { fixtureList(limit:, type:) }``
* 单场比分：``sportFixture(fixtureId: <UUID>) { eventStatus { homeScore awayScore matchStatus } }``

常见失败原因都翻译成了可操作的中文提示：

* 403 + ip-blocked：当前出口 IP 被 Stake 屏蔽（地区限制 / 机房 IP）
* 403 + Just a moment：Cookie / User-Agent 与 cf_clearance 不匹配，或被风控
* 401：access token 失效
* GraphQL errors：schema 变了，用 ``queries/events.graphql`` 覆盖查询
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Sequence

import requests

from config import Settings
from data import stake_graphql, stake_v1
from models import Event, MatchResult

log = logging.getLogger(__name__)

GRAPHQL_PATH = "/_api/v1/graphql"
#: 一次查多场比分时每批的条数，避免单个 GraphQL 文档过长
RESULT_BATCH = 10


class StakeError(RuntimeError):
    """抓取失败的总基类。"""


class StakeAuthError(StakeError):
    """凭据无效 / 人机校验未通过。"""


class StakeBlockedError(StakeError):
    """IP 被屏蔽（地区限制、维护、机房 IP）。"""


class StakeSchemaError(StakeError):
    """接口结构变化，解析不出数据。"""


@dataclass(frozen=True, slots=True)
class ProbeReport:
    ok: bool
    status: int
    detail: str
    cf_ray: str = ""
    blocked: bool = False
    sample: str = ""


class StakeClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._session: requests.Session | None = None
        self.last_payload: Any = None

    # -- 生命周期 -------------------------------------------------------- #
    def __enter__(self) -> "StakeClient":
        self._ensure_session()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def _ensure_session(self) -> requests.Session:
        if self._session is not None:
            return self._session
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": self.settings.user_agent,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Content-Type": "application/json",
                "Origin": self.settings.api_base,
                "Referer": f"{self.settings.api_base}/",
                "X-Language": "en",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
            }
        )
        if self.settings.access_token:
            session.headers["x-access-token"] = self.settings.access_token
        cookie = self.settings.cookie or (
            f"cf_clearance={self.settings.cf_clearance}" if self.settings.cf_clearance else None
        )
        if cookie:
            session.headers["Cookie"] = cookie
        if self.settings.proxy:
            session.proxies.update({"http": self.settings.proxy, "https": self.settings.proxy})
        self._session = session
        return session

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None

    # -- 底层请求 -------------------------------------------------------- #
    def _graphql(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
        operation: str | None = None,
    ) -> dict[str, Any]:
        session = self._ensure_session()
        payload: dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables
        if operation:
            payload["operationName"] = operation
        url = f"{self.settings.api_base}{GRAPHQL_PATH}"
        try:
            response = session.post(url, json=payload, timeout=self.settings.timeout)
        except requests.exceptions.ProxyError as exc:
            raise StakeError(f"代理连接失败（{self.settings.proxy}）：{exc}") from exc
        except requests.exceptions.Timeout as exc:
            raise StakeError(f"请求超时（{self.settings.timeout}s），请检查网络或代理") from exc
        except requests.exceptions.RequestException as exc:
            raise StakeError(f"网络错误：{exc}") from exc

        body = response.text or ""
        cf_ray = response.headers.get("cf-ray", "")
        if response.status_code == 403:
            if "ip-blocked" in body or "Maintenance" in body or "stake-errors" in body:
                raise StakeBlockedError(
                    "Stake 屏蔽了当前出口 IP（地区限制或机房 IP）。"
                    f"请换用住宅网络，或在 .env 里配置 STAKE_PROXY 走代理。（cf-ray {cf_ray}）"
                )
            if "Just a moment" in body or "challenge" in body.lower():
                raise StakeAuthError(
                    f"Cloudflare 拦截了请求（cf-ray {cf_ray}）。常见原因：cf_clearance 已过期、"
                    "与 User-Agent 不一致，或当前出口 IP 被风控。请从浏览器重新复制完整 Cookie 与 UA；"
                    "若仍失败，用 STAKE_PROXY 走一个干净的住宅 IP。"
                )
            raise StakeAuthError(f"HTTP 403（cf-ray {cf_ray}）：{body[:200]}")
        if response.status_code in (401, 419):
            raise StakeAuthError("access token 无效或已过期，请重新获取")
        if response.status_code >= 400:
            raise StakeError(f"HTTP {response.status_code}：{body[:300]}")

        try:
            data = response.json()
        except ValueError as exc:
            raise StakeSchemaError(f"响应不是 JSON（可能被 Cloudflare 拦截）：{body[:200]}") from exc

        self.last_payload = data
        if isinstance(data, dict) and data.get("errors"):
            messages = "; ".join(str(item.get("message", item)) for item in data["errors"])
            raise StakeSchemaError(
                f"接口返回错误：{messages}。若提示未知字段，请把浏览器抓到的查询写入 queries/events.graphql"
            )
        if not isinstance(data, dict):
            raise StakeSchemaError("响应结构异常（不是对象）")
        return data.get("data") or data

    # -- 业务接口 -------------------------------------------------------- #
    def fetch_events(self, sport: str = "football") -> list[Event]:
        sport_id = stake_v1.SPORT_IDS.get(sport.lower(), self.settings.sport_id)
        query = stake_graphql.load_query("events", stake_graphql.EVENTS_QUERY)
        data = self._graphql(
            query,
            {"sportId": sport_id, "limit": self.settings.event_limit},
            "SportFixtures",
        )
        events = stake_v1.parse_fixtures(data)
        if not events:
            raise StakeSchemaError(
                "接口返回成功，但没有解析到任何足球赛事。"
                "请确认 STAKE_SPORT_ID 仍是足球的 UUID；若是字段变化，"
                "把抓包查询写入 queries/events.graphql 后重试。"
            )
        return events

    def fetch_results(self, fixture_ids: Sequence[str] | None = None) -> list[MatchResult]:
        ids = [item for item in (fixture_ids or []) if item]
        if not ids:
            return []
        results: list[MatchResult] = []
        for batch in stake_v1.chunked(ids, RESULT_BATCH):
            try:
                query = stake_v1.build_result_query(batch)
            except ValueError:
                continue
            data = self._graphql(query, None, "SportResults")
            results.extend(stake_v1.parse_results(data))
        return results

    # -- 诊断 ------------------------------------------------------------ #
    def probe(self) -> ProbeReport:
        """只发一个最小请求，判断凭据/IP 是否可用。"""
        try:
            data = self._graphql("query GroundBetProbe { __typename }", operation="GroundBetProbe")
        except StakeBlockedError as exc:
            return ProbeReport(False, 403, str(exc), blocked=True)
        except StakeAuthError as exc:
            return ProbeReport(False, 403, str(exc))
        except StakeError as exc:
            return ProbeReport(False, 0, str(exc))
        sample = json.dumps(data, ensure_ascii=False)[:120]
        return ProbeReport(True, 200, "接口可用", sample=sample)

    def introspect_sports(self) -> list[str]:
        """列出 schema 里与体育相关的查询字段（Introspection 关闭时返回空）。"""
        try:
            data = self._graphql(stake_graphql.INTROSPECTION_QUERY, operation="IntrospectSports")
        except StakeError:
            return []
        fields = (
            data.get("__schema", {}).get("queryType", {}).get("fields", [])
            if isinstance(data, dict)
            else []
        )
        return [
            str(field.get("name"))
            for field in fields
            if isinstance(field, dict) and field.get("name")
        ]
