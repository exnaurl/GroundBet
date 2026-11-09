"""手工录入赛果的对话框。

抓取被 Cloudflare / 地区限制挡住时，仍然可以手工录入比分，
让模拟下注的结算、盈亏统计这些逻辑正常跑完。
"""

from __future__ import annotations

from typing import Sequence

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QSpinBox,
    QVBoxLayout,
)

from models import Event
from storage.repository import Repository


class ResultDialog(QDialog):
    def __init__(self, events: Sequence[Event], repo: Repository, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("录入赛果")
        self.setMinimumWidth(460)
        self.repo = repo
        self._events = list(events)

        layout = QVBoxLayout(self)
        tip = QLabel("选择比赛并填写最终比分。保存后会立即结算该场所有待结算注单。")
        tip.setWordWrap(True)
        layout.addWidget(tip)

        form = QFormLayout()
        self.event_combo = QComboBox()
        for event in self._events:
            self.event_combo.addItem(
                f"{event.start_time.astimezone().strftime('%m-%d %H:%M')}  {event.title}（{event.status_label}）",
                event.id,
            )
        self.home_spin = QSpinBox()
        self.away_spin = QSpinBox()
        for spin in (self.home_spin, self.away_spin):
            spin.setRange(0, 30)
        form.addRow("比赛", self.event_combo)
        form.addRow("主队进球", self.home_spin)
        form.addRow("客队进球", self.away_spin)
        layout.addLayout(form)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        self.buttons.button(QDialogButtonBox.Ok).setText("保存并结算")
        self.buttons.button(QDialogButtonBox.Cancel).setText("取消")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self.event_combo.currentIndexChanged.connect(self._prefill)
        if self._events:
            self._prefill()
        else:
            self.buttons.button(QDialogButtonBox.Ok).setEnabled(False)

    def _prefill(self) -> None:
        event = self._current_event()
        if event is None:
            return
        result = self.repo.get_result(event.id)
        if result is not None:
            self.home_spin.setValue(result.home_score)
            self.away_spin.setValue(result.away_score)
        elif event.score is not None:
            self.home_spin.setValue(event.score[0])
            self.away_spin.setValue(event.score[1])
        else:
            self.home_spin.setValue(0)
            self.away_spin.setValue(0)

    def _current_event(self) -> Event | None:
        index = self.event_combo.currentIndex()
        return self._events[index] if 0 <= index < len(self._events) else None

    def selection(self) -> tuple[str, int, int] | None:
        event = self._current_event()
        if event is None:
            return None
        return event.id, self.home_spin.value(), self.away_spin.value()
