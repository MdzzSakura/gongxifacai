import duckdb
import pandas as pd
import pytest

from gxfc.store.duck_store import DuckStore


def _one_row_daily() -> pd.DataFrame:
    return pd.DataFrame({
        "代码": ["600000"], "日期": ["2026-07-08"], "开盘": [10.0], "收盘": [11.0],
        "最高": [11.1], "最低": [9.9], "成交量": [1e6], "成交额": [1e7], "换手率": [1.0],
    })


def test_只读模式可读禁写(tmp_path):
    db = str(tmp_path / "ro.duckdb")
    w = DuckStore(db)
    w.append_daily(_one_row_daily())
    w.close()
    r = DuckStore(db, read_only=True)
    try:
        assert r.daily_max_date() == "2026-07-08"
        with pytest.raises(duckdb.Error):
            r.con.execute("CREATE TABLE t(i INTEGER)")
    finally:
        r.close()


def test_只读模式不执行DDL(tmp_path):
    db = str(tmp_path / "bare.duckdb")
    duckdb.connect(db).close()          # 裸空库,无任何表
    r = DuckStore(db, read_only=True)   # 只读构造不得执行 DDL,否则此处抛错
    try:
        assert r.table_exists("daily") is False
    finally:
        r.close()
