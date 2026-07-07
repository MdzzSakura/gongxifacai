import pandas as pd
import pytest
import gxfc.data.fetcher as fetcher_mod
from gxfc.data.fetcher import (
    Fetcher,
    _sina_symbol,
    _sina_daily_to_em_schema,
    _sina_sector_to_em_schema,
    _sina_detail_to_em_schema,
    _baostock_symbol,
    _baostock_daily_to_em_schema,
    _em_spot_to_daily_snapshot,
    _sina_spot_to_daily_snapshot,
)


def test_loader成功时返回数据():
    fetcher = Fetcher(min_interval=0)
    calls = {"n": 0}

    def loader():
        calls["n"] += 1
        return pd.DataFrame({"a": [1]})

    got = fetcher.fetch("k1", loader)
    assert list(got["a"]) == [1]
    assert calls["n"] == 1


def test_loader失败时按次数重试():
    fetcher = Fetcher(retries=3, min_interval=0)
    calls = {"n": 0}

    def loader():
        calls["n"] += 1
        raise RuntimeError("模拟网络错误")

    with pytest.raises(RuntimeError):
        fetcher.fetch("k2", loader)
    assert calls["n"] == 3


def test_重试中途成功则返回():
    fetcher = Fetcher(retries=3, min_interval=0)
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


def test_baostock代码前缀映射():
    assert _baostock_symbol("000001") == "sz.000001"   # 深
    assert _baostock_symbol("300750") == "sz.300750"   # 创业板
    assert _baostock_symbol("603435") == "sh.603435"   # 沪
    assert _baostock_symbol("688981") == "sh.688981"   # 科创板


def test_baostock日K归一化为东财列并转数值():
    # baostock 数值以字符串返回
    bao = pd.DataFrame({
        "date": ["2026-06-26", "2026-06-29"],
        "open": ["62.88", "64.45"], "high": ["63.38", "65.20"], "low": ["58.88", "64.45"],
        "close": ["59.27", "65.20"], "volume": ["3665070", "6247219"],
        "amount": ["220254957.17", "406823970.50"],
    })
    em = _baostock_daily_to_em_schema(bao)
    assert {"日期", "开盘", "最高", "最低", "收盘", "成交量", "成交额"} <= set(em.columns)
    assert em["开盘"].dtype.kind == "f"   # 已转 float
    assert list(em["日期"]) == ["2026-06-26", "2026-06-29"]


def test_日K优先用baostock_其他源不触发(monkeypatch):
    import akshare as ak
    fetcher = Fetcher(retries=1, min_interval=0)
    monkeypatch.setattr(
        fetcher_mod, "_baostock_daily",
        lambda code, s, e: pd.DataFrame({"日期": ["2026-06-29"], "开盘": [64.0], "最高": [65.2]}),
    )
    monkeypatch.setattr(ak, "stock_zh_a_daily", lambda **k: pytest.fail("不应调用新浪"))
    monkeypatch.setattr(ak, "stock_zh_a_hist", lambda **k: pytest.fail("不应调用东财"))

    df = fetcher.stock_daily("001237", "20260620", "20260630")
    assert list(df["日期"]) == ["2026-06-29"]


def test_日K_baostock失败回退新浪并归一化(monkeypatch):
    import akshare as ak
    fetcher = Fetcher(retries=1, min_interval=0)
    monkeypatch.setattr(
        fetcher_mod, "_baostock_daily",
        lambda code, s, e: (_ for _ in ()).throw(RuntimeError("baostock 无数据")),
    )

    def 新浪成功(symbol, **kwargs):
        assert symbol == "sz001237"  # 6位代码应转成带前缀
        return pd.DataFrame({
            "date": ["2026-06-26", "2026-06-29"],
            "open": [62.0, 64.0], "high": [63.0, 65.0], "low": [58.0, 64.0],
            "close": [59.0, 65.0], "volume": [1, 2], "amount": [3, 4], "turnover": [0.1, 0.2],
        })

    monkeypatch.setattr(ak, "stock_zh_a_daily", 新浪成功)
    monkeypatch.setattr(ak, "stock_zh_a_hist", lambda **k: pytest.fail("不应走到东财"))

    df = fetcher.stock_daily("001237", "20260620", "20260630")
    assert "日期" in df.columns and "开盘" in df.columns and "最高" in df.columns
    assert list(df["日期"]) == ["2026-06-26", "2026-06-29"]


