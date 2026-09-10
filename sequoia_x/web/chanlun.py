"""通过已审计的 easy-tdx 运行单股缠论分析。"""

from __future__ import annotations

from datetime import datetime, time
import math
import re
from threading import Lock
from time import perf_counter
from typing import Any, Callable

from sequoia_x.data.providers.base import ProviderError
from sequoia_x.data.providers.easy_tdx import _load_runtime


MARKETS = {"SZ": 0, "SH": 1, "BJ": 2}
PERIODS = {"DAILY", "WEEKLY", "MONTHLY", "60MIN", "30MIN", "15MIN", "5MIN"}
PERIOD_ATTRIBUTES = {
    "DAILY": "DAILY",
    "WEEKLY": "WEEKLY",
    "MONTHLY": "MONTHLY",
    "60MIN": "MIN_60",
    "30MIN": "MIN_30",
    "15MIN": "MIN_15",
    "5MIN": "MIN_5",
}
ADJUSTMENTS = {"NONE", "QFQ", "HFQ"}


def infer_market(symbol: str) -> str:
    if symbol.startswith(("4", "8", "92")):
        return "BJ"
    if symbol.startswith(("6", "9")):
        return "SH"
    return "SZ"


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return round(result, 4) if math.isfinite(result) else None


def _date(value: Any, period: str) -> str:
    parsed = value.to_pydatetime() if hasattr(value, "to_pydatetime") else value
    fmt = "%Y-%m-%d %H:%M" if "MIN" in period else "%Y-%m-%d"
    return parsed.strftime(fmt)


def _trade_fee(amount: float, rate: float) -> float:
    """按 A 股常见规则估算佣金；单笔最低 5 元。"""

    return max(5.0, amount * rate)


def _mmd_identity(mmd: Any, period: str) -> tuple[str, str, str]:
    """用稳定的结构日期识别同一个买卖点，避免跨快照重复触发。"""

    if not mmd.bi:
        return (str(mmd.mmd_type.value), "", "")
    return (
        str(mmd.mmd_type.value),
        _date(mmd.bi.start.k.date, period),
        _date(mmd.bi.end.k.date, period),
    )


def replay_chanlun_signals(
    frame: Any,
    *,
    analyser_factory: Callable[..., Any],
    code: str,
    period: str,
) -> list[dict[str, Any]]:
    """逐根K线重算缠论，记录信号在当时首次可见的日期。

    图形锚点可能位于更早的笔端点，但回测只能在程序首次从截止当日数据中
    算出信号后执行。信号后来消失时仍保留首次出现记录，以复现真实观察者
    当时能够看到的信息。
    """

    if frame is None or frame.empty:
        return []
    date_column = "datetime" if "datetime" in frame.columns else "date"
    bar_dates = [_date(value, period) for value in frame[date_column].tolist()]
    date_indexes = {value: index for index, value in enumerate(bar_dates)}
    first_prefix = min(3, len(frame))
    events: list[dict[str, Any]] = []
    by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    active_keys: set[tuple[str, str, str]] = set()

    for end_index in range(first_prefix - 1, len(frame)):
        snapshot = frame.iloc[: end_index + 1].copy()
        result = analyser_factory(code=code, frequency=period).process_klines(snapshot)
        available_date = bar_dates[end_index]
        current: dict[tuple[str, str, str], Any] = {}
        for mmd in result.mmds:
            if not mmd.bi:
                continue
            current[_mmd_identity(mmd, period)] = mmd

        current_keys = set(current)
        for key in active_keys - current_keys:
            event = by_key[key]
            if event["retracted_date"] is None:
                event["retracted_date"] = available_date

        for key, mmd in current.items():
            if key in by_key:
                if key not in active_keys:
                    by_key[key]["reappearance_count"] += 1
                by_key[key]["last_seen_date"] = available_date
                continue
            anchor_date = key[2]
            anchor_index = date_indexes.get(anchor_date, end_index)
            zs_start_date = (
                _date(mmd.zs.start.k.date, period)
                if mmd.zs and mmd.zs.start else None
            )
            event = {
                "type": key[0],
                "date": anchor_date,
                "anchor_date": anchor_date,
                "available_date": available_date,
                "last_seen_date": available_date,
                "confirmation_lag_bars": max(0, end_index - anchor_index),
                "retracted_date": None,
                "reappearance_count": 0,
                "active_at_end": False,
                "msg": mmd.msg,
                "zs_start_date": zs_start_date,
                "anchor_precedes_center": bool(
                    zs_start_date and anchor_date < zs_start_date
                ),
                # 首次可见日来自逐日快照，执行层无需再过滤最终视角的错配。
                "temporal_mismatch": False,
            }
            events.append(event)
            by_key[key] = event
        active_keys = current_keys

    for key in active_keys:
        by_key[key]["active_at_end"] = True
    return events


