"""Baostock Provider：保持项目原有请求方式和复权口径。"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Callable, ContextManager, Iterator

import pandas as pd

from sequoia_x.data.baostock_session import baostock_session, pace_request, read_rows

from .base import Adjustment, StockRecord, normalize_daily_frame

_ADJUST_FLAGS: dict[Adjustment, str] = {"hfq": "1", "qfq": "2", "none": "3"}


class _BaostockSession:
    def __init__(self, client, adjustment: Adjustment, pace: Callable[[], None]) -> None:
        self._client = client
        self._adjustment = adjustment
        self._pace = pace

    @staticmethod
    def _code(symbol: str) -> str:
        prefix = "sh" if symbol.startswith(("6", "9")) else "sz"
        return f"{prefix}.{symbol}"

    def list_stocks(self) -> list[StockRecord]:
        self._pace()
        result = self._client.query_stock_basic(code_name="", code="")
        rows = read_rows(result, "查询股票列表")
        records = []
        for row in rows:
            info = dict(zip(result.fields, row))
            if info.get("type") != "1":
                continue
            records.append(StockRecord(
                symbol=info["code"].split(".")[-1],
                name=info.get("code_name", "").strip(),
                active=info.get("status") == "1",
            ))
        return records

    def fetch_daily(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        self._pace()
        result = self._client.query_history_k_data_plus(
            self._code(symbol),
            "date,open,high,low,close,volume,amount",
            start_date=start_date,
            end_date=end_date,
            frequency="d",
            adjustflag=_ADJUST_FLAGS[self._adjustment],
        )
        rows = read_rows(result, f"查询 {symbol}")
        raw = pd.DataFrame(rows, columns=result.fields) if rows else pd.DataFrame()
        return normalize_daily_frame(raw, symbol)


class BaostockProvider:
    name = "baostock"

    def __init__(
        self,
        adjustment: Adjustment = "hfq",
        *,
        session_factory: Callable[[], ContextManager] = baostock_session,
        pace: Callable[[], None] = pace_request,
    ) -> None:
        self.adjustment = adjustment
        self._session_factory = session_factory
        self._pace = pace

    @contextmanager
    def session(self) -> Iterator[_BaostockSession]:
        with self._session_factory() as client:
            yield _BaostockSession(client, self.adjustment, self._pace)