def test_日K东财兜底时成交量手转股(monkeypatch):
    import akshare as ak
    fetcher = Fetcher(retries=1, min_interval=0)
    monkeypatch.setattr(
        fetcher_mod, "_baostock_daily",
        lambda code, s, e: (_ for _ in ()).throw(RuntimeError("baostock 挂了")),
    )
    monkeypatch.setattr(
        ak, "stock_zh_a_daily",
        lambda **k: (_ for _ in ()).throw(RuntimeError("新浪也挂了")),
    )
    monkeypatch.setattr(ak, "stock_zh_a_hist", lambda **k: pd.DataFrame({
        "日期": ["2026-06-29"], "开盘": [64.0], "最高": [65.2], "成交量": [1000],  # 手
    }))
    df = fetcher.stock_daily("001237", "20260620", "20260630")
    assert df.iloc[0]["成交量"] == 100000   # 手 → 股


def test_非东财源连续失败三次后本轮熔断(monkeypatch):
    import akshare as ak
    fetcher = Fetcher(retries=1, min_interval=0)
    bao_calls = {"n": 0}

    def baostock挂(code, s, e):
        bao_calls["n"] += 1
        raise RuntimeError("baostock 维护中")

    monkeypatch.setattr(fetcher_mod, "_baostock_daily", baostock挂)
    monkeypatch.setattr(ak, "stock_zh_a_daily", lambda **k: pd.DataFrame({
        "date": ["2026-06-29"], "open": [1.0], "high": [1.0], "low": [1.0],
        "close": [1.0], "volume": [1], "amount": [1], "turnover": [0.1],
    }))

    for i in range(5):
        fetcher.stock_daily(f"00000{i + 1}", "20260620", "20260630")
    # 前3次尝试 baostock 均失败触发熔断,第4/5次直接走新浪不再碰 baostock
    assert bao_calls["n"] == 3
    assert fetcher.health["baostock"]["consec"] == 3
    assert fetcher.health["新浪"]["ok"] == 5


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
    fetcher = Fetcher(retries=1, min_interval=0)
    monkeypatch.setattr(ak, "stock_zh_a_spot_em", lambda: (_ for _ in ()).throw(RuntimeError("断连")))
    monkeypatch.setattr(ak, "stock_zh_a_spot", lambda: pd.DataFrame({"涨跌幅": [1.0, -2.0, 0.0]}))
    df = fetcher.spot()
    assert "涨跌幅" in df.columns
    assert int((df["涨跌幅"] > 0).sum()) == 1


def test_东财板块榜失败时回退新浪行业(monkeypatch):
    import akshare as ak
    fetcher = Fetcher(retries=1, min_interval=0)
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


def test_东财成分股失败时回退新浪并按名反查label(monkeypatch):
    import akshare as ak
    fetcher = Fetcher(retries=1, min_interval=0)
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


def test_全市场快照归一化代码去前缀(monkeypatch):
    import akshare as ak
    fetcher = Fetcher(retries=1, min_interval=0)
    monkeypatch.setattr(ak, "stock_zh_a_spot", lambda: pd.DataFrame({
        "代码": ["bj920000", "sh600000", "sz000001"],
        "名称": ["甲", "乙", "丙"],
        "涨跌幅": [8.0, -1.0, 2.0],
        "最新价": [11.0, 10.0, 5.0],
        "成交额": [1e8, 2e8, 3e7],
        "今开": [10.0, 10.1, 4.9],   # 多余列应被丢弃
    }))
    df = fetcher.market_spot()
    assert list(df["代码"]) == ["920000", "600000", "000001"]   # 前缀去掉
    assert list(df.columns) == ["代码", "名称", "涨跌幅", "最新价", "成交额"]


def test_东财连接被掐后熔断_后续跳过东财(monkeypatch):
    import akshare as ak
    fetcher = Fetcher(retries=3, min_interval=0)
    em_calls = {"n": 0}

    def 板块东财断():
        em_calls["n"] += 1
        raise ConnectionError("RemoteDisconnected")

    monkeypatch.setattr(ak, "stock_board_industry_name_em", 板块东财断)
    monkeypatch.setattr(
        ak, "stock_sector_spot",
        lambda indicator: pd.DataFrame({
            "label": ["x"], "板块": ["甲"], "涨跌幅": [1.0], "股票名称": ["S"],
        }),
    )

    fetcher.industry_board()   # 首次触发熔断
    assert em_calls["n"] == 1   # 有兜底,东财只试 1 次(不再重试 3 次)
    fetcher.industry_board()   # 熔断后
    assert em_calls["n"] == 1   # 完全跳过东财,直接走新浪