def simulate_signal_backtest(
    klines: list[dict[str, Any]],
    signals: list[dict[str, Any]],
    *,
    exclude_temporal_mismatch: bool,
    initial_cash: float = 100_000.0,
    commission_rate: float = 0.0003,
    stamp_duty_rate: float = 0.0005,
) -> dict[str, Any]:
    """按买卖点做只做多、全仓、下一根 K 线开盘成交的探索性回放。"""

    if not klines:
        return {}

    dated_bars = [bar for bar in klines if bar.get("date") and _number(bar.get("open"))]
    cash = float(initial_cash)
    shares = 0
    entry: dict[str, Any] | None = None
    trades: list[dict[str, Any]] = []
    executions: list[dict[str, Any]] = []
    ignored_signals = 0
    unavailable_signals = 0
    filtered_signals = 0
    total_fees = 0.0

    candidates: list[tuple[int, int, dict[str, Any]]] = []
    for order, signal in enumerate(signals):
        if exclude_temporal_mismatch and signal.get("temporal_mismatch"):
            filtered_signals += 1
            continue
        signal_date = signal.get("date")
        available_date = signal.get("available_date") or signal_date
        signal_type = str(signal.get("type", "")).lower()
        if not signal_date or not available_date or not signal_type.endswith(("buy", "sell")):
            ignored_signals += 1
            continue
        # 信号所在 K 线结束后才允许成交，避免直接使用信号 K 线的已知价格。
        execution_index = next(
            (index for index, bar in enumerate(dated_bars) if bar["date"] > available_date),
            None,
        )
        if execution_index is None:
            unavailable_signals += 1
            continue
        candidates.append((execution_index, order, signal))

    candidates.sort(key=lambda item: (item[0], item[1]))
    for execution_index, _, signal in candidates:
        bar = dated_bars[execution_index]
        price = float(bar["open"])
        signal_type = str(signal["type"]).lower()
        if signal_type.endswith("buy"):
            if shares:
                ignored_signals += 1
                continue
            quantity = int(cash / price / 100) * 100
            while quantity > 0:
                amount = quantity * price
                fee = _trade_fee(amount, commission_rate)
                if amount + fee <= cash:
                    break
                quantity -= 100
            if quantity <= 0:
                ignored_signals += 1
                continue
            amount = quantity * price
            fee = _trade_fee(amount, commission_rate)
            cash -= amount + fee
            shares = quantity
            total_fees += fee
            entry = {
                "signal_type": signal["type"],
                "signal_date": signal["date"],
                "available_date": available_date,
                "date": bar["date"],
                "index": execution_index,
                "price": price,
                "shares": shares,
                "fee": fee,
                "temporal_mismatch": bool(signal.get("temporal_mismatch")),
            }
            executions.append({"side": "buy", **entry})
            continue

        if not shares or entry is None:
            ignored_signals += 1
            continue
        amount = shares * price
        commission = _trade_fee(amount, commission_rate)
        stamp_duty = amount * stamp_duty_rate
        fee = commission + stamp_duty
        proceeds = amount - fee
        cash += proceeds
        total_fees += fee
        invested = entry["shares"] * entry["price"] + entry["fee"]
        profit = proceeds - invested
        trades.append(
            {
                "status": "closed",
                "buy_signal": entry["signal_type"],
                "sell_signal": signal["type"],
                "buy_signal_date": entry["signal_date"],
                "sell_signal_date": signal["date"],
                "buy_available_date": entry["available_date"],
                "sell_available_date": signal.get("available_date") or signal["date"],
                "buy_date": entry["date"],
                "sell_date": bar["date"],
                "buy_price": round(entry["price"], 4),
                "sell_price": round(price, 4),
                "shares": shares,
                "holding_bars": execution_index - entry["index"],
                "profit": round(profit, 2),
                "return_pct": round(profit / invested * 100, 2),
                "fees": round(entry["fee"] + fee, 2),
                "temporal_mismatch": bool(
                    entry["temporal_mismatch"] or signal.get("temporal_mismatch")
                ),
            }
        )
        executions.append(
            {
                "side": "sell",
                "signal_type": signal["type"],
                "signal_date": signal["date"],
                "available_date": signal.get("available_date") or signal["date"],
                "date": bar["date"],
                "index": execution_index,
                "price": price,
                "shares": shares,
                "fee": fee,
                "temporal_mismatch": bool(signal.get("temporal_mismatch")),
            }
        )
        shares = 0
        entry = None

    last_bar = dated_bars[-1]
    last_close = float(last_bar["close"])
    liquidation_fee = 0.0
    if shares:
        amount = shares * last_close
        liquidation_fee = _trade_fee(amount, commission_rate) + amount * stamp_duty_rate
    final_value = cash + shares * last_close - liquidation_fee

    equity_curve = []
    peak = initial_cash
    max_drawdown = 0.0
    execution_cursor = 0
    curve_cash = float(initial_cash)
    curve_shares = 0
    for index, bar in enumerate(dated_bars):
        while execution_cursor < len(executions) and executions[execution_cursor]["index"] == index:
            action = executions[execution_cursor]
            if action["side"] == "buy":
                curve_cash -= action["shares"] * action["price"] + action["fee"]
                curve_shares = action["shares"]
            else:
                curve_cash += action["shares"] * action["price"] - action["fee"]
                curve_shares = 0
            execution_cursor += 1
        equity = curve_cash + curve_shares * float(bar["close"])
        peak = max(peak, equity)
        drawdown = (equity / peak - 1) * 100 if peak else 0.0
        max_drawdown = min(max_drawdown, drawdown)
        equity_curve.append({"date": bar["date"], "value": round(equity, 2)})

    wins = sum(trade["profit"] > 0 for trade in trades)
    benchmark_start = float(dated_bars[0]["close"])
    benchmark_return = (last_close / benchmark_start - 1) * 100 if benchmark_start else 0.0
    return {
        "mode": "time_safe" if exclude_temporal_mismatch else "original",
        "initial_cash": round(initial_cash, 2),
        "final_value": round(final_value, 2),
        "profit": round(final_value - initial_cash, 2),
        "return_pct": round((final_value / initial_cash - 1) * 100, 2),
        "benchmark_return_pct": round(benchmark_return, 2),
        "excess_return_pct": round((final_value / initial_cash - 1) * 100 - benchmark_return, 2),
        "max_drawdown_pct": round(max_drawdown, 2),
        "closed_trades": len(trades),
        "winning_trades": wins,
        "win_rate_pct": round(wins / len(trades) * 100, 2) if trades else None,
        "open_position": bool(shares),
        "open_shares": shares,
        "open_entry": entry,
        "estimated_liquidation_fee": round(liquidation_fee, 2),
        "total_realized_fees": round(total_fees, 2),
        "signals_used": len(executions),
        "signals_filtered": filtered_signals,
        "signals_ignored": ignored_signals,
        "signals_without_next_bar": unavailable_signals,
        "start_date": dated_bars[0]["date"],
        "end_date": last_bar["date"],
        "trades": trades,
        "equity_curve": equity_curve,
    }


