"""离线筛选测试:预置 DuckDB 数据组装面板,全程零网络(screen 不依赖 fetcher)。"""
import pandas as pd
import pytest

from gxfc.screen import build_board_offline, run_screen
from gxfc.store.duck_store import DuckStore
from gxfc.store.journal_store import JournalStore

_DATE = "2026-07-06"
_QUARTER = "20260630"

_CONFIG = {
    "emotion": {"hot_up_count": 4500, "cold_up_count": 800},
    "sector": {"top_n": 10, "core_drill_top_n": 3, "core_top_n": 5},
    "profit_fault": {"growth_threshold": 50.0},
    "bottom_volume": {
        "rise_threshold": 7.0, "volume_ratio_threshold": 2.0, "bottom_ratio": 0.6,
        "bottom_window": 60, "volume_baseline": 5, "max_survivors": 60, "top_n": 50,
    },
}


@pytest.fixture()
def store(tmp_path):
    s = DuckStore(str(tmp_path / "t.duckdb"))
    yield s
    s.close()


def _seed_daily_series(store, code, closes, highs=None, volumes=None):
    """连续交易日日K(2026-03 起,确保全部早于目标日),便于构造量比/底部场景。"""
    n = len(closes)
    highs = highs or closes
    volumes = volumes or [1e6] * n
    days = pd.date_range("2026-03-02", periods=n + 10, freq="B")[:n]
    dates = [d.strftime("%Y-%m-%d") for d in days]
    store.append_daily(pd.DataFrame({
        "代码": [code] * n, "日期": dates,
        "开盘": closes, "收盘": closes, "最高": highs, "最低": closes,
        "成交量": volumes, "成交额": [c * v for c, v in zip(closes, volumes)],
    }))
    return dates


def test_未采集时全面板降级但不崩(store):
    board = build_board_offline(store, _DATE, _QUARTER, _CONFIG)
    assert "未采集" in board.emotion.sentiment_hint
    assert board.sectors.empty
    assert board.candidates.empty
    assert board.surge_candidates.empty


def test_情绪段离线组装(store):
    store.upsert_snapshot("zt_pool", "trade_date", _DATE, pd.DataFrame({
        "代码": ["600001", "600002"], "名称": ["甲", "乙"],
        "涨跌幅": [10.0, 10.0], "连板数": [3, 1],
    }))
    store.upsert_snapshot("zb_pool", "trade_date", _DATE, pd.DataFrame({
        "代码": ["600003"], "名称": ["丙"], "涨跌幅": [5.0], "炸板次数": [1],
    }))
    store.log("r", "zt_pool", _DATE, "ok", rows=2)
    store.log("r", "dt_pool", _DATE, "empty", rows=0)
    store.log("r", "zb_pool", _DATE, "ok", rows=1)
    # 全市场两票:一涨一跌 → 家数 1/1
    store.append_daily(pd.DataFrame({
        "代码": ["000001", "000002"] * 2,
        "日期": ["2026-07-03", "2026-07-03", "2026-07-06", "2026-07-06"],
        "开盘": [10, 5, 10, 5], "收盘": [10.0, 5.0, 11.0, 4.5],
        "最高": [10, 5, 11, 5], "最低": [10, 4, 10, 4.4],
        "成交量": [1e6] * 4, "成交额": [1e7] * 4,
    }))

    board = build_board_offline(store, _DATE, _QUARTER, _CONFIG)
    e = board.emotion
    assert e.limit_up == 2 and e.limit_down == 0
    assert e.highest_streak == 3
    assert e.broken_board_rate == pytest.approx(1 / 3)
    assert e.up_count == 1 and e.down_count == 1


def test_板块段离线组装(store):
    store.upsert_snapshot("industry_board", "trade_date", _DATE, pd.DataFrame({
        "板块名称": ["半导体", "白酒"], "涨跌幅": [3.0, 1.0], "领涨股票": ["甲", "乙"],
    }))
    store.upsert_snapshot("industry_cons", "trade_date", _DATE, pd.DataFrame({
        "板块名称": ["半导体", "半导体"], "名称": ["甲", "丁"],
        "涨跌幅": [10.0, 8.0], "成交额": [1e8, 5e7],
    }))
    board = build_board_offline(store, _DATE, _QUARTER, _CONFIG)
    assert list(board.sectors["板块名称"]) == ["半导体", "白酒"]
    assert "半导体" in board.sector_cores
    assert list(board.sector_cores["半导体"]["名称"]) == ["甲", "丁"]


def test_断层段离线组装_含跳空(store):
    store.upsert_snapshot("yjyg", "quarter_end", _QUARTER, pd.DataFrame({
        "股票代码": ["600001", "600002"], "股票简称": ["甲", "乙"],
        "预测指标": ["归属于上市公司股东的净利润"] * 2,
        "业绩变动幅度": [80.0, 30.0],   # 乙增速不达标
    }))
    # 甲:今开(11.0) > 昨高(10.5) → 跳空
    store.append_daily(pd.DataFrame({
        "代码": ["600001", "600001"], "日期": ["2026-07-03", "2026-07-06"],
        "开盘": [10.0, 11.0], "收盘": [10.2, 11.5], "最高": [10.5, 11.8],
        "最低": [9.9, 10.9], "成交量": [1e6, 2e6], "成交额": [1e7, 2e7],
    }))
    board = build_board_offline(store, _DATE, _QUARTER, _CONFIG)
    assert list(board.candidates["股票代码"]) == ["600001"]


