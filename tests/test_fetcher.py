import pandas as pd
import pytest
from gxfc.data.cache import DataFrameCache
from gxfc.data.fetcher import (
    Fetcher,
    _sina_symbol,
    _sina_daily_to_em_schema,
    _sina_sector_to_em_schema,
    _sina_detail_to_em_schema,
)


def test_loader成功时返回数据并写入缓存(tmp_path):
    cache = DataFrameCache(str(tmp_path / "t.db"))
    fetcher = Fetcher(cache=cache)
    calls = {"n": 0}

    def loader():
        calls["n"] += 1
        return pd.DataFrame({"a": [1]})

    got = fetcher.fetch("k1", loader)
    assert list(got["a"]) == [1]
    assert calls["n"] == 1
    # 第二次应命中缓存,不再调用 loader
    fetcher.fetch("k1", loader)
    assert calls["n"] == 1


def test_loader失败时按次数重试(tmp_path):
    fetcher = Fetcher(cache=None, retries=3)
    calls = {"n": 0}

    def loader():
        calls["n"] += 1
        raise RuntimeError("模拟网络错误")

    with pytest.raises(RuntimeError):
        fetcher.fetch("k2", loader)
    assert calls["n"] == 3


def test_重试中途成功则返回(tmp_path):
    fetcher = Fetcher(cache=None, retries=3)
    calls = {"n": 0}

    def loader():
        calls["n"] += 1
        if calls["n"] < 2:
            raise RuntimeError("第一次失败")
        return pd.DataFrame({"ok": [1]})

    got = fetcher.fetch("k3", loader)
    assert calls["n"] == 2
    assert list(got["ok"]) == [1]


def test_新浪代码市场前缀映射():
    assert _sina_symbol("000001") == "sz000001"   # 深主板
    assert _sina_symbol("301531") == "sz301531"   # 创业板
    assert _sina_symbol("603435") == "sh603435"   # 沪主板
    assert _sina_symbol("688981") == "sh688981"   # 科创板
    assert _sina_symbol("920136") == "bj920136"   # 北交所
    assert _sina_symbol("1237") == "sz001237"     # 不足6位自动补零


def test_新浪日K归一化为东财列结构():
    sina = pd.DataFrame({
        "date": ["2026-06-26", "2026-06-29"],
        "open": [62.0, 64.0], "high": [63.0, 65.0], "low": [58.0, 64.0],
        "close": [59.0, 65.0], "volume": [1, 2], "amount": [3, 4], "turnover": [0.1, 0.2],
    })
    em = _sina_daily_to_em_schema(sina)
    assert {"日期", "开盘", "最高", "最低", "收盘", "成交量", "成交额", "换手率"} <= set(em.columns)
    assert list(em["日期"]) == ["2026-06-26", "2026-06-29"]


def test_东财日K失败时回退新浪并归一化(tmp_path, monkeypatch):
    import akshare as ak
    cache = DataFrameCache(str(tmp_path / "d.db"))
    fetcher = Fetcher(cache=cache, retries=1, min_interval=0)

    def 东财失败(**kwargs):
        raise RuntimeError("Connection aborted RemoteDisconnected")

    def 新浪成功(symbol, **kwargs):
        assert symbol == "sz001237"  # 6位代码应转成带前缀
        return pd.DataFrame({
            "date": ["2026-06-26", "2026-06-29"],
            "open": [62.0, 64.0], "high": [63.0, 65.0], "low": [58.0, 64.0],
            "close": [59.0, 65.0], "volume": [1, 2], "amount": [3, 4], "turnover": [0.1, 0.2],
        })

    monkeypatch.setattr(ak, "stock_zh_a_hist", 东财失败)
    monkeypatch.setattr(ak, "stock_zh_a_daily", 新浪成功)

    df = fetcher.stock_daily("001237", "20260620", "20260630")
    # 回退结果对下游透明:含东财中文列,可直接喂给 detect_gap
    assert "日期" in df.columns and "开盘" in df.columns and "最高" in df.columns
    assert list(df["日期"]) == ["2026-06-26", "2026-06-29"]
    # 成功结果应已写入缓存:再次调用直接命中,不再触发任何源
    monkeypatch.setattr(ak, "stock_zh_a_daily", lambda **k: pytest.fail("不应再触网"))
    again = fetcher.stock_daily("001237", "20260620", "20260630")
    assert list(again["日期"]) == ["2026-06-26", "2026-06-29"]


