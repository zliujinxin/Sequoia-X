"""选股策略严格逐日验证回归测试；不访问外部数据源。"""

import tempfile
from pathlib import Path

import pandas as pd

from sequoia_x.backtest import StrategyValidationService
from sequoia_x.core.config import Settings
from sequoia_x.data.engine import DataEngine


def test_validation_uses_next_open_and_writes_reviewable_report() -> None:
    with tempfile.TemporaryDirectory() as root:
        settings = Settings(
            _env_file=None,
            db_path=str(Path(root) / "test.db"),
            report_dir=str(Path(root) / "reports"),
            feishu_webhook_url="https://example.invalid/hook",
        )
        engine = DataEngine(settings)
        dates = pd.bdate_range("2025-01-02", periods=155).strftime("%Y-%m-%d")
        rows = []
        for symbol in ["000001", "000002", "600001"]:
            for index, day in enumerate(dates):
                close = 10.0
                open_price = 10.0
                high = 10.0
                turnover = 20_000_000
                if symbol == "000001" and index == 130:
                    open_price, high, close, turnover = 10.5, 11.5, 11.0, 200_000_000
                elif symbol == "000001" and index > 130:
                    open_price, high, close = 11.0, 12.2, 12.0
                rows.append(
                    {
                        "symbol": symbol,
                        "date": day,
                        "open": open_price,
                        "high": high,
                        "low": min(open_price, close) * 0.99,
                        "close": close,
                        "volume": 1_000_000,
                        "turnover": turnover,
                    }
                )
        engine._save_daily(pd.DataFrame(rows))

        service = StrategyValidationService(engine, settings)
        report = service.run((5, 20))
        turtle = report["strategies"]["TurtleTradeStrategy"]["horizons"]["5"]

        assert report["complete_days"] == 155
        assert report["source_symbols"] == 3
        assert turtle["observations"] >= 1
        assert turtle["median_return_pct"] > 0
        assert turtle["median_excess_pct"] > 0

        html_path, json_path = service.write(report)
        assert html_path.exists()
        assert json_path.exists()
        assert (Path(settings.report_dir) / "strategy-validation-latest.html").exists()
        assert "先验证，再相信" in html_path.read_text(encoding="utf-8")
        assert "__VALIDATION_DATA__" not in html_path.read_text(encoding="utf-8")


def test_validation_rejects_invalid_horizon() -> None:
    with tempfile.TemporaryDirectory() as root:
        settings = Settings(
            _env_file=None,
            db_path=str(Path(root) / "test.db"),
            report_dir=str(Path(root) / "reports"),
            feishu_webhook_url="https://example.invalid/hook",
        )
        service = StrategyValidationService(DataEngine(settings), settings)

        try:
            service.run((0,))
        except ValueError as exc:
            assert "1到120" in str(exc)
        else:
            raise AssertionError("无效持有周期应被拒绝")
