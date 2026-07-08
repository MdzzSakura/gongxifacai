"""本地 DuckDB 数据存储:采集阶段写入,筛选阶段只读。

三类表:
- 业务快照表(zt_pool/industry_board/yjyg 等):按周期列(trade_date 或 quarter_end)
  upsert 累积,列结构以首次入库的真实数据为准,后续自动对齐(缺列补 NULL、多列丢弃)。
- 序列表 daily:全市场日K,(代码,日期) 主键增量追加;成交量单位统一为"股"。
- 系统表 trade_calendar(交易日历,增量判断的骨架)、ingest_log(采集台账,快照类
  续传依据)、securities(代码→名称,离线筛选展示用)。

中文列名在 SQL 中一律用双引号包裹。日期在库内统一 'YYYY-MM-DD' 字符串,
对外接口兼容 'YYYYMMDD' 传参(内部归一)。
"""
import logging
from datetime import datetime
from typing import Iterable, List, Optional

import duckdb
import pandas as pd

from gxfc.dates import dash

logger = logging.getLogger(__name__)

# daily 表固定列(东财中文列口径,全源归一后统一;成交量单位=股)
_DAILY_COLS = ["代码", "日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额", "换手率"]

_DDL = [
    '''CREATE TABLE IF NOT EXISTS daily (
        "代码" TEXT, "日期" TEXT, "开盘" DOUBLE, "收盘" DOUBLE, "最高" DOUBLE,
        "最低" DOUBLE, "成交量" DOUBLE, "成交额" DOUBLE, "换手率" DOUBLE,
        PRIMARY KEY ("代码", "日期"))''',
    '''CREATE TABLE IF NOT EXISTS trade_calendar (
        calendar_date TEXT PRIMARY KEY)''',
    '''CREATE TABLE IF NOT EXISTS ingest_log (
        run_id TEXT, dataset TEXT, period TEXT, status TEXT,
        rows INTEGER, source TEXT, error TEXT, finished_at TIMESTAMP)''',
    '''CREATE TABLE IF NOT EXISTS securities (
        "代码" TEXT PRIMARY KEY, "名称" TEXT)''',
]


