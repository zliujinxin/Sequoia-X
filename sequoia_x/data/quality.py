"""候选行情源的影子校验；读取正式库，但绝不写入 stock_daily。"""

from __future__ import annotations

import html
import json
import math
from datetime import datetime
from pathlib import Path

import pandas as pd

from sequoia_x.core.stock_profile import stock_profile
from sequoia_x.data.providers.base import MarketDataProvider, ProviderError

PRICE_COLUMNS = ["open", "high", "low", "close"]


def _finite(value: float | int | None) -> float | None:
    if value is None:
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _median_ratio(candidate: pd.Series, reference: pd.Series) -> float | None:
    valid = reference.notna() & candidate.notna() & (reference != 0)
    if not valid.any():
        return None
    return _finite((candidate[valid] / reference[valid]).median())


def compare_daily_frames(
    symbol: str, reference: pd.DataFrame, candidate: pd.DataFrame
) -> dict:
    """在相同日期上比较两个已归一化日线 DataFrame。"""
    ref = reference.copy().drop_duplicates("date", keep="last")
    cand = candidate.copy().drop_duplicates("date", keep="last")
    merged = ref.merge(cand, on="date", suffixes=("_reference", "_candidate"))
    reference_rows = len(ref)
    candidate_rows = len(cand)
    overlap_rows = len(merged)
    coverage = overlap_rows / reference_rows if reference_rows else 0.0

    price_mape: dict[str, float | None] = {}
    scale_adjusted_price_mape: dict[str, float | None] = {}
    close_scale = _median_ratio(
        merged.get("close_candidate", pd.Series(dtype=float)),
        merged.get("close_reference", pd.Series(dtype=float)),
    )
    for column in PRICE_COLUMNS:
        left = merged[f"{column}_reference"]
        right = merged[f"{column}_candidate"]
        valid = left.notna() & right.notna() & (left != 0)
        price_mape[column] = _finite(((right[valid] - left[valid]).abs() / left[valid]).mean()) \
            if valid.any() else None
        scale_adjusted_price_mape[column] = (
            _finite(((right[valid] / close_scale - left[valid]).abs() / left[valid]).mean())
            if valid.any() and close_scale not in (None, 0) else None
        )

    ordered = merged.sort_values("date")
    reference_return = ordered["close_reference"].pct_change(fill_method=None)
    candidate_return = ordered["close_candidate"].pct_change(fill_method=None)
    return_valid = reference_return.notna() & candidate_return.notna()
    return_mae = _finite((candidate_return[return_valid] - reference_return[return_valid]).abs().mean()) \
        if return_valid.any() else None

    latest_reference = str(ref["date"].max()) if reference_rows else None
    latest_candidate = str(cand["date"].max()) if candidate_rows else None
    volume_ratio = _median_ratio(
        merged.get("volume_candidate", pd.Series(dtype=float)),
        merged.get("volume_reference", pd.Series(dtype=float)),
    )
    turnover_ratio = _median_ratio(
        merged.get("turnover_candidate", pd.Series(dtype=float)),
        merged.get("turnover_reference", pd.Series(dtype=float)),
    )
    scaled_close_mape = scale_adjusted_price_mape["close"]
    candidate_quality = candidate.attrs.get("quality", {})

    if not reference_rows or not candidate_rows or overlap_rows < 5:
        status = "fail"
    elif (
        coverage < 0.8
        or scaled_close_mape is None
        or scaled_close_mape > 0.02
        or return_mae is None
        or return_mae > 0.02
        or candidate_quality.get("invalid_rows", 0) > 0
    ):
        status = "fail"
    elif (
        coverage < 0.95
        or latest_reference != latest_candidate
        or scaled_close_mape > 0.005
        or return_mae > 0.005
        or candidate_quality.get("duplicate_dates", 0) > 0
        or volume_ratio is None
        or not 0.95 <= volume_ratio <= 1.05
        or turnover_ratio is None
        or not 0.95 <= turnover_ratio <= 1.05
    ):
        status = "warn"
    else:
        status = "pass"

    return {
        "symbol": symbol,
        "status": status,
        "reference_rows": reference_rows,
        "candidate_rows": candidate_rows,
        "overlap_rows": overlap_rows,
        "coverage_ratio": coverage,
        "latest_reference": latest_reference,
        "latest_candidate": latest_candidate,
        "latest_date_match": latest_reference == latest_candidate,
        "price_mape": price_mape,
        "close_scale": close_scale,
        "scale_adjusted_price_mape": scale_adjusted_price_mape,
        "daily_return_mae": return_mae,
        "volume_ratio": volume_ratio,
        "turnover_ratio": turnover_ratio,
        "candidate_quality": candidate_quality,
    }


