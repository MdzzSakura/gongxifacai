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


def test_开仓自动编号与持仓清单(stores):
    _, j = stores
    tid1 = j.add_trade("600000", "甲", "profit_fault", "断层+情绪回暖,破5日线止损",
                       "20260707", 10.0, 1000)
    tid2 = j.add_trade("1", "乙", "bottom_volume", "爆量首板,烂板即走",
                       "20260707", 5.0, 2000)
    assert tid1 == "T20260707-001"
    assert tid2 == "T20260707-002"
    trades = j.list_trades(open_only=True)
    assert len(trades) == 2
    assert list(trades["代码"]) == ["600000", "000001"]  # zfill(6),按开仓日+编号排序


def test_平仓与重复平仓拒绝(stores):
    _, j = stores
    tid = j.add_trade("600000", "甲", "profit_fault", "断层", "20260707", 10.0, 1000)
    j.close_trade(tid, "20260710", 11.0, "规则卖点", True, "按计划止盈")
    assert j.list_trades(open_only=True).empty
    closed = j.list_trades().iloc[0]
    assert closed["close_price"] == 11.0
    assert bool(closed["followed_plan"]) is True
    with pytest.raises(ValueError, match="已平仓"):
        j.close_trade(tid, "20260711", 12.0, "x", True)
    with pytest.raises(ValueError, match="不存在"):
        j.close_trade("T20990101-001", "20260711", 12.0, "x", True)
