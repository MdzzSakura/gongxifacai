"""采集编排测试:注入 FakeFetcher 与临时 DuckDB,全程不联网。"""
from datetime import datetime

import pandas as pd
import pytest

from gxfc.dates import derive_quarter_end
from gxfc.ingest import (
    IngestAborted,
    latest_closed_trading_day,
    run_ingest,
)
from gxfc.store.duck_store import DuckStore

# 固定"现在":2026-07-06(周一) 18:00,已收盘
_NOW = datetime(2026, 7, 6, 18, 0)


class FakeFetcher:
    """可编程假抓取器:按需覆盖各方法,记录调用次数。"""

    def __init__(self):
        self.calls = {}
        self.health = {}
        self.last_source = "假源"
        self._eastmoney_down = False

    def _count(self, name):
        self.calls[name] = self.calls.get(name, 0) + 1

    def trade_dates(self, start, end):
        self._count("trade_dates")
        return ["2026-07-02", "2026-07-03", "2026-07-06", "2026-07-07"]

    def zt_pool(self, date):
        self._count("zt_pool")
        return pd.DataFrame({"代码": ["600001"], "名称": ["甲"], "涨跌幅": [10.0]})

    def dt_pool(self, date):
        self._count("dt_pool")
        return pd.DataFrame({"代码": [], "名称": [], "涨跌幅": []})

    def zb_pool(self, date):
        self._count("zb_pool")
        return pd.DataFrame({"代码": ["000002"], "名称": ["乙"], "涨跌幅": [5.0]})

    def yjyg(self, quarter_end):
        self._count("yjyg")
        return pd.DataFrame({"股票代码": ["600001"], "股票简称": ["甲"],
                             "预测指标": ["归属于上市公司股东的净利润"], "业绩变动幅度": [80.0]})

    def industry_board(self):
        self._count("industry_board")
        return pd.DataFrame({"板块名称": ["半导体", "白酒"], "涨跌幅": [3.0, 1.0],
                             "领涨股票": ["甲", "乙"]})

    def industry_cons(self, board):
        self._count("industry_cons")
        return pd.DataFrame({"名称": ["甲"], "涨跌幅": [10.0], "成交额": [1e8]})

    def daily_snapshot(self):
        self._count("daily_snapshot")
        return pd.DataFrame({
            "代码": ["600001", "000002"], "名称": ["甲", "乙"],
            "今开": [10.0, 5.0], "最高": [11.5, 5.6], "最低": [9.9, 4.9],
            "收盘": [11.0, 5.5], "昨收": [10.0, 5.0],
            "成交量": [1e6, 2e6], "成交额": [1.1e7, 1.1e7], "换手率": [1.2, 0.5],
        })

    def stock_daily(self, code, start, end):
        self._count(f"stock_daily:{code}")
        self._count("stock_daily")
        return pd.DataFrame({
            "日期": ["2026-07-03", "2026-07-06"],
            "开盘": [9.5, 10.0], "收盘": [10.0, 11.0], "最高": [10.1, 11.5],
            "最低": [9.4, 9.9], "成交量": [8e5, 1e6], "成交额": [8e6, 1.1e7],
        })


@pytest.fixture()
def store(tmp_path):
    s = DuckStore(str(tmp_path / "t.duckdb"))
    yield s
    s.close()


def test_季度末推导():
    assert derive_quarter_end("2026-07-06") == "20260630"
    assert derive_quarter_end("20260331") == "20260331"
    assert derive_quarter_end("20260215") == "20251231"
    assert derive_quarter_end("20261001") == "20260930"


def test_最新已收盘交易日(store):
    store.upsert_calendar(["2026-07-03", "2026-07-06", "2026-07-07"])
    # 收盘后:取当天
    assert latest_closed_trading_day(store, datetime(2026, 7, 6, 18, 0)) == "2026-07-06"
    # 盘中:取上一交易日
    assert latest_closed_trading_day(store, datetime(2026, 7, 6, 10, 0)) == "2026-07-03"
    # 周日:取上周五(此处日历里最近是周一07-06,示意回溯语义)
    assert latest_closed_trading_day(store, datetime(2026, 7, 7, 9, 0)) == "2026-07-06"


