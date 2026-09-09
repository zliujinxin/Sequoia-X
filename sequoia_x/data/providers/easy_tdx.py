"""easy-tdx Provider，固定面向已审计的 1.20.8 保存版本。"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
from typing import Any, Callable, Iterator

import pandas as pd

from .base import Adjustment, ProviderError, StockRecord, normalize_daily_frame

PINNED_COMMIT = "7c9e19de937946d231735dabb4a275440578b753"
EXPECTED_VERSION = "1.20.8"
INSTALL_HINT = (
    "python scripts/install_easy_tdx.py"
)


def _load_runtime() -> dict[str, Any]:
    try:
        installed = version("easy-tdx")
        import easy_tdx
        from easy_tdx import Adjust, MacClient, Market, Period, TdxClient
    except (ImportError, PackageNotFoundError) as exc:
        raise ProviderError(f"easy-tdx 尚未安装。请使用固定提交安装：{INSTALL_HINT}") from exc
    if installed != EXPECTED_VERSION:
        raise ProviderError(
            f"easy-tdx 版本为 {installed}，已审计版本应为 {EXPECTED_VERSION}；请重新固定安装"
        )
    marker_path = Path(easy_tdx.__file__).resolve().parent / "_sequoia_provenance.json"
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProviderError(
            f"easy-tdx 缺少 Sequoia-X 来源标记。请重新执行：{INSTALL_HINT}"
        ) from exc
    if marker.get("commit") != PINNED_COMMIT:
        raise ProviderError("easy-tdx 来源提交与已审计提交不一致，请重新执行安装脚本")
    return {
        "Adjust": Adjust,
        "MacClient": MacClient,
        "Market": Market,
        "Period": Period,
        "TdxClient": TdxClient,
    }


def market_number(symbol: str) -> int:
    """通达信市场号：深市 0、沪市 1、北交所 2。"""
    if symbol.startswith(("43", "83", "87", "92", "93", "4", "8")):
        return 2
    return 1 if symbol.startswith(("6", "9")) else 0


class _EasyTdxSession:
    def __init__(
        self,
        runtime: dict[str, Any],
        adjustment: Adjustment,
        timeout: float,
        host: str | None,
        mac_host: str | None,
        port: int | None,
    ) -> None:
        self._runtime = runtime
        self._adjustment = adjustment
        self._timeout = timeout
        self._host = host
        self._mac_host = mac_host
        self._port = port
        self._main = None
        self._mac = None

    def _connect(self, key: str, host: str | None):
        attr = "_main" if key == "TdxClient" else "_mac"
        current = getattr(self, attr)
        if current is not None:
            return current
        client = self._runtime[key](
            host=host,
            port=self._port,
            timeout=self._timeout,
            heartbeat_interval=0,
        )
        try:
            client.connect()
        except Exception as exc:
            try:
                client.close()
            except Exception:
                pass
            raise ProviderError(f"easy-tdx {key} 连接失败：{exc}") from exc
        setattr(self, attr, client)
        return client

    def close(self) -> None:
        for client in (self._mac, self._main):
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    def list_stocks(self) -> list[StockRecord]:
        client = self._connect("TdxClient", self._host)
        try:
            frame = client.get_security_list_all()
        except Exception as exc:
            raise ProviderError(f"easy-tdx 查询股票列表失败：{exc}") from exc
        if frame is None or frame.empty:
            raise ProviderError("easy-tdx 股票列表为空")
        return [
            StockRecord(symbol=str(row.code).zfill(6), name=str(row.name).strip())
            for row in frame[["code", "name"]].itertuples(index=False)
            if str(row.code).strip() and str(row.name).strip()
        ]

    def fetch_daily(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
        if start > end:
            return normalize_daily_frame(pd.DataFrame(), symbol)
        # 接口按“从最新向前 N 根”取数。用日历天数加缓冲，能覆盖交易日且不过度请求。
        count = min(12_000, max(64, (end - start).days + 45))
        adjust = getattr(self._runtime["Adjust"], self._adjustment.upper())
        client = self._connect("MacClient", self._mac_host)
        try:
            raw = client.get_stock_kline(
                market=market_number(symbol),
                code=symbol,
                period=self._runtime["Period"].DAILY,
                start=0,
                count=count,
                adjust=adjust,
            )
        except Exception as exc:
            raise ProviderError(f"easy-tdx 查询 {symbol} 日线失败：{exc}") from exc
        if raw is not None and not raw.empty:
            date_column = "datetime" if "datetime" in raw.columns else "date"
            raw_dates = pd.to_datetime(raw[date_column], errors="coerce")
            raw = raw[(raw_dates >= pd.Timestamp(start)) & (raw_dates <= pd.Timestamp(end))]
        data = normalize_daily_frame(raw, symbol)
        if data.empty:
            return data
        result = data[(data["date"] >= start_date) & (data["date"] <= end_date)].reset_index(drop=True)
        result.attrs = data.attrs.copy()
        result.attrs["quality"]["normalized_rows"] = len(result)
        return result


class EasyTdxProvider:
    name = "easy_tdx"

    def __init__(
        self,
        adjustment: Adjustment = "hfq",
        *,
        timeout: float = 10.0,
        host: str | None = None,
        mac_host: str | None = None,
        port: int | None = None,
        runtime_loader: Callable[[], dict[str, Any]] = _load_runtime,
    ) -> None:
        self.adjustment = adjustment
        self.timeout = timeout
        self.host = host
        self.mac_host = mac_host
        self.port = port
        self._runtime_loader = runtime_loader

    @contextmanager
    def session(self) -> Iterator[_EasyTdxSession]:
        session = _EasyTdxSession(
            self._runtime_loader(), self.adjustment, self.timeout,
            self.host, self.mac_host, self.port,
        )
        try:
            yield session
        finally:
            session.close()
