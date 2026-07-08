"""JournalStore 信号表测试。"""
import json

import pandas as pd
import pytest

from gxfc.store.duck_store import DuckStore
from gxfc.store.journal_store import JournalStore


@pytest.fixture()
def stores(tmp_path):
    s = DuckStore(str(tmp_path / "t.duckdb"))
    j = JournalStore(s.con)
    yield s, j
    s.close()


def test_信号落库与幂等重写(stores):
    _, j = stores
    df = pd.DataFrame({"代码": ["600000", "1"], "名称": ["甲", "乙"], "量比": [3.2, 2.5]})
    assert j.record_signals("20260707", "bottom_volume", df) == 2
    got = j.read_signals(strategy="bottom_volume")
    assert list(got["代码"]) == ["000001", "600000"]  # zfill(6) + 按代码升序
    assert list(got["signal_date"]) == ["2026-07-07", "2026-07-07"]
    # 同日同策略重写:旧行覆盖而非累积
    df2 = pd.DataFrame({"代码": ["600000"], "名称": ["甲"], "量比": [3.5]})
    assert j.record_signals("20260707", "bottom_volume", df2) == 1
    assert len(j.read_signals()) == 1


def test_空表信号只清旧行(stores):
    _, j = stores
    j.record_signals("20260707", "profit_fault",
                     pd.DataFrame({"代码": ["600000"], "名称": ["甲"]}))
    assert j.record_signals("20260707", "profit_fault", pd.DataFrame()) == 0
    assert j.read_signals().empty


def test_detail保留额外指标列(stores):
    _, j = stores
    df = pd.DataFrame({"代码": ["600000"], "名称": ["甲"], "量比": [3.2], "业绩高增": [True]})
    j.record_signals("20260707", "bottom_volume", df)
    detail = json.loads(j.read_signals().iloc[0]["detail"])
    assert float(detail["量比"]) == 3.2
    assert "代码" not in detail  # 主列不重复进 detail


def test_按日期与策略过滤(stores):
    _, j = stores
    j.record_signals("20260706", "profit_fault", pd.DataFrame({"代码": ["600000"], "名称": ["甲"]}))
    j.record_signals("20260707", "bottom_volume", pd.DataFrame({"代码": ["000001"], "名称": ["乙"]}))
    assert len(j.read_signals(strategy="profit_fault")) == 1
    assert len(j.read_signals(start="20260707")) == 1
    assert len(j.read_signals(end="20260706")) == 1
