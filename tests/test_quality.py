import pandas as pd
import pytest

from gxfc.data.quality import QualityError, validate


def _snapshot_df(n=3):
    return pd.DataFrame({
        "代码": [f"{600000 + i}" for i in range(n)],
        "名称": ["甲"] * n,
        "今开": [10.0] * n, "最高": [11.5] * n, "最低": [9.9] * n,
        "收盘": [11.0] * n, "昨收": [10.0] * n,
        "成交量": [1e6] * n, "成交额": [1.1e7] * n, "换手率": [1.2] * n,
    })


def test_缺必需列拦截():
    df = pd.DataFrame({"代码": ["600000"], "名称": ["甲"]})
    with pytest.raises(QualityError, match="缺必需列"):
        validate("daily_snapshot", df)


def test_空表放行():
    out = validate("zt_pool", pd.DataFrame())
    assert out.empty


def test_快照行数骤降拒写():
    df = _snapshot_df(3)
    with pytest.raises(QualityError, match="骤降"):
        validate("daily_snapshot", df, prev_rows=100)
    # 行数正常则通过
    out = validate("daily_snapshot", df, prev_rows=3)
    assert len(out) == 3


def test_价格非正的行被剔除():
    df = _snapshot_df(3)
    df.loc[1, "收盘"] = 0.0
    out = validate("daily_snapshot", df)
    assert len(out) == 2
    assert "600001" not in set(out["代码"])


def test_涨跌幅越界的行被剔除():
    df = _snapshot_df(3)
    df.loc[2, "收盘"] = 20.0   # 昨收10 → +100%,越界
    out = validate("daily_snapshot", df)
    assert len(out) == 2
    assert "600002" not in set(out["代码"])


def test_日K重复行保留最后一条():
    df = pd.DataFrame({
        "代码": ["600000", "600000"], "日期": ["2026-07-06", "2026-07-06"],
        "开盘": [10.0, 10.1], "收盘": [11.0, 11.1], "最高": [11.5, 11.5],
        "最低": [9.9, 9.9], "成交量": [1e6, 1e6], "成交额": [1.1e7, 1.1e7],
    })
    out = validate("daily", df)
    assert len(out) == 1
    assert out.iloc[0]["收盘"] == 11.1


def test_返回None拦截():
    with pytest.raises(QualityError):
        validate("daily", None)