def test_新浪行业榜归一化为东财板块榜列():
    sina = pd.DataFrame({
        "label": ["new_blhy"], "板块": ["玻璃行业"], "涨跌幅": [2.85],
        "股票名称": ["旗滨集团"], "个股-涨跌幅": [10.0],
    })
    em = _sina_sector_to_em_schema(sina)
    assert {"板块名称", "涨跌幅", "领涨股票"} <= set(em.columns)
    assert em.iloc[0]["板块名称"] == "玻璃行业"
    assert em.iloc[0]["领涨股票"] == "旗滨集团"


def test_新浪成分股归一化为东财成分股列():
    sina = pd.DataFrame({
        "name": ["中国船舶", "亚星锚链"], "changepercent": [4.42, 10.0],
        "amount": [9.4e9, 1.2e9], "code": ["600150", "601890"],
    })
    em = _sina_detail_to_em_schema(sina)
    assert {"名称", "涨跌幅", "成交额"} <= set(em.columns)
    assert list(em["名称"]) == ["中国船舶", "亚星锚链"]


def test_东财实时快照失败时回退新浪(monkeypatch):
    import akshare as ak
    fetcher = Fetcher(cache=None, retries=1, min_interval=0)
    monkeypatch.setattr(ak, "stock_zh_a_spot_em", lambda: (_ for _ in ()).throw(RuntimeError("断连")))
    monkeypatch.setattr(ak, "stock_zh_a_spot", lambda: pd.DataFrame({"涨跌幅": [1.0, -2.0, 0.0]}))
    df = fetcher.spot()
    assert "涨跌幅" in df.columns
    assert int((df["涨跌幅"] > 0).sum()) == 1


def test_东财板块榜失败时回退新浪行业(tmp_path, monkeypatch):
    import akshare as ak
    fetcher = Fetcher(cache=DataFrameCache(str(tmp_path / "b.db")), retries=1, min_interval=0)
    monkeypatch.setattr(
        ak, "stock_board_industry_name_em",
        lambda: (_ for _ in ()).throw(RuntimeError("RemoteDisconnected")),
    )
    monkeypatch.setattr(
        ak, "stock_sector_spot",
        lambda indicator: pd.DataFrame({
            "label": ["new_blhy"], "板块": ["玻璃行业"], "涨跌幅": [2.85], "股票名称": ["旗滨集团"],
        }),
    )
    df = fetcher.industry_board()
    assert df.iloc[0]["板块名称"] == "玻璃行业" and df.iloc[0]["领涨股票"] == "旗滨集团"


def test_东财成分股失败时回退新浪并按名反查label(tmp_path, monkeypatch):
    import akshare as ak
    fetcher = Fetcher(cache=DataFrameCache(str(tmp_path / "c.db")), retries=1, min_interval=0)
    monkeypatch.setattr(
        ak, "stock_board_industry_cons_em",
        lambda symbol: (_ for _ in ()).throw(RuntimeError("断连")),
    )
    monkeypatch.setattr(
        ak, "stock_sector_spot",
        lambda indicator: pd.DataFrame({"label": ["new_blhy"], "板块": ["玻璃行业"]}),
    )

    captured = {}

    def 假下钻(sector):
        captured["label"] = sector  # 应传 label 而非中文名
        return pd.DataFrame({"name": ["旗滨集团"], "changepercent": [10.0], "amount": [1e9]})

    monkeypatch.setattr(ak, "stock_sector_detail", 假下钻)
    df = fetcher.industry_cons("玻璃行业")
    assert captured["label"] == "new_blhy"
    assert {"名称", "涨跌幅", "成交额"} <= set(df.columns)
    assert df.iloc[0]["名称"] == "旗滨集团"
