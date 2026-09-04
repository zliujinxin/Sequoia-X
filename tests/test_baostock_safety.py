"""不联网的同步与本地模式回归测试，可用标准库 unittest 直接运行。"""

from datetime import date, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

import main as main_module
from sequoia_x.core.config import Settings
from sequoia_x.data.baostock_session import BaostockError, context, read_rows
from sequoia_x.data.engine import DataEngine
from sequoia_x.notify.feishu import FeishuNotifier
from sequoia_x.strategy.private_placement import PrivatePlacementStrategy


def result(rows=(), code="0"):
    response = MagicMock(error_code=code, error_msg="test failure")
    response.fields = ["date", "open", "high", "low", "close", "volume", "amount"]
    response.next.side_effect = [True] * len(rows) + [False]
    response.get_row_data.side_effect = rows
    return response


class BaostockSafetyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.settings = Settings(
            _env_file=None, db_path=str(Path(tmp.name) / "test.db"),
            report_dir=str(Path(tmp.name) / "reports"),
            feishu_webhook_url="https://example.invalid/test",
        )
        self.engine = DataEngine(self.settings)
        self.login = self.enterContext(patch(
            "baostock.login", return_value=result(),
        ))
        self.logout = self.enterContext(patch("baostock.logout", return_value=result()))
        self.query = self.enterContext(patch("baostock.query_history_k_data_plus"))
        self.sock = MagicMock()
        self.enterContext(patch.object(context, "default_socket", self.sock, create=True))
        self.enterContext(patch("sequoia_x.data.engine.pace_request"))

    def seed(self, symbols, day=None, close=10):
        day = day or (date.today() - timedelta(days=1)).isoformat()
        self.engine._save_daily(pd.DataFrame([
            dict(symbol=s, date=day, open=10, high=11, low=9,
                 close=close, volume=1000, turnover=200_000_000)
            for s in symbols
        ]))

    def test_login_failure_stops_without_query_or_logout(self):
        self.seed(["000001", "000002"])
        self.login.return_value = result(code="10001011")
        with self.assertRaises(BaostockError):
            self.engine.sync_today_bulk()
        self.query.assert_not_called()
        self.logout.assert_not_called()
        self.sock.close.assert_called_once()

    def test_partial_failure_preserves_progress_and_other_stocks(self):
        today = date.today().isoformat()
        self.seed(["000001", "000002", "000003"])
        self.seed(["600000"], today)
        self.query.side_effect = [
            result([[today, "10", "12", "9", "11", "1000", "200000000"]]),
            result(code="10002007"),
        ]
        with self.assertRaises(BaostockError):
            self.engine.sync_today_bulk()
        self.login.assert_called_once()
        self.assertEqual(self.query.call_count, 2)
        self.logout.assert_not_called()
        self.assertEqual(self.engine._get_last_date("000001"), today)
        self.assertEqual(len(self.engine.get_ohlcv("600000")), 1)
        self.assertEqual(len(self.engine.get_ohlcv("000003")), 1)
        self.sock.close.assert_called_once()

    def test_upsert_does_not_duplicate_or_delete_other_symbols(self):
        self.seed(["000001", "000002"])
        self.seed(["000001"], close=12)
        self.assertEqual(len(self.engine.get_ohlcv("000001")), 1)
        self.assertEqual(self.engine.get_ohlcv("000001").iloc[0]["close"], 12)
        self.assertEqual(len(self.engine.get_ohlcv("000002")), 1)

    def test_current_data_does_not_login(self):
        self.seed(["000001"], date.today().isoformat())
        self.assertEqual(self.engine.sync_today_bulk(), 0)
        self.login.assert_not_called()

    def test_success_uses_one_session_for_all_stocks(self):
        self.seed(["000001", "000002"])
        today = date.today().isoformat()
        self.query.side_effect = [
            result([[today, "10", "12", "9", "11", "1000", "200000000"]])
            for _ in range(2)
        ]
        self.assertEqual(self.engine.sync_today_bulk(), 2)
        self.login.assert_called_once()
        self.logout.assert_called_once()
        self.sock.close.assert_called_once()

    def test_backfill_failure_is_not_retried(self):
        self.query.return_value = result(code="10002007")
        with self.assertRaises(BaostockError):
            self.engine.backfill(["000001", "000002"])
        self.query.assert_called_once()
        self.logout.assert_not_called()

    def test_error_after_iteration_is_not_treated_as_success(self):
        rs = result()
        def next_page():
            rs.error_code = "10002007"
            return False
        rs.next.side_effect = next_page
        with self.assertRaises(BaostockError):
            read_rows(rs, "test")

    def test_local_mode_runs_turtle_and_builds_card_without_data_requests(self):
        for i in range(21):
            self.seed(["000001"], (date.today() - timedelta(days=21-i)).isoformat())
        self.seed(["000001"], date.today().isoformat(), close=30)
        self.login.side_effect = AssertionError("Local mode must not login")
        with (
            patch.object(main_module, "get_settings", return_value=self.settings),
            patch("sys.argv", ["main.py", "--local-only"]),
            patch.object(FeishuNotifier, "_get_stock_names") as names,
            patch.object(PrivatePlacementStrategy, "run") as placement,
            patch.object(DataEngine, "sync_today_bulk") as sync,
            patch("requests.post") as post,
        ):
            post.return_value.status_code = 200
            post.return_value.json.return_value = {"code": 0}
            main_module.main()
            self.login.assert_not_called()
            self.query.assert_not_called()
            names.assert_not_called()
            placement.assert_not_called()
            sync.assert_not_called()
            payloads = [call.kwargs["data"] for call in post.call_args_list]
            self.assertTrue(any("海龟突破" in p.decode("utf-8") for p in payloads))
            card = FeishuNotifier(self.settings.model_copy(update={"local_only": True}))
            self.assertIn("未联网更新行情", str(card._build_card(["000001"], "Test")))

    def test_main_stops_before_notifications_when_sync_fails(self):
        self.seed(["000001"])
        self.login.return_value = result(code="10001011")
        with (
            patch.object(main_module, "get_settings", return_value=self.settings),
            patch("sys.argv", ["main.py"]),
            patch.object(FeishuNotifier, "send_report") as send,
        ):
            with self.assertRaises(SystemExit) as exc:
                main_module.main()
            self.assertEqual(exc.exception.code, 1)
            send.assert_not_called()
            self.query.assert_not_called()


if __name__ == "__main__":
    unittest.main()
