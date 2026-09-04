"""报告与通知回归测试：使用合成行情和模拟 HTTP，不访问外部服务。"""

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

import main as main_module
from sequoia_x.core.config import Settings
from sequoia_x.data.engine import DataEngine
from sequoia_x.notify.feishu import FeishuNotifier
from sequoia_x.reporting.service import ReportService, compare, save_json, snapshot
from sequoia_x.strategy.rps_breakout import RpsBreakoutStrategy


class ReportingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.settings = Settings(
            _env_file=None, db_path=str(Path(tmp.name) / "test.db"),
            report_dir=str(Path(tmp.name) / "reports"), local_only=True,
            feishu_webhook_url="https://example.invalid/default",
        ).model_copy(update={"strategy_webhooks": {}})
        self.engine = DataEngine(self.settings)
        dates = pd.bdate_range("2025-01-01", periods=200).strftime("%Y-%m-%d")
        rows = []
        for s in range(1, 11):
            for i, day in enumerate(dates):
                # 收益非常接近，用于捕捉排名前错误舍入导致的并列。
                close = 10 + i * .01 + s * i * .000000001
                rows.append(dict(symbol=f"{s:06}", date=day, open=close-.01,
                                 high=close+.02, low=close-.02, close=close,
                                 volume=1000 if i < 199 else 2000, turnover=2e8))
        self.engine._save_daily(pd.DataFrame(rows))
        self.service = ReportService(self.engine, self.settings)
        self.results = {"TurtleTradeStrategy": ["000001", "000001", "000002"],
                        "RpsBreakoutStrategy": ["000001"]}
        self.enterContext(patch("baostock.login", side_effect=AssertionError("No data requests")))
        self.post = self.enterContext(patch("requests.post"))
        self.post.return_value.status_code = 200
        self.post.return_value.json.return_value = {"code": 0}

    def test_merge_chart_and_metric_windows(self):
        report = self.service.build(self.results)
        self.assertEqual(len(report["records"]), 2)
        row = next(r for r in report["records"] if r["symbol"] == "000001")
        self.assertEqual(len(row["signals"]), 2)
        self.assertEqual(len(row["chart"]), 120)
        self.assertIsNotNone(row["chart"][0]["ma60"])
        self.assertEqual(row["metrics"]["volume_ratio"], 2)
        self.assertAlmostEqual(row["metrics"]["volume_ratio_inclusive"], 2000/1050, places=4)
        self.assertEqual(len(self.engine.get_ohlcv("000001")), 180)

    def test_rps_matches_strategy_without_rounding_ties(self):
        selected = RpsBreakoutStrategy(self.engine, self.settings).run()
        report = self.service.build({"RpsBreakoutStrategy": [f"{n:06}" for n in range(1, 11)]})
        expected = [r["symbol"] for r in report["records"] if r["metrics"]["rps"] >= 90]
        self.assertEqual(set(selected), set(expected))
        self.assertEqual(set(expected), {"000009", "000010"})

    def test_compare_new_retained_changed_removed(self):
        report = self.service.build(self.results)
        self.assertTrue(all(r["change"] == "initial" for r in report["records"]))
        previous = snapshot(report)
        rows, removed = compare(report["records"], previous)
        self.assertTrue(all(r["change"] == "retained" for r in rows))
        changed = copy.deepcopy(rows[0])
        changed["metrics"]["pct"] += 2.1
        new = {**copy.deepcopy(changed), "symbol": "999999"}
        rows, removed = compare([changed, new], previous)
        self.assertEqual([r["change"] for r in rows], ["changed", "new"])
        self.assertEqual(len(removed), 1)

    def test_html_escapes_data_and_preserves_preview_baseline(self):
        report = self.service.build(self.results)
        report["records"][0]["name"] = '</script><script>alert("x")</script>'
        path = self.service.write(report)
        html = path.read_text(encoding="utf-8")
        self.assertNotIn('</script><script>alert', html)
        self.assertNotIn('__REPORT_DATA__', html)
        self.assertNotIn('<script src=', html)
        self.assertTrue((path.parent / "latest.html").exists())
        self.assertEqual(len(list((path.parent / "state").glob("sent-*"))), 0)
        second = self.service.build(self.results)
        self.assertTrue(all(r["change"] == "retained" for r in second["records"]))
        FeishuNotifier(self.settings).send_report(second)
        self.assertTrue(self.post.called, "Preview must not consume initial notification")

    def test_stale_and_missing_history_are_explicit(self):
        report = self.service.build({"PrivatePlacementStrategy": ["999999"]})
        row = report["records"][0]
        self.assertTrue(row["stale"])
        self.assertEqual(row["chart"], [])
        self.assertIsNone(row["metrics"]["rps"])
        self.service.write(report)  # 不产生 NaN 或序列化异常。

    def test_price_discontinuity_is_flagged_and_deprioritized(self):
        df = self.engine.get_ohlcv("000001").tail(1).copy()
        df["close"] = 100
        df["high"] = 101
        self.engine._save_daily(df)
        report = self.service.build(self.results)
        row = next(r for r in report["records"] if r["symbol"] == "000001")
        self.assertTrue(any("复权口径" in w for w in row["warnings"]))
        self.assertEqual(report["records"][-1]["symbol"], "000001")

    def test_repeated_notifications_suppressed_and_forced(self):
        report = self.service.build(self.results)
        notifier = FeishuNotifier(self.settings)
        self.assertTrue(notifier.send_report(report))
        self.assertEqual(self.post.call_count, 1)
        payload = json.loads(self.post.call_args.kwargs["data"])
        text = json.dumps(payload, ensure_ascii=False)
        self.assertEqual(text.count('· 000001]'), 1)
        self.assertIn('RPS 强势 / 海龟突破', text)
        notifier.send_report(report)
        self.assertEqual(self.post.call_count, 1)
        notifier.send_report(report, force=True)
        self.assertEqual(self.post.call_count, 2)

    def test_failure_does_not_advance_delivery_baseline(self):
        report = self.service.build(self.results)
        notifier = FeishuNotifier(self.settings)
        self.post.return_value.json.return_value = {"code": 19001}
        self.assertFalse(notifier.send_report(report))
        self.assertFalse(list(Path(self.settings.report_dir).glob("state/sent-*")))
        self.post.return_value.json.return_value = {"code": 0}
        self.assertTrue(notifier.send_report(report))
        self.assertEqual(self.post.call_count, 2)

    def test_empty_results_send_removals_once(self):
        notifier = FeishuNotifier(self.settings)
        notifier.send_report(self.service.build(self.results))
        empty = self.service.build({s: [] for s in self.results})
        notifier.send_report(empty)
        self.assertIn("移出 2 只", self.post.call_args.kwargs["data"].decode())
        notifier.send_report(empty)
        self.assertEqual(self.post.call_count, 2)

    def test_routes_are_independent_and_respect_strategy_categories(self):
        settings = self.settings.model_copy(update={"strategy_webhooks": {"limit_down": "https://example.invalid/other"}})
        report = self.service.build({"TurtleTradeStrategy": ["000001"], "UptrendLimitDownStrategy": ["000001"]})
        FeishuNotifier(settings).send_report(report)
        self.assertEqual(self.post.call_count, 2)
        by_url = {c.args[0]: c.kwargs["data"].decode() for c in self.post.call_args_list}
        self.assertIn("趋势突破 · 展示", by_url[settings.feishu_webhook_url])
        self.assertIn("异常大跌 · 展示", by_url["https://example.invalid/other"])

    def test_card_chunks_stay_under_byte_limit(self):
        blocks = ["中文内容" * 100 for _ in range(40)]
        cards = FeishuNotifier._chunk_cards(blocks)
        self.assertGreater(len(cards), 1)
        self.assertTrue(all(len(json.dumps(c, ensure_ascii=False).encode()) <= 16000 for c in cards))
        self.assertEqual(sum(len(c["card"]["elements"]) for c in cards), 40)

    def test_cli_preview_never_posts(self):
        with (patch.object(main_module, "get_settings", return_value=self.settings),
              patch("sys.argv", ["main.py", "--local-only", "--no-notify"])):
            main_module.main()
        self.post.assert_not_called()
        self.assertTrue((Path(self.settings.report_dir) / "latest.html").exists())

    def test_corrupt_baseline_and_cached_names(self):
        report = self.service.build(self.results)
        state = Path(self.settings.report_dir) / "state" / f"report-{report['scope']}.json"
        state.parent.mkdir(parents=True)
        state.write_text("{bad-json", encoding="utf-8")
        save_json(Path(self.settings.report_dir) / "stock_names.json", {"names": {"000001": "测试名称"}})
        rebuilt = self.service.build(self.results)
        self.assertIsNone(rebuilt["baseline"])
        self.assertEqual(next(r["name"] for r in rebuilt["records"] if r["symbol"] == "000001"), "测试名称")

    def test_stock_list_preserves_names_for_offline_reports(self):
        from unittest.mock import MagicMock
        rs = MagicMock(error_code="0")
        rs.fields = ["code", "code_name", "type", "status"]
        rs.next.side_effect = [True, True, False]
        rs.get_row_data.side_effect = [["sz.000001", "平安银行", "1", "1"],
                                       ["sh.000001", "上证指数", "2", "1"]]
        with patch("sequoia_x.data.engine.baostock_session") as session:
            session.return_value.__enter__.return_value.query_stock_basic.return_value = rs
            self.assertEqual(self.engine.get_all_symbols(), ["000001"])
        fresh_engine = DataEngine(self.settings)
        self.assertEqual(fresh_engine.get_stock_names(), {"000001": "平安银行"})
        report = ReportService(fresh_engine, self.settings).build(self.results)
        self.assertEqual(next(r["name"] for r in report["records"] if r["symbol"] == "000001"), "平安银行")

    def test_name_refresh_updates_html_without_changing_baselines(self):
        report = self.service.build(self.results)
        self.service.write(report)
        state = Path(self.settings.report_dir) / "state" / f"report-{report['scope']}.json"
        before = state.read_bytes()
        with patch.object(self.engine, "refresh_stock_names", return_value={"000001": "平安银行", "000002": "万科Ａ"}):
            self.assertEqual(self.service.refresh_names(), 2)
        html = (Path(self.settings.report_dir) / "latest.html").read_text(encoding="utf-8")
        self.assertIn('平安银行', html)
        self.assertEqual(state.read_bytes(), before)
        self.post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
