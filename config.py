"""集中配置：从环境变量 / .env 读取，并做基本校验。

设计原则：改配置不改代码。切换真实数据源、离线演示数据、代理、初始资金
都通过 .env 完成，方便部署到别的机器上（比如换一台没被 Stake 屏蔽的 IP）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"

#: .env 里已存在的变量优先，命令行/系统环境变量可以覆盖它。
load_dotenv(ENV_FILE, override=False)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
)

DATA_SOURCES = ("auto", "stake", "mock")


def _env(*names: str, default: str | None = None) -> str | None:
    """按顺序取第一个非空环境变量（第一个名字是规范名，后面是历史别名）。"""
    for name in names:
        value = os.getenv(name)
        if value is not None and value.strip():
            return value.strip()
    return default


def _env_float(*names: str, default: float) -> float:
    raw = _env(*names)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(*names: str, default: int) -> int:
    raw = _env(*names)
    if raw is None:
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


@dataclass(frozen=True, slots=True)
class Settings:
    """运行期配置快照。"""

    access_token: str | None = None
    cf_clearance: str | None = None
    cookie: str | None = None
    user_agent: str = DEFAULT_USER_AGENT
    api_base: str = "https://stake.com"
    proxy: str | None = None
    timeout: float = 25.0
    data_source: str = "auto"
    sport_id: str = "5b4b60b9-ed95-41e7-97e3-f33aa172cf12"
    event_limit: int = 50
    initial_balance: Decimal = Decimal("1000.00")
    currency: str = "USD"
    db_path: Path = field(default_factory=lambda: BASE_DIR / "data" / "groundbet.db")
    mock_dir: Path = field(default_factory=lambda: BASE_DIR / "fixtures")
    refresh_seconds: int = 60

    # ------------------------------------------------------------------ #
    @property
    def has_credentials(self) -> bool:
        return bool(self.access_token and (self.cookie or self.cf_clearance))

    def warnings(self) -> list[str]:
        """启动时展示给用户的提示（不是致命错误）。"""
        messages: list[str] = []
        if self.data_source not in DATA_SOURCES:
            messages.append(f"GROUNDBET_DATA_SOURCE={self.data_source!r} 无效，可选值：{', '.join(DATA_SOURCES)}")
        if self.data_source in ("auto", "stake"):
            if not self.access_token:
                messages.append("缺少 STAKE_ACCESS_TOKEN，无法访问 Stake 接口，将退回离线演示数据")
            if not self.cookie and not self.cf_clearance:
                messages.append("缺少 STAKE_CF_CLEARANCE，Cloudflare 会返回 403（本机 IP 被屏蔽时也会出现同样提示）")
            if self.cf_clearance and self.user_agent == DEFAULT_USER_AGENT:
                messages.append("STAKE_USER_AGENT 为默认值，必须与签发 cf_clearance 的浏览器 UA 完全一致，否则 403")
            ua = self.user_agent.lower()
            if "applewebkit" not in ua and "gecko" not in ua and "firefox" not in ua:
                messages.append(
                    "STAKE_USER_AGENT 看起来不完整（缺少浏览器内核/版本标记），"
                    "请从浏览器直接复制完整 UA，否则 Cloudflare 会返回 403"
                )
        if self.proxy and "://" not in self.proxy:
            messages.append("STAKE_PROXY 需要带协议头，例如 http://127.0.0.1:7890")
        return messages

    def describe_source(self) -> str:
        return {"auto": "自动（真实优先）", "stake": "Stake 线上", "mock": "离线演示"}.get(
            self.data_source, self.data_source
        )


def load_settings() -> Settings:
    """每次调用都重新读取环境变量，便于测试与运行期改配置。"""
    db_path = Path(_env("GROUNDBET_DB", default=str(BASE_DIR / "data" / "groundbet.db")) or "")
    mock_dir = Path(_env("GROUNDBET_MOCK_DIR", default=str(BASE_DIR / "fixtures")) or "")
    try:
        balance = Decimal(_env("GROUNDBET_BALANCE", default="1000") or "1000")
    except ArithmeticError:
        balance = Decimal("1000")
    return Settings(
        access_token=_env("STAKE_ACCESS_TOKEN"),
        # .env 里原本拼写成了 STAKE_CF_CLEARAENCE，这里兼容旧写法
        cf_clearance=_env("STAKE_CF_CLEARANCE", "STAKE_CF_CLEARAENCE"),
        cookie=_env("STAKE_COOKIE"),
        user_agent=_env("STAKE_USER_AGENT", default=DEFAULT_USER_AGENT) or DEFAULT_USER_AGENT,
        api_base=(_env("STAKE_API_BASE", default="https://stake.com") or "https://stake.com").rstrip("/"),
        proxy=_env("STAKE_PROXY"),
        timeout=_env_float("STAKE_TIMEOUT", default=25.0),
        data_source=(_env("GROUNDBET_DATA_SOURCE", default="auto") or "auto").lower(),
        sport_id=_env("STAKE_SPORT_ID", default="5b4b60b9-ed95-41e7-97e3-f33aa172cf12")
        or "5b4b60b9-ed95-41e7-97e3-f33aa172cf12",
        event_limit=max(1, _env_int("STAKE_EVENT_LIMIT", default=50)),
        initial_balance=balance,
        currency=_env("GROUNDBET_CURRENCY", default="USD") or "USD",
        db_path=db_path,
        mock_dir=mock_dir,
        refresh_seconds=max(10, _env_int("GROUNDBET_REFRESH", default=60)),
    )