def test_全流程落库与台账(store):
    f = FakeFetcher()
    summary = run_ingest(fetcher=f, store=store, now=_NOW)

    # 快照类入库
    assert len(store.read_snapshot("zt_pool", "trade_date", "2026-07-06")) == 1
    assert len(store.read_snapshot("yjyg", "quarter_end", "20260630")) == 1
    assert len(store.read_snapshot("industry_cons", "trade_date", "2026-07-06")) == 2
    # 空跌停池:台账记 empty,不建表
    assert store.has_ok("dt_pool", "2026-07-06") is True
    # 日K快照成型:2 票当日行入库 + 证券名录
    assert not store.read_daily("600001", "20260706", "20260706").empty
    assert set(store.security_codes()) == {"600001", "000002"}
    # 历史深度回补:首采两票各整段拉一次并打 daily_hist 标记
    assert f.calls["stock_daily"] == 2
    assert store.ok_periods("daily_hist") == {"600001", "000002"}
    statuses = dict(zip(summary["dataset"], summary["status"]))
    assert statuses["daily_snapshot"] == "ok"


def test_重跑续传_快照类不重采(store):
    f = FakeFetcher()
    run_ingest(fetcher=f, store=store, now=_NOW)
    first_calls = dict(f.calls)
    run_ingest(fetcher=f, store=store, now=_NOW)
    # 快照类与日K快照第二轮全部续传跳过,不再触网
    for name in ("zt_pool", "dt_pool", "zb_pool", "yjyg", "industry_board", "daily_snapshot"):
        assert f.calls[name] == first_calls[name] == 1, name
    # 历史深度回补只在首轮发生,第二轮零逐股请求
    assert f.calls["stock_daily"] == first_calls["stock_daily"] == 2
    # 日历每轮都刷新(1 次请求,可接受)
    assert f.calls["trade_dates"] == 2


def test_新股与缺口走回补(store):
    f = FakeFetcher()
    # 预置:名录里有两票,但库内只有 600001 到 07-03(缺 07-06),000002 全无
    store.upsert_calendar(["2026-07-03", "2026-07-06"])
    store.upsert_securities(pd.DataFrame({"代码": ["600001", "000002"], "名称": ["甲", "乙"]}))
    store.append_daily(pd.DataFrame({
        "代码": ["600001"], "日期": ["2026-07-03"], "开盘": [9.5], "收盘": [10.0],
        "最高": [10.1], "最低": [9.4], "成交量": [8e5], "成交额": [8e6],
    }))
    # 标记快照类均已采,聚焦回补;日K快照也标记已采(模拟快照源失败后的补救路径)
    for ds, period in [("zt_pool", "2026-07-06"), ("dt_pool", "2026-07-06"),
                       ("zb_pool", "2026-07-06"), ("yjyg", "20260630"),
                       ("industry_board", "2026-07-06"), ("industry_cons", "2026-07-06"),
                       ("daily_snapshot", "2026-07-06")]:
        store.log("pre", ds, period, "ok", rows=1)

    run_ingest(fetcher=f, store=store, now=_NOW)
    assert f.calls.get("stock_daily:600001") == 1   # 缺口票重拉
    assert f.calls.get("stock_daily:000002") == 1   # 新票回补
    assert not store.read_daily("000002", "20260701", "20260706").empty
    # 再跑一轮:均已到最新,零逐股请求
    run_ingest(fetcher=f, store=store, now=_NOW)
    assert f.calls["stock_daily"] == 2


def test_除权检测_删历史并重拉(store):
    f = FakeFetcher()
    store.upsert_calendar(["2026-07-03", "2026-07-06"])
    # 库内 600001 前收 20.0,而快照昨收 10.0 → 判为除权(比如 10转10)
    store.append_daily(pd.DataFrame({
        "代码": ["600001", "000002"], "日期": ["2026-07-03", "2026-07-03"],
        "开盘": [19.5, 4.8], "收盘": [20.0, 5.0], "最高": [20.1, 5.1],
        "最低": [19.4, 4.7], "成交量": [8e5, 9e5], "成交额": [8e6, 9e6],
    }))
    # 两票历史深度均已采过(聚焦除权路径,避免深度回补混入)
    store.log("pre", "daily_hist", "600001", "ok", rows=1)
    store.log("pre", "daily_hist", "000002", "ok", rows=1)
    for ds, period in [("zt_pool", "2026-07-06"), ("dt_pool", "2026-07-06"),
                       ("zb_pool", "2026-07-06"), ("yjyg", "20260630"),
                       ("industry_board", "2026-07-06"), ("industry_cons", "2026-07-06")]:
        store.log("pre", ds, period, "ok", rows=1)

    run_ingest(fetcher=f, store=store, now=_NOW)
    # 600001 旧历史被删,由回补重拉(FakeFetcher 返回 07-03/07-06 两行前复权)
    got = store.read_daily("600001", "20260701", "20260706")
    assert list(got["日期"]) == ["2026-07-03", "2026-07-06"]
    assert got.iloc[0]["收盘"] == 10.0   # 重拉后的前复权价,而非旧的 20.0
    assert f.calls.get("stock_daily:600001") == 1
    # 000002 昨收一致,不除权:当日行来自快照,历史行保留,不逐股拉
    assert f.calls.get("stock_daily:000002") is None
    got2 = store.read_daily("000002", "20260701", "20260706")
    assert list(got2["日期"]) == ["2026-07-03", "2026-07-06"]


