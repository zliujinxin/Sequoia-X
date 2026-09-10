"""板块分类、ST识别及报告/通知共用过滤结果。"""

import unittest
from unittest.mock import patch

from sequoia_x.core.config import Settings
from sequoia_x.core.stock_profile import daily_price_limit_ratio, stock_profile
from sequoia_x.notify.feishu import FeishuNotifier
from sequoia_x.reporting.service import ReportService, compare, snapshot
from tests import test_reporting


class ProfileTests(unittest.TestCase):
    def test_board_and_exchange_are_separate(self):
        cases = {"600153": ("sh_main", "sh"), "605001": ("sh_main", "sh"),
                 "688485": ("star", "sh"), "689009": ("star", "sh"),
                 "000001": ("sz_main", "sz"), "002528": ("sz_main", "sz"),
                 "003018": ("sz_main", "sz"), "301234": ("chinext", "sz"),
                 "300750": ("chinext", "sz"), "920001": ("bse", "bj"),
                 "900901": ("sh_b", "sh"), "200002": ("sz_b", "sz"),
                 "830001": ("unknown", "unknown"), "430047": ("unknown", "unknown"),
                 "399001": ("unknown", "unknown"), "bad": ("unknown", "unknown")}
        for code, expected in cases.items():
            with self.subTest(code=code):
                profile = stock_profile(code, "名称")
                self.assertEqual((profile["board"], profile["exchange"]), expected)

    def test_st_variants_and_unknown_are_distinct(self):
        for name, expected in [("ST南新", "st"), ("*ST英飞", "star_st"),
                               ("＊ＳＴ 测试", "star_st"), ("S*ST测试", "star_st"),
                               ("SST测试", "st"), ("XDST测试", "st"),
                               ("Best科技", "normal"), ("宁德时代", "normal"),
                               ("", "unknown"), ("   ", "unknown")]:
            with self.subTest(name=name):
                self.assertEqual(stock_profile("600000", name)["st_status"], expected)

    def test_invalid_board_config_rejected(self):
        with self.assertRaises(ValueError):
            Settings(_env_file=None, feishu_webhook_url="https://example.invalid", include_boards=["typo"])

    def test_daily_price_limit_ratio_uses_board_and_st_rules(self):
        self.assertEqual(daily_price_limit_ratio("600000", "浦发银行"), 0.10)
        self.assertEqual(daily_price_limit_ratio("688001", "华兴源创"), 0.20)
        self.assertEqual(daily_price_limit_ratio("300750", "宁德时代"), 0.20)
        self.assertEqual(daily_price_limit_ratio("920001", "北交样本"), 0.30)
        self.assertEqual(daily_price_limit_ratio("688001", "*ST样本"), 0.05)


class OutputFilterTests(unittest.TestCase):
    # 复用合成数据准备，避免运行完整行情或查询网络。
    setUp = test_reporting.ReportingTests.setUp

    def test_filter_counts_scope_and_notification(self):
        names = {"000001": "平安银行", "000002": "*ST测试", "688485": "九州一轨"}
        results = {"TurtleTradeStrategy": list(names) + ["600000"], "RpsBreakoutStrategy": ["000001"]}
        settings = self.settings.model_copy(update={"include_boards": ["sz_main"], "exclude_st": True})
        with patch.object(ReportService, "_names", return_value=names):
            unfiltered = self.service.build(results)
            filtered = ReportService(self.engine, settings).build(results)
        self.assertEqual([r["symbol"] for r in filtered["records"]], ["000001"])
        self.assertEqual(filtered["raw_count"], 4)
        self.assertEqual(filtered["excluded_count"], 3)
        self.assertEqual(filtered["strategies"]["TurtleTradeStrategy"]["count"], 1)
        self.assertNotEqual(unfiltered["scope"], filtered["scope"])
        before = next(r for r in unfiltered["records"] if r["symbol"] == "000001")
        self.assertEqual(before["metrics"]["rps"], filtered["records"][0]["metrics"]["rps"])
        FeishuNotifier(settings).send_report(filtered)
        payload = self.post.call_args.kwargs["data"].decode()
        self.assertIn('深市主板', payload)
        self.assertIn('平安银行', payload)
        self.assertNotIn('*ST测试', payload)
        self.assertNotIn('九州一轨', payload)

    def test_st_status_change_is_tracked(self):
        with patch.object(ReportService, "_names", return_value={"000001": "普通名称"}):
            initial = self.service.build({"TurtleTradeStrategy": ["000001"]})
        with patch.object(ReportService, "_names", return_value={"000001": "ST名称"}):
            updated = self.service.build({"TurtleTradeStrategy": ["000001"]})
        rows, _ = compare(updated["records"], snapshot(initial))
        self.assertEqual(rows[0]["change"], "changed")
        self.assertIn('ST状态', rows[0]["change_details"][0])


if __name__ == '__main__':
    unittest.main()
