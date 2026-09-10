"""Sequoia-X V2 主程序入口。

运行模式：
  python main.py               # 串行增量更新 + 策略 + 飞书推送
  python main.py --backfill    # 串行回填历史行情
  python main.py --local-only  # 使用本地行情选股并推送，不请求行情数据源
  python main.py --check-provider easy_tdx  # 影子比较，不写正式行情
  python main.py --serve        # 启动本地观察台与个股缠论查询
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
from sequoia_x.data.providers import ProviderError, create_provider
from sequoia_x.data.quality import ProviderQualityChecker
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
        help="回填模式：通过配置的数据源单会话串行拉取历史 K 线",
    )
    modes.add_argument(
        "--check-provider",
        choices=["easy_tdx"],
        help="影子校验候选行情源；只读本地正式行情，不写 stock_daily",
    )
    modes.add_argument("--serve", action="store_true", help="启动本地观察台与个股缠论查询")
    parser.add_argument("--port", type=int, default=8765, help="本地页面端口（默认 8765）")
    parser.add_argument("--no-browser", action="store_true", help="启动页面服务后不自动打开浏览器")
    parser.add_argument("--check-sample-size", type=int, help="影子校验股票样本数（1-100）")
    parser.add_argument("--check-days", type=int, help="每只股票最多比较的最近日线数（30-2000）")
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

        if args.serve:
            if not 1 <= args.port <= 65535:
                parser.error("--port 必须在 1 到 65535 之间")
            from sequoia_x.web import run_server

            run_server(settings, port=args.port, open_browser=not args.no_browser)
            return

        # 3. 初始化数据引擎
        reference_settings = settings
        if args.check_provider:
            reference_settings = settings.model_copy(update={"data_provider": "baostock"})
        engine = DataEngine(reference_settings)

        if args.check_provider:
            sample_size = args.check_sample_size or settings.provider_check_sample_size
            days = args.check_days or settings.provider_check_days
            if not 1 <= sample_size <= 100:
                parser.error("--check-sample-size 必须在 1 到 100 之间")
            if not 30 <= days <= 2000:
                parser.error("--check-days 必须在 30 到 2000 之间")
            candidate_settings = settings.model_copy(update={"data_provider": args.check_provider})
            provider = create_provider(candidate_settings)
            logger.info(
                f"开始影子校验：候选源={provider.name}，复权={provider.adjustment}，"
                f"样本={sample_size}，日线={days}"
            )
            checker = ProviderQualityChecker(engine, provider, settings.report_dir)
            report = checker.run(sample_size=sample_size, days=days)
            html_path, json_path = checker.write(report)
            summary = report["summary"]
            logger.info(
                f"影子校验完成：PASS={summary['pass']} WARN={summary['warn']} "
                f"FAIL={summary['fail']}"
            )
            logger.info(f"HTML 报告：{html_path}")
            logger.info(f"JSON 报告：{json_path}")
            if report["sample_size"] == 0:
                raise ProviderError("影子校验没有产生可比较样本")
            return

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

    except ProviderError as exc:
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
