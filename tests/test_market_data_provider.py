"""行情 Provider 与影子校验的离线回归测试。"""

from contextlib import contextmanager
from datetime import date, timedelta
from enum import IntEnum
from pathlib import Path
import tempfile
import unittest

import pandas as pd

from sequoia_x.core.config import Settings
from sequoia_x.data.engine import DataEngine
from sequoia_x.data.providers.base import ProviderError, StockRecord, normalize_daily_frame
from sequoia_x.data.providers.easy_tdx import EasyTdxProvider, market_number
from sequoia_x.data.quality import ProviderQualityChecker, compare_daily_frames


def bars(symbol: str, start: str, closes=(10.0, 11.0)) -> pd.DataFrame:
    days = pd.date_range(start, periods=len(closes), freq="D")
    return pd.DataFrame({
        "symbol": symbol,
        "date": days.strftime("%Y-%m-%d"),
        "open": closes,
        "high": [value + 1 for value in closes],
        "low": [value - 1 for value in closes],
        "close": closes,
        "volume": [1000.0] * len(closes),
        "turnover": [10000.0] * len(closes),
    })


class FakeProvider:
    name = "fake"
    adjustment = "hfq"

    def __init__(self, frames=None, error=None):
        self.frames = frames or {}
        self.error = error
        self.sessions = 0
        self.fetches = []

    @contextmanager
    def session(self):
        self.sessions += 1
        yield self

    def list_stocks(self):
        return [StockRecord("000001", "平安银行")]

    def fetch_daily(self, symbol, start_date, end_date):
        self.fetches.append((symbol, start_date, end_date))
        if self.error:
            raise ProviderError(self.error)
        return self.frames.get(symbol, pd.DataFrame(columns=[
            "symbol", "date", "open", "high", "low", "close", "volume", "turnover"
        ]))


