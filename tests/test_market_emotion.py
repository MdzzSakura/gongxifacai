import pandas as pd
from gxfc.factors.market_emotion import compute_market_emotion


def _activity(up, down, limit_up, limit_down, broken):
    return pd.DataFrame(
        {
            "item": ["上涨", "下跌", "涨停", "跌停", "炸板"],
            "value": [up, down, limit_up, limit_down, broken],
        }
    )


def test_基础计数与炸板率():
    activity = _activity(3000, 1800, 40, 5, 10)
    zt = pd.DataFrame({"连板数": [1, 2, 5, 3]})
    e = compute_market_emotion(activity, zt)
    assert e.up_count == 3000
    assert e.down_count == 1800
    assert e.limit_up == 40
    assert e.limit_down == 5
    # 炸板率 = 炸板/(涨停+炸板) = 10/(40+10) = 0.2
    assert abs(e.broken_board_rate - 0.2) < 1e-9
    assert e.highest_streak == 5


def test_情绪高点提示():
    activity = _activity(4600, 300, 80, 2, 5)
    zt = pd.DataFrame({"连板数": [1]})
    e = compute_market_emotion(activity, zt)
    assert "情绪高点" in e.sentiment_hint


def test_接近冰点提示():
    activity = _activity(700, 4000, 10, 60, 8)
    zt = pd.DataFrame({"连板数": [1]})
    e = compute_market_emotion(activity, zt)
    assert "冰点" in e.sentiment_hint


def test_量能放量判定():
    activity = _activity(3000, 1800, 40, 5, 10)
    zt = pd.DataFrame({"连板数": [1]})
    e = compute_market_emotion(activity, zt, today_amount=12000, avg5_amount=10000)
    assert e.volume_state == "放量"


def test_量能数据不足():
    activity = _activity(3000, 1800, 40, 5, 10)
    zt = pd.DataFrame({"连板数": [1]})
    e = compute_market_emotion(activity, zt)
    assert e.volume_state == "数据不足"


def test_涨停池为空时最高板为0():
    activity = _activity(3000, 1800, 0, 5, 0)
    e = compute_market_emotion(activity, pd.DataFrame({"连板数": []}))
    assert e.highest_streak == 0
    assert e.broken_board_rate == 0.0