def select_quality_sample(symbols: list[str], names: dict[str, str], size: int) -> list[str]:
    """按板块和 ST 状态轮询取样，结果可重复。"""
    buckets: dict[tuple[str, str], list[str]] = {}
    for symbol in sorted(set(symbols)):
        profile = stock_profile(symbol, names.get(symbol, ""))
        key = (profile["board"], profile["st_status"])
        buckets.setdefault(key, []).append(symbol)
    selected: list[str] = []
    # 第一轮先保证主要板块各有一个样本。
    board_order = ["sh_main", "sz_main", "star", "chinext", "bse", "sh_b", "sz_b", "unknown"]
    for board in board_order:
        matches = [symbol for symbol in sorted(set(symbols))
                   if stock_profile(symbol, names.get(symbol, ""))["board"] == board]
        if matches and len(selected) < size:
            selected.append(matches[0])
    # 第二轮补齐 ST、*ST 和名称缺失状态。
    for state in ("st", "star_st", "unknown"):
        matches = [symbol for symbol in sorted(set(symbols))
                   if stock_profile(symbol, names.get(symbol, ""))["st_status"] == state]
        if matches and matches[0] not in selected and len(selected) < size:
            selected.append(matches[0])
    ordered = [buckets[key] for key in sorted(buckets)]
    offset = 0
    while len(selected) < min(size, len(set(symbols))):
        added = False
        for bucket in ordered:
            if offset < len(bucket) and len(selected) < size:
                symbol = bucket[offset]
                if symbol not in selected:
                    selected.append(symbol)
                    added = True
        if not added:
            break
        offset += 1
    return selected


