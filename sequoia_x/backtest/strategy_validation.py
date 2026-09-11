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
]
MARKET_STRATEGIES = BASELINE_STRATEGIES + RESEARCH_STRATEGIES


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
        breadth_above_ma20 = (data["close"] > ma20).where(complete_mask).groupby(
            data["date"]
        ).transform("mean")
        market_median_return20 = return20.where(complete_mask).groupby(
            data["date"]
        ).transform("median")
        favorable_market = (
            (breadth_above_ma20 >= 0.55) & (market_median_return20 > 0)
        )

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
            "schema": 2,
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
            "strategies": result,
            "limitations": [
                "这是逐日事件验证，不是多股票组合回测，因此不计算组合最大回撤。",
                "同一股票在持有周期内的重复信号只保留第一次，减少重叠样本。",
                "市场基准是同日可交易股票的等权平均收益，不是沪深300等可投资指数。",
                "股票简称只保存当前状态，无法准确还原历史日期的ST状态。",
                "股票池来自本地回填记录，仍可能存在退市股票缺失造成的存活偏差。",
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
