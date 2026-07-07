import pandas as pd
import pytest

from gxfc.store.duck_store import DuckStore


@pytest.fixture()
def store(tmp_path):
    s = DuckStore(str(tmp_path / "t.duckdb"))
    yield s
    s.close()


def _daily_df(rows):
    return pd.DataFrame(rows, columns=["代码", "日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额", "换手率"])


def test_新建库含系统表_业务快照表懒建(store):
    assert store.table_exists("daily") is True
    assert store.table_exists("trade_calendar") is True
    assert store.table_exists("ingest_log") is True
    assert store.table_exists("zt_pool") is False


def test_快照upsert同期幂等覆盖(store):
    df1 = pd.DataFrame({"代码": ["600000"], "名称": ["甲"], "涨跌幅": [10.0]})
    assert store.upsert_snapshot("zt_pool", "trade_date", "20260706", df1) == 1
    # 同期重写:旧行被覆盖而非累积
    df2 = pd.DataFrame({"代码": ["600000", "000001"], "名称": ["甲", "乙"], "涨跌幅": [10.0, 9.9]})
    assert store.upsert_snapshot("zt_pool", "trade_date", "20260706", df2) == 2
    got = store.read_snapshot("zt_pool", "trade_date", "20260706")
    assert len(got) == 2
    # 不同期各自保留
    store.upsert_snapshot("zt_pool", "trade_date", "20260707", df1)
    assert len(store.read_snapshot("zt_pool", "trade_date", "20260707")) == 1
    assert len(store.read_snapshot("zt_pool", "trade_date", "20260706")) == 2


def test_快照列变更自动对齐(store):
    store.upsert_snapshot("zt_pool", "trade_date", "20260706",
                          pd.DataFrame({"代码": ["600000"], "名称": ["甲"]}))
    # 后续批次缺"名称"列、多"新列":缺列补NULL,新列丢弃,不报错
    store.upsert_snapshot("zt_pool", "trade_date", "20260707",
                          pd.DataFrame({"代码": ["000001"], "新列": [1]}))
    got = store.read_snapshot("zt_pool", "trade_date", "20260707")
    assert list(got["代码"]) == ["000001"]
    assert pd.isna(got.iloc[0]["名称"])
    assert "新列" not in got.columns


def test_快照空表不写入(store):
    assert store.upsert_snapshot("zt_pool", "trade_date", "20260706", pd.DataFrame()) == 0
    assert store.table_exists("zt_pool") is False
    assert store.read_snapshot("zt_pool", "trade_date", "20260706").empty


def test_日K追加去重与读取(store):
    df = _daily_df([
        ["600000", "2026-07-06", 10, 11, 11.5, 9.9, 1e6, 1.1e7, 1.2],
        ["600000", "2026-07-06", 10, 11, 11.5, 9.9, 1e6, 1.1e7, 1.2],  # 批内重复
        ["000001", "20260706", 5, 5.5, 5.6, 4.9, 2e6, 1.1e7, None],    # 兼容YYYYMMDD
    ])
    assert store.append_daily(df) == 2
    # 库内已有 (代码,日期) 不重插
    assert store.append_daily(df) == 0
    got = store.read_daily("600000", "20260701", "20260710")
    assert list(got["日期"]) == ["2026-07-06"]
    assert got.iloc[0]["收盘"] == 11


def test_日K缺换手率列自动补NULL(store):
    df = pd.DataFrame({"代码": ["600000"], "日期": ["2026-07-06"], "开盘": [10.0],
                       "收盘": [11.0], "最高": [11.5], "最低": [9.9],
                       "成交量": [1e6], "成交额": [1.1e7]})
    assert store.append_daily(df) == 1
    got = store.read_daily("600000", "20260701", "20260710")
    assert pd.isna(got.iloc[0]["换手率"])


def test_每票最后收盘与删除重拉(store):
    store.append_daily(_daily_df([
        ["600000", "2026-07-03", 10, 10.5, 11, 9.9, 1e6, 1e7, 1.0],
        ["600000", "2026-07-06", 10, 11, 11.5, 9.9, 1e6, 1.1e7, 1.2],
        ["000001", "2026-07-06", 5, 5.5, 5.6, 4.9, 2e6, 1.1e7, 0.5],
    ]))
    last = store.daily_last_close().set_index("代码")
    assert last.loc["600000", "日期"] == "2026-07-06"
    assert last.loc["600000", "收盘"] == 11
    store.delete_daily(["600000"])
    assert store.read_daily("600000", "20260101", "20261231").empty
    assert not store.read_daily("000001", "20260101", "20261231").empty


def test_离线重建市场行情视图(store):
    store.append_daily(_daily_df([
        ["600000", "2026-07-03", 10, 10.0, 11, 9.9, 1e6, 1e7, 1.0],
        ["600000", "2026-07-06", 10, 11.0, 11.5, 9.9, 1e6, 1.1e7, 1.2],
        ["000001", "2026-07-06", 5, 5.5, 5.6, 4.9, 2e6, 1.1e7, 0.5],   # 无前收,应缺席
    ]))
    store.upsert_securities(pd.DataFrame({"代码": ["600000"], "名称": ["甲"]}))
    got = store.read_market_pct("20260706")
    assert list(got["代码"]) == ["600000"]
    assert got.iloc[0]["涨跌幅"] == 10.0
    assert got.iloc[0]["名称"] == "甲"
    assert got.iloc[0]["最新价"] == 11.0


def test_交易日历upsert幂等与查询(store):
    store.upsert_calendar(["2026-07-03", "20260706", "2026-07-07"])
    store.upsert_calendar(["2026-07-06"])  # 重复插入不报错
    assert store.trading_days("20260704", "20260707") == ["2026-07-06", "2026-07-07"]
    assert store.calendar_max() == "2026-07-07"
    assert store.prev_trading_day("20260707") == "2026-07-06"
    assert store.prev_trading_day("20260703") is None


def test_台账记录与续传判断(store):
    assert store.has_ok("zt_pool", "2026-07-06") is False
    store.log("run1", "zt_pool", "2026-07-06", "failed", error="断连")
    assert store.has_ok("zt_pool", "2026-07-06") is False   # 失败不算采过
    store.log("run2", "zt_pool", "2026-07-06", "ok", rows=30, source="东财")
    assert store.has_ok("zt_pool", "2026-07-06") is True
    store.log("run2", "dt_pool", "2026-07-06", "empty", rows=0, source="东财")
    assert store.has_ok("dt_pool", "2026-07-06") is True    # 空结果也算采过
    summary = store.run_summary("run2")
    assert set(summary["dataset"]) == {"zt_pool", "dt_pool"}


def test_证券名录upsert覆盖(store):
    store.upsert_securities(pd.DataFrame({"代码": ["600000"], "名称": ["旧名"]}))
    store.upsert_securities(pd.DataFrame({"代码": ["600000", "1"], "名称": ["新名", "乙"]}))
    got = store.read_market_pct  # 名称经 read_market_pct 验证过,这里直接查表
    rows = dict(store._con.execute('SELECT "代码","名称" FROM securities').fetchall())
    assert rows["600000"] == "新名"
    assert rows["000001"] == "乙"   # 代码补零
