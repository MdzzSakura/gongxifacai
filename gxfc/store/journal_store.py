"""信号与交易日志存储:与行情库同一 DuckDB 文件,复用外部连接。

- signals:每日筛选产出的候选(信号),(signal_date, strategy, 代码) 主键,
  同 (日期, 策略) 重写幂等。**不落价格**——收益一律评估时从 daily 表现算,
  保证与除权自愈重拉后的前复权序列同源(存快照价反而会在除权后失真)。
- trades:手工交易日志,开仓(计划)/平仓(执行)两阶段,支撑纪律统计。

连接由调用方传入(DuckDB 单写者,与 DuckStore 共用连接避免文件锁冲突)。
"""
import json
import logging
from typing import Optional

import pandas as pd

from gxfc.dates import dash

logger = logging.getLogger(__name__)

_DDL = [
    '''CREATE TABLE IF NOT EXISTS signals (
        signal_date TEXT, strategy TEXT, "代码" TEXT, "名称" TEXT,
        detail TEXT,
        PRIMARY KEY (signal_date, strategy, "代码"))''',
    '''CREATE TABLE IF NOT EXISTS trades (
        trade_id TEXT PRIMARY KEY,
        "代码" TEXT, "名称" TEXT, strategy TEXT, plan TEXT,
        open_date TEXT, open_price DOUBLE, shares INTEGER,
        close_date TEXT, close_price DOUBLE,
        exit_reason TEXT, followed_plan BOOLEAN, note TEXT)''',
]


class JournalStore:
    def __init__(self, con):
        self._con = con
        for ddl in _DDL:
            self._con.execute(ddl)

    # —— 信号 ——

    def record_signals(self, signal_date: str, strategy: str, df: pd.DataFrame) -> int:
        """记录某日某策略的候选。df 需含 代码/名称 列,其余列打包进 detail(JSON)。

        同 (日期, 策略) 先删后插,幂等;空表只清旧行返回 0(重跑 screen 且
        当日无候选时,不残留上一次的旧信号)。返回写入行数。
        """
        d = dash(signal_date)
        self._con.execute(
            "DELETE FROM signals WHERE signal_date = ? AND strategy = ?", [d, strategy]
        )
        if df is None or df.empty:
            return 0
        rows = []
        for _, r in df.iterrows():
            code = str(r["代码"]).strip().zfill(6)
            extra = {k: v for k, v in r.items() if k not in ("代码", "名称")}
            rows.append({
                "signal_date": d, "strategy": strategy, "代码": code,
                "名称": str(r.get("名称", "")),
                "detail": json.dumps(extra, ensure_ascii=False, default=str),
            })
        data = pd.DataFrame(rows).drop_duplicates(subset=["代码"], keep="first")
        self._con.register("_sig_v", data)
        try:
            self._con.execute(
                'INSERT INTO signals '
                'SELECT signal_date, strategy, "代码", "名称", detail FROM _sig_v'
            )
        finally:
            self._con.unregister("_sig_v")
        return len(data)

    def read_signals(self, strategy: Optional[str] = None,
                     start: Optional[str] = None,
                     end: Optional[str] = None) -> pd.DataFrame:
        """按策略/日期范围读信号,升序。start/end 兼容 'YYYYMMDD'。"""
        sql = "SELECT * FROM signals WHERE 1=1"
        params: list = []
        if strategy:
            sql += " AND strategy = ?"
            params.append(strategy)
        if start:
            sql += " AND signal_date >= ?"
            params.append(dash(start))
        if end:
            sql += " AND signal_date <= ?"
            params.append(dash(end))
        sql += ' ORDER BY signal_date, strategy, "代码"'
        return self._con.execute(sql, params).df()

    # —— 交易日志 ——

    def _next_trade_id(self, open_date: str) -> str:
        """按开仓日生成 T<YYYYMMDD>-<3位序号>(同日自增)。"""
        d = dash(open_date).replace("-", "")
        row = self._con.execute(
            "SELECT count(*) FROM trades WHERE trade_id LIKE ?", [f"T{d}-%"]
        ).fetchone()
        return f"T{d}-{row[0] + 1:03d}"

    def add_trade(self, code: str, name: str, strategy: str, plan: str,
                  open_date: str, open_price: float, shares: int) -> str:
        """开仓:计划(plan)是买入理由+卖出规则,开仓时必须写全。返回 trade_id。"""
        trade_id = self._next_trade_id(open_date)
        self._con.execute(
            "INSERT INTO trades VALUES (?, ?, ?, ?, ?, ?, ?, ?, "
            "NULL, NULL, NULL, NULL, NULL)",
            [trade_id, str(code).strip().zfill(6), name, strategy, plan,
             dash(open_date), float(open_price), int(shares)],
        )
        return trade_id

    def close_trade(self, trade_id: str, close_date: str, close_price: float,
                    exit_reason: str, followed_plan: bool, note: str = "") -> None:
        """平仓:记录执行结果与是否守纪。不存在或已平仓抛 ValueError。"""
        row = self._con.execute(
            "SELECT close_date FROM trades WHERE trade_id = ?", [trade_id]
        ).fetchone()
        if row is None:
            raise ValueError(f"交易 {trade_id} 不存在")
        if row[0] is not None:
            raise ValueError(f"交易 {trade_id} 已平仓({row[0]}),不可重复平仓")
        self._con.execute(
            "UPDATE trades SET close_date = ?, close_price = ?, exit_reason = ?, "
            "followed_plan = ?, note = ? WHERE trade_id = ?",
            [dash(close_date), float(close_price), exit_reason,
             bool(followed_plan), note, trade_id],
        )

    def list_trades(self, open_only: bool = False) -> pd.DataFrame:
        """交易清单;open_only=True 只看持仓中。按开仓日+编号升序。"""
        sql = "SELECT * FROM trades"
        if open_only:
            sql += " WHERE close_date IS NULL"
        sql += " ORDER BY open_date, trade_id"
        return self._con.execute(sql).df()
