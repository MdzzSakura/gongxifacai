"""底部爆量大涨扫描测试。

今日值(涨幅/收盘/量)来自快照行,历史(前N日均量/区间最高)来自日K。
覆盖:三条件全过、各条件单独不达标、历史不足、业绩叠加、排序。
"""
import pandas as pd

from gxfc.factors.bottom_volume import (
    compute_volume_ratio,
    is_bottom,
    scan_bottom_volume,
)


def _hist(highs, volumes):
    """历史日K(今日之前),仅需 最高/成交量。"""
    return pd.DataFrame({"最高": highs, "成交量": volumes})


def _survivor(code, name, pct, close, volume, amount=4e8):
    return {"代码": code, "名称": name, "涨跌幅": pct, "最新价": close,
            "成交量": volume, "成交额": amount}


# 标准底部爆量样本:历史59天高点20、长期低迷,前5日均量1000;
# 今日(快照)涨12%、收9.0、量5000 → 量比5、底部(9≤20×0.6=12)。
def _good_hist():
    return _hist([20.0] + [8.5] * 58, [1000] * 59)


def test_量比计算():
    assert compute_volume_ratio(5000, [1000] * 5, 5) == 5.0
    assert compute_volume_ratio(5000, [1000] * 3, 5) == 0.0      # 历史不足
    assert compute_volume_ratio(100, [0] * 5, 5) == 0.0          # 均量为0


def test_底部判定():
    ok, discount = is_bottom(9.0, [20.0] + [8.5] * 58, window=60, ratio=0.6)
    assert ok is True
    assert round(discount, 1) == 55.0          # (1 - 9/20)*100
    ok2, _ = is_bottom(18.0, [20.0] * 60, window=60, ratio=0.6)
    assert ok2 is False                        # 18 > 20×0.6=12,非底部


def test_三条件全过则入选():
    df = scan_bottom_volume(
        pd.DataFrame([_survivor("000001", "甲", 12.0, 9.0, 5000)]),
        {"000001": _good_hist()},
    )
    assert list(df["代码"]) == ["000001"]
    assert df.iloc[0]["今日涨跌幅"] == 12.0
    assert df.iloc[0]["量比"] == 5.0


def test_涨幅不足被排除():
    df = scan_bottom_volume(
        pd.DataFrame([_survivor("000001", "甲", 2.5, 9.0, 5000)]),  # 仅涨2.5%
        {"000001": _good_hist()},
    )
    assert df.empty


def test_量比不足被排除():
    df = scan_bottom_volume(
        pd.DataFrame([_survivor("000001", "甲", 12.0, 9.0, 1100)]),  # 量比1.1
        {"000001": _good_hist()},
    )
    assert df.empty


def test_非底部被排除():
    # 区间最高10,今收9.5 > 10×0.6
    df = scan_bottom_volume(
        pd.DataFrame([_survivor("000001", "甲", 12.0, 9.5, 5000)]),
        {"000001": _hist([10.0] + [9.0] * 58, [1000] * 59)},
    )
    assert df.empty


def test_历史不足跳过不报错():
    df = scan_bottom_volume(
        pd.DataFrame([_survivor("000001", "甲", 12.0, 9.0, 5000)]),
        {"000001": _hist([20.0, 8.5], [1000, 1000])},  # 仅2天<baseline
    )
    assert df.empty


def test_业绩高增叠加标记():
    df = scan_bottom_volume(
        pd.DataFrame([_survivor("000001", "甲", 12.0, 9.0, 5000)]),
        {"000001": _good_hist()},
        high_growth_codes={"000001"},
    )
    assert bool(df.iloc[0]["业绩高增"]) is True


def test_按量比降序():
    df = scan_bottom_volume(
        pd.DataFrame([
            _survivor("000001", "甲", 12.0, 9.0, 5000),   # 量比5
            _survivor("000002", "乙", 12.0, 9.0, 3000),   # 量比3
        ]),
        {"000001": _good_hist(), "000002": _good_hist()},
    )
    assert list(df["代码"]) == ["000001", "000002"]
    assert df.iloc[0]["量比"] >= df.iloc[1]["量比"]
