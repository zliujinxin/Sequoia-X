"""本地缠论页面与 API 回归测试；不访问外部行情。"""

from datetime import datetime
import json
from pathlib import Path
from types import SimpleNamespace
from threading import Thread
import tempfile
import unittest
from urllib.request import urlopen

import pandas as pd

from sequoia_x.core.config import Settings
from sequoia_x.web.chanlun import (
    ChanlunAnalysisService,
    infer_market,
    replay_chanlun_signals,
    simulate_signal_backtest,
)
from sequoia_x.web.server import create_server


class FakeAnalysis:
    def analyse(self, **kwargs):
        return {"symbol": kwargs["symbol"], "latest_data_date": "2026-09-09"}


class FakeClient:
    closed = False

    @classmethod
    def from_best_host(cls):
        return cls()

    def connect(self):
        return None

    def get_stock_kline(self, **kwargs):
        return pd.DataFrame([{"datetime": datetime(2026, 9, 9), "close": 10}])

    def close(self):
        self.closed = True


class FakeResult:
    def __init__(self):
        old = SimpleNamespace(date=datetime(2026, 8, 28), index=1)
        future = SimpleNamespace(date=datetime(2026, 9, 1), index=2)
        self.klines = [
            SimpleNamespace(date=old.date, open=9, high=11, low=8, close=10, amount=100),
            SimpleNamespace(date=datetime(2026, 9, 9), open=10, high=12, low=9, close=11, amount=120),
        ]
        self.fractals = [
            SimpleNamespace(
                fx_type=SimpleNamespace(value="ding"),
                k=old,
                val=11,
                done=True,
            )
        ]
        bi = SimpleNamespace(
            start=SimpleNamespace(k=old), end=SimpleNamespace(k=old),
            direction=SimpleNamespace(value="up"), high=11, low=8,
        )
        zs = SimpleNamespace(start=SimpleNamespace(k=future), end=SimpleNamespace(k=future))
        self.mmds = [SimpleNamespace(
            mmd_type=SimpleNamespace(value="3sell"), bi=bi, zs=zs, msg="test",
        )]

    def to_dict(self):
        return {
            "code": "002475", "frequency": "DAILY", "kline_count": 2,
            "ckline_count": 2, "fractal_count": 1, "bi_count": 1,
            "zs_count": 1, "xd_count": 0, "mmd_count": 1, "bc_count": 0,
            "bis": [{"index": 0, "direction": "up", "start_date": "2026-08-28",
                     "end_date": "2026-08-28", "high": 11, "low": 8, "done": True}],
            "zss": [{"index": 0, "start_date": "2026-08-28", "end_date": "2026-09-01",
                     "zg": 11, "zd": 9, "gg": 12, "dd": 8, "line_count": 3, "done": False}],
            "xds": [], "mmds": [], "bcs": [],
        }


class FakeAnalyser:
    def __init__(self, **kwargs):
        pass

    def process_klines(self, frame):
        return FakeResult()


class ChanlunWebTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.settings = Settings(
            _env_file=None,
            db_path=str(Path(tmp.name) / "test.db"),
            report_dir=str(Path(tmp.name) / "reports"),
            local_only=True,
            feishu_webhook_url="https://example.invalid/hook",
        )

    def test_market_inference_and_validation(self):
        self.assertEqual(infer_market("002475"), "SZ")
        self.assertEqual(infer_market("600519"), "SH")
        self.assertEqual(infer_market("920001"), "BJ")
        with self.assertRaisesRegex(ValueError, "6位数字"):
            ChanlunAnalysisService.validate("abc", "AUTO", "DAILY", "QFQ", 800)

    def test_signal_backtest_uses_next_open_and_can_filter_time_mismatch(self):
        klines = [
            {"date": f"2026-01-0{day}", "open": price, "close": price, "high": price,
             "low": price, "volume": 1000}
            for day, price in enumerate([9, 10, 11, 12, 10, 11, 13], start=1)
        ]
        signals = [
            {"type": "1buy", "date": "2026-01-01", "temporal_mismatch": False},
            {"type": "2sell", "date": "2026-01-03", "temporal_mismatch": False},
            {"type": "3buy", "date": "2026-01-04", "temporal_mismatch": True},
            {"type": "1sell", "date": "2026-01-06", "temporal_mismatch": False},
            {"type": "2buy", "date": "2026-01-07", "temporal_mismatch": False},
        ]

        original = simulate_signal_backtest(
            klines, signals, exclude_temporal_mismatch=False
        )
        safe = simulate_signal_backtest(
            klines, signals, exclude_temporal_mismatch=True
        )

        self.assertEqual(original["closed_trades"], 2)
        self.assertEqual(original["trades"][0]["buy_date"], "2026-01-02")
        self.assertEqual(original["trades"][0]["sell_date"], "2026-01-04")
        self.assertGreater(original["profit"], 0)
        self.assertEqual(original["signals_without_next_bar"], 1)
        self.assertEqual(safe["closed_trades"], 1)
        self.assertEqual(safe["signals_filtered"], 1)

    def test_signal_backtest_uses_first_visible_date_instead_of_anchor(self):
        klines = [
            {"date": f"2026-01-0{day}", "open": price, "close": price,
             "high": price, "low": price, "volume": 1000}
            for day, price in enumerate([9, 10, 11, 12, 13], start=1)
        ]
        signals = [{"type": "1buy", "date": "2026-01-01",
                    "available_date": "2026-01-03", "temporal_mismatch": False}]
        result = simulate_signal_backtest(
            klines, signals, exclude_temporal_mismatch=True
        )
        self.assertEqual(result["open_entry"]["signal_date"], "2026-01-01")
        self.assertEqual(result["open_entry"]["available_date"], "2026-01-03")
        self.assertEqual(result["open_entry"]["date"], "2026-01-04")

    def test_replay_records_first_visible_date_and_later_retraction(self):
        dates = pd.date_range("2026-01-01", periods=6)
        frame = pd.DataFrame([
            {"datetime": day, "open": 10, "high": 11, "low": 9,
             "close": 10, "vol": 100}
            for day in dates
        ])

        class PrefixAwareAnalyser:
            def __init__(self, **kwargs):
                pass

            def process_klines(self, prefix):
                mmds = []
                if 4 <= len(prefix) <= 5:
                    start = SimpleNamespace(k=SimpleNamespace(date=dates[0]))
                    end = SimpleNamespace(k=SimpleNamespace(date=dates[1]))
                    bi = SimpleNamespace(start=start, end=end)
                    zs = SimpleNamespace(start=SimpleNamespace(k=SimpleNamespace(date=dates[2])))
                    mmds = [SimpleNamespace(
                        mmd_type=SimpleNamespace(value="1buy"), bi=bi, zs=zs,
                        msg="首次可见测试",
                    )]
                return SimpleNamespace(mmds=mmds)

        signals = replay_chanlun_signals(
            frame, analyser_factory=PrefixAwareAnalyser,
            code="000001", period="DAILY",
        )
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["anchor_date"], "2026-01-02")
        self.assertEqual(signals[0]["available_date"], "2026-01-04")
        self.assertEqual(signals[0]["confirmation_lag_bars"], 2)
        self.assertEqual(signals[0]["retracted_date"], "2026-01-06")
        self.assertFalse(signals[0]["active_at_end"])

    def test_analysis_payload_marks_date_lag_and_temporal_mismatch(self):
        runtime = {
            "MacClient": FakeClient,
            "Period": SimpleNamespace(DAILY="daily", MIN_30="30min"),
            "Adjust": SimpleNamespace(QFQ="qfq"),
        }
        service = ChanlunAnalysisService(
            self.settings, {"002475": "立讯精密"},
            runtime_loader=lambda: runtime, analyser_factory=FakeAnalyser,
        )
        result = service.analyse("002475", count=800)
        self.assertEqual(result["latest_data_date"], "2026-09-09")
        self.assertEqual(result["latest_structure_date"], "2026-08-28")
        self.assertEqual(result["structure_lag_bars"], 1)
        self.assertEqual(result["name"], "立讯精密")
        self.assertEqual(result["temporal_mismatch_count"], 1)
        self.assertTrue(result["mmds"][0]["temporal_mismatch"])

    def test_local_server_serves_page_health_and_api(self):
        server = create_server(self.settings, "127.0.0.1", 0, FakeAnalysis())
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        with urlopen(base + "/chanlun", timeout=2) as response:
            page = response.read().decode("utf-8")
            self.assertIn("个股结构实验室", page)
            self.assertIn('id="count" name="count" type="number"', page)
            self.assertIn('min="100" max="2000"', page)
        with urlopen(base + "/api/health", timeout=2) as response:
            self.assertEqual(json.load(response), {"status": "ok"})
        with urlopen(base + "/api/chanlun?symbol=002475", timeout=2) as response:
            self.assertEqual(json.load(response)["symbol"], "002475")


if __name__ == "__main__":
    unittest.main()