def test_北交所日K仅尝试东财_免费源不调用(monkeypatch):
    import akshare as ak
    fetcher = Fetcher(retries=3, min_interval=0)
    monkeypatch.setattr(fetcher_mod, "_baostock_daily", lambda *a: pytest.fail("北交所不应调用baostock"))
    monkeypatch.setattr(ak, "stock_zh_a_daily", lambda **k: pytest.fail("北交所不应调用新浪"))
    monkeypatch.setattr(
        ak, "stock_zh_a_hist",
        lambda **k: (_ for _ in ()).throw(ConnectionError("RemoteDisconnected")),
    )
    with pytest.raises(Exception):
        fetcher.stock_daily("920136", "20260620", "20260630")


# —— 全市场日K快照(快照一次成型) ——

def test_东财快照归一化为日K快照列并换算成交量():
    em = pd.DataFrame({
        "代码": ["600000"], "名称": ["甲"], "最新价": [11.0], "今开": [10.0],
        "最高": [11.5], "最低": [9.9], "昨收": [10.0], "成交量": [10000],  # 手
        "成交额": [1.1e8], "换手率": [1.2], "涨跌幅": [10.0],
    })
    out = _em_spot_to_daily_snapshot(em)
    assert list(out.columns) == ["代码", "名称", "今开", "最高", "最低", "收盘", "昨收",
                                 "成交量", "成交额", "换手率"]
    assert out.iloc[0]["收盘"] == 11.0
    assert out.iloc[0]["成交量"] == 1000000   # 手 → 股


def test_新浪快照归一化为日K快照列_换手率补NULL():
    sina = pd.DataFrame({
        "代码": ["sh600000"], "名称": ["甲"], "最新价": [11.0], "今开": [10.0],
        "最高": [11.5], "最低": [9.9], "昨收": [10.0], "成交量": [1000000],  # 股
        "成交额": [1.1e8], "涨跌幅": [10.0],
    })
    out = _sina_spot_to_daily_snapshot(sina)
    assert out.iloc[0]["代码"] == "600000"
    assert out.iloc[0]["成交量"] == 1000000   # 新浪原生股,不换算
    assert out.iloc[0]["换手率"] is None or pd.isna(out.iloc[0]["换手率"])


def test_日K快照东财失败回退新浪(monkeypatch):
    import akshare as ak
    fetcher = Fetcher(retries=1, min_interval=0)
    monkeypatch.setattr(ak, "stock_zh_a_spot_em", lambda: (_ for _ in ()).throw(RuntimeError("断连")))
    monkeypatch.setattr(ak, "stock_zh_a_spot", lambda: pd.DataFrame({
        "代码": ["sz000001"], "名称": ["乙"], "最新价": [5.5], "今开": [5.0],
        "最高": [5.6], "最低": [4.9], "昨收": [5.0], "成交量": [2000000],
        "成交额": [1.1e7],
    }))
    df = fetcher.daily_snapshot()
    assert df.iloc[0]["代码"] == "000001"
    assert df.iloc[0]["收盘"] == 5.5


# —— 交易日历 ——

def test_交易日历baostock不可用时回退新浪(monkeypatch):
    import akshare as ak
    fetcher = Fetcher(retries=1, min_interval=0)
    monkeypatch.setattr(fetcher_mod, "_baostock_ready", lambda: False)
    monkeypatch.setattr(ak, "tool_trade_date_hist_sina", lambda: pd.DataFrame({
        "trade_date": ["2026-07-03", "2026-07-06", "2026-07-07", "2026-08-03"],
    }))
    days = fetcher.trade_dates("2026-07-01", "2026-07-31")
    assert days == ["2026-07-03", "2026-07-06", "2026-07-07"]   # 区间外被过滤


def test_交易日历baostock失败回退新浪(monkeypatch):
    import akshare as ak
    fetcher = Fetcher(retries=1, min_interval=0)
    monkeypatch.setattr(fetcher_mod, "_baostock_ready", lambda: True)
    monkeypatch.setattr(
        fetcher_mod, "_baostock_trade_dates",
        lambda s, e: (_ for _ in ()).throw(RuntimeError("baostock 维护")),
    )
    monkeypatch.setattr(ak, "tool_trade_date_hist_sina", lambda: pd.DataFrame({
        "trade_date": ["2026-07-06", "2026-07-07"],
    }))
    days = fetcher.trade_dates("2026-07-01", "2026-07-31")
    assert days == ["2026-07-06", "2026-07-07"]
