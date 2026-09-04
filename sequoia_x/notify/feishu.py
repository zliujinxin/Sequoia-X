"""飞书通知模块：将选股结果通过 Webhook 推送至飞书群。"""

import json
import hashlib
from datetime import date
from pathlib import Path

import requests

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger
from sequoia_x.data.baostock_session import baostock_session, pace_request, read_rows
from sequoia_x.reporting.catalog import CATEGORIES, STRATEGIES
from sequoia_x.reporting.service import compare, fmt, load_json, priority, save_json, snapshot

logger = get_logger(__name__)


class FeishuNotifier:
    """飞书 Webhook 推送器。

    根据策略的 webhook_key 路由到对应的飞书机器人。
    若 webhook_key 未在 Settings.strategy_webhooks 中配置，
    则 fallback 到 Settings.feishu_webhook_url。
    """

    def __init__(self, settings: Settings) -> None:
        """
        初始化 FeishuNotifier。

        Args:
            settings: Settings 实例，提供 Webhook URL 配置。
        """
        self.settings = settings

    def send_report(self, report: dict, force: bool = False) -> bool:
        """同一 Webhook 的策略合并推送；仅成功发送后保存通知基准。"""
        routes = {}
        for strategy, info in report["strategies"].items():
            url = self.settings.get_webhook_url(info["key"])
            routes.setdefault(url, set()).add(strategy)
        all_ok = True
        for url, strategies in routes.items():
            route_id = hashlib.sha256(url.encode()).hexdigest()[:16]
            state_path = Path(self.settings.report_dir) / "state" / f"sent-{report['scope']}-{route_id}.json"
            previous = load_json(state_path)
            if previous and (previous.get("schema") != 1 or (previous.get("data_date") or "") > (report["data_date"] or "")):
                previous = None
            rows = []
            for record in report["records"]:
                signals = [s for s in record["signals"] if s in strategies]
                if signals:
                    categories = [c for c in CATEGORIES if any(STRATEGIES[s]["category"] == c for s in signals)]
                    rows.append({**record, "signals": signals,
                                 "category": "异常大跌" if "异常大跌" in categories else categories[0],
                                 "reasons": [r for r in record["reasons"] if r["strategy"] in strategies]})
            rows, removed = compare(rows, previous)
            changed = [row for row in rows if row["change"] != "retained"]
            if previous is not None and not changed and not removed and not force:
                logger.info("飞书摘要无新增或明显变化，跳过重复推送")
                continue
            if not rows and not removed and not force:
                logger.info("该机器人无选股结果或移出记录，跳过推送")
                continue
            blocks = self._summary_blocks(report, rows, removed, previous, force)
            success = True
            for card in self._chunk_cards(blocks):
                try:
                    response = requests.post(
                        url, data=json.dumps(card, ensure_ascii=False).encode("utf-8"),
                        headers={"Content-Type": "application/json; charset=utf-8"}, timeout=10,
                    )
                    body = response.json()
                    if response.status_code != 200 or not isinstance(body, dict) or body.get("code") != 0:
                        logger.error(f"飞书摘要发送失败：HTTP={response.status_code}，code={body.get('code') if isinstance(body, dict) else '未知'}；通知基准未更新")
                        success = False
                        break
                except (requests.RequestException, ValueError):
                    logger.error("飞书摘要发送或响应解析失败，通知基准未更新")
                    success = False
                    break
            if success:
                save_json(state_path, snapshot({**report, "records": rows}))
                logger.info(f"飞书摘要推送成功：合并 {len(rows)} 只股票，移出 {len(removed)} 只")
            all_ok = all_ok and success
        return all_ok

    def _summary_blocks(self, report, rows, removed, previous, force):
        labels = {"initial": "首次", "new": "新增", "changed": "变化", "retained": "持续"}
        fresh = sum(r["change"] == "new" for r in rows)
        changed = sum(r["change"] == "changed" for r in rows)
        baseline = f"相对上次成功通知 {previous['generated_at'].replace('T', ' ')}" if previous else "首次通知，无比较基准"
        blocks = [
            f"**行情日期：** {report['data_date'] or '未知'}｜{'本地数据模式，未联网更新行情' if report['local_only'] else '同步后快照'}\n"
            f"**去重候选 {len(rows)} 只**｜新增 {fresh}｜明显变化 {changed}｜移出 {len(removed)}\n{baseline}\n"
            "价格为后复权值，不能直接作为下单价格；排序用于安排查看，不是收益评分。"
        ]
        visible = rows if force or previous is None else [r for r in rows if r["change"] != "retained"]
        for category in CATEGORIES:
            candidates = sorted([r for r in visible if r["category"] == category], key=priority)
            if not candidates:
                continue
            top = candidates[:self.settings.report_top_n]
            blocks.append(f"**{category} · 展示 {len(top)}/{len(candidates)} 只**")
            for row in top:
                m = row["metrics"]
                market = row.get("market", {})
                # 不做线上名称查询；只使用报告中已有缓存。
                name = (row["name"] or row["symbol"]).replace("[", "（").replace("]", "）")
                signals = " / ".join(report["strategies"][s]["name"] for s in row["signals"])
                blocks.append(
                    f"**[{name} · {row['symbol']}]({row['url']})**｜{labels[row['change']]}\n"
                    f"{market.get('exchange_name', '待核对')} · {market.get('board_name', '待核对')} · {market.get('st_label', 'ST待核对')}（按缓存简称）\n"
                    f"{signals}｜数据 {row['date'] or '缺失'}\n"
                    f"涨跌 {fmt(m.get('pct'), '%', True)}｜成交额 {fmt(m.get('turnover') / 1e8 if m.get('turnover') is not None else None, '亿')}｜相对前20日均量 {fmt(m.get('volume_ratio'), '倍')}\n"
                    f"RPS {fmt(m.get('rps'))}｜近10日 {fmt(m.get('return10'), '%', True)}\n"
                    + "\n".join(reason["text"] for reason in row["reasons"][:2])
                    + ("\n留意：" + "；".join(row["warnings"]) if row["warnings"] else "")
                )
        if removed:
            examples = "、".join(r["symbol"] for r in removed[:15])
            blocks.append(f"**移出 {len(removed)} 只：** {examples}{'…' if len(removed) > 15 else ''}\n移出仅表示本次未再命中，不是卖出指令。")
        blocks.append("完整名单、入选理由、变化记录和K线已保存到运行电脑的报告目录 latest.html。该文件可离线打开，手机不能直接访问本地路径。")
        return blocks

    @staticmethod
    def _chunk_cards(blocks):
        """保守限制 UTF-8 请求体大小，防止大量候选使卡片超长。"""
        def card(parts):
            return {"msg_type": "interactive", "card": {
                "header": {"title": {"tag": "plain_text", "content": "Sequoia-X · 选股观察摘要"}, "template": "green"},
                "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": p}} for p in parts],
            }}
        cards, parts = [], []
        for block in blocks:
            if parts and len(json.dumps(card(parts + [block]), ensure_ascii=False).encode("utf-8")) > 16000:
                cards.append(card(parts))
                parts = []
            parts.append(block)
        if parts:
            cards.append(card(parts))
        return cards

    @staticmethod
    def _to_xueqiu_code(code: str) -> str:
        """将纯数字代码转为雪球格式：6开头→SH，4/8开头→BJ，其余→SZ。"""
        if code.startswith("6"):
            return f"SH{code}"
        elif code.startswith(("4", "8")):
            return f"BJ{code}"
        return f"SZ{code}"

    @staticmethod
    def _get_stock_names(symbols: list[str]) -> dict[str, str]:
        """通过 baostock 批量查询股票名称，返回 {code: name} 映射。"""
        mapping = {}
        with baostock_session() as bs:
            for code in symbols:
                pace_request()
                prefix = "sh" if code.startswith(("6", "9")) else "sz"
                rs = bs.query_stock_basic(code=f"{prefix}.{code}")
                for row in read_rows(rs, f"查询 {code} 名称"):
                    mapping[code] = row[1]  # 第2个字段是股票名称
        return mapping

    def _build_card(self, symbols: list[str], strategy_name: str) -> dict:
        today = date.today().strftime("%Y-%m-%d")
        names = {} if self.settings.local_only else self._get_stock_names(symbols)
        mode_note = "\n**本地数据模式：** 未联网更新行情，请核对各股票数据日期。" if self.settings.local_only else ""

        links: list[str] = []
        for code in symbols:
            xq_code = self._to_xueqiu_code(code)
            name = names.get(code, xq_code)
            links.append(f"[{name}](https://xueqiu.com/S/{xq_code})")

        symbol_text = " ".join(links) if links else "（无选股结果）"

        return {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": f"📈 Sequoia-X 选股播报 | {strategy_name}",
                    },
                    "template": "blue",
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"**发送日期：** {today}\n**策略：** {strategy_name}\n**选股数量：** {len(symbols)}{mode_note}",
                        },
                    },
                    {"tag": "hr"},
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"**选股列表：**\n{symbol_text}",
                        },
                    },
                ],
            },
        }

    def send(
        self,
        symbols: list[str],
        strategy_name: str,
        webhook_key: str = "default",
    ) -> None:
        """
        将选股结果格式化为飞书卡片消息并 POST 至对应 Webhook。

        根据 webhook_key 从 Settings 中查找专属 URL；
        若未配置，则 fallback 到 feishu_webhook_url。

        Args:
            symbols: 选股结果代码列表。
            strategy_name: 策略名称，用于卡片标题。
            webhook_key: 策略标识，用于路由到对应飞书机器人。

        Raises:
            不抛出异常，HTTP 失败时记录 ERROR 日志。
        """
        url = self.settings.get_webhook_url(webhook_key)
        payload = self._build_card(symbols, strategy_name)

        try:
            resp = requests.post(
                url,
                data=json.dumps(payload),
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            # 解析飞书真正的返回体
            resp_json = resp.json()

            # 飞书真正的成功标志是内部的 code == 0
            if resp.status_code != 200 or resp_json.get("code") != 0:
                logger.error(
                    f"飞书推送失败 [{webhook_key}] "
                    f"HTTP状态={resp.status_code} 飞书响应={resp.text}"
                )
            else:
                logger.info(f"飞书推送成功 [{webhook_key}]，共 {len(symbols)} 只股票")

        except requests.RequestException as exc:
            logger.error(f"飞书推送请求异常 [{webhook_key}]：{exc}")
