"""GUI 冒烟测试：验证窗口、表格模型、下注对话框的接线（离屏运行）。

只检查「能不能构建 + 数据能不能流通」，不做像素级断言。
"""

from __future__ import annotations

import os
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from betting.engine import SimulationEngine  # noqa: E402
from config import load_settings  # noqa: E402
from data.providers import MockProvider  # noqa: E402
from storage.repository import Repository  # noqa: E402

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "mock_events.json"


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_main_window_and_bet_dialog(qapp, tmp_path):
    from ui.bet_dialog import BetDialog
    from ui.main_window import MainWindow

    repo = Repository(tmp_path / "ui.db")
    settings = replace(load_settings(), data_source="mock", db_path=tmp_path / "ui.db")
    engine = SimulationEngine(repo, Decimal("1000"))

    events = MockProvider(FIXTURE).fetch_events()
    repo.upsert_events(events)

    window = MainWindow(settings, repo, engine)
    window._load_cached_events()

    assert window.event_model.rowCount() == len(events)
    assert "余额" in window.status_label.text()

    target = next(event for event in events if event.status == "upcoming")
    dialog = BetDialog(engine, target, window)
    assert dialog.market_combo.count() >= 1
    assert dialog.outcome_combo.count() >= 2

    dialog.stake_spin.setValue(25.0)
    dialog._confirm()
    assert dialog.bet is not None

    window._refresh_bets()
    assert window.bet_model.rowCount() == 1
    assert engine.balance == Decimal("975.00")

    window.close()
    repo.close()
