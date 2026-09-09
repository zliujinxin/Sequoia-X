"""行情数据源的稳定边界与公共字段归一化。"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Literal, Protocol

import pandas as pd

Adjustment = Literal["none", "qfq", "hfq"]
DAILY_COLUMNS = [
    "symbol", "date", "open", "high", "low", "close", "volume", "turnover"
]


class ProviderError(RuntimeError):
    """行情数据源不可用、返回错误或数据格式不合法。"""


@dataclass(frozen=True)
class StockRecord:
    """跨数据源统一的股票基础信息。"""

    symbol: str
    name: str
    active: bool = True
    security_type: str = "stock"


class MarketDataSession(Protocol):
    """一次物理连接内可执行的行情操作。"""

    def list_stocks(self) -> list[StockRecord]: ...

    def fetch_daily(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame: ...


class MarketDataProvider(Protocol):
    """数据源协议；策略层只依赖归一化后的 SQLite 数据。"""

    name: str
    adjustment: Adjustment

    def session(self) -> AbstractContextManager[MarketDataSession]: ...


def normalize_daily_frame(frame: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """将不同数据源的日线字段归一为 Sequoia-X 的八个标准字段。"""
    if frame is None or frame.empty:
        return pd.DataFrame(columns=DAILY_COLUMNS)

    data = frame.copy()
    data = data.rename(columns={"datetime": "date", "vol": "volume", "amount": "turnover"})
    required = {"date", "open", "high", "low", "close", "volume", "turnover"}
    missing = sorted(required.difference(data.columns))
    if missing:
        raise ProviderError(f"{symbol} 日线缺少字段：{', '.join(missing)}")

    raw_rows = len(data)
    parsed_dates = pd.to_datetime(data["date"], errors="coerce")
    data["date"] = parsed_dates.dt.strftime("%Y-%m-%d")
    for column in ["open", "high", "low", "close", "volume", "turnover"]:
        data[column] = pd.to_numeric(data[column], errors="coerce")
    data["symbol"] = symbol

    required_columns = ["date", "open", "high", "low", "close", "volume"]
    invalid_mask = data[required_columns].isna().any(axis=1)
    invalid_mask |= (data[["open", "high", "low", "close"]] <= 0).any(axis=1)
    invalid_mask |= data["volume"] < 0
    invalid_rows = int(invalid_mask.sum())
    zero_volume_rows = int((data["volume"] == 0).sum())
    duplicate_dates = int(data.loc[~data["date"].isna(), "date"].duplicated(keep="last").sum())
    data = data.dropna(subset=["date", "open", "high", "low", "close", "volume"])
    data = data[(data[["open", "high", "low", "close"]] > 0).all(axis=1)]
    data = data[data["volume"] > 0]
    data = data.drop_duplicates(subset=["date"], keep="last").sort_values("date")
    result = data[DAILY_COLUMNS].reset_index(drop=True)
    result.attrs["quality"] = {
        "raw_rows": raw_rows,
        "invalid_rows": invalid_rows,
        "zero_volume_rows": zero_volume_rows,
        "duplicate_dates": duplicate_dates,
        "normalized_rows": len(result),
    }
    return result