class DuckStore:
    def __init__(self, db_path: str):
        self._con = duckdb.connect(db_path)
        for ddl in _DDL:
            self._con.execute(ddl)

    def close(self) -> None:
        self._con.close()

    # —— 通用 ——

    def table_exists(self, table: str) -> bool:
        row = self._con.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name = ?",
            [table],
        ).fetchone()
        return row[0] > 0

    def _table_columns(self, table: str) -> List[str]:
        rows = self._con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = ? ORDER BY ordinal_position",
            [table],
        ).fetchall()
        return [r[0] for r in rows]

    # —— 快照表(按期 upsert) ——

    def upsert_snapshot(self, table: str, period_col: str, period: str, df: pd.DataFrame) -> int:
        """删 period 旧行再插,幂等。空表不建表不写入(由台账记 empty)。

        表结构以首批数据为准;后续批次自动对齐:缺列补 NULL,新增列丢弃并告警
        (akshare 升级列变更时不让采集整体失败,靠质量闸门保证必需列存在)。
        返回写入行数。
        """
        if df is None or df.empty:
            return 0
        data = df.copy()
        data.insert(0, period_col, dash(period) if period_col == "trade_date" else str(period))
        self._con.register("_snap_v", data)
        try:
            if not self.table_exists(table):
                self._con.execute(f'CREATE TABLE "{table}" AS SELECT * FROM _snap_v')
                return len(data)
            existing = self._table_columns(table)
            extra = [c for c in data.columns if c not in existing]
            if extra:
                logger.warning("表 %s 出现新列 %s,本次丢弃(如需保留请迁移表结构)", table, extra)
            cols = [c for c in existing if c in data.columns]
            col_sql = ", ".join(f'"{c}"' for c in cols)
            self._con.execute(
                f'DELETE FROM "{table}" WHERE "{period_col}" = ?', [data.iloc[0][period_col]]
            )
            self._con.execute(
                f'INSERT INTO "{table}" ({col_sql}) SELECT {col_sql} FROM _snap_v'
            )
            return len(data)
        finally:
            self._con.unregister("_snap_v")

    def read_snapshot(self, table: str, period_col: str, period: str) -> pd.DataFrame:
        """读某期快照;表不存在或无该期数据返回空表。"""
        if not self.table_exists(table):
            return pd.DataFrame()
        period = dash(period) if period_col == "trade_date" else str(period)
        return self._con.execute(
            f'SELECT * FROM "{table}" WHERE "{period_col}" = ?', [period]
        ).df()

    # —— 日K 序列表 ——

    def append_daily(self, df: pd.DataFrame) -> int:
        """插入日K,(代码,日期) 去重(批内保留最后一条,库内已有则跳过)。返回新增行数。"""
        if df is None or df.empty:
            return 0
        data = df.copy()
        data["日期"] = data["日期"].map(dash)
        data["代码"] = data["代码"].astype(str).str.zfill(6)
        for col in _DAILY_COLS:
            if col not in data.columns:
                data[col] = None
        data = data[_DAILY_COLS]
        self._con.register("_daily_v", data)
        try:
            row = self._con.execute(
                '''INSERT INTO daily
                   SELECT * FROM (
                     SELECT * FROM _daily_v
                     QUALIFY row_number() OVER (PARTITION BY "代码","日期" ORDER BY 1 DESC) = 1
                   ) v
                   WHERE NOT EXISTS (
                     SELECT 1 FROM daily d WHERE d."代码" = v."代码" AND d."日期" = v."日期"
                   )'''
            ).fetchone()
            return int(row[0])
        finally:
            self._con.unregister("_daily_v")

    def read_daily(self, code: str, start: str, end: str) -> pd.DataFrame:
        """读单票日K窗口(闭区间),按日期升序。start/end 兼容 'YYYYMMDD'。"""
        return self._con.execute(
            'SELECT * FROM daily WHERE "代码" = ? AND "日期" BETWEEN ? AND ? ORDER BY "日期"',
            [str(code).zfill(6), dash(start), dash(end)],
        ).df()

    def daily_last_close(self) -> pd.DataFrame:
        """每票最后一日的 (代码, 日期, 收盘),用于除权检测与缺口计算。空库返回空表。"""
        return self._con.execute(
            '''SELECT "代码", "日期", "收盘" FROM daily
               QUALIFY row_number() OVER (PARTITION BY "代码" ORDER BY "日期" DESC) = 1'''
        ).df()

    def delete_daily(self, codes: Iterable[str]) -> None:
        """删除指定票的全部历史(除权自愈:删旧序列后重拉前复权全窗口)。"""
        codes = [str(c).zfill(6) for c in codes]
        if not codes:
            return
        self._con.register("_del_v", pd.DataFrame({"代码": codes}))
        try:
            self._con.execute(
                'DELETE FROM daily WHERE "代码" IN (SELECT "代码" FROM _del_v)'
            )
        finally:
            self._con.unregister("_del_v")

    def read_market_pct(self, date: str) -> pd.DataFrame:
        """离线重建某交易日全市场行情视图:代码/名称/涨跌幅/最新价/成交量/成交额。

        涨跌幅由 daily 表当日收盘对上一有效收盘现算,口径与实时快照一致;
        当日无行的票(停牌/未采集)自然缺席。供 screen 的情绪家数与底部爆量粗筛。
        """
        d = dash(date)
        return self._con.execute(
            '''WITH r AS (
                 SELECT "代码", "日期", "收盘", "成交量", "成交额",
                        lag("收盘") OVER (PARTITION BY "代码" ORDER BY "日期") AS prev_close
                 FROM daily WHERE "日期" <= ?
               )
               SELECT r."代码",
                      coalesce(s."名称", '') AS "名称",
                      round(("收盘" / prev_close - 1) * 100, 2) AS "涨跌幅",
                      "收盘" AS "最新价",
                      "成交量",
                      "成交额"
               FROM r LEFT JOIN securities s ON r."代码" = s."代码"
               WHERE r."日期" = ? AND prev_close IS NOT NULL''',
            [d, d],
        ).df()

    def daily_max_date(self) -> Optional[str]:
        """daily 表最新日期(离线筛选默认目标日);空库返回 None。"""
        row = self._con.execute('SELECT max("日期") FROM daily').fetchone()
        return row[0]

    # —— 交易日历 ——

    def upsert_calendar(self, dates: Iterable[str]) -> None:
        df = pd.DataFrame({"calendar_date": [dash(d) for d in dates]})
        if df.empty:
            return
        self._con.register("_cal_v", df)
        try:
            self._con.execute(
                "INSERT OR IGNORE INTO trade_calendar SELECT calendar_date FROM _cal_v"
            )
        finally:
            self._con.unregister("_cal_v")

    def trading_days(self, start: str, end: str) -> List[str]:
        """[start, end] 闭区间内的交易日,升序。"""
        rows = self._con.execute(
            "SELECT calendar_date FROM trade_calendar "
            "WHERE calendar_date BETWEEN ? AND ? ORDER BY calendar_date",
            [dash(start), dash(end)],
        ).fetchall()
        return [r[0] for r in rows]

    def calendar_max(self) -> Optional[str]:
        row = self._con.execute("SELECT max(calendar_date) FROM trade_calendar").fetchone()
        return row[0]

    def prev_trading_day(self, date: str) -> Optional[str]:
        row = self._con.execute(
            "SELECT max(calendar_date) FROM trade_calendar WHERE calendar_date < ?",
            [dash(date)],
        ).fetchone()
        return row[0]

    # —— 证券名录 ——

    def upsert_securities(self, df: pd.DataFrame) -> None:
        """按代码 upsert 名称(来源:全市场快照)。"""
        if df is None or df.empty:
            return
        data = df[["代码", "名称"]].copy()
        data["代码"] = data["代码"].astype(str).str.zfill(6)
        data = data.drop_duplicates(subset=["代码"], keep="last")
        self._con.register("_sec_v", data)
        try:
            self._con.execute(
                'INSERT OR REPLACE INTO securities SELECT "代码", "名称" FROM _sec_v'
            )
        finally:
            self._con.unregister("_sec_v")

    def security_codes(self) -> List[str]:
        rows = self._con.execute('SELECT "代码" FROM securities ORDER BY "代码"').fetchall()
        return [r[0] for r in rows]

    # —— 采集台账 ——

    def log(self, run_id: str, dataset: str, period: str, status: str,
            rows: int = 0, source: str = "", error: str = "") -> None:
        self._con.execute(
            "INSERT INTO ingest_log VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [run_id, dataset, str(period), status, rows, source, error[:500], datetime.now()],
        )

    def has_ok(self, dataset: str, period: str) -> bool:
        """该 (dataset, period) 是否已成功采集(含"采过但为空")。快照类续传依据。"""
        row = self._con.execute(
            "SELECT count(*) FROM ingest_log "
            "WHERE dataset = ? AND period = ? AND status IN ('ok', 'empty')",
            [dataset, str(period)],
        ).fetchone()
        return row[0] > 0

    def ok_periods(self, dataset: str) -> set:
        """该数据集所有已成功采集的 period 集合(一次查询,替代逐 period has_ok)。

        历史深度回补用 dataset='daily_hist'、period=股票代码,全市场 5000+ 只
        逐只查询太慢,这里整批取回由调用方做集合差。
        """
        rows = self._con.execute(
            "SELECT DISTINCT period FROM ingest_log "
            "WHERE dataset = ? AND status IN ('ok', 'empty')",
            [dataset],
        ).fetchall()
        return {r[0] for r in rows}

    def clear_ok(self, dataset: str, period: str) -> None:
        """撤销采集完成标记(除权删历史后须撤销该票的 daily_hist 标记以触发重拉)。"""
        self._con.execute(
            "DELETE FROM ingest_log WHERE dataset = ? AND period = ?",
            [dataset, str(period)],
        )

    def last_ok_rows(self, dataset: str) -> Optional[int]:
        """该数据集最近一次成功采集的行数,供快照行数骤降检查作基准。"""
        row = self._con.execute(
            "SELECT rows FROM ingest_log WHERE dataset = ? AND status = 'ok' "
            "ORDER BY finished_at DESC LIMIT 1",
            [dataset],
        ).fetchone()
        return row[0] if row else None

    def run_summary(self, run_id: str) -> pd.DataFrame:
        return self._con.execute(
            "SELECT dataset, period, status, rows, source, error FROM ingest_log "
            "WHERE run_id = ? ORDER BY finished_at",
            [run_id],
        ).df()
