"""市场情绪因子测试。

使用固定 fixture(涨停/跌停/炸板三池 DataFrame),不联网。
覆盖:基础计数、炸板率、最高板、空池、无 spot 偏冷/偏热/中性、
有 spot 情绪高点/冰点。
"""
import pandas as pd
from gxfc.factors.market_emotion import compute_market_emotion


# ——— fixtures ———

def _zt(streaks):
    """构造涨停池 fixture,streaks 为连板数列表。"""
    return pd.DataFrame({"连板数": streaks})


def _dt(n):
    """构造跌停池 fixture,n 为跌停股数量。"""
    return pd.DataFrame({"代码": [f"6000{i:02d}" for i in range(n)]})


def _zb(n):
    """构造炸板池 fixture,n 为炸板股数量。"""
    return pd.DataFrame({"代码": [f"0000{i:02d}" for i in range(n)]})


def _spot(up, down, flat=0):
    """构造全市场快照 fixture,up/down/flat 为涨/跌/平家数。"""
    records = (
        [{"涨跌幅": 1.5}] * up
        + [{"涨跌幅": -1.5}] * down
        + [{"涨跌幅": 0.0}] * flat
    )
    return pd.DataFrame(records)


# ——— 基础计数与炸板率 ———

def test_基础计数与炸板率():
    zt = _zt([1, 2, 5, 3])   # 4 涨停
    dt = _dt(1)               # 1 跌停
    zb = _zb(1)               # 1 炸板
    e = compute_market_emotion(zt, dt, zb)
    assert e.limit_up == 4
    assert e.limit_down == 1
    # 炸板率 = 1/(4+1) = 0.2
    assert abs(e.broken_board_rate - 0.2) < 1e-9


def test_最高板():
    zt = _zt([1, 3, 7, 2])
    e = compute_market_emotion(zt, _dt(1), _zb(0))
    assert e.highest_streak == 7


def test_空池时全零():
    e = compute_market_emotion(_zt([]), _dt(0), _zb(0))
    assert e.limit_up == 0
    assert e.limit_down == 0
    assert e.broken_board_rate == 0.0
    assert e.highest_streak == 0


def test_无spot时up_count与down_count为None():
    e = compute_market_emotion(_zt([1, 2]), _dt(1), _zb(0))
    assert e.up_count is None
    assert e.down_count is None


def test_量能状态固定为数据不足():
    e = compute_market_emotion(_zt([1]), _dt(1), _zb(0))
    assert e.volume_state == "数据不足"


# ——— 无 spot 时用涨跌停池判断情绪 ———

def test_无spot偏冷提示():
    # limit_down(5) > limit_up(2) 且 highest_streak(2) <= 3
    zt = _zt([1, 2])   # 2 涨停,最高2板
    dt = _dt(5)        # 5 跌停
    zb = _zb(0)
    e = compute_market_emotion(zt, dt, zb)
    assert "偏冷" in e.sentiment_hint


def test_无spot偏热提示():
    # limit_up(10) > limit_down(2)*2=4 且 highest_streak(7) >= 5
    zt = _zt([1, 2, 3, 4, 5, 6, 7, 1, 2, 3])  # 10 涨停,最高7板
    dt = _dt(2)   # 2 跌停
    zb = _zb(2)
    e = compute_market_emotion(zt, dt, zb)
    assert "偏热" in e.sentiment_hint


def test_无spot中性提示():
    # 不满足偏冷/偏热任一条件
    zt = _zt([1, 2, 3, 4, 2])   # 5 涨停,最高4板
    dt = _dt(3)                  # 3 跌停;5 > 3*2=6? No;3 > 5? No → 中性
    zb = _zb(1)
    e = compute_market_emotion(zt, dt, zb)
    assert e.sentiment_hint == "中性"


# ——— 有 spot 时沿用精确阈值 ———

def test_有spot情绪高点():
    spot = _spot(up=4600, down=300)
    e = compute_market_emotion(_zt([1]), _dt(1), _zb(0), spot_df=spot)
    assert e.up_count == 4600
    assert "情绪高点" in e.sentiment_hint


def test_有spot接近冰点():
    spot = _spot(up=700, down=4000)
    e = compute_market_emotion(_zt([1]), _dt(1), _zb(0), spot_df=spot)
    assert e.up_count == 700
    assert "冰点" in e.sentiment_hint


def test_有spot中性():
    spot = _spot(up=3000, down=1800)
    e = compute_market_emotion(_zt([1]), _dt(1), _zb(0), spot_df=spot)
    assert e.sentiment_hint == "中性"


def test_有spot时down_count正确():
    spot = _spot(up=2000, down=1500, flat=200)
    e = compute_market_emotion(_zt([1]), _dt(1), _zb(0), spot_df=spot)
    assert e.down_count == 1500
