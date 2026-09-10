"""数据引擎模块：负责 SQLite 存储，并通过统一 Provider 同步行情。"""

import math
import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path

import pandas as pd

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger
from sequoia_x.data.baostock_session import baostock_session, pace_request
from sequoia_x.data.providers.base import MarketDataProvider, StockRecord
from sequoia_x.data.providers.baostock import BaostockProvider
from sequoia_x.data.providers.factory import create_provider

logger = get_logger(__name__)


_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS stock_daily (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol   TEXT    NOT NULL,
    date     TEXT    NOT NULL,
    open     REAL,
    high     REAL,
    low      REAL,
    close    REAL,
    volume   REAL,
    turnover REAL,
    UNIQUE (symbol, date)
);
"""

_CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_symbol_date ON stock_daily (symbol, date);
"""


class DataEngine:
    """行情数据引擎，负责 SQLite 存储和可替换的数据源同步。"""

    def __init__(self, settings: Settings, provider: MarketDataProvider | None = None) -> None:
        self.db_path: str = settings.db_path
        self.start_date: str = settings.start_date
        self.analysis_min_coverage: float = settings.analysis_min_coverage
        # lambda 在调用时解析模块全局，保留原有测试/调用方对 engine 中函数的 patch 能力。
        if provider is None and settings.data_provider != "baostock":
            from sequoia_x.data.providers.base import ProviderError

            raise ProviderError(
                "easy_tdx 当前仅开放 --check-provider 影子校验；"
                "尚未完成来源元数据迁移，不能写入现有正式行情表"
            )
        if provider is None:
            provider = BaostockProvider(
                adjustment=settings.data_adjustment,
                session_factory=lambda: baostock_session(),
                pace=lambda: pace_request(),
            )
        self.provider = provider or create_provider(settings)
        self._analysis_frame = None
        self._analysis_history = None
        self._analysis_date: str | None = None
        self._analysis_coverage: dict[str, int | float | str | None] = {}
        self._init_db()

    def prepare_analysis(self) -> None:
        """选择最近的高覆盖交易日，并读取截至该日的统一行情快照。

        数据同步过程中，较新的日期可能只有部分股票已经写入。若直接使用每只
        股票各自的最后一根日线，会把多个交易日混在同一次选股中。这里以历史
        单日最大覆盖数为基准，选择最近一个达到最低覆盖率的日期，并排除该日
        没有行情的股票。
        """
        with closing(sqlite3.connect(self.db_path)) as conn:
            coverage_rows = conn.execute(
                """SELECT date, COUNT(DISTINCT symbol) AS symbol_count
                   FROM stock_daily GROUP BY date ORDER BY date DESC"""
            ).fetchall()
            if not coverage_rows:
                self._analysis_frame = pd.DataFrame()
                self._analysis_history = {}
                self._analysis_date = None
                self._analysis_coverage = {}
                return

            peak_count = max(int(row[1]) for row in coverage_rows)
            minimum_count = math.ceil(peak_count * self.analysis_min_coverage)
            analysis_date, coverage_count = next(
                (str(day), int(count))
                for day, count in coverage_rows
                if int(count) >= minimum_count
            )
            frame = pd.read_sql_query(
                """SELECT symbol, date, open, high, low, close, volume, turnover FROM (
                    SELECT *, ROW_NUMBER() OVER (
                        PARTITION BY symbol ORDER BY date DESC
                    ) AS row_num FROM stock_daily
                    WHERE date <= ?
                ) WHERE row_num <= 180 ORDER BY symbol, date""", conn,
                params=(analysis_date,),
            )
        if not frame.empty:
            current_symbols = frame.groupby("symbol")["date"].transform("max") == analysis_date
            frame = frame[current_symbols].reset_index(drop=True)
        self._analysis_frame = frame
        self._analysis_history = {
            symbol: group.reset_index(drop=True)
            for symbol, group in frame.groupby("symbol", sort=False)
        }
        self._analysis_date = analysis_date
        self._analysis_coverage = {
            "date": analysis_date,
            "count": coverage_count,
            "peak_count": peak_count,
            "minimum_count": minimum_count,
            "ratio": coverage_count / peak_count if peak_count else 0.0,
            "latest_available_date": str(coverage_rows[0][0]),
            "latest_available_count": int(coverage_rows[0][1]),
        }
        logger.info(
            f"分析基准日 {analysis_date}：覆盖 {coverage_count}/{peak_count} 只 "
            f"({coverage_count / peak_count:.1%})"
        )

    @property
    def analysis_date(self) -> str | None:
        return self._analysis_date

    @property
    def analysis_coverage(self) -> dict[str, int | float | str | None]:
        return dict(self._analysis_coverage)

    def get_analysis_frame(self) -> pd.DataFrame:
        if self._analysis_frame is None:
            self.prepare_analysis()
        return self._analysis_frame.copy()

    def _init_db(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(_CREATE_TABLE_SQL)
            conn.execute(_CREATE_INDEX_SQL)
            conn.execute("""CREATE TABLE IF NOT EXISTS stock_basic (
                symbol TEXT PRIMARY KEY, name TEXT NOT NULL, updated_at TEXT NOT NULL
            )""")
            conn.commit()
        logger.info(f"数据库初始化完成：{self.db_path}")

    def _get_last_date(self, symbol: str) -> str | None:
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            row = conn.execute(
                "SELECT MAX(date) FROM stock_daily WHERE symbol = ?",
                (symbol,),
            ).fetchone()
        return row[0] if row and row[0] else None

    def get_ohlcv(self, symbol: str) -> pd.DataFrame:
        if self._analysis_history is not None:
            return self._analysis_history.get(symbol, pd.DataFrame()).copy()
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            df = pd.read_sql(
                "SELECT * FROM stock_daily WHERE symbol = ? ORDER BY date",
                conn,
                params=(symbol,),
            )
        return df

    # ── 数据同步 ──

    def sync_today_bulk(self) -> int:
        """单连接串行更新库内股票，任何数据源错误均停止后续请求。"""
        symbols = self.get_local_symbols()
        if not symbols:
            logger.warning("本地无股票数据，请先执行 --backfill")
            return 0
        return self._sync_symbols(symbols)

    def backfill(self, symbols: list[str]) -> None:
        """串行回填；按股票保存进度，失败后可在服务恢复时继续。"""
        count = self._sync_symbols(symbols)
        logger.info(f"回填完成，写入 {count} 条日线")

    def _sync_symbols(self, symbols: list[str]) -> int:
        from datetime import date, timedelta

        today_str = date.today().isoformat()
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            last_dates = dict(conn.execute(
                "SELECT symbol, MAX(date) FROM stock_daily GROUP BY symbol"
            ).fetchall())

        tasks = []
        for symbol in dict.fromkeys(symbols):
            last_date = last_dates.get(symbol)
            if last_date and last_date >= today_str:
                continue
            start = self.start_date
            if last_date:
                start = (date.fromisoformat(last_date) + timedelta(days=1)).isoformat()
            tasks.append((symbol, start))

        if not tasks:
            logger.info("无待更新股票")
            return 0

        logger.info(
            f"需要更新 {len(tasks)} 只股票，使用 {self.provider.name} "
            f"单会话串行拉取（复权={self.provider.adjustment}）"
        )
        count = 0
        with self.provider.session() as session:
            for i, (symbol, start) in enumerate(tasks, 1):
                df = session.fetch_daily(symbol, start, today_str)
                if not df.empty:
                    count += self._save_daily(df)
                if i % 100 == 0 or i == len(tasks):
                    logger.info(f"同步进度 {i}/{len(tasks)}，已写入 {count} 条日线")
        return count

    def _save_daily(self, df: pd.DataFrame) -> int:
        """只更新匹配的股票和日期，保留同日其他股票；每批事务提交。"""
        self._analysis_frame = None
        self._analysis_history = None
        self._analysis_date = None
        self._analysis_coverage = {}
        columns = ["symbol", "date", "open", "high", "low", "close", "volume", "turnover"]
        rows = list(df[columns].itertuples(index=False, name=None))
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.executemany(
                """
                INSERT INTO stock_daily (symbol, date, open, high, low, close, volume, turnover)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, date) DO UPDATE SET
                    open=excluded.open, high=excluded.high, low=excluded.low,
                    close=excluded.close, volume=excluded.volume, turnover=excluded.turnover
                """,
                rows,
            )
        return len(rows)

    def get_all_symbols(self) -> list[str]:
        """通过当前 Provider 获取上市股票代码；失败时终止。"""
        with self.provider.session() as session:
            records = session.list_stocks()
        self._save_stock_basic(records)
        symbols = [record.symbol for record in records if record.active]
        logger.info(f"获取股票列表完成，共 {len(symbols)} 只")
        return symbols

    def _save_stock_basic(self, records: list[StockRecord]) -> None:
        rows = [
            (record.symbol, record.name.strip(), date.today().isoformat())
            for record in records
            if record.security_type == "stock" and record.name.strip()
        ]
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.executemany("""INSERT INTO stock_basic(symbol, name, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET name=excluded.name, updated_at=excluded.updated_at
            """, rows)

    def get_stock_names(self) -> dict[str, str]:
        """读取持久化的股票简称，本方法不联网。"""
        with closing(sqlite3.connect(self.db_path)) as conn:
            return dict(conn.execute("SELECT symbol, name FROM stock_basic").fetchall())

    def get_stock_name_dates(self) -> dict[str, str]:
        with closing(sqlite3.connect(self.db_path)) as conn:
            return dict(conn.execute("SELECT symbol, updated_at FROM stock_basic").fetchall())

    def refresh_stock_names(self) -> dict[str, str]:
        """单连接批量补齐名称，不查询日线；失败时保留原有名称。"""
        with self.provider.session() as session:
            records = session.list_stocks()
        self._save_stock_basic(records)
        return self.get_stock_names()

    def get_local_symbols(self) -> list[str]:
        if self._analysis_history is not None:
            return list(self._analysis_history)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            rows = conn.execute(
                "SELECT DISTINCT symbol FROM stock_daily ORDER BY symbol"
            ).fetchall()
        return [row[0] for row in rows]