class ProviderTests(unittest.TestCase):
    def settings(self, root: str) -> Settings:
        return Settings(
            _env_file=None,
            db_path=str(Path(root) / "test.db"),
            report_dir=str(Path(root) / "reports"),
            feishu_webhook_url="https://example.invalid/hook",
        )

    def test_normalization_renames_sorts_deduplicates_and_drops_bad_rows(self):
        raw = pd.DataFrame({
            "datetime": ["2024-01-02", "2024-01-01", "2024-01-02", "bad"],
            "open": [11, 10, 12, 1], "high": [12, 11, 13, 1],
            "low": [10, 9, 11, 1], "close": [11, 10, 12, 1],
            "vol": [100, 100, 200, 0], "amount": [1100, 1000, 2400, 0],
        })
        result = normalize_daily_frame(raw, "000001")
        self.assertEqual(result["date"].tolist(), ["2024-01-01", "2024-01-02"])
        self.assertEqual(result["close"].tolist(), [10, 12])
        self.assertEqual(result["symbol"].unique().tolist(), ["000001"])
        self.assertEqual(result.attrs["quality"]["invalid_rows"], 1)
        self.assertEqual(result.attrs["quality"]["zero_volume_rows"], 1)
        self.assertEqual(result.attrs["quality"]["duplicate_dates"], 1)

    def test_engine_uses_one_provider_session_and_keeps_empty_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            yesterday = (date.today() - timedelta(days=1)).isoformat()
            provider = FakeProvider({"000001": bars("000001", date.today().isoformat(), (12,))})
            engine = DataEngine(self.settings(tmp), provider=provider)
            engine._save_daily(bars("000001", yesterday, (10,)))
            engine._save_daily(bars("000002", yesterday, (10,)))
            self.assertEqual(engine.sync_today_bulk(), 1)
            self.assertEqual(provider.sessions, 1)
            self.assertEqual(len(provider.fetches), 2)
            self.assertEqual(engine._get_last_date("000001"), date.today().isoformat())
            self.assertEqual(engine._get_last_date("000002"), yesterday)

    def test_provider_error_stops_later_symbols(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = FakeProvider(error="offline")
            engine = DataEngine(self.settings(tmp), provider=provider)
            yesterday = (date.today() - timedelta(days=1)).isoformat()
            engine._save_daily(bars("000001", yesterday, (10,)))
            engine._save_daily(bars("000002", yesterday, (10,)))
            with self.assertRaises(ProviderError):
                engine.sync_today_bulk()
            self.assertEqual(len(provider.fetches), 1)

    def test_easy_tdx_cannot_write_formal_table_before_metadata_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.settings(tmp).model_copy(update={"data_provider": "easy_tdx"})
            with self.assertRaisesRegex(ProviderError, "仅开放 --check-provider"):
                DataEngine(settings)

    def test_easy_tdx_adapter_uses_mac_adjustment_and_market(self):
        class Adjust(IntEnum):
            NONE = 0
            QFQ = 1
            HFQ = 2

        class Period(IntEnum):
            DAILY = 4

        calls = []

        class MacClient:
            def __init__(self, **kwargs):
                calls.append(("init", kwargs))

            def connect(self):
                calls.append(("connect",))

            def close(self):
                calls.append(("close",))

            def get_stock_kline(self, **kwargs):
                calls.append(("bars", kwargs))
                return pd.DataFrame({
                    "datetime": ["2024-01-01", "2024-01-02"],
                    "open": [10, 11], "high": [11, 12], "low": [9, 10],
                    "close": [10, 11], "vol": [100, 110], "amount": [1000, 1210],
                })

        runtime = {
            "Adjust": Adjust, "Period": Period, "MacClient": MacClient,
            "TdxClient": object, "Market": object,
        }
        provider = EasyTdxProvider(adjustment="hfq", runtime_loader=lambda: runtime)
        with provider.session() as session:
            result = session.fetch_daily("600000", "2024-01-01", "2024-01-02")
        request = next(call[1] for call in calls if call[0] == "bars")
        self.assertEqual(request["market"], 1)
        self.assertEqual(request["adjust"], Adjust.HFQ)
        self.assertEqual(result["volume"].tolist(), [100, 110])
        self.assertEqual(market_number("920002"), 2)

    def test_quality_comparison_detects_unit_mismatch(self):
        reference = bars("000001", "2024-01-01", tuple(range(10, 20)))
        candidate = reference.copy()
        candidate["volume"] *= 100
        report = compare_daily_frames("000001", reference, candidate)
        self.assertEqual(report["status"], "warn")
        self.assertEqual(report["volume_ratio"], 100.0)
        self.assertEqual(report["price_mape"]["close"], 0.0)

    def test_quality_comparison_accepts_constant_hfq_scale(self):
        reference = bars("600000", "2024-01-01", tuple(range(10, 20)))
        candidate = reference.copy()
        for column in ["open", "high", "low", "close"]:
            candidate[column] *= 3.5
        report = compare_daily_frames("600000", reference, candidate)
        self.assertEqual(report["status"], "pass")
        self.assertAlmostEqual(report["close_scale"], 3.5)
        self.assertAlmostEqual(report["scale_adjusted_price_mape"]["close"], 0.0)

    def test_quality_checker_writes_reports_without_touching_daily_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.settings(tmp)
            frame = bars("000001", "2024-01-01", tuple(range(10, 50)))
            engine = DataEngine(settings, provider=FakeProvider())
            engine._save_daily(frame)
            before = len(engine.get_ohlcv("000001"))
            checker = ProviderQualityChecker(
                engine, FakeProvider({"000001": frame.copy()}), settings.report_dir
            )
            report = checker.run(sample_size=1, days=30)
            html_path, json_path = checker.write(report)
            self.assertEqual(report["summary"]["pass"], 1)
            self.assertTrue(html_path.exists())
            self.assertTrue(json_path.exists())
            self.assertEqual(len(engine.get_ohlcv("000001")), before)


if __name__ == "__main__":
    unittest.main()
