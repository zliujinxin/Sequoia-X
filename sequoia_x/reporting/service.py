"""将选股结果转成可解释的观察报告，不额外请求行情。"""

from datetime import datetime
import hashlib
import json
import math
import re
from pathlib import Path
import uuid

import pandas as pd

from sequoia_x.core.logger import get_logger
from sequoia_x.core.stock_profile import stock_profile, matches_output_filter, output_filter
from sequoia_x.data.baostock_session import BaostockError
from sequoia_x.reporting.catalog import CATEGORIES, STRATEGIES

logger = get_logger(__name__)


def number(value):
    try:
        value = float(value)
        return round(value, 4) if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def ratio(a, b, percent=False):
    a, b = number(a), number(b)
    if a is None or b is None or b <= 0:
        return None
    return number((a / b - 1) * 100 if percent else a / b)


def fmt(value, suffix="", signed=False):
    if value is None:
        return "—"
    return (f"{value:+.2f}" if signed else f"{value:.2f}") + suffix


def load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (ValueError, OSError):
        logger.warning(f"无法读取历史记录 {Path(path).name}，本次按无可用基准处理")
        return None


def write_atomic(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def save_json(path, value):
    write_atomic(path, json.dumps(value, ensure_ascii=False, allow_nan=False))


def stock_url(symbol):
    prefix = "BJ" if symbol.startswith(("4", "8", "92")) else "SH" if symbol.startswith(("6", "9")) else "SZ"
    return f"https://xueqiu.com/S/{prefix}{symbol}"


def snapshot(report):
    return {
        "schema": 1, "scope": report["scope"], "generated_at": report["generated_at"],
        "data_date": report["data_date"],
        "records": [{k: row[k] for k in [
            "symbol", "name", "signals", "date", "metrics", "warnings", "category", "stale"
        ]} | {"market": row.get("market", {})} for row in report["records"]],
    }


def compare(records, previous):
    """比较名单、命中策略及显著指标变化；仅日期前进不算显著变化。"""
    old = {row["symbol"]: row for row in previous["records"]} if previous else {}
    current = {row["symbol"] for row in records}
    compared = []
    for row in records:
        row = dict(row)
        before = old.get(row["symbol"])
        details = []
        if before:
            if before.get("market") and row.get("market", {}).get("st_status") != before["market"].get("st_status"):
                details.append("缓存简称识别的ST状态发生变化")
            if row["signals"] != before["signals"]:
                details.append("命中策略发生变化")
            if row["stale"] != before.get("stale"):
                details.append("数据新鲜度发生变化")
            if row["warnings"] != before.get("warnings", []):
                details.append("风险提示发生变化")
            for key, threshold, label in [
                ("pct", 2, "涨跌幅变化至少2个百分点"),
                ("volume_ratio", 0.5, "相对成交量变化至少0.5倍"),
            ]:
                a, b = row["metrics"].get(key), before["metrics"].get(key)
                if a is not None and b is not None and abs(a - b) >= threshold:
                    details.append(label)
        row["change"] = "initial" if previous is None else "new" if before is None else "changed" if details else "retained"
        row["change_details"] = details
        compared.append(row)
    removed = [row for key, row in old.items() if key not in current]
    return compared, removed


def priority(row):
    """查看顺序，不计算收益评分；异动优先，再按 RPS 和成交额排序。"""
    return (
        row["stale"] or (row["metrics"].get("max_jump120") or 0) >= 35,
        row.get("change") == "retained",
        -(row["metrics"].get("rps") or 0),
        -(row["metrics"].get("turnover") or 0), row["symbol"],
    )


class ReportService:
    def __init__(self, engine, settings):
        self.engine, self.settings = engine, settings
        self.directory = Path(settings.report_dir)

    def _names(self):
        path = self.directory / "stock_names.json"
        cached = load_json(path) or {}
        names = {**cached.get("names", {}), **self.engine.get_stock_names()}
        # 本地模式从不补查询。联网模式至多每日一次批量查询，避免逐股请求。
        if not self.settings.local_only and cached.get("date") != datetime.now().date().isoformat():
            try:
                names.update(self.engine.refresh_stock_names())
                cached = {"date": datetime.now().date().isoformat(), "names": names}
                save_json(path, cached)
            except (BaostockError, OSError):
                logger.warning("名称更新失败，使用已有缓存或股票代码生成报告")
        return names

    def refresh_names(self):
        """补名称并更新当前报告，不重算策略或推进报告/通知比较基准。"""
        names = self.engine.refresh_stock_names()
        save_json(self.directory / "stock_names.json", {
            "date": datetime.now().date().isoformat(), "names": names,
        })
        latest = self.directory / "latest.html"
        if latest.exists():
            html = latest.read_text(encoding="utf-8")
            match = re.search(r'<script id="report-data" type="application/json">(.*?)</script>', html, re.S)
            if not match:
                raise ValueError("名称已保存，但现有报告格式无法识别；请重新生成报告")
            report = json.loads(match.group(1))
            name_dates = self.engine.get_stock_name_dates()
            for row in report["records"] + report["removed"]:
                row["name"] = names.get(row["symbol"], row["name"])
                row["market"] = stock_profile(row["symbol"], row["name"], name_dates.get(row["symbol"]))
            if report.get("output_filter", {}).get("exclude_st"):
                previous_count = len(report["records"])
                report["records"] = [row for row in report["records"] if row["market"]["st_status"] == "normal"]
                report["excluded_count"] = report.get("excluded_count", 0) + previous_count - len(report["records"])
                for strategy, info in report["strategies"].items():
                    info["count"] = sum(strategy in row["signals"] for row in report["records"])
            write_atomic(latest, self._render(report))
            named = sum(bool(row["name"]) for row in report["records"])
            logger.info(f"当前报告名称已补齐：{named}/{len(report['records'])} 只；行情和名单变化基准未改动")
        return len(names)

    def build(self, results):
        frame = self.engine.get_analysis_frame()
        latest = str(frame["date"].max()) if not frame.empty else None
        returns = {}
        for symbol, group in frame.groupby("symbol", sort=False):
            if len(group) >= 121 and str(group.iloc[-1]["date"]) == latest:
                close, prior = float(group.iloc[-1]["close"]), float(group.iloc[-121]["close"])
                if math.isfinite(close) and math.isfinite(prior) and prior > 0:
                    # 排名使用与策略相同的未舍入收益；仅展示时舍入。
                    returns[symbol] = (close - prior) / prior
        ranks = (pd.Series(returns, dtype=float).rank(pct=True) * 100).to_dict()
        hits = {}
        for strategy, symbols in results.items():
            for symbol in set(symbols):
                hits.setdefault(symbol, []).append(strategy)
        names = self._names()
        name_dates = self.engine.get_stock_name_dates()
        profiles = {s: stock_profile(s, names.get(s, ""), name_dates.get(s)) for s in hits}
        raw_count = len(hits)
        hits = {s: signals for s, signals in hits.items() if matches_output_filter(profiles[s], self.settings)}
        filtered_results = {s: [symbol for symbol in set(symbols) if symbol in hits] for s, symbols in results.items()}
        records = []
        for symbol, signals in sorted(hits.items()):
            history = self.engine.get_ohlcv(symbol)
            metrics, chart = self._metrics(history, ranks.get(symbol))
            categories = [c for c in CATEGORIES if any(STRATEGIES[s]["category"] == c for s in signals)]
            row_date = str(history.iloc[-1]["date"]) if not history.empty else None
            warnings = []
            stale = row_date is None or row_date != latest
            if stale:
                warnings.append("缺少全库最新日期的行情，不能视为当日信号")
            if (metrics.get("max_jump120") or 0) >= 35:
                warnings.append("近120根日线存在相邻收盘跳变至少35%，请核对复权口径或特殊交易情形")
            if (metrics.get("return10") or 0) >= 15:
                warnings.append("近10根日线累计涨幅达到15%，留意回撤")
            if (metrics.get("breakout") or 0) >= 5:
                warnings.append("收盘超过此前20日高点5%以上，留意追高距离")
            if "UptrendLimitDownStrategy" in signals:
                warnings.append("异常大跌仅是观察信号，尚未验证止跌或反包")
            if "LimitUpShakeoutStrategy" in signals:
                warnings.append("固定9.5%阈值不等于精确涨停；放量收阴不能证明洗盘")
            if "PrivatePlacementStrategy" in signals:
                warnings.append("按发行日期筛选定增事件，未评估发行条件和事件影响")
            records.append({
                "symbol": symbol, "name": names.get(symbol, ""), "url": stock_url(symbol),
                "market": profiles[symbol],
                "signals": sorted(signals), "categories": categories,
                "category": "异常大跌" if "异常大跌" in categories else categories[0],
                "date": row_date, "stale": stale, "metrics": metrics,
                "warnings": warnings, "reasons": self._reasons(signals, metrics), "chart": chart,
            })
        scope_text = str(Path(self.engine.db_path).resolve()) + repr(sorted(results)) + str(self.settings.local_only)
        filters = output_filter(self.settings)
        if filters["boards"] or filters["exclude_st"]:
            scope_text += json.dumps(filters, sort_keys=True)
        scope = hashlib.sha256(scope_text.encode()).hexdigest()[:16]
        previous_path = self.directory / "state" / f"report-{scope}.json"
        previous = load_json(previous_path)
        if previous and (previous.get("schema") != 1 or (previous.get("data_date") or "") > (latest or "")):
            previous = None
        records, removed = compare(records, previous)
        dates = frame.groupby("symbol")["date"].max() if not frame.empty else pd.Series(dtype=str)
        report = {
            "schema": 1, "scope": scope, "generated_at": datetime.now().isoformat(timespec="seconds"),
            "data_date": latest, "local_only": self.settings.local_only,
            "output_filter": filters, "raw_count": raw_count, "excluded_count": raw_count - len(hits),
            "baseline": previous.get("generated_at") if previous else None,
            "baseline_date": previous.get("data_date") if previous else None,
            "analysis_coverage": self.engine.analysis_coverage,
            "universe_count": int(self.engine.analysis_coverage.get("peak_count", len(dates))),
            "fresh_count": int(self.engine.analysis_coverage.get(
                "count", int((dates == latest).sum())
            )),
            "rps_universe_count": len(ranks), "strategies": {
                key: {**STRATEGIES[key], "count": len(set(symbols)), "raw_count": len(set(results[key]))} for key, symbols in filtered_results.items()
            },
            "records": sorted(records, key=priority), "removed": removed,
        }
        return report

    @staticmethod
    def _metrics(df, rps):
        if df.empty:
            return {"rps": number(rps)}, []
        df = df.copy()
        for col in ["open", "high", "low", "close", "volume", "turnover"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        last = df.iloc[-1]
        prev = df.iloc[-2] if len(df) >= 2 else None
        ma = {n: df["close"].rolling(n).mean() for n in [5, 20, 60]}
        prior_high = df["high"].iloc[-21:-1].max() if len(df) >= 21 else None
        avg_volume = df["volume"].iloc[-21:-1].mean() if len(df) >= 21 else None
        metrics = {
            "close": number(last["close"]), "pct": ratio(last["close"], prev["close"], True) if prev is not None else None,
            "turnover": number(last["turnover"]), "volume_ratio": ratio(last["volume"], avg_volume),
            "volume_ratio_inclusive": ratio(last["volume"], df["volume"].tail(20).mean()) if len(df) >= 20 else None,
            "volume_vs_yesterday": ratio(last["volume"], prev["volume"]) if prev is not None else None,
            "prev_pct": ratio(prev["close"], df.iloc[-3]["close"], True) if len(df) >= 3 else None,
            "support_pct": ratio(last["low"], prev["close"], True) if prev is not None else None,
            "body_pct": ratio(last["close"], last["open"], True),
            "prior_high20": number(prior_high), "breakout": ratio(last["close"], prior_high, True),
            "high120_ratio": ratio(last["close"], df["high"].tail(120).max()),
            "return10": ratio(last["close"], df.iloc[-11]["close"], True) if len(df) >= 11 else None,
            "volatility20": number(df["close"].pct_change(fill_method=None).tail(20).std() * 100) if len(df) >= 21 else None,
            "max_jump120": number(df["close"].pct_change(fill_method=None).tail(120).abs().max() * 100) if len(df) >= 2 else None,
            "range40": ratio(df["high"].tail(40).max(), df["low"].tail(40).min()) if len(df) >= 40 else None,
            "range10": ratio(df["high"].tail(10).max(), df["low"].tail(10).min()) if len(df) >= 10 else None,
            "flag_support": ratio(df["low"].tail(10).min(), df["high"].tail(40).max()) if len(df) >= 40 else None,
            "rps": number(rps),
        }
        for n, series in ma.items():
            metrics[f"ma{n}"] = number(series.iloc[-1])
            metrics[f"prev_ma{n}"] = number(series.iloc[-2]) if len(series) >= 2 else None
            metrics[f"ma{n}_direction"] = (
                "上行" if series.iloc[-1] > series.iloc[-2] else "下行" if series.iloc[-1] < series.iloc[-2] else "持平"
            ) if len(df) > n else "数据不足"
            df[f"ma{n}"] = series
        chart = [
            {"date": str(row["date"]), **{key: number(row[key]) for key in [
                "open", "high", "low", "close", "volume", "ma5", "ma20", "ma60"
            ]}}
            for row in df.tail(120).to_dict("records")
        ]
        return metrics, chart

    @staticmethod
    def _reasons(signals, m):
        f = lambda key, suffix="", signed=False: fmt(m.get(key), suffix, signed)
        details = {
            "MaVolumeStrategy": f"MA5 {f('prev_ma5')} → {f('ma5')}，MA20 {f('prev_ma20')} → {f('ma20')}；相对含今日20日均量 {f('volume_ratio_inclusive', '倍')}。",
            "TurtleTradeStrategy": f"超过此前20日高点 {f('breakout', '%', True)}；成交额 {fmt((m.get('turnover') or 0) / 1e8, '亿元')}；实体涨幅 {f('body_pct', '%', True)}，较前收 {f('pct', '%', True)}。",
            "HighTightFlagStrategy": f"40日高低价比 {f('range40')}；10日高低价比 {f('range10')}；整理低点/40日高点 {f('flag_support')}；相对前20日均量 {f('volume_ratio', '倍')}。",
            "LimitUpShakeoutStrategy": f"昨日涨幅 {f('prev_pct', '%', True)}；今日实体涨幅 {f('body_pct', '%', True)}；量为昨日 {f('volume_vs_yesterday', '倍')}；最低价较昨收 {f('support_pct', '%', True)}。",
            "UptrendLimitDownStrategy": f"昨日MA20 {f('prev_ma20')} > MA60 {f('prev_ma60')}；今日涨跌幅 {f('pct', '%', True)}；相对含今日20日均量 {f('volume_ratio_inclusive', '倍')}。",
            "RpsBreakoutStrategy": f"本地有效股票池120日收益排名 RPS {f('rps')}；收盘/120日最高价 {f('high120_ratio')}。",
            "PrivatePlacementStrategy": "增发接口返回近期发行的定向增发记录；需结合发行对象、用途及价格进一步核实。",
        }
        return [{"strategy": s, "name": STRATEGIES[s]["name"], "text": details[s]} for s in sorted(signals)]

    @staticmethod
    def _render(report):
        template = Path(__file__).with_name("template.html").read_text(encoding="utf-8")
        tdx_js = Path(__file__).with_name("tdx_export.js").read_text(encoding="utf-8")
        template = template.replace("__TDX_EXPORT_JS__", tdx_js)
        payload = json.dumps(report, ensure_ascii=False, allow_nan=False).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
        return template.replace("__REPORT_DATA__", payload)

    def write(self, report):
        html = self._render(report)
        name = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        path = self.directory / f"report-{name}.html"
        write_atomic(path, html)
        write_atomic(self.directory / "latest.html", html)
        save_json(self.directory / "state" / f"report-{report['scope']}.json", snapshot(report))
        return path.resolve()
