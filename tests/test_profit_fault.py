import numpy as np
import pandas as pd
from gxfc.factors.profit_fault import detect_gap, passes_growth, scan_profit_fault


def _daily(opens_highs):
    """opens_highs: [(开盘,最高), ...] 按日期升序。"""
    return pd.DataFrame(
        {
            "日期": [f"2026-06-{i+1:02d}" for i in range(len(opens_highs))],
            "开盘": [o for o, _ in opens_highs],
            "最高": [h for _, h in opens_highs],
        }
    )


def test_跳空缺口成立():
    # 最后一行开盘11.0 > 前一行最高10.5
    df = _daily([(10.0, 10.5), (11.0, 11.8)])
    assert detect_gap(df) is True


def test_无跳空缺口():
    df = _daily([(10.0, 10.5), (10.3, 10.8)])
    assert detect_gap(df) is False


def test_不足两行返回False():
    df = _daily([(10.0, 10.5)])
    assert detect_gap(df) is False


def test_增速达标判定():
    assert passes_growth(60.0, 50.0) is True
    assert passes_growth(40.0, 50.0) is False
    assert passes_growth(None, 50.0) is False
    assert passes_growth(np.nan, 50.0) is False


def test_扫描出增速达标且跳空的候选():
    yjyg = pd.DataFrame(
        {
            "股票代码": ["000001", "000002", "000003"],
            "股票简称": ["甲", "乙", "丙"],
            "预测净利润-同比增长": [80.0, 30.0, 120.0],
        }
    )
    daily_map = {
        "000001": _daily([(10.0, 10.5), (11.0, 11.8)]),   # 增速达标+跳空 → 入选
        "000002": _daily([(10.0, 10.5), (11.0, 11.8)]),   # 增速不达标 → 落选
        "000003": _daily([(10.0, 10.5), (10.2, 10.6)]),   # 增速达标但无跳空 → 落选
    }
    out = scan_profit_fault(yjyg, daily_map, growth_threshold=50.0)
    assert list(out["股票代码"]) == ["000001"]
    assert bool(out.iloc[0]["有跳空"]) is True


def test_缺失日K的票被跳过():
    yjyg = pd.DataFrame(
        {"股票代码": ["000009"], "股票简称": ["缺数据"], "预测净利润-同比增长": [99.0]}
    )
    out = scan_profit_fault(yjyg, {}, growth_threshold=50.0)
    assert len(out) == 0
