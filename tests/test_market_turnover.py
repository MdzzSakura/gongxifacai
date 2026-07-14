"""DuckStore.market_turnover:两市总成交额离线重建的三态行为。"""
import pandas as pd

from gxfc.store.duck_store import DuckStore

_DAYS = ["2026-07-01", "2026-07-02", "2026-07-03", "2026-07-06", "2026-07-07", "2026-07-08"]


def _daily(code: str, dates_amounts) -> pd.DataFrame:
    return pd.DataFrame([
        {"代码": code, "日期": d, "开盘": 10.0, "收盘": 10.0, "最高": 10.0,
         "最低": 10.0, "成交量": 1e6, "成交额": amt, "换手率": 1.0}
        for d, amt in dates_amounts
    ])


def test_正常_今日总额与前5日均额(tmp_path):
    store = DuckStore(str(tmp_path / "t.duckdb"))
    try:
        # 单票逐日 100,200,...,600;另一票每日 50,验证按日加总
        store.append_daily(_daily("600000", [(d, 100.0 * (i + 1)) for i, d in enumerate(_DAYS)]))
        store.append_daily(_daily("000001", [(d, 50.0) for d in _DAYS]))
        turnover, baseline = store.market_turnover("2026-07-08")
        assert turnover == 650.0                    # 600 + 50
        assert baseline == 350.0                    # (150+250+350+450+550)/5
    finally:
        store.close()


def test_当日无行返回双None(tmp_path):
    store = DuckStore(str(tmp_path / "t.duckdb"))
    try:
        store.append_daily(_daily("600000", [(d, 100.0) for d in _DAYS[:-1]]))
        assert store.market_turnover("2026-07-08") == (None, None)
    finally:
        store.close()


def test_历史不足5日基准为None(tmp_path):
    store = DuckStore(str(tmp_path / "t.duckdb"))
    try:
        store.append_daily(_daily("600000", [(d, 100.0) for d in _DAYS[2:]]))  # 仅4天
        turnover, baseline = store.market_turnover("2026-07-08")
        assert turnover == 100.0
        assert baseline is None
    finally:
        store.close()