class ProviderQualityChecker:
    """将候选 Provider 与本地正式行情逐股对比并输出报告。"""

    def __init__(self, engine, provider: MarketDataProvider, report_dir: str) -> None:
        self.engine = engine
        self.provider = provider
        self.report_dir = Path(report_dir)

    def run(self, sample_size: int = 30, days: int = 250) -> dict:
        symbols = self.engine.get_local_symbols()
        if not symbols:
            raise ProviderError("本地没有行情，无法执行候选数据源影子校验")
        names = self.engine.get_stock_names()
        sample = select_quality_sample(symbols, names, sample_size)
        rows = []
        with self.provider.session() as session:
            for symbol in sample:
                reference = self.engine.get_ohlcv(symbol).tail(days).copy()
                if reference.empty:
                    continue
                start_date = str(reference["date"].min())
                end_date = str(reference["date"].max())
                try:
                    candidate = session.fetch_daily(symbol, start_date, end_date)
                    compared = compare_daily_frames(symbol, reference, candidate)
                    profile = stock_profile(symbol, names.get(symbol, ""))
                    compared.update({
                        "name": names.get(symbol, ""),
                        "board": profile["board"],
                        "board_name": profile["board_name"],
                        "st_status": profile["st_status"],
                        "st_label": profile["st_label"],
                    })
                    rows.append(compared)
                except Exception as exc:
                    profile = stock_profile(symbol, names.get(symbol, ""))
                    rows.append({
                        "symbol": symbol,
                        "name": names.get(symbol, ""),
                        "board": profile["board"],
                        "board_name": profile["board_name"],
                        "st_status": profile["st_status"],
                        "st_label": profile["st_label"],
                        "status": "fail",
                        "error": str(exc),
                        "reference_rows": len(reference),
                        "candidate_rows": 0,
                        "overlap_rows": 0,
                    })

        counts = {state: sum(row["status"] == state for row in rows)
                  for state in ("pass", "warn", "fail")}
        return {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "reference": "local_sqlite",
            "candidate": self.provider.name,
            "adjustment": self.provider.adjustment,
            "requested_sample_size": sample_size,
            "sample_size": len(rows),
            "days": days,
            "summary": counts,
            "boards": sorted({row["board"] for row in rows}),
            "st_states": sorted({row["st_status"] for row in rows}),
            "rows": rows,
        }

    def write(self, report: dict) -> tuple[Path, Path]:
        self.report_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        json_path = self.report_dir / f"provider-check-{stamp}.json"
        html_path = self.report_dir / f"provider-check-{stamp}.html"
        content = json.dumps(report, ensure_ascii=False, indent=2)
        json_path.write_text(content, encoding="utf-8")
        (self.report_dir / "latest-provider-check.json").write_text(content, encoding="utf-8")
        rendered = self._render(report)
        html_path.write_text(rendered, encoding="utf-8")
        (self.report_dir / "latest-provider-check.html").write_text(rendered, encoding="utf-8")
        return html_path.resolve(), json_path.resolve()

    @staticmethod
    def _render(report: dict) -> str:
        def pct(value):
            return "-" if value is None else f"{value * 100:.3f}%"

        lines = []
        for row in report["rows"]:
            error = html.escape(row.get("error", ""))
            scaled_close_mape = row.get("scale_adjusted_price_mape", {}).get("close")
            lines.append(
                "<tr>"
                f"<td>{html.escape(row.get('name', ''))}<br><code>{html.escape(row['symbol'])}</code></td>"
                f"<td>{html.escape(row.get('board_name', '-'))}</td>"
                f"<td>{html.escape(row.get('st_label', '-'))}</td>"
                f"<td class='{row['status']}'>{row['status'].upper()}</td>"
                f"<td>{row.get('reference_rows', 0)}</td>"
                f"<td>{row.get('candidate_rows', 0)}</td>"
                f"<td>{row.get('overlap_rows', 0)}</td>"
                f"<td>{pct(row.get('coverage_ratio'))}</td>"
                f"<td>{row.get('close_scale', '-')}</td>"
                f"<td>{pct(scaled_close_mape)}</td>"
                f"<td>{pct(row.get('daily_return_mae'))}</td>"
                f"<td>{row.get('volume_ratio', '-')}</td>"
                f"<td>{row.get('turnover_ratio', '-')}</td>"
                f"<td>{error}</td></tr>"
            )
        summary = report["summary"]
        return f"""<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>
<title>行情源影子校验</title><style>
body{{font:14px 'Microsoft YaHei',sans-serif;margin:30px;color:#243029}}
table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #d8ddd9;padding:8px;text-align:left}}
th{{background:#eef2ef}}.pass{{color:#18794e}}.warn{{color:#996a13}}.fail{{color:#b42318}}
code{{background:#f3f5f3;padding:2px 5px}}</style></head><body>
<h1>行情源影子校验</h1><p>参考：<code>{html.escape(report['reference'])}</code>；候选：
<code>{html.escape(report['candidate'])}</code>；复权：<code>{report['adjustment']}</code></p>
<p>PASS {summary['pass']} / WARN {summary['warn']} / FAIL {summary['fail']}；
样本 {report['sample_size']} 只，每只最多 {report['days']} 根。</p>
<p>板块：{html.escape(', '.join(report['boards']))}；ST 状态：{html.escape(', '.join(report['st_states']))}</p>
<table><thead><tr><th>股票</th><th>板块</th><th>ST</th><th>状态</th><th>本地行</th><th>候选行</th><th>重合</th>
<th>覆盖率</th><th>价格缩放</th><th>缩放后收盘价 MAPE</th><th>日收益 MAE</th>
<th>成交量比</th><th>成交额比</th><th>错误</th></tr></thead>
<tbody>{''.join(lines)}</tbody></table></body></html>"""