def test_底部爆量段离线组装_历史严格取当日之前(store):
    # 60001A:前期高点 20,长期缩量,目标日放量大涨至 11(距高点回落 45%)
    closes = [20.0] + [10.0] * 58
    volumes = [1e6] * 59
    dates = _seed_daily_series(store, "600010", closes, volumes=volumes)
    # 目标日行:涨 10%,量 5 倍
    store.append_daily(pd.DataFrame({
        "代码": ["600010"], "日期": [_DATE], "开盘": [10.0], "收盘": [11.0],
        "最高": [11.2], "最低": [9.9], "成交量": [5e6], "成交额": [5.5e7],
    }))
    store.upsert_securities(pd.DataFrame({"代码": ["600010"], "名称": ["丙"]}))
    assert dates[-1] < _DATE   # 预置历史确实都在目标日之前

    board = build_board_offline(store, _DATE, _QUARTER, _CONFIG)
    surge = board.surge_candidates
    assert list(surge["代码"]) == ["600010"]
    assert surge.iloc[0]["量比"] == pytest.approx(5.0)   # 量比只用之前5日均量,不含当日
    assert surge.iloc[0]["业绩高增"] is False or surge.iloc[0]["业绩高增"] == False  # noqa: E712


def _daily_rows_for_surge():
    """构造一只票:低位横盘 6 日后,目标日放量 10 倍大涨 10%,命中底部爆量三条件。"""
    cols = ["代码", "日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额", "换手率"]
    rows = [
        ["600000", "2026-06-29", 19.0, 19.0, 20.0, 18.5, 100, 1900, 1.0],
        ["600000", "2026-06-30", 13.0, 12.0, 12.5, 11.8, 100, 1200, 1.0],
        ["600000", "2026-07-01", 12.0, 11.0, 11.2, 10.8, 100, 1100, 1.0],
        ["600000", "2026-07-02", 11.0, 10.5, 10.8, 10.3, 100, 1050, 1.0],
        ["600000", "2026-07-03", 10.5, 10.2, 10.4, 10.0, 100, 1020, 1.0],
        ["600000", "2026-07-06", 10.2, 10.0, 10.2, 9.9, 100, 1000, 1.0],
        # 目标日:涨幅 +10%(≥7),量比 1000/100=10(≥2),收11 ≤ 前高20×0.6=12(底部)
        ["600000", "2026-07-07", 10.0, 11.0, 11.1, 10.0, 1000, 11000, 5.0],
    ]
    return pd.DataFrame(rows, columns=cols)


def test_run_screen候选自动落库为信号(tmp_path):
    db = str(tmp_path / "t.duckdb")
    store = DuckStore(db)
    store.upsert_calendar(["2026-06-29", "2026-06-30", "2026-07-01", "2026-07-02",
                           "2026-07-03", "2026-07-06", "2026-07-07"])
    store.append_daily(_daily_rows_for_surge())
    store.close()

    run_screen(date="20260707", db_path=db, out_dir=str(tmp_path / "out"))

    store = DuckStore(db)
    try:
        j = JournalStore(store.con)
        surge = j.read_signals(strategy="bottom_volume")
        assert list(surge["代码"]) == ["600000"]
        assert surge.iloc[0]["signal_date"] == "2026-07-07"
        # 断层段无业绩预告数据 → 空信号但不报错
        assert j.read_signals(strategy="profit_fault").empty
    finally:
        store.close()


def test_量能状态接线(tmp_path):
    """三池已采集且日K充足时,情绪段量能不再是"数据不足"。"""
    from gxfc.screen import _emotion_offline
    store = DuckStore(str(tmp_path / "s.duckdb"))
    try:
        days = ["2026-07-01", "2026-07-02", "2026-07-03",
                "2026-07-06", "2026-07-07", "2026-07-08"]
        store.append_daily(pd.DataFrame([
            {"代码": "600000", "日期": d, "开盘": 10.0, "收盘": 10.0, "最高": 10.0,
             "最低": 10.0, "成交量": 1e6, "成交额": 1e8, "换手率": 1.0}
            for d in days
        ]))
        date = "2026-07-08"
        store.upsert_snapshot("zt_pool", "trade_date", date,
                              pd.DataFrame({"代码": ["600000"], "名称": ["甲"],
                                            "连板数": [1], "炸板次数": [0]}))
        store.upsert_snapshot("dt_pool", "trade_date", date,
                              pd.DataFrame({"代码": ["000002"], "名称": ["乙"]}))
        store.upsert_snapshot("zb_pool", "trade_date", date,
                              pd.DataFrame({"代码": ["000003"], "名称": ["丙"]}))
        store.log("t", "zt_pool", date, "ok")
        emo_cfg = {"hot_up_count": 4500, "cold_up_count": 800,
                   "volume_up_ratio": 1.15, "volume_down_ratio": 0.85}
        e = _emotion_offline(store, date, emo_cfg)
        assert e.volume_state == "平量(1.00)"   # 每日总额相同 → 比值恰为 1
    finally:
        store.close()
