"""track CLI 端到端测试(以库内种子数据驱动,零网络)。"""
import pandas as pd
import pytest

from gxfc.store.duck_store import DuckStore
from gxfc.store.journal_store import JournalStore
from gxfc.track import run_track

_COLS = ["代码", "日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额", "换手率"]


@pytest.fixture()
def seeded_db(tmp_path):
    db = str(tmp_path / "t.duckdb")
    store = DuckStore(db)
    store.append_daily(pd.DataFrame([
        ["600000", "2026-07-06", 10.0, 10.0, 10.2, 9.9, 100, 1000, 1.0],
        ["600000", "2026-07-07", 10.0, 11.0, 11.1, 10.0, 1000, 11000, 5.0],
    ], columns=_COLS))
    j = JournalStore(store.con)
    j.record_signals("2026-07-06", "bottom_volume",
                     pd.DataFrame({"代码": ["600000"], "名称": ["甲"]}))
    store.close()
    return db


def test_run_track出明细与汇总(seeded_db, tmp_path):
    perf, summary = run_track(db_path=seeded_db, out_dir=str(tmp_path / "out"))
    assert len(perf) == 1
    assert bool(perf.iloc[0]["可追踪"]) is True
    assert perf.iloc[0]["T+1收益%"] == 10.0
    t1 = summary[summary["持有期"] == "T+1"].iloc[0]
    assert t1["样本数"] == 1 and t1["胜率%"] == 100.0


def test_无信号时友好退出(tmp_path, capsys):
    db = str(tmp_path / "empty.duckdb")
    DuckStore(db).close()
    perf, summary = run_track(db_path=db, out_dir=str(tmp_path / "out"))
    assert perf is None and summary is None
    assert "无信号" in capsys.readouterr().out
