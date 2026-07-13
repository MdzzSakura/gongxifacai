"""web 层测试共享夹具：临时 DuckDB 造最小数据集。"""
import pandas as pd
import pytest

from gxfc.store.duck_store import DuckStore
from gxfc.store.journal_store import JournalStore


@pytest.fixture
def seeded_db(tmp_path) -> str:
    """含 6 行日K、1 条信号、1 笔已平仓交易的临时库，写完即关（供只读层测试）。"""
    db = str(tmp_path / "seeded.duckdb")
    store = DuckStore(db)
    store.append_daily(pd.DataFrame({
        "代码": ["600000"] * 6,
        "日期": ["2026-07-01", "2026-07-02", "2026-07-03",
               "2026-07-06", "2026-07-07", "2026-07-08"],
        "开盘": [10.0] * 6,
        "收盘": [10.0, 10.2, 10.5, 10.4, 10.8, 11.0],
        "最高": [10.1, 10.3, 10.6, 10.5, 10.9, 11.1],
        "最低": [9.9] * 6,
        "成交量": [1e6] * 6, "成交额": [1e7] * 6, "换手率": [1.0] * 6,
    }))
    store.upsert_securities(pd.DataFrame({"代码": ["600000"], "名称": ["浦发银行"]}))
    journal = JournalStore(store.con)
    journal.record_signals("2026-07-02", "profit_fault",
                           pd.DataFrame({"代码": ["600000"], "名称": ["浦发银行"]}))
    tid = journal.add_trade("600000", "浦发银行", "profit_fault",
                            "测试计划：破5日线止损", "2026-07-02", 10.2, 1000)
    journal.close_trade(tid, "2026-07-07", 10.8, "规则卖点", True, "")
    store.close()
    return db
