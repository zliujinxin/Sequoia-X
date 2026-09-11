"""对 Sequoia-X 行情策略做无未来数据的历史事件验证。"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from sequoia_x.core.logger import get_logger
from sequoia_x.core.stock_profile import (
    daily_price_limit_ratio,
    matches_output_filter,
    stock_profile,
)
from sequoia_x.reporting.catalog import STRATEGIES
from sequoia_x.reporting.service import write_atomic

logger = get_logger(__name__)

BASELINE_STRATEGIES = [
    "MaVolumeStrategy",
    "TurtleTradeStrategy",
    "HighTightFlagStrategy",
    "LimitUpShakeoutStrategy",
    "UptrendLimitDownStrategy",
    "RpsBreakoutStrategy",
]
RESEARCH_STRATEGIES = [
    "MaVolumeV2Strategy",
    "HighTightFlagBreakoutV2Strategy",
    "CompositeTrendRankStrategy",
    "PriceQualityMultiFactorV1Strategy",
]
MARKET_STRATEGIES = BASELINE_STRATEGIES + RESEARCH_STRATEGIES

QUALITY_ENTRY_RANK = 20
QUALITY_EXIT_RANK = 40
QUALITY_REBALANCE_DAYS = 5


def _rolling(grouped: Any, column: str, window: int, function: str) -> pd.Series:
    values = grouped[column].rolling(window, min_periods=window)
    result = getattr(values, function)()
    return result.reset_index(level=0, drop=True)


def _non_overlapping(frame: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """同一股票持有期内只保留首次信号，避免连续命中被重复统计。"""

    if frame.empty:
        return frame
    keep: list[int] = []
    last_position: dict[str, int] = {}
    for row in frame[["symbol", "bar_index"]].itertuples():
        previous = last_position.get(row.symbol)
        if previous is None or row.bar_index - previous >= horizon:
            keep.append(row.Index)
            last_position[row.symbol] = row.bar_index
    return frame.loc[keep]


def _number(value: Any, digits: int = 3) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return round(result, digits) if math.isfinite(result) else None


def _summary(values: pd.DataFrame) -> dict[str, Any]:
    if values.empty:
        return {
            "observations": 0,
            "mean_return_pct": None,
            "median_return_pct": None,
            "win_rate_pct": None,
            "mean_excess_pct": None,
            "median_excess_pct": None,
            "beat_market_pct": None,
        }
    return {
        "observations": len(values),
        "mean_return_pct": _number(values["return"].mean() * 100),
        "median_return_pct": _number(values["return"].median() * 100),
        "win_rate_pct": _number((values["return"] > 0).mean() * 100, 2),
        "mean_excess_pct": _number(values["excess"].mean() * 100),
        "median_excess_pct": _number(values["excess"].median() * 100),
        "beat_market_pct": _number((values["excess"] > 0).mean() * 100, 2),
    }


def _positive_evidence(metrics: dict[str, Any]) -> bool:
    return (
        metrics["median_excess_pct"] is not None
        and metrics["median_excess_pct"] > 0
        and metrics["beat_market_pct"] is not None
        and metrics["beat_market_pct"] > 50
    )


def _evidence_label(
    development: dict[str, Any], holdout: dict[str, Any]
) -> str:
    if development["observations"] < 100 or holdout["observations"] < 100:
        return "样本不足"
    if _positive_evidence(development) and _positive_evidence(holdout):
        return "初步正向"
    if _positive_evidence(development) or _positive_evidence(holdout):
        return "结果分化"
    return "暂未证明"


def _portfolio_summary(periods: pd.DataFrame) -> dict[str, Any]:
    """汇总按调仓周期记录的组合收益。"""

    if periods.empty:
        return {
            "periods": 0,
            "total_return_pct": None,
            "benchmark_return_pct": None,
            "excess_return_pct": None,
            "annualized_return_pct": None,
            "annualized_volatility_pct": None,
            "sharpe": None,
            "max_drawdown_pct": None,
            "positive_period_pct": None,
            "average_holdings": None,
            "average_turnover_pct": None,
        }
    returns = periods["return"].astype(float).fillna(0.0)
    benchmark = periods["benchmark_return"].astype(float).fillna(0.0)
    equity = (1 + returns).cumprod()
    benchmark_equity = (1 + benchmark).cumprod()
    drawdown = equity / equity.cummax() - 1
    periods_per_year = 252 / QUALITY_REBALANCE_DAYS
    annualized_return = equity.iloc[-1] ** (periods_per_year / len(periods)) - 1
    volatility = returns.std(ddof=0) * math.sqrt(periods_per_year)
    sharpe = (
        returns.mean() / returns.std(ddof=0) * math.sqrt(periods_per_year)
        if returns.std(ddof=0) > 0
        else None
    )
    return {
        "periods": len(periods),
        "total_return_pct": _number((equity.iloc[-1] - 1) * 100),
        "benchmark_return_pct": _number((benchmark_equity.iloc[-1] - 1) * 100),
        "excess_return_pct": _number(
            (equity.iloc[-1] / benchmark_equity.iloc[-1] - 1) * 100
        ),
        "annualized_return_pct": _number(annualized_return * 100),
        "annualized_volatility_pct": _number(volatility * 100),
        "sharpe": _number(sharpe),
        "max_drawdown_pct": _number(drawdown.min() * 100),
        "positive_period_pct": _number((returns > 0).mean() * 100, 2),
        "average_holdings": _number(periods["holdings"].mean(), 1),
        "average_turnover_pct": _number(periods["turnover"].mean() * 100, 2),
    }


def _portfolio_evidence(
    development: dict[str, Any], holdout: dict[str, Any]
) -> str:
    if development["periods"] < 24 or holdout["periods"] < 12:
        return "样本不足"
    development_positive = (
        development["excess_return_pct"] is not None
        and development["excess_return_pct"] > 0
        and development["sharpe"] is not None
        and development["sharpe"] > 0
    )
    holdout_positive = (
        holdout["excess_return_pct"] is not None
        and holdout["excess_return_pct"] > 0
        and holdout["sharpe"] is not None
        and holdout["sharpe"] > 0
    )
    if development_positive and holdout_positive:
        return "初步正向"
    if development_positive or holdout_positive:
        return "结果分化"
    return "暂未证明"


def _ranked_portfolio_periods(
    data: pd.DataFrame,
    complete_dates: pd.Index,
    *,
    commission_rate: float,
    stamp_duty_rate: float,
    slippage_rate: float,
) -> tuple[pd.DataFrame, list[str]]:
    """按收盘排名、下一交易日开盘成交，模拟每周等权组合。"""

    columns = [
        "open",
        "close",
        "quality_rank",
        "quality_pool",
        "quality_market",
        "board_name",
        "output_allowed",
    ]
    lookup = data.set_index(["date", "symbol"])[columns].sort_index()
    dates = [str(value) for value in complete_dates]
    current_weights: dict[str, float] = {}
    periods: list[dict[str, Any]] = []
    latest_holdings: list[str] = []

    for signal_position in range(
        0, len(dates) - QUALITY_REBALANCE_DAYS - 1, QUALITY_REBALANCE_DAYS
    ):
        signal_date = dates[signal_position]
        execution_date = dates[signal_position + 1]
        exit_date = dates[signal_position + 1 + QUALITY_REBALANCE_DAYS]
        try:
            signal_frame = lookup.xs(signal_date, level="date")
            execution_frame = lookup.xs(execution_date, level="date")
            exit_frame = lookup.xs(exit_date, level="date")
        except KeyError:
            continue

        candidates = signal_frame[
            signal_frame["quality_pool"].fillna(False)
            & signal_frame["output_allowed"].fillna(False)
            & signal_frame["quality_rank"].notna()
        ].sort_values("quality_rank")
        market_open = bool(signal_frame["quality_market"].fillna(False).any())
        if candidates.empty and not current_weights:
            continue

        target: list[str] = []
        if market_open:
            board_count = max(1, candidates["board_name"].nunique())
            board_cap = (
                QUALITY_ENTRY_RANK
                if board_count == 1
                else max(
                    math.ceil(QUALITY_ENTRY_RANK / board_count),
                    math.ceil(QUALITY_ENTRY_RANK * 0.4),
                )
            )
            selected_by_board: dict[str, int] = {}

            retained = candidates[
                candidates.index.isin(current_weights)
                & candidates["quality_rank"].le(QUALITY_EXIT_RANK)
            ]
            for symbol, row in retained.iterrows():
                board = str(row["board_name"])
                if selected_by_board.get(board, 0) >= board_cap:
                    continue
                target.append(str(symbol))
                selected_by_board[board] = selected_by_board.get(board, 0) + 1

            for symbol, row in candidates.iterrows():
                symbol = str(symbol)
                if symbol in target:
                    continue
                board = str(row["board_name"])
                if selected_by_board.get(board, 0) >= board_cap:
                    continue
                target.append(symbol)
                selected_by_board[board] = selected_by_board.get(board, 0) + 1
                if len(target) >= QUALITY_ENTRY_RANK:
                    break

        target = [
            symbol
            for symbol in target
            if symbol in execution_frame.index
            and pd.notna(execution_frame.at[symbol, "open"])
            and float(execution_frame.at[symbol, "open"]) > 0
        ]
        target_weights = (
            {symbol: 1 / len(target) for symbol in target} if target else {}
        )
        all_symbols = set(current_weights) | set(target_weights)
        buy_turnover = sum(
            max(0.0, target_weights.get(symbol, 0.0) - current_weights.get(symbol, 0.0))
            for symbol in all_symbols
        )
        sell_turnover = sum(
            max(0.0, current_weights.get(symbol, 0.0) - target_weights.get(symbol, 0.0))
            for symbol in all_symbols
        )
        transaction_cost = (
            buy_turnover * (commission_rate + slippage_rate)
            + sell_turnover * (commission_rate + stamp_duty_rate + slippage_rate)
        )

        holding_returns: list[float] = []
        for symbol in target:
            entry_open = float(execution_frame.at[symbol, "open"])
            if symbol in exit_frame.index and pd.notna(exit_frame.at[symbol, "open"]):
                exit_open = float(exit_frame.at[symbol, "open"])
                if exit_open > 0:
                    holding_returns.append(exit_open / entry_open - 1)
                    continue
            holding_returns.append(0.0)
        gross_return = float(np.mean(holding_returns)) if holding_returns else 0.0
        net_return = (1 - transaction_cost) * (1 + gross_return) - 1

        benchmark_frame = execution_frame[
            execution_frame["output_allowed"].fillna(False)
            & execution_frame["open"].gt(0)
        ][["open"]].join(
            exit_frame[["open"]].rename(columns={"open": "exit_open"}), how="inner"
        )
        benchmark_frame = benchmark_frame[benchmark_frame["exit_open"].gt(0)]
        benchmark_return = (
            float((benchmark_frame["exit_open"] / benchmark_frame["open"] - 1).mean())
            if not benchmark_frame.empty
            else 0.0
        )
        periods.append(
            {
                "signal_date": signal_date,
                "execution_date": execution_date,
                "exit_date": exit_date,
                "return": net_return,
                "benchmark_return": benchmark_return,
                "holdings": len(target),
                "turnover": (buy_turnover + sell_turnover) / 2,
                "market_open": market_open,
            }
        )
        current_weights = target_weights
        latest_holdings = target

    return pd.DataFrame(periods), latest_holdings


class StrategyValidationService:
    """从本地 SQLite 构造逐日信号并生成可复查的验证报告。"""

    commission_rate = 0.0003
    stamp_duty_rate = 0.0005
    slippage_rate = 0.0005

    def __init__(self, engine: Any, settings: Any) -> None:
        self.engine = engine
        self.settings = settings
        self.directory = Path(settings.report_dir)

    def _load(self) -> pd.DataFrame:
        with sqlite3.connect(self.engine.db_path) as connection:
            frame = pd.read_sql_query(
                """SELECT symbol,date,open,high,low,close,volume,turnover
                   FROM stock_daily ORDER BY symbol,date""",
                connection,
            )
        for column in ["open", "high", "low", "close", "volume", "turnover"]:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame = frame.dropna(
            subset=["symbol", "date", "open", "high", "low", "close", "volume"]
        ).reset_index(drop=True)
        return frame

    def run(self, horizons: tuple[int, ...] = (5, 20)) -> dict[str, Any]:
        if not horizons or any(value < 1 or value > 120 for value in horizons):
            raise ValueError("验证持有周期必须在1到120个交易日之间")
        data = self._load()
        if data.empty:
            raise ValueError("本地数据库没有可验证的日线")
        logger.info(
            f"策略验证读取完成：{len(data)} 条日线，{data['symbol'].nunique()} 只股票"
        )

        coverage = data.groupby("date")["symbol"].nunique().sort_index()
        peak_count = int(coverage.max())
        minimum_count = math.ceil(peak_count * self.settings.analysis_min_coverage)
        complete_dates = coverage[coverage >= minimum_count].index
        complete_mask = data["date"].isin(complete_dates)
        complete_timestamps = pd.to_datetime(complete_dates)
        latest_year = int(complete_timestamps.max().year)
        latest_year_dates = complete_dates[complete_timestamps.year == latest_year]
        if len(latest_year_dates) >= 120:
            holdout_start = str(latest_year_dates.min())
        else:
            split_position = max(1, math.floor(len(complete_dates) * 0.8))
            holdout_start = str(complete_dates[split_position])

        grouped = data.groupby("symbol", sort=False)
        data["bar_index"] = grouped.cumcount()
        previous_close = grouped["close"].shift(1)
        previous2_close = grouped["close"].shift(2)
        previous_volume = grouped["volume"].shift(1)
        ma5 = _rolling(grouped, "close", 5, "mean")
        ma20 = _rolling(grouped, "close", 20, "mean")
        ma60 = _rolling(grouped, "close", 60, "mean")
        volume20 = _rolling(grouped, "volume", 20, "mean")
        turnover20 = _rolling(grouped, "turnover", 20, "mean")
        prior_volume20 = grouped["volume"].shift(1).groupby(data["symbol"]).rolling(
            20, min_periods=20
        ).mean().reset_index(level=0, drop=True)
        prior_high20 = grouped["high"].shift(1).groupby(data["symbol"]).rolling(
            20, min_periods=20
        ).max().reset_index(level=0, drop=True)
        prior_high40 = grouped["high"].shift(1).groupby(data["symbol"]).rolling(
            40, min_periods=40
        ).max().reset_index(level=0, drop=True)
        prior_low40 = grouped["low"].shift(1).groupby(data["symbol"]).rolling(
            40, min_periods=40
        ).min().reset_index(level=0, drop=True)
        prior_high10 = grouped["high"].shift(1).groupby(data["symbol"]).rolling(
            10, min_periods=10
        ).max().reset_index(level=0, drop=True)
        prior_low10 = grouped["low"].shift(1).groupby(data["symbol"]).rolling(
            10, min_periods=10
        ).min().reset_index(level=0, drop=True)
        prior_volume10 = grouped["volume"].shift(1).groupby(data["symbol"]).rolling(
            10, min_periods=10
        ).mean().reset_index(level=0, drop=True)
        high40 = _rolling(grouped, "high", 40, "max")
        low40 = _rolling(grouped, "low", 40, "min")
        high10 = _rolling(grouped, "high", 10, "max")
        low10 = _rolling(grouped, "low", 10, "min")
        return120 = data["close"] / grouped["close"].shift(120) - 1
        high120 = grouped["high"].rolling(120, min_periods=60).max().reset_index(
            level=0, drop=True
        )
        rps = return120.groupby(data["date"]).rank(pct=True) * 100
        return20 = data["close"] / grouped["close"].shift(20) - 1
        return60 = data["close"] / grouped["close"].shift(60) - 1
        daily_return = data["close"] / previous_close - 1
        volatility20 = daily_return.groupby(data["symbol"]).rolling(
            20, min_periods=20
        ).std().reset_index(level=0, drop=True)
        breadth_above_ma20 = (data["close"] > ma20).where(complete_mask).groupby(
            data["date"]
        ).transform("mean")
        market_median_return20 = return20.where(complete_mask).groupby(
            data["date"]
        ).transform("median")
        favorable_market = (
            (breadth_above_ma20 >= 0.55) & (market_median_return20 > 0)
        )
        rank120 = return120.where(complete_mask).groupby(data["date"]).rank(pct=True)
        rank60 = return60.where(complete_mask).groupby(data["date"]).rank(pct=True)
        rank20 = return20.where(complete_mask).groupby(data["date"]).rank(pct=True)
        trend_strength = (data["close"] / ma60 - 1).where(complete_mask)
        rank_trend = trend_strength.groupby(data["date"]).rank(pct=True)
        rank_low_volatility = 1 - volatility20.where(complete_mask).groupby(
            data["date"]
        ).rank(pct=True)
        rank_liquidity = turnover20.where(complete_mask).groupby(
            data["date"]
        ).rank(pct=True)
        composite_score = (
            rank120 * 0.30
            + rank60 * 0.20
            + rank20 * 0.15
            + rank_trend * 0.15
            + rank_low_volatility * 0.15
            + rank_liquidity * 0.05
        )
        composite_pool = (
            (data["close"] > ma20)
            & (ma20 > ma60)
            & (turnover20 >= 50_000_000)
            & return120.notna()
        )
        composite_daily_rank = composite_score.where(composite_pool).groupby(
            data["date"]
        ).rank(method="first", ascending=False)

        names = self.engine.get_stock_names()
        ratios = {
            symbol: daily_price_limit_ratio(symbol, names.get(symbol, ""))
            for symbol in data["symbol"].unique()
        }
        limit_ratio = data["symbol"].map(ratios)
        profiles = {
            symbol: stock_profile(symbol, names.get(symbol, ""))
            for symbol in data["symbol"].unique()
        }
        board_names = {
            symbol: profile["board_name"] for symbol, profile in profiles.items()
        }
        data["board_name"] = data["symbol"].map(board_names)
        allowed = {
            symbol for symbol, profile in profiles.items()
            if matches_output_filter(profile, self.settings)
        }
        output_mask = data["symbol"].isin(allowed)
        logger.info(
            f"策略验证口径：{len(complete_dates)} 个完整交易日，"
            f"输出股票池 {len(allowed)} 只"
        )

        # 价格质量多因子 V1：先用本地已有的日线和成交额验证框架。
        # 财务质量与行业中性化必须等逐日财务快照、行业历史分类补齐后再加入。
        return120_skip20 = grouped["close"].shift(20) / grouped["close"].shift(120) - 1
        return60_skip5 = grouped["close"].shift(5) / grouped["close"].shift(60) - 1
        return5 = data["close"] / grouped["close"].shift(5) - 1
        positive_ratio60 = daily_return.gt(0).astype(float).groupby(
            data["symbol"]
        ).rolling(60, min_periods=60).mean().reset_index(level=0, drop=True)
        downside_deviation60 = daily_return.clip(upper=0).groupby(
            data["symbol"]
        ).rolling(60, min_periods=60).std().reset_index(level=0, drop=True)
        close_high120 = grouped["close"].rolling(
            120, min_periods=120
        ).max().reset_index(level=0, drop=True)
        drawdown_resilience = data["close"] / close_high120
        turnover_std20 = grouped["turnover"].rolling(
            20, min_periods=20
        ).std().reset_index(level=0, drop=True)
        turnover_stability = turnover_std20 / turnover20.replace(0, np.nan)

        def cross_rank(values: pd.Series, *, ascending: bool = True) -> pd.Series:
            return values.where(complete_mask).groupby(data["date"]).rank(
                pct=True, ascending=ascending
            )

        quality_components = {
            "momentum_120_skip_20": cross_rank(return120_skip20),
            "momentum_60_skip_5": cross_rank(return60_skip5),
            "trend_consistency": cross_rank(positive_ratio60),
            "low_downside_volatility": cross_rank(
                downside_deviation60, ascending=False
            ),
            "drawdown_resilience": cross_rank(drawdown_resilience),
            "liquidity": cross_rank(turnover20),
            "turnover_stability": cross_rank(
                turnover_stability, ascending=False
            ),
        }
        quality_raw_score = (
            quality_components["momentum_120_skip_20"] * 0.25
            + quality_components["momentum_60_skip_5"] * 0.20
            + quality_components["trend_consistency"] * 0.15
            + quality_components["low_downside_volatility"] * 0.15
            + quality_components["drawdown_resilience"] * 0.10
            + quality_components["liquidity"] * 0.10
            + quality_components["turnover_stability"] * 0.05
            - cross_rank(return5) * 0.05
        )
        quality_global_score = cross_rank(quality_raw_score)
        quality_board_score = quality_raw_score.where(complete_mask).groupby(
            [data["date"], data["board_name"]]
        ).rank(pct=True)
        quality_score = quality_global_score * 0.8 + quality_board_score * 0.2
        quality_market = (
            (breadth_above_ma20 >= 0.50) & (market_median_return20 >= -0.02)
        )
        quality_pool = (
            (data["close"] >= 3)
            & (data["close"] > ma20)
            & (ma20 > ma60)
            & (ma60 > ma60.groupby(data["symbol"]).shift(20))
            & (data["close"] <= ma20 * 1.15)
            & (turnover20 >= 50_000_000)
            & return120_skip20.notna()
            & downside_deviation60.notna()
        )
        quality_daily_rank = quality_score.where(quality_pool).groupby(
            data["date"]
        ).rank(method="first", ascending=False)
        data["quality_rank"] = quality_daily_rank
        data["quality_pool"] = quality_pool
        data["quality_market"] = quality_market
        data["output_allowed"] = output_mask

        ma_volume_signal = (
            ma5.groupby(data["symbol"]).shift(1)
            < ma20.groupby(data["symbol"]).shift(1)
        ) & (ma5 > ma20) & (data["volume"] > volume20 * 1.5)
        high_tight_setup = (
            (prior_high40 / prior_low40 > 1.6)
            & (prior_high10 / prior_low10 < 1.15)
            & (prior_low10 >= prior_high40 * 0.8)
            & (prior_volume10 < prior_volume20 * 0.75)
        )
        signals = {
            "MaVolumeStrategy": ma_volume_signal,
            "TurtleTradeStrategy": (
                (data["close"] > prior_high20)
                & (data["turnover"] > 100_000_000)
                & (data["close"] > data["open"])
                & (data["close"] > previous_close)
            ),
            "HighTightFlagStrategy": (
                (high40 / low40 > 1.6)
                & (high10 / low10 < 1.15)
                & (low10 >= high40 * 0.8)
                & (data["volume"] < prior_volume20 * 0.6)
            ),
            "LimitUpShakeoutStrategy": (
                (previous_close / previous2_close - 1 >= limit_ratio - 0.005)
                & (data["close"] < data["open"])
                & (data["volume"] > previous_volume * 2)
                & (data["low"] >= previous_close)
            ),
            "UptrendLimitDownStrategy": (
                (ma20.groupby(data["symbol"]).shift(1)
                 > ma60.groupby(data["symbol"]).shift(1))
                & (data["close"] / previous_close - 1 <= -(limit_ratio - 0.005))
                & (data["volume"] > volume20 * 2)
            ),
            "RpsBreakoutStrategy": (rps >= 90) & (data["close"] >= high120 * 0.90),
            "MaVolumeV2Strategy": (
                ma_volume_signal
                & favorable_market
                & (data["close"] > ma60)
                & (ma20 > ma20.groupby(data["symbol"]).shift(5))
                & (rps >= 70)
                & (data["turnover"] >= 50_000_000)
                & (data["close"] <= ma20 * 1.10)
            ),
            "HighTightFlagBreakoutV2Strategy": (
                high_tight_setup
                & favorable_market
                & (data["close"] > prior_high10)
                & (data["close"] > data["open"])
                & (data["volume"] > prior_volume20 * 1.2)
                & (data["turnover"] >= 50_000_000)
                & (rps >= 80)
            ),
            "CompositeTrendRankStrategy": composite_daily_rank <= 20,
            "PriceQualityMultiFactorV1Strategy": (
                (quality_daily_rank <= QUALITY_ENTRY_RANK) & quality_market
            ),
        }

        regime_by_date = pd.DataFrame(
            {
                "date": data["date"],
                "breadth": breadth_above_ma20,
                "median_return20": market_median_return20,
                "favorable": favorable_market,
            }
        ).loc[complete_mask].drop_duplicates("date")

        next_open = grouped["open"].shift(-1)
        next_date = grouped["date"].shift(-1)
        date_gap = (
            pd.to_datetime(next_date, errors="coerce")
            - pd.to_datetime(data["date"], errors="coerce")
        ).dt.days
        entry_valid = next_open.gt(0) & date_gap.le(7)
        result: dict[str, Any] = {}
        for strategy_name in MARKET_STRATEGIES:
            logger.info(f"逐日验证：{STRATEGIES[strategy_name]['name']}")
            strategy_result: dict[str, Any] = {
                "name": STRATEGIES[strategy_name]["name"],
                "description": STRATEGIES[strategy_name]["description"],
                "stage": "研究候选" if strategy_name in RESEARCH_STRATEGIES else "现有基准",
                "horizons": {},
                "years": {},
                "boards": {},
                "regimes": {},
                "development": {},
                "holdout": {},
            }
            base_signal = signals[strategy_name].fillna(False) & complete_mask & output_mask
            for horizon in horizons:
                exit_close = grouped["close"].shift(-horizon)
                entry_cost = next_open * (
                    1 + self.commission_rate + self.slippage_rate
                )
                exit_value = exit_close * (
                    1 - self.commission_rate - self.stamp_duty_rate - self.slippage_rate
                )
                net_return = exit_value / entry_cost - 1
                valid_return = entry_valid & net_return.replace(
                    [np.inf, -np.inf], np.nan
                ).notna()
                market_return = net_return.where(
                    complete_mask & valid_return
                ).groupby(data["date"]).transform("mean")
                observations = data.loc[
                    base_signal & valid_return,
                    ["symbol", "date", "bar_index", "board_name"],
                ].copy()
                observations["return"] = net_return.loc[observations.index]
                observations["excess"] = (
                    net_return.loc[observations.index]
                    - market_return.loc[observations.index]
                )
                observations["favorable_market"] = favorable_market.loc[
                    observations.index
                ]
                observations = _non_overlapping(observations, horizon)
                metrics = _summary(observations)
                strategy_result["horizons"][str(horizon)] = metrics

                if horizon == max(horizons):
                    years = pd.to_datetime(observations["date"]).dt.year
                    strategy_result["years"] = {
                        str(year): _summary(group)
                        for year, group in observations.groupby(years)
                    }
                    strategy_result["boards"] = {
                        str(board): _summary(group)
                        for board, group in observations.groupby("board_name")
                    }
                    strategy_result["regimes"] = {
                        "市场有利": _summary(
                            observations[observations["favorable_market"]]
                        ),
                        "市场不利": _summary(
                            observations[~observations["favorable_market"]]
                        ),
                    }
                    development = _summary(
                        observations[observations["date"] < holdout_start]
                    )
                    holdout = _summary(
                        observations[observations["date"] >= holdout_start]
                    )
                    strategy_result["development"] = development
                    strategy_result["holdout"] = holdout
                    strategy_result["evidence"] = _evidence_label(
                        development, holdout
                    )
            result[strategy_name] = strategy_result

        portfolio_periods, latest_holdings = _ranked_portfolio_periods(
            data,
            complete_dates,
            commission_rate=self.commission_rate,
            stamp_duty_rate=self.stamp_duty_rate,
            slippage_rate=self.slippage_rate,
        )
        portfolio_development = _portfolio_summary(
            portfolio_periods[portfolio_periods["signal_date"] < holdout_start]
            if not portfolio_periods.empty
            else portfolio_periods
        )
        portfolio_holdout = _portfolio_summary(
            portfolio_periods[portfolio_periods["signal_date"] >= holdout_start]
            if not portfolio_periods.empty
            else portfolio_periods
        )
        portfolio_curve: list[dict[str, Any]] = []
        if not portfolio_periods.empty:
            strategy_equity = (1 + portfolio_periods["return"]).cumprod()
            benchmark_equity = (1 + portfolio_periods["benchmark_return"]).cumprod()
            portfolio_curve = [
                {
                    "date": str(row.execution_date),
                    "equity": _number(strategy_equity.loc[row.Index], 4),
                    "benchmark": _number(benchmark_equity.loc[row.Index], 4),
                    "holdings": int(row.holdings),
                    "market_open": bool(row.market_open),
                }
                for row in portfolio_periods.itertuples()
            ]
        result["PriceQualityMultiFactorV1Strategy"]["portfolio"] = {
            "method": (
                "每5个完整交易日调仓；收盘计算排名，下一交易日开盘等权成交；"
                "新买入前20名，原持仓跌出前40名才卖出；市场过滤不通过时持有现金。"
            ),
            "overall": _portfolio_summary(portfolio_periods),
            "development": portfolio_development,
            "holdout": portfolio_holdout,
            "evidence": _portfolio_evidence(
                portfolio_development, portfolio_holdout
            ),
            "latest_holdings": [
                {
                    "symbol": symbol,
                    "name": names.get(symbol, ""),
                    "board": board_names.get(symbol, "其他/待核对"),
                }
                for symbol in latest_holdings
            ],
            "curve": portfolio_curve,
        }

        result["PrivatePlacementStrategy"] = {
            "name": STRATEGIES["PrivatePlacementStrategy"]["name"],
            "description": STRATEGIES["PrivatePlacementStrategy"]["description"],
            "evidence": "无法验证",
            "reason": "本地数据库没有历史定增事件快照，不能用日线反推当时可见事件。",
            "horizons": {},
            "years": {},
            "boards": {},
            "regimes": {},
            "development": {},
            "holdout": {},
        }
        latest_horizon = str(max(horizons))
        positive_count = sum(
            item.get("evidence") == "初步正向" for item in result.values()
        )
        total_observations = sum(
            item.get("horizons", {}).get(latest_horizon, {}).get("observations", 0)
            for item in result.values()
        )
        return {
            "schema": 3,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "database": str(Path(self.engine.db_path).resolve()),
            "source_rows": len(data),
            "source_symbols": int(data["symbol"].nunique()),
            "source_start": str(data["date"].min()),
            "source_end": str(data["date"].max()),
            "complete_start": str(complete_dates.min()),
            "complete_end": str(complete_dates.max()),
            "complete_days": len(complete_dates),
            "holdout_start": holdout_start,
            "coverage_peak": peak_count,
            "coverage_minimum": minimum_count,
            "coverage_ratio": self.settings.analysis_min_coverage,
            "horizons": list(horizons),
            "primary_horizon": int(latest_horizon),
            "strategy_count": len(MARKET_STRATEGIES),
            "baseline_count": len(BASELINE_STRATEGIES),
            "research_count": len(RESEARCH_STRATEGIES),
            "positive_count": positive_count,
            "total_observations": total_observations,
            "filters": {
                "boards": list(self.settings.include_boards),
                "exclude_st": self.settings.exclude_st,
            },
            "costs": {
                "commission_each_side": self.commission_rate,
                "stamp_duty_on_sell": self.stamp_duty_rate,
                "slippage_each_side": self.slippage_rate,
            },
            "market_regime": {
                "definition": "全市场收盘高于MA20的股票比例不低于55%，且20日收益中位数为正",
                "favorable_days": int(regime_by_date["favorable"].sum()),
                "favorable_pct": _number(regime_by_date["favorable"].mean() * 100, 2),
                "latest_date": str(regime_by_date.iloc[-1]["date"]),
                "latest_favorable": bool(regime_by_date.iloc[-1]["favorable"]),
                "latest_breadth_pct": _number(regime_by_date.iloc[-1]["breadth"] * 100, 2),
                "latest_median_return20_pct": _number(
                    regime_by_date.iloc[-1]["median_return20"] * 100
                ),
            },
            "research_policy": {
                "composite_daily_top_n": 20,
                "composite_weights": {
                    "momentum_120": 0.30,
                    "momentum_60": 0.20,
                    "momentum_20": 0.15,
                    "trend_strength": 0.15,
                    "low_volatility": 0.15,
                    "liquidity": 0.05,
                },
                "price_quality_v1": {
                    "entry_rank": QUALITY_ENTRY_RANK,
                    "exit_rank": QUALITY_EXIT_RANK,
                    "rebalance_days": QUALITY_REBALANCE_DAYS,
                    "market_filter": "市场宽度不低于50%，且全市场20日收益中位数不低于-2%",
                    "weights": {
                        "momentum_120_skip_20": 0.25,
                        "momentum_60_skip_5": 0.20,
                        "trend_consistency": 0.15,
                        "low_downside_volatility": 0.15,
                        "drawdown_resilience": 0.10,
                        "liquidity": 0.10,
                        "turnover_stability": 0.05,
                        "short_term_overheat_penalty": -0.05,
                    },
                },
            },
            "strategies": result,
            "limitations": [
                "普通策略仍采用逐日事件验证；价格质量多因子 V1 另有每周等权组合回测和最大回撤。",
                "同一股票在持有周期内的重复信号只保留第一次，减少重叠样本。",
                "市场基准是同日可交易股票的等权平均收益，不是沪深300等可投资指数。",
                "股票简称只保存当前状态，无法准确还原历史日期的ST状态。",
                "股票池来自本地回填记录，仍可能存在退市股票缺失造成的存活偏差。",
                "价格质量多因子 V1 的质量只表示趋势连续性、下行波动和成交稳定性，不代表公司财务质量。",
                "本地历史库目前只有OHLCV和成交额，缺少逐日财务、估值与行业分类；V1只能做板块内排名，不能做行业中性化。",
                "组合回测按调仓日开盘可成交处理；个股停牌时按该周期收益为0，尚未完整模拟涨跌停排队、冲击成本和持仓权重漂移。",
                "定增策略缺少逐日事件快照，本报告不伪造其历史回测。",
                f"证据标签以 {holdout_start} 起的留出期为核心，并要求此前开发期与留出期同时为正；研究候选未进入正式选股。",
            ],
        }

    def write(self, report: dict[str, Any]) -> tuple[Path, Path]:
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(report, ensure_ascii=False, allow_nan=False)
        safe_payload = payload.replace("<", "\\u003c").replace("\u2028", "\\u2028")
        template = Path(__file__).with_name("strategy_validation.html").read_text(
            encoding="utf-8"
        )
        html = template.replace("__VALIDATION_DATA__", safe_payload)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        html_path = self.directory / f"strategy-validation-{stamp}.html"
        json_path = self.directory / f"strategy-validation-{stamp}.json"
        write_atomic(html_path, html)
        write_atomic(json_path, json.dumps(report, ensure_ascii=False, indent=2))
        write_atomic(self.directory / "strategy-validation-latest.html", html)
        write_atomic(self.directory / "strategy-validation-latest.json", json.dumps(
            report, ensure_ascii=False, indent=2
        ))
        return html_path.resolve(), json_path.resolve()
