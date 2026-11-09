"""下注对话框。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from betting.engine import BetError, SimulationEngine
from models import Event, money


class BetDialog(QDialog):
    """选择盘口 / 选项 / 金额，确认后在本地模拟账户里记一笔。"""

    def __init__(self, engine: SimulationEngine, event: Event, parent=None) -> None:
        super().__init__(parent)
        self.engine = engine
        # 注意：不能叫 self.event —— 那会覆盖 QDialog.event() 虚函数
        self.match = event
        self.bet = None
        self.setWindowTitle(f"模拟下注 · {event.title}")
        self.setMinimumWidth(480)

        layout = QVBoxLayout(self)
        info = QLabel(
            f"<b>{event.title}</b><br>"
            f"{event.league}　状态：{event.status_label}<br>"
            f"开赛：{event.start_time.astimezone().strftime('%Y-%m-%d %H:%M')}<br>"
            f"可用余额：<b>{engine.balance:.2f}</b>"
        )
        info.setTextFormat(Qt.RichText)
        layout.addWidget(info)

        form = QFormLayout()
        self.market_combo = QComboBox()
        self.outcome_combo = QComboBox()
        self.stake_spin = QDoubleSpinBox()
        self.stake_spin.setDecimals(2)
        self.stake_spin.setSingleStep(1.0)
        self.stake_spin.setMinimum(0.01)
        self.stake_spin.setMaximum(max(0.01, float(engine.balance)))
        self.stake_spin.setValue(min(10.0, float(engine.balance)) or 0.01)
        self.stake_spin.setSuffix(" （模拟）")

        for market in event.tradable_markets():
            self.market_combo.addItem(market.name, market.key)
        form.addRow("盘口", self.market_combo)
        form.addRow("选项", self.outcome_combo)
        form.addRow("金额", self.stake_spin)
        layout.addLayout(form)

        quick = QHBoxLayout()
        quick.addWidget(QLabel("快捷金额："))
        for percent in (2, 5, 10):
            button = QPushButton(f"{percent}%")
            button.clicked.connect(lambda _checked=False, p=percent: self._apply_percent(p))
            quick.addWidget(button)
        fixed = QPushButton("10")
        fixed.clicked.connect(lambda: self.stake_spin.setValue(min(10.0, float(self.engine.balance))))
        quick.addWidget(fixed)
        quick.addStretch(1)
        layout.addLayout(quick)

        self.return_label = QLabel()
        self.return_label.setTextFormat(Qt.RichText)
        layout.addWidget(self.return_label)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        self.buttons.button(QDialogButtonBox.Ok).setText("确认下注")
        self.buttons.button(QDialogButtonBox.Cancel).setText("取消")
        self.buttons.accepted.connect(self._confirm)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self.market_combo.currentIndexChanged.connect(self._reload_outcomes)
        self.outcome_combo.currentIndexChanged.connect(self._update_return)
        self.stake_spin.valueChanged.connect(self._update_return)

        if self.market_combo.count() == 0:
            self.buttons.button(QDialogButtonBox.Ok).setEnabled(False)
            self.return_label.setText("<span style='color:#c62828'>该比赛暂无可自动结算的盘口</span>")
        else:
            self._reload_outcomes()

    # -- 事件 ------------------------------------------------------------ #
    def _current_market(self):
        return self.match.market(self.market_combo.currentData())

    def _current_outcome(self):
        market = self._current_market()
        return market.outcome(self.outcome_combo.currentData()) if market else None

    def _reload_outcomes(self) -> None:
        market = self._current_market()
        self.outcome_combo.blockSignals(True)
        self.outcome_combo.clear()
        if market:
            for outcome in market.outcomes:
                self.outcome_combo.addItem(f"{outcome.label}　@ {outcome.odds:.2f}", outcome.key)
        self.outcome_combo.blockSignals(False)
        self._update_return()

    def _apply_percent(self, percent: int) -> None:
        balance = float(self.engine.balance)
        self.stake_spin.setValue(max(0.01, round(balance * percent / 100, 2)))

    def _update_return(self) -> None:
        outcome = self._current_outcome()
        stake = money(self.stake_spin.value())
        if outcome is None:
            self.return_label.setText("")
            return
        ret = money(stake * outcome.odds)
        profit = money(ret - stake)
        self.return_label.setText(
            f"赔率 <b>{outcome.odds:.2f}</b>　潜在回报 <b>{ret:.2f}</b>　净赚 <b>{profit:+.2f}</b>"
        )

    def _confirm(self) -> None:
        market = self._current_market()
        outcome = self._current_outcome()
        if market is None or outcome is None:
            QMessageBox.warning(self, "无法下注", "请先选择盘口与选项")
            return
        try:
            self.bet = self.engine.place_bet(
                self.match, market.key, outcome.key, money(self.stake_spin.value())
            )
        except BetError as exc:
            QMessageBox.warning(self, "下注被拒绝", str(exc))
            return
        self.accept()