class ChanlunAnalysisService:
    """串行请求 easy-tdx，并将分析结果整理成页面 API 数据。"""

    def __init__(
        self,
        settings: Any,
        names: dict[str, str] | None = None,
        *,
        runtime_loader: Callable[[], dict[str, Any]] = _load_runtime,
        analyser_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.settings = settings
        self.names = names or {}
        self._runtime_loader = runtime_loader
        self._analyser_factory = analyser_factory
        self._lock = Lock()

    @staticmethod
    def validate(
        symbol: str, market: str, period: str, adjustment: str, count: int
    ) -> tuple[str, str, str, str, int]:
        symbol = symbol.strip()
        if not re.fullmatch(r"\d{6}", symbol):
            raise ValueError("股票代码必须是6位数字，例如 002475")
        market = market.strip().upper() or infer_market(symbol)
        if market == "AUTO":
            market = infer_market(symbol)
        if market not in MARKETS:
            raise ValueError("市场只能是 AUTO、SZ、SH 或 BJ")
        period = period.strip().upper()
        if period not in PERIODS:
            raise ValueError("不支持这个K线周期")
        adjustment = adjustment.strip().upper()
        if adjustment not in ADJUSTMENTS:
            raise ValueError("复权方式只能是 NONE、QFQ 或 HFQ")
        if not 100 <= count <= 2000:
            raise ValueError("K线数量必须在100到2000之间")
        return symbol, market, period, adjustment, count

    def analyse(
        self,
        symbol: str,
        market: str = "AUTO",
        period: str = "DAILY",
        adjustment: str = "QFQ",
        count: int = 800,
    ) -> dict[str, Any]:
        symbol, market, period, adjustment, count = self.validate(
            symbol, market, period, adjustment, count
        )
        # easy-tdx 的服务器选择会写用户配置，且行情服务不适合由页面并发打连接。
        with self._lock:
            return self._analyse_locked(symbol, market, period, adjustment, count)

    def _analyse_locked(
        self, symbol: str, market: str, period: str, adjustment: str, count: int
    ) -> dict[str, Any]:
        runtime = self._runtime_loader()
        if self._analyser_factory is None:
            from easy_tdx.chanlun.analyser import ChanlunAnalyser

            analyser_factory = ChanlunAnalyser
        else:
            analyser_factory = self._analyser_factory

        client_class = runtime["MacClient"]
        if self.settings.easy_tdx_mac_host:
            client = client_class(
                host=self.settings.easy_tdx_mac_host,
                port=self.settings.easy_tdx_port,
                timeout=self.settings.easy_tdx_timeout,
                heartbeat_interval=0,
            )
        else:
            client = client_class.from_best_host()
        try:
            client.connect()
            frame = client.get_stock_kline(
                market=MARKETS[market],
                code=symbol,
                period=getattr(runtime["Period"], PERIOD_ATTRIBUTES[period]),
                start=0,
                count=count,
                adjust=getattr(runtime["Adjust"], adjustment),
            )
        except Exception as exc:
            raise ProviderError(f"easy-tdx 查询 {market}{symbol} 失败：{exc}") from exc
        finally:
            try:
                client.close()
            except Exception:
                pass

        if frame is None or frame.empty:
            raise ProviderError(f"easy-tdx 没有返回 {market}{symbol} 的行情")

        incomplete_bar_excluded = False
        if period == "DAILY":
            date_column = "datetime" if "datetime" in frame.columns else "date"
            latest_row = frame.iloc[-1]
            latest_timestamp = latest_row[date_column]
            latest_day = latest_timestamp.date() if hasattr(latest_timestamp, "date") else None
            volume = latest_row.get("vol", latest_row.get("volume", None))
            now = datetime.now()
            if latest_day == now.date() and (now.time() < time(15, 5) or not _number(volume)):
                frame = frame.iloc[:-1].reset_index(drop=True)
                incomplete_bar_excluded = True
        if frame.empty:
            raise ProviderError(f"{market}{symbol} 只有未完成的当日行情，无法分析")

        date_column = "datetime" if "datetime" in frame.columns else "date"
        frame = frame.sort_values(date_column).reset_index(drop=True)

        result = analyser_factory(code=symbol, frequency=period).process_klines(frame)
        replay_started = perf_counter()
        replay_signals = replay_chanlun_signals(
            frame,
            analyser_factory=analyser_factory,
            code=symbol,
            period=period,
        )
        payload = result.to_dict()
        klines = [
            {
                "date": _date(k.date, period),
                "open": _number(k.open),
                "high": _number(k.high),
                "low": _number(k.low),
                "close": _number(k.close),
                "volume": _number(k.amount),
            }
            for k in result.klines
        ]
        fractals = [
            {
                "type": fx.fx_type.value,
                "date": _date(fx.k.date, period),
                "value": _number(fx.val),
                "done": fx.done,
            }
            for fx in result.fractals
        ]

        latest_data_date = klines[-1]["date"] if klines else None
        latest_structure_date = payload["bis"][-1]["end_date"] if payload["bis"] else None
        structure_lag = (
            sum(k["date"] > latest_structure_date for k in klines)
            if latest_structure_date else len(klines)
        )
        temporal_mismatches = sum(
            bool(mmd.bi and mmd.zs and mmd.zs.start and mmd.zs.start.k.date > mmd.bi.end.k.date)
            for mmd in result.mmds
        )
        payload["mmds"] = [
            {
                "type": mmd.mmd_type.value,
                "date": _date(mmd.bi.end.k.date, period) if mmd.bi else None,
                "msg": mmd.msg,
                "temporal_mismatch": bool(
                    mmd.bi
                    and mmd.zs
                    and mmd.zs.start
                    and mmd.zs.start.k.date > mmd.bi.end.k.date
                ),
            }
            for mmd in result.mmds
        ]
        payload["backtests"] = {
            "original": simulate_signal_backtest(
                klines, payload["mmds"], exclude_temporal_mismatch=False
            ),
            "time_safe": simulate_signal_backtest(
                klines, replay_signals, exclude_temporal_mismatch=True
            ),
        }
        payload["replay_signals"] = replay_signals
        payload["replay_diagnostics"] = {
            "signal_count": len(replay_signals),
            "retracted_count": sum(bool(item["retracted_date"]) for item in replay_signals),
            "active_count": sum(bool(item["active_at_end"]) for item in replay_signals),
            "max_confirmation_lag_bars": max(
                (item["confirmation_lag_bars"] for item in replay_signals), default=0
            ),
            "elapsed_seconds": round(perf_counter() - replay_started, 2),
        }

        latest_center = payload["zss"][-1] if payload["zss"] else None
        latest_close = klines[-1]["close"] if klines else None
        center_position = None
        if latest_center and latest_close is not None:
            if latest_close > latest_center["zg"]:
                center_position = "above"
            elif latest_close < latest_center["zd"]:
                center_position = "below"
            else:
                center_position = "inside"

        payload.update(
            {
                "symbol": symbol,
                "market": market,
                "name": self.names.get(symbol, ""),
                "adjustment": adjustment,
                "requested_count": count,
                "fetched_at": datetime.now().isoformat(timespec="seconds"),
                "latest_data_date": latest_data_date,
                "latest_structure_date": latest_structure_date,
                "structure_lag_bars": structure_lag,
                "latest_close": latest_close,
                "latest_center": latest_center,
                "center_position": center_position,
                "temporal_mismatch_count": temporal_mismatches,
                "incomplete_bar_excluded": incomplete_bar_excluded,
                "klines": klines,
                "fractals": fractals,
                "warnings": [
                    "笔和中枢用于结构观察；最新结构通常晚于最新行情，这是确认规则造成的。",
                    "模拟收益按信号后下一根K线开盘成交、10万元初始资金、全仓且只做多计算；它是历史回放，不是交易指令。",
                    "严格逐日回放使用每个买卖点的首次可见日期，并在下一根K线开盘成交；图形锚点日期只用于展示。",
                ],
            }
        )
        if temporal_mismatches:
            payload["warnings"].append(
                f"检测到 {temporal_mismatches} 个买卖点引用了其发生日期之后的中枢；相关信号存在时间错配。"
            )
        if incomplete_bar_excluded:
            payload["warnings"].insert(
                0, "行情源返回了今天尚未收盘的日K；本次已排除该K线，避免未完成价格改变结构。"
            )
        return payload
