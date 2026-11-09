"""主窗口：赛事赔率表 + 注单表 + 模拟账户状态栏。"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Callable

from PySide6.QtCore import QThreadPool, QTimer, Qt, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from betting.engine import BetError, SimulationEngine
from config import Settings
from data.fetcher import OddsFetcher, ResultsFetcher
from data.match_model import MatchModel
from data.providers import DataProvider, build_provider
from models import Event, MatchResult
from storage.repository import Repository
from ui.bet_dialog import BetDialog
from ui.bets_model import BetsModel
from ui.result_dialog import ResultDialog

log = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    def __init__(
        self,
        settings: Settings,
        repo: Repository,
        engine: SimulationEngine,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.repo = repo
        self.engine = engine
        self._fetcher = None
        self._busy = False
        self._last_source = "未刷新"

        self.setWindowTitle("GroundBet · Stake 足球赔率抓取 & 模拟下注")
        self.resize(1280, 760)

        self.event_model = MatchModel(self)
        self.bet_model = BetsModel(self)

        central = QWidget(self)
        layout = QVBoxLayout(central)

        # -- 顶部工具栏 ---------------------------------------------------- #
        bar = QHBoxLayout()
        bar.addWidget(QLabel("数据源"))
        self.source_combo = QComboBox()
        for key, label in (
            ("auto", "自动（真实优先，失败降级）"),
            ("stake", "仅 Stake 线上"),
            ("mock", "仅离线演示"),
        ):
            self.source_combo.addItem(label, key)
        index = self.source_combo.findData(settings.data_source)
        self.source_combo.setCurrentIndex(max(0, index))
        self.source_combo.currentIndexChanged.connect(self._on_source_changed)
        bar.addWidget(self.source_combo)

        self.refresh_btn = QPushButton("刷新赔率")
        self.refresh_btn.clicked.connect(self.refresh_odds)
        bar.addWidget(self.refresh_btn)

        self.bet_btn = QPushButton("下注")
        self.bet_btn.clicked.connect(self.open_bet_panel)
        bar.addWidget(self.bet_btn)

        self.results_btn = QPushButton("拉取赛果并结算")
        self.results_btn.clicked.connect(self.fetch_results)
        bar.addWidget(self.results_btn)

        self.manual_btn = QPushButton("录入赛果")
        self.manual_btn.clicked.connect(self.enter_result)
        bar.addWidget(self.manual_btn)

        self.stats_btn = QPushButton("账户统计")
        self.stats_btn.clicked.connect(self.show_stats)
        bar.addWidget(self.stats_btn)

        self.reset_btn = QPushButton("重置账户")
        self.reset_btn.clicked.connect(self.reset_account)
        bar.addWidget(self.reset_btn)

        bar.addStretch(1)
        self.auto_check = QCheckBox(f"自动刷新（{settings.refresh_seconds}s）")
        self.auto_check.toggled.connect(self._toggle_auto_refresh)
        bar.addWidget(self.auto_check)
        layout.addLayout(bar)

        # -- 提示条 -------------------------------------------------------- #
        self.notice = QLabel()
        self.notice.setWordWrap(True)
        self.notice.setStyleSheet(
            "background:#fff4e5; color:#8a4b00; border:1px solid #f0c98a;"
            "border-radius:4px; padding:6px;"
        )
        self.notice.hide()
        layout.addWidget(self.notice)

        # -- 表格 ---------------------------------------------------------- #
        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(
            self._table_panel(
                "赛事与赔率（双击行可直接下注）",
                self.event_model,
                self._on_event_double_clicked,
                widths={0: 116, 1: 180, 3: 80, 4: 76, 5: 76, 6: 76, 7: 150, 8: 120, 9: 76},
                stretch_column=2,
            )
        )
        splitter.addWidget(
            self._table_panel(
                "我的模拟注单",
                self.bet_model,
                None,
                widths={0: 116, 2: 120, 3: 160, 4: 76, 5: 86, 6: 86, 7: 86, 8: 86},
                stretch_column=1,
            )
        )
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([470, 250])
        layout.addWidget(splitter, 1)

        self.setCentralWidget(central)
        self.status_label = QLabel()
        self.statusBar().addPermanentWidget(self.status_label)

        # -- 定时刷新 ------------------------------------------------------ #
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._auto_tick)

        self._load_cached_events()
        self._refresh_bets()
        self._update_status()
        self._show_settings_warnings()
        QTimer.singleShot(300, self.refresh_odds)

    # -- 构建辅助 -------------------------------------------------------- #
    def _table_panel(
        self,
        title: str,
        model,
        double_clicked,
        widths: dict[int, int],
        stretch_column: int,
    ) -> QWidget:
        panel = QWidget()
        box = QVBoxLayout(panel)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(QLabel(title))
        table = QTableView()
        table.setModel(model)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        # 显式列宽，避免不同字体下「2.45」被挤成「245」
        for column, width in widths.items():
            table.setColumnWidth(column, width)
        table.horizontalHeader().setSectionResizeMode(stretch_column, QHeaderView.Stretch)
        if double_clicked:
            table.doubleClicked.connect(double_clicked)
        if model is self.event_model:
            self.event_table = table
        else:
            self.bet_table = table
        box.addWidget(table)
        return panel

    # -- 数据源 ---------------------------------------------------------- #
    def _provider_factory(self) -> Callable[[], DataProvider]:
        source = self.source_combo.currentData() or "auto"

        def factory() -> DataProvider:
            return build_provider(replace(self.settings, data_source=str(source)))

        return factory

    def _on_source_changed(self) -> None:
        self.repo.set_meta("data_source", str(self.source_combo.currentData()))
        self.notice.hide()
        self.refresh_odds()

    def _show_settings_warnings(self) -> None:
        warnings = self.settings.warnings()
        if warnings:
            self._show_notice("；".join(warnings))

    def _show_notice(self, message: str) -> None:
        self.notice.setText(message)
        self.notice.show()

    # -- 抓取 ------------------------------------------------------------ #
    @Slot()
    def refresh_odds(self) -> None:
        if self._busy:
            return
        self._busy = True
        self._set_buttons_enabled(False)
        self.refresh_btn.setText("抓取中…")
        fetcher = OddsFetcher(self._provider_factory())
        fetcher.signals.finished.connect(self.on_events_ready)
        fetcher.signals.failed.connect(self.on_fetch_failed)
        fetcher.signals.note.connect(self._show_notice)
        self._fetcher = fetcher
        QThreadPool.globalInstance().start(fetcher)

    @Slot()
    def fetch_results(self) -> None:
        if self._busy:
            return
        event_ids = sorted({bet.event_id for bet in self.repo.pending_bets()})
        if not event_ids:
            QMessageBox.information(self, "提示", "当前没有待结算的注单，不需要拉取赛果")
            return
        self._busy = True
        self._set_buttons_enabled(False)
        self.results_btn.setText("拉取中…")
        fetcher = ResultsFetcher(self._provider_factory(), event_ids)
        fetcher.signals.finished.connect(self.on_results_ready)
        fetcher.signals.failed.connect(self.on_fetch_failed)
        fetcher.signals.note.connect(self._show_notice)
        self._fetcher = fetcher
        QThreadPool.globalInstance().start(fetcher)

    @Slot(object, str)
    def on_events_ready(self, events: object, source: str) -> None:
        payload: list[Event] = list(events)  # type: ignore[arg-type]
        self.repo.upsert_events(payload)
        self.repo.record_odds_snapshot(payload)
        self._last_source = source or "未知"
        cleaned = None
        if source != "Stake 线上":
            self._show_notice(f"当前显示的是「{source}」数据，不是 Stake 实时赔率。")
        else:
            # 真实数据到手后清掉离线演示数据，避免两种比赛混在一张表里
            removed = self.engine.clean_demo_data()
            if removed["events"] or removed["bets"]:
                cleaned = (
                    f"已清理离线演示数据：{removed['events']} 场比赛"
                    f"、{removed['bets']} 张演示注单"
                )
                self._refresh_bets()
        self._refresh_events()
        self._finish_fetch()
        self._update_status()
        if cleaned:
            self.statusBar().showMessage(cleaned, 8000)
        else:
            self.statusBar().showMessage(f"已更新 {len(payload)} 场比赛（{source}）", 5000)

    @Slot(object, str)
    def on_results_ready(self, results: object, source: str) -> None:
        payload: list[MatchResult] = list(results)  # type: ignore[arg-type]
        for result in payload:
            self.repo.upsert_result(result)
        settled = self.engine.settle_pending()
        self._last_source = source or self._last_source
        self._finish_fetch()
        self._refresh_events()
        self._refresh_bets()
        self._update_status()
        message = f"更新 {len(payload)} 场赛果，结算 {len(settled)} 张注单（{source}）"
        self.statusBar().showMessage(message, 6000)

    @Slot(str)
    def on_fetch_failed(self, message: str) -> None:
        self._finish_fetch()
        QMessageBox.warning(self, "抓取失败", message)
        self._show_notice(message)

    def _finish_fetch(self) -> None:
        self._busy = False
        self._fetcher = None
        self._set_buttons_enabled(True)
        self.refresh_btn.setText("刷新赔率")
        self.results_btn.setText("拉取赛果并结算")

    def _set_buttons_enabled(self, enabled: bool) -> None:
        for button in (
            self.refresh_btn,
            self.bet_btn,
            self.results_btn,
            self.manual_btn,
            self.stats_btn,
            self.reset_btn,
        ):
            button.setEnabled(enabled)

    # -- 下注 / 结算 ----------------------------------------------------- #
    def _selected_event(self) -> Event | None:
        index = self.event_table.currentIndex()
        return self.event_model.event_at(index.row()) if index.isValid() else None

    @Slot()
    def open_bet_panel(self) -> None:
        event = self._selected_event()
        if event is None:
            QMessageBox.information(self, "提示", "请先在赛事列表里选择一个比赛")
            return
        if not event.is_open():
            QMessageBox.information(self, "提示", "该比赛已结束，不能下注")
            return
        dialog = BetDialog(self.engine, event, self)
        if dialog.exec() == BetDialog.Accepted and dialog.bet is not None:
            self._refresh_bets()
            self._update_status()
            self.statusBar().showMessage(
                f"已下注：{dialog.bet.event_label} / {dialog.bet.outcome_label} "
                f"{dialog.bet.stake:.2f} @ {dialog.bet.odds:.2f}",
                6000,
            )

    @Slot()
    def _on_event_double_clicked(self, index) -> None:
        self.event_table.setCurrentIndex(index)
        self.open_bet_panel()

    @Slot()
    def enter_result(self) -> None:
        events = self.repo.list_events()
        if not events:
            QMessageBox.information(self, "提示", "还没有比赛数据，请先刷新赔率")
            return
        dialog = ResultDialog(events, self.repo, self)
        if dialog.exec() != ResultDialog.Accepted:
            return
        selection = dialog.selection()
        if selection is None:
            return
        event_id, home, away = selection
        try:
            settled = self.engine.settle_event(event_id, home, away)
        except BetError as exc:
            QMessageBox.warning(self, "结算失败", str(exc))
            return
        self._refresh_events()
        self._refresh_bets()
        self._update_status()
        self.statusBar().showMessage(f"比分 {home}-{away} 已保存，结算 {len(settled)} 张注单", 6000)

    @Slot()
    def show_stats(self) -> None:
        stats = self.engine.stats()
        lines = self.engine.summary_lines()
        recent = [bet for bet in self.repo.list_bets() if bet.status != "pending"][:5]
        if recent:
            lines.append("")
            lines.append("最近结算：")
            for bet in recent:
                lines.append(
                    f"  {bet.event_label} {bet.outcome_label} @{bet.odds:.2f} "
                    f"{bet.stake:.2f} → {bet.status_label} {bet.profit:+.2f}"
                )
        lines.append("")
        lines.append(f"数据库：{self.repo.path}")
        lines.append(f"记录数：{self.repo.counts()}")
        QMessageBox.information(self, f"模拟账户统计（{stats.bet_count} 张注单）", "\n".join(lines))

    @Slot()
    def reset_account(self) -> None:
        answer = QMessageBox.question(
            self,
            "重置账户",
            "确认清空所有注单并把余额恢复到初始资金？\n（比赛与赛果缓存会保留）",
        )
        if answer != QMessageBox.Yes:
            return
        self.engine.reset()
        self._refresh_bets()
        self._update_status()
        self.statusBar().showMessage("模拟账户已重置", 4000)

    # -- 视图刷新 -------------------------------------------------------- #
    def _load_cached_events(self) -> None:
        cached = self.repo.list_events()
        if cached:
            self.event_model.set_events(cached)
            self.statusBar().showMessage(f"已载入本地缓存的 {len(cached)} 场比赛", 3000)

    def _refresh_events(self) -> None:
        self.event_model.set_events(self.repo.list_events())

    def _refresh_bets(self) -> None:
        self.bet_model.set_bets(self.repo.list_bets(limit=300))

    def _update_status(self) -> None:
        stats = self.engine.stats()
        self.status_label.setText(
            f"余额 {stats.balance:.2f}　|　待结算 {stats.pending_stake:.2f}　|　"
            f"已实现 {stats.realized_profit:+.2f}（ROI {stats.roi * 100:.1f}%）　|　"
            f"源：{self._last_source}"
        )

    # -- 自动刷新 -------------------------------------------------------- #
    def _toggle_auto_refresh(self, enabled: bool) -> None:
        if enabled:
            self.timer.start(max(10, self.settings.refresh_seconds) * 1000)
            self.statusBar().showMessage(f"已开启自动刷新（{self.settings.refresh_seconds}s）", 3000)
        else:
            self.timer.stop()

    def _auto_tick(self) -> None:
        if not self._busy:
            self.refresh_odds()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        self.timer.stop()
        # 等正在跑的抓取线程结束，避免它向已销毁的窗口发信号
        QThreadPool.globalInstance().waitForDone(3000)
        super().closeEvent(event)
