"""唯一读取层：全部查询走 read_only 短连接，即用即关。

DuckDB 同一文件只允许"一个写者，或多个只读者"：本层绝不持有常驻连接，
保证网页开着时采集/记账子进程随时能拿到写锁。
本模块不做任何 Streamlit 调用，保持纯函数可单测；缓存由页面层套壳。
"""
import contextlib
from pathlib import Path
from typing import Optional

import pandas as pd

from gxfc.dates import dash, derive_quarter_end
from gxfc.review.daily_board import DailyBoard
from gxfc.review.tracker import summarize, track_signals, trade_stats
from gxfc.screen import build_board_offline, load_config
from gxfc.store.duck_store import DuckStore


@contextlib.contextmanager
def open_store(db_path: str):
    """只读短连接上下文：进入即连，退出即关。"""
    store = DuckStore(db_path, read_only=True)
    try:
        yield store
    finally:
        store.close()


def trading_dates(db_path: str, limit: int = 120) -> list:
    """daily 表内有数据的交易日，倒序；无表/无数据返回空列表。"""
    with open_store(db_path) as store:
        if not store.table_exists("daily"):
            return []
        rows = store.con.execute(
            'SELECT DISTINCT "日期" FROM daily ORDER BY "日期" DESC LIMIT ?', [limit]
        ).fetchall()
    return [r[0] for r in rows]


def load_board(db_path: str, date: str) -> DailyBoard:
    """组装某日复盘面板，各段独立降级（口径与 python -m gxfc.screen 完全一致）。"""
    config = load_config()
    with open_store(db_path) as store:
        return build_board_offline(store, date, derive_quarter_end(dash(date)), config)


def signal_strategies(db_path: str) -> list:
    """signals 表中出现过的策略名；表不存在返回空列表。"""
    with open_store(db_path) as store:
        if not store.table_exists("signals"):
            return []
        rows = store.con.execute(
            "SELECT DISTINCT strategy FROM signals ORDER BY strategy"
        ).fetchall()
    return [r[0] for r in rows]


def tracking_report(db_path: str, strategy: Optional[str] = None,
                    start: Optional[str] = None,
                    end: Optional[str] = None) -> tuple:
    """信号前向收益 (明细, 汇总)。signals 缺失或无信号返回两张空表。"""
    horizons = tuple(load_config().get("tracking", {}).get("horizons", (1, 3, 5, 10)))
    with open_store(db_path) as store:
        if not store.table_exists("signals"):
            return pd.DataFrame(), pd.DataFrame()
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
        signals = store.con.execute(sql, params).df()
        if signals.empty:
            return pd.DataFrame(), pd.DataFrame()
        perf = track_signals(signals, store.read_daily, horizons)
    return perf, summarize(perf, horizons)


def list_trades(db_path: str, open_only: bool = False) -> pd.DataFrame:
    """交易清单；trades 表不存在返回空表。"""
    with open_store(db_path) as store:
        if not store.table_exists("trades"):
            return pd.DataFrame()
        sql = "SELECT * FROM trades"
        if open_only:
            sql += " WHERE close_date IS NULL"
        sql += " ORDER BY open_date, trade_id"
        return store.con.execute(sql).df()


def trade_stats_report(db_path: str) -> pd.DataFrame:
    """计划-执行-纪律三组统计（口径同 python -m gxfc.journal stats）。"""
    return trade_stats(list_trades(db_path))


def db_overview(db_path: str) -> Optional[dict]:
    """库状态总览：各表行数、日K最新日期、最近台账。库文件不存在返回 None。"""
    if not Path(db_path).exists():
        return None
    with open_store(db_path) as store:
        tables = [r[0] for r in store.con.execute("SHOW TABLES").fetchall()]
        counts = pd.DataFrame(
            [(t, store.con.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0])
             for t in tables],
            columns=["表", "行数"],
        )
        daily_max = store.daily_max_date() if store.table_exists("daily") else None
        recent_log = (
            store.con.execute(
                "SELECT run_id, dataset, period, status, rows, source, finished_at "
                "FROM ingest_log ORDER BY finished_at DESC LIMIT 20"
            ).df()
            if store.table_exists("ingest_log") else pd.DataFrame()
        )
    return {"tables": counts, "daily_max": daily_max, "recent_log": recent_log}
