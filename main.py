"""程序入口：python main.py

只负责组装依赖（配置 → 数据库 → 引擎 → 窗口），业务逻辑都在各自的模块里。
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from betting.engine import SimulationEngine
from config import DATA_SOURCES, load_settings
from storage.repository import Repository


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="GroundBet · Stake 足球赔率抓取与模拟下注")
    parser.add_argument("--source", choices=DATA_SOURCES, help="数据源：auto / stake / mock")
    parser.add_argument("--db", help="SQLite 数据库路径（默认 data/groundbet.db）")
    parser.add_argument("--balance", type=float, help="模拟账户初始资金（默认 1000）")
    parser.add_argument("--log-level", default="INFO", help="日志级别，默认 INFO")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    settings = load_settings()
    overrides: dict = {}
    if args.source:
        overrides["data_source"] = args.source
    if args.db:
        overrides["db_path"] = Path(args.db)
    if args.balance is not None:
        overrides["initial_balance"] = Decimal(str(args.balance))
    if overrides:
        settings = replace(settings, **overrides)

    for warning in settings.warnings():
        logging.getLogger("groundbet").warning(warning)

    from PySide6.QtWidgets import QApplication

    from ui.main_window import MainWindow

    app = QApplication([sys.argv[0]])
    app.setApplicationName("GroundBet")
    app.setApplicationDisplayName("GroundBet · 模拟下注")

    repo = Repository(settings.db_path)
    engine = SimulationEngine(repo, settings.initial_balance)
    window = MainWindow(settings, repo, engine)
    window.show()
    try:
        return app.exec()
    finally:
        repo.close()


if __name__ == "__main__":
    raise SystemExit(main())
