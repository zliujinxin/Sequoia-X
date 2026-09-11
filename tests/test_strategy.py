"""策略引擎属性测试。"""

import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd
from hypothesis import given, settings as h_settings
from hypothesis import strategies as st

from sequoia_x.core.config import Settings
from sequoia_x.data.engine import DataEngine
from sequoia_x.strategy.ma_volume import MaVolumeStrategy
from sequoia_x.strategy.turtle_trade import TurtleTradeStrategy


# Feature: sequoia-x-v2, Property 9: 策略 run() 返回值类型正确
@given(
    symbols=st.lists(
        st.text(min_size=6, max_size=6, alphabet="0123456789"),
        min_size=0, max_size=3, unique=True,
    )
)
@h_settings(max_examples=30, deadline=None)
def test_strategy_run_returns_list_of_str(symbols: list[str]) -> None:
    """属性 9：run() 应返回 list[str]，每个元素为非空字符串。"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        settings = Settings(
            db_path=str(Path(tmp_dir) / "test.db"),
            start_date="2024-01-01",
            feishu_webhook_url="https://example.com/hook",
        )
        engine = DataEngine(settings)

        with patch.object(engine, "get_all_symbols", return_value=symbols):
            with patch.object(engine, "get_ohlcv", return_value=pd.DataFrame()):
                strategy = MaVolumeStrategy(engine=engine, settings=settings)
                result = strategy.run()

    assert isinstance(result, list)
    assert all(isinstance(s, str) and len(s) > 0 for s in result)


def test_turtle_uses_local_turnover_for_sorting_without_market_request() -> None:
    dates = pd.bdate_range("2026-01-01", periods=21).strftime("%Y-%m-%d")

    def history(turnover: float) -> pd.DataFrame:
        rows = [
            {"date": day, "open": 9.5, "high": 10.0, "low": 9.0,
             "close": 9.5, "volume": 1000, "turnover": 50_000_000}
            for day in dates
        ]
        rows[-1].update(
            {"open": 10.2, "high": 11.2, "low": 10.1, "close": 11.0,
             "turnover": turnover}
        )
        return pd.DataFrame(rows)

    engine = Mock()
    engine.get_local_symbols.return_value = ["000001", "600001"]
    engine.get_ohlcv.side_effect = lambda symbol: history(
        300_000_000 if symbol == "600001" else 200_000_000
    )
    settings = Settings(
        _env_file=None,
        feishu_webhook_url="https://example.invalid/hook",
        local_only=False,
    )

    assert TurtleTradeStrategy(engine, settings).run() == ["600001", "000001"]