def test_连续失败超预算中止(store):
    f = FakeFetcher()

    def 全挂(code, start, end):
        f._count("stock_daily")
        raise RuntimeError("网络整体故障")

    f.stock_daily = 全挂
    store.upsert_calendar(["2026-07-03", "2026-07-06"])
    codes = [f"60{i:04d}" for i in range(30)]
    store.upsert_securities(pd.DataFrame({"代码": codes, "名称": ["x"] * 30}))
    for ds, period in [("zt_pool", "2026-07-06"), ("dt_pool", "2026-07-06"),
                       ("zb_pool", "2026-07-06"), ("yjyg", "20260630"),
                       ("industry_board", "2026-07-06"), ("industry_cons", "2026-07-06"),
                       ("daily_snapshot", "2026-07-06")]:
        store.log("pre", ds, period, "ok", rows=1)

    with pytest.raises(IngestAborted):
        run_ingest(fetcher=f, store=store, now=_NOW)
    assert f.calls["stock_daily"] == 20   # 预算内即停,不烧完 30 只


def test_日K快照失败降级_当日交由回补(store):
    f = FakeFetcher()

    def 快照挂():
        f._count("daily_snapshot")
        raise RuntimeError("两源全挂")

    f.daily_snapshot = 快照挂
    store.upsert_calendar(["2026-07-03", "2026-07-06"])
    store.upsert_securities(pd.DataFrame({"代码": ["600001"], "名称": ["甲"]}))
    for ds, period in [("zt_pool", "2026-07-06"), ("dt_pool", "2026-07-06"),
                       ("zb_pool", "2026-07-06"), ("yjyg", "20260630"),
                       ("industry_board", "2026-07-06"), ("industry_cons", "2026-07-06")]:
        store.log("pre", ds, period, "ok", rows=1)

    run_ingest(fetcher=f, store=store, now=_NOW)
    # 快照失败不致命:600001 通过逐股回补补齐当日
    assert not store.read_daily("600001", "20260706", "20260706").empty
    # 台账记 failed,下轮会重试快照
    assert store.has_ok("daily_snapshot", "2026-07-06") is False


def test_盘中运行_快照跳过走回补(store):
    """盘中跑采集:目标日是上一交易日,但实时快照反映的是今天盘中价,
    绝不能当作目标日的日K写库(会污染 OHLC 与成交量),当日行走逐股回补。"""
    f = FakeFetcher()
    store.upsert_securities(pd.DataFrame({"代码": ["600001"], "名称": ["甲"]}))
    run_ingest(fetcher=f, store=store, now=datetime(2026, 7, 7, 10, 0))  # 盘中,目标日 07-06

    assert f.calls.get("daily_snapshot") is None          # 快照一次都不能拉
    assert store.has_ok("daily_snapshot", "2026-07-06") is False
    # 07-06 的日K由逐股回补补齐(来源是真正的日K接口,非实时快照)
    assert not store.read_daily("600001", "20260706", "20260706").empty


def test_显式指定未收盘日_拒绝采集(store):
    """显式传今天的日期但尚未收盘:拒绝采集(否则快照/回补都会拿到盘中残缺数据)。"""
    f = FakeFetcher()
    summary = run_ingest(date="20260707", fetcher=f, store=store,
                         now=datetime(2026, 7, 7, 10, 0))
    assert f.calls.get("daily_snapshot") is None
    assert f.calls.get("zt_pool") is None
    assert len(summary) == 0


def test_非交易日直接退出(store):
    f = FakeFetcher()
    summary = run_ingest(date="20260705", fetcher=f, store=store, now=_NOW)  # 周日
    assert f.calls.get("zt_pool") is None
    assert len(summary) == 0
