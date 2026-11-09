"""Qt 后台抓取线程。

网络请求一律放到 QThreadPool 里执行，界面线程只负责把结果写进数据库并刷新表格。
抓取线程不碰数据库，避免 SQLite 跨线程写入的时序问题。
"""

from __future__ import annotations

import logging
from typing import Callable, Sequence

from PySide6.QtCore import QObject, QRunnable, Signal

from data.providers import DataProvider

log = logging.getLogger(__name__)

ProviderFactory = Callable[[], DataProvider]


class WorkerSignals(QObject):
    """QRunnable 本身不是 QObject，信号单独挂在这里。"""

    finished = Signal(object, str)  # 结果列表, 数据源标签
    failed = Signal(str)
    note = Signal(str)


class _FetchWorker(QRunnable):
    def __init__(self, provider_factory: ProviderFactory) -> None:
        super().__init__()
        self.signals = WorkerSignals()
        self._factory = provider_factory
        self.setAutoDelete(True)

    def _fetch(self, provider: DataProvider):
        raise NotImplementedError

    def run(self) -> None:
        try:
            provider = self._factory()
            data = self._fetch(provider)
            label = str(getattr(provider, "label", getattr(provider, "name", "")))
            warning = str(getattr(provider, "last_warning", "") or "")
            if warning:
                self._emit(self.signals.note, f"真实数据源不可用，已使用离线演示数据：{warning}")
            self._emit(self.signals.finished, list(data), label)
        except Exception as exc:  # noqa: BLE001 - 任何异常都要回报给界面
            log.exception("抓取失败")
            self._safe_fail(str(exc))

    @staticmethod
    def _emit(signal, *args) -> None:
        """窗口在抓取过程中被关闭时，Qt 对象可能已经销毁，忽略这类错误。"""
        try:
            signal.emit(*args)
        except RuntimeError:
            log.debug("窗口已关闭，丢弃抓取结果")

    def _safe_fail(self, message: str) -> None:
        self._emit(self.signals.failed, message)


class OddsFetcher(_FetchWorker):
    """拉取赛事与赔率。"""

    def _fetch(self, provider: DataProvider):
        return provider.fetch_events()


class ResultsFetcher(_FetchWorker):
    """拉取指定比赛的赛果（只查有未结算注单的比赛，省流量）。"""

    def __init__(self, provider_factory: ProviderFactory, event_ids: Sequence[str] | None = None) -> None:
        super().__init__(provider_factory)
        self._event_ids = list(event_ids or [])

    def _fetch(self, provider: DataProvider):
        return provider.fetch_results(self._event_ids)
