"""赛事表格模型（Qt）。"""

from __future__ import annotations

from decimal import Decimal
from typing import Sequence

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor

from models import Event, KIND_1X2, STATUS_ENDED, STATUS_LIVE

EVENT_ROLE = Qt.UserRole + 1


def fmt_odds(value: Decimal | None) -> str:
    return "-" if value is None else f"{value:.2f}"


class MatchModel(QAbstractTableModel):
    HEADERS = ["开赛", "联赛", "比赛", "状态", "主胜", "平局", "客胜", "大小球", "双方进球", "比分"]

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._events: list[Event] = []

    # -- 数据装载 -------------------------------------------------------- #
    def set_events(self, events: Sequence[Event]) -> None:
        self.beginResetModel()
        self._events = list(events)
        self.endResetModel()

    def event_at(self, row: int) -> Event | None:
        if 0 <= row < len(self._events):
            return self._events[row]
        return None

    # -- Qt 接口 --------------------------------------------------------- #
    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._events)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section: int, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal and 0 <= section < len(self.HEADERS):
            return self.HEADERS[section]
        return None

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        event = self._events[index.row()]
        column = index.column()

        if role == EVENT_ROLE:
            return event
        if role == Qt.ToolTipRole:
            markets = "、".join(market.name for market in event.markets) or "暂无盘口"
            return f"{event.title}\n{event.league}\n盘口：{markets}"
        if role == Qt.ForegroundRole and column == 3:
            if event.status == STATUS_LIVE:
                return QColor("#d9534f")
            if event.status == STATUS_ENDED:
                return QColor("#888888")
            return None
        if role != Qt.DisplayRole:
            return None

        if column == 0:
            return event.start_time.astimezone().strftime("%m-%d %H:%M")
        if column == 1:
            return event.league
        if column == 2:
            return event.title
        if column == 3:
            return event.status_label
        if column == 9:
            return f"{event.score[0]} - {event.score[1]}" if event.score else "-"

        main = event.market(KIND_1X2)
        if column in (4, 5, 6):
            key = ("home", "draw", "away")[column - 4]
            outcome = main.outcome(key) if main else None
            return fmt_odds(outcome.odds if outcome else None)

        if column == 7:
            totals = [market for market in event.markets if market.kind == "totals"]
            if not totals:
                return "-"
            market = totals[0]
            over = market.outcome("over")
            under = market.outcome("under")
            line = market.line if market.line is not None else ""
            return f"{fmt_odds(over.odds if over else None)} / {fmt_odds(under.odds if under else None)} ({line})"

        if column == 8:
            btts = next((market for market in event.markets if market.kind == "btts"), None)
            if not btts:
                return "-"
            yes = btts.outcome("yes")
            no = btts.outcome("no")
            return f"{fmt_odds(yes.odds if yes else None)} / {fmt_odds(no.odds if no else None)}"
        return None
