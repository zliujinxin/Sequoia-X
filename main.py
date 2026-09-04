"""Sequoia-X V2 主程序入口。

运行模式：
  python main.py               # 串行增量更新 + 策略 + 飞书推送
  python main.py --backfill    # 串行回填历史行情
  python main.py --local-only  # 使用本地行情选股并推送，不请求行情数据源
"""

import argparse
import sys
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")

import socket
socket.setdefaulttimeout(10.0)

from sequoia_x.core.config import get_settings
from sequoia_x.core.stock_profile import BOARDS
from sequoia_x.core.logger import get_logger
from sequoia_x.data.engine import DataEngine
from sequoia_x.data.baostock_session import BaostockError
from sequoia_x.notify.feishu import FeishuNotifier
from sequoia_x.reporting.service import ReportService
from sequoia_x.strategy.base import BaseStrategy
from sequoia_x.strategy.high_tight_flag import HighTightFlagStrategy
from sequoia_x.strategy.limit_up_shakeout import LimitUpShakeoutStrategy
from sequoia_x.strategy.ma_volume import MaVolumeStrategy
from sequoia_x.strategy.turtle_trade import TurtleTradeStrategy
from sequoia_x.strategy.uptrend_limit_down import UptrendLimitDownStrategy
from sequoia_x.strategy.rps_breakout import RpsBreakoutStrategy
from sequoia_x.strategy.private_placement import PrivatePlacementStrategy


def main() -> None:
    parser = argparse.ArgumentParser(description="Sequoia-X V2 选股系统")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--refresh-names", action="store_true", help="仅批量更新股票名称及现有报告，不拉行情、不运行策略、不推送")
    modes.add_argument(
        "--backfill",
        action="store_true",
        help="回填模式：通过 baostock 单连接串行拉取历史 K 线",
    )
    parser.add_argument("--no-notify", action="store_true", help="生成本地报告，不发送飞书消息")
    parser.add_argument("--force-notify", action="store_true", help="即使名单无明显变化，也发送当前摘要")
    parser.add_argument("--boards", nargs="+", choices=list(BOARDS), help="只输出这些板块：sh_main沪主板 sz_main深主板 star科创 chinext创业 bse北交所；不改变RPS计算池")
    parser.add_argument("--exclude-st", action=argparse.BooleanOptionalAction, default=None, help="排除ST、*ST及名称缺失者；可用--no-exclude-st取消配置中的排除")
    modes.add_argument(
        "--local-only", action="store_true",
        help="仅使用本地行情；跳过定增、市值和名称查询，仍发送飞书消息",
    )
    args = parser.parse_args()

    try:
        # 1. 初始化配置
        settings = get_settings()
        settings = settings.model_copy(update={"local_only": args.local_only})
        if args.boards is not None:
            settings = settings.model_copy(update={"include_boards": args.boards})
        if args.exclude_st is not None:
            settings = settings.model_copy(update={"exclude_st": args.exclude_st})

        # 2. 初始化日志
        logger = get_logger(__name__)
        logger.info("Sequoia-X V2 启动")

        # 3. 初始化数据引擎
        engine = DataEngine(settings)

        if args.refresh_names:
            count = ReportService(engine, settings).refresh_names()
            logger.info(f"股票名称已保存，共 {count} 只；后续本地模式可直接使用")
            return

        if args.backfill:
            # ── 回填模式：单连接串行拉取，失败即停止 ──
            logger.info("进入回填模式...")
            all_symbols = engine.get_all_symbols()
            engine.backfill(all_symbols)
            logger.info("Sequoia-X V2 回填模式运行完成")
            return

        if settings.local_only:
            logger.warning("本地数据模式：不更新行情，不查询名称/市值，跳过定增策略")
            if not engine.get_local_symbols():
                raise ValueError("本地没有行情数据，无法选股")
        else:
            logger.info("开始串行同步增量行情...")
            count = engine.sync_today_bulk()
            logger.info(f"行情同步完成，写入 {count} 条日线")

        engine.prepare_analysis()

        # 4. 策略列表（新增策略在此追加即可）
        strategies: list[BaseStrategy] = [
            MaVolumeStrategy(engine=engine, settings=settings),
            TurtleTradeStrategy(engine=engine, settings=settings),
            HighTightFlagStrategy(engine=engine, settings=settings),
            LimitUpShakeoutStrategy(engine=engine, settings=settings),
            UptrendLimitDownStrategy(engine=engine, settings=settings),
            RpsBreakoutStrategy(engine=engine, settings=settings),
        ]
        if not settings.local_only:
            strategies.append(PrivatePlacementStrategy(engine=engine, settings=settings))

        # 5. 策略共用一份行情快照，先收集结果，再按股票合并。
        results = {}
        for strategy in strategies:
            strategy_name = type(strategy).__name__
            logger.info(f"执行策略：{strategy_name}")

            selected: list[str] = strategy.run()
            logger.info(f"{strategy_name} 选出 {len(selected)} 只股票")

            results[strategy_name] = selected

        reports = ReportService(engine, settings)
        report = reports.build(results)
        path = reports.write(report)
        logger.info(f"观察报告已生成：{path}")
        logger.info(f"固定入口：{(Path(settings.report_dir) / 'latest.html').resolve()}")
        if args.no_notify:
            logger.info("仅生成报告，未发送飞书消息")
        elif not FeishuNotifier(settings).send_report(report, force=args.force_notify):
            logger.error("报告已保存，但部分飞书摘要发送失败；下次运行将再次尝试")
            sys.exit(1)

    except BaostockError as exc:
        get_logger(__name__).error(str(exc))
        sys.exit(1)
    except Exception:
        try:
            _logger = get_logger(__name__)
            _logger.exception("主流程发生未捕获异常，程序终止")
        except Exception:
            import traceback
            traceback.print_exc()
        sys.exit(1)

    logger.info("Sequoia-X V2 运行完成")


if __name__ == "__main__":
    main()
