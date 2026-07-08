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
