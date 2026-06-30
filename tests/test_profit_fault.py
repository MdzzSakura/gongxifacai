"""净利润断层扫描测试。

使用多行结构 fixture(每只股票含净利润行 + 营收行),覆盖:
跳空判定、增速判定、净利润行筛选(营收行不参与)、空日K、
None/NaN 增速处理、缺失日K 跳过。
"""
import numpy as np
import pandas as pd
from gxfc.factors.profit_fault import detect_gap, passes_growth, scan_profit_fault

_NET = "归属于上市公司股东的净利润"
_REV = "营业收入"


def _daily(opens_highs):
    """opens_highs: [(开盘,最高), ...] 按日期升序。"""
    return pd.DataFrame(
        {
            "日期": [f"2026-06-{i+1:02d}" for i in range(len(opens_highs))],
            "开盘": [o for o, _ in opens_highs],
            "最高": [h for _, h in opens_highs],
        }
    )


def _yjyg(records):
    """构造多行业绩预告 fixture。
    records: list of (股票代码, 股票简称, 预测指标, 业绩变动幅度)
    """
    return pd.DataFrame(
        records,
        columns=["股票代码", "股票简称", "预测指标", "业绩变动幅度"],
    )


# ——— detect_gap ———

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


# ——— passes_growth ———

def test_增速达标判定():
    assert passes_growth(60.0, 50.0) is True
    assert passes_growth(40.0, 50.0) is False
    assert passes_growth(None, 50.0) is False
    assert passes_growth(np.nan, 50.0) is False


# ——— scan_profit_fault ———

def test_扫描出增速达标且跳空的候选():
    """仅净利润增速达标且跳空的票入选;营收行增速再高也不影响结果。"""
    yjyg = _yjyg([
        ("000001", "甲", _NET, 80.0),    # 净利润+80% → 达标
        ("000001", "甲", _REV, 200.0),   # 营收行,不参与筛选
        ("000002", "乙", _NET, 30.0),    # 净利润+30% → 不达标
        ("000002", "乙", _REV, 50.0),    # 营收行,不参与筛选
        ("000003", "丙", _NET, 120.0),   # 净利润+120% → 达标,但无跳空
        ("000003", "丙", _REV, 300.0),
    ])
    daily_map = {
        "000001": _daily([(10.0, 10.5), (11.0, 11.8)]),   # 有跳空
        "000002": _daily([(10.0, 10.5), (11.0, 11.8)]),   # 增速不达标,无论跳空与否
        "000003": _daily([(10.0, 10.5), (10.2, 10.6)]),   # 无跳空
    }
    out = scan_profit_fault(yjyg, daily_map, growth_threshold=50.0)
    assert list(out["股票代码"]) == ["000001"]
    assert bool(out.iloc[0]["有跳空"]) is True


def test_营收行增速高但无净利润行则不入选():
    """只有营收行没有净利润行时,该票不进入候选。"""
    yjyg = _yjyg([
        ("000099", "测试", _REV, 500.0),  # 仅营收行,无净利润行
    ])
    daily_map = {
        "000099": _daily([(10.0, 10.5), (11.0, 11.8)]),
    }
    out = scan_profit_fault(yjyg, daily_map, growth_threshold=50.0)
    assert len(out) == 0


def test_缺失日K的票被跳过():
    yjyg = _yjyg([
        ("000009", "缺数据", _NET, 99.0),
        ("000009", "缺数据", _REV, 150.0),
    ])
    out = scan_profit_fault(yjyg, {}, growth_threshold=50.0)
    assert len(out) == 0


def test_增速为None被跳过():
    yjyg = _yjyg([
        ("000008", "无增速", _NET, None),
    ])
    daily_map = {"000008": _daily([(10.0, 10.5), (11.0, 11.8)])}
    out = scan_profit_fault(yjyg, daily_map, growth_threshold=50.0)
    assert len(out) == 0


def test_增速为NaN被跳过():
    yjyg = _yjyg([
        ("000007", "NaN增速", _NET, np.nan),
    ])
    daily_map = {"000007": _daily([(10.0, 10.5), (11.0, 11.8)])}
    out = scan_profit_fault(yjyg, daily_map, growth_threshold=50.0)
    assert len(out) == 0
