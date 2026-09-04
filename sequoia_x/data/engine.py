"""数据引擎模块：负责 SQLite 行情数据存储与 baostock 增量同步。"""

import sqlite3
from datetime import date
from contextlib import closing
from pathlib import Path

import pandas as pd

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger
from sequoia_x.data.baostock_session import baostock_session, pace_request, read_rows

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
    """行情数据引擎，负责 SQLite 存储和 baostock 数据同步。"""

    def __init__(self, settings: Settings) -> None:
        self.db_path: str = settings.db_path
        self.start_date: str = settings.start_date
        self._analysis_frame = None
        self._analysis_history = None
        self._init_db()

    def prepare_analysis(self) -> None:
        """一次性读取每只股票最近 180 根日线，供所有策略和报告共用同一快照。"""
        with closing(sqlite3.connect(self.db_path)) as conn:
            frame = pd.read_sql_query(
                """SELECT symbol, date, open, high, low, close, volume, turnover FROM (
                    SELECT *, ROW_NUMBER() OVER (
                        PARTITION BY symbol ORDER BY date DESC
                    ) AS row_num FROM stock_daily
                ) WHERE row_num <= 180 ORDER BY symbol, date""", conn,
            )
        self._analysis_frame = frame
        self._analysis_history = {
            symbol: group.reset_index(drop=True)
            for symbol, group in frame.groupby("symbol", sort=False)
        }

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

    @staticmethod
    def _to_baostock_code(symbol: str) -> str:
        """将纯数字代码转为 baostock 格式：6/9开头 -> sh，其余 -> sz。"""
        prefix = "sh" if symbol.startswith(("6", "9")) else "sz"
        return f"{prefix}.{symbol}"

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

        logger.info(f"需要更新 {len(tasks)} 只股票，使用单连接串行拉取")
        count = 0
        with baostock_session() as bs:
            for i, (symbol, start) in enumerate(tasks, 1):
                pace_request()
                rs = bs.query_history_k_data_plus(
                    self._to_baostock_code(symbol),
                    "date,open,high,low,close,volume,amount",
                    start_date=start, end_date=today_str,
                    frequency="d", adjustflag="1",
                )
                rows = read_rows(rs, f"查询 {symbol}")
                if rows:
                    df = pd.DataFrame(rows, columns=rs.fields)
                    for col in ["open", "high", "low", "close", "volume", "amount"]:
                        df[col] = pd.to_numeric(df[col], errors="coerce")
                    df = df.dropna(subset=["close"])
                    df = df[df["volume"] > 0]
                    df["symbol"] = symbol
                    df = df.rename(columns={"amount": "turnover"})
                    count += self._save_daily(df)
                if i % 100 == 0 or i == len(tasks):
                    logger.info(f"同步进度 {i}/{len(tasks)}，已写入 {count} 条日线")
        return count

    def _save_daily(self, df: pd.DataFrame) -> int:
        """只更新匹配的股票和日期，保留同日其他股票；每批事务提交。"""
        self._analysis_frame = None
        self._analysis_history = None
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
        """通过 baostock 获取上市股票代码；失败时终止，而非返回空成功。"""
        with baostock_session() as bs:
            pace_request()
            rs = bs.query_stock_basic(code_name="", code="")
            rows = read_rows(rs, "查询股票列表")
            self._save_stock_basic([dict(zip(rs.fields, row)) for row in rows])
            symbols = []
            for row in rows:
                info = dict(zip(rs.fields, row))
                if info["status"] == "1" and info["type"] == "1":
                    symbols.append(info["code"].split(".")[1])
        logger.info(f"获取股票列表完成，共 {len(symbols)} 只")
        return symbols

    def _save_stock_basic(self, records: list[dict]) -> None:
        rows = [(r["code"].split(".")[-1], r["code_name"].strip(), date.today().isoformat())
                for r in records if r.get("type") == "1" and r.get("code_name", "").strip()]
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
        with baostock_session() as bs:
            pace_request()
            rs = bs.query_stock_basic(code_name="", code="")
            rows = read_rows(rs, "批量查询股票名称")
            records = [dict(zip(rs.fields, row)) for row in rows]
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
