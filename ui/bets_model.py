"""注单表格模型（Qt）。"""

from __future__ import annotations

from typing import Sequence

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor

from models import BET_LOST, BET_PENDING, BET_VOID, BET_WON, Bet

BET_ROLE = Qt.UserRole + 1


class BetsModel(QAbstractTableModel):
    HEADERS = ["下单时间", "比赛", "盘口", "选项", "赔率", "本金", "状态", "回报", "盈亏"]

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._bets: list[Bet] = []

    def set_bets(self, bets: Sequence[Bet]) -> None:
        self.beginResetModel()
        self._bets = list(bets)
        self.endResetModel()

    def bet_at(self, row: int) -> Bet | None:
        return self._bets[row] if 0 <= row < len(self._bets) else None

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._bets)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section: int, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal and 0 <= section < len(self.HEADERS):
            return self.HEADERS[section]
        return None

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        bet = self._bets[index.row()]
        column = index.column()

        if role == BET_ROLE:
            return bet
        if role == Qt.ToolTipRole:
            return f"{bet.id}\n{bet.event_label}\n{bet.market_name} / {bet.outcome_label}\n备注：{bet.note or '-'}"
        if role == Qt.TextAlignmentRole and column in (4, 5, 7, 8):
            return int(Qt.AlignRight | Qt.AlignVCenter)
        if role == Qt.ForegroundRole:
            if bet.status == BET_WON:
                return QColor("#1a7f37")
            if bet.status == BET_LOST:
                return QColor("#c62828")
            if bet.status == BET_VOID:
                return QColor("#888888")
            return None
        if role != Qt.DisplayRole:
            return None

        if column == 0:
            return bet.placed_at.astimezone().strftime("%m-%d %H:%M")
        if column == 1:
            return bet.event_label
        if column == 2:
            return bet.market_name
        if column == 3:
            return bet.outcome_label
        if column == 4:
            return f"{bet.odds:.2f}"
        if column == 5:
            return f"{bet.stake:.2f}"
        if column == 6:
            return bet.status_label
        if column == 7:
            return f"{bet.payout:.2f}" if bet.status != BET_PENDING else f"{bet.potential_return:.2f}"
        if column == 8:
            if bet.status == BET_PENDING:
                return "-"
            return f"{bet.profit:+.2f}"
        return None
