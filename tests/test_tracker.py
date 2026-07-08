"""信号前向收益追踪纯函数测试。"""
import pandas as pd

from gxfc.review.tracker import summarize, track_one, track_signals, trade_stats

_COLS = ["代码", "日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额", "换手率"]


def _win(rows):
    return pd.DataFrame(rows, columns=_COLS)


def _sample_window():
    return _win([
        ["600000", "2026-07-07", 10.0, 10.0, 10.5, 9.8, 100, 1000, 1.0],
        ["600000", "2026-07-08", 10.0, 11.0, 11.2, 9.9, 100, 1100, 1.0],
        ["600000", "2026-07-09", 11.0, 10.5, 11.5, 10.4, 100, 1050, 1.0],
        ["600000", "2026-07-10", 10.0, 12.0, 12.4, 10.4, 100, 1200, 1.0],
    ])


def test_单信号前向收益():
    out = track_one(_sample_window(), "2026-07-07", horizons=(1, 3, 5))
    assert out["T+1收益%"] == 10.0        # 11/10-1
    assert out["T+3收益%"] == 20.0        # 12/10-1
    assert out["T+5收益%"] is None        # 未来行不足 5 根
    assert out["区间最大涨幅%"] == 24.0    # 12.4/10-1
    assert out["区间最大回撤%"] == -1.0    # 9.9/10-1


def test_信号日无行返回None():
    assert track_one(_win([]), "2026-07-07") is None
    assert track_one(_sample_window(), "2026-07-05") is None  # 窗口内无该日


def test_track_signals区分可追踪():
    signals = pd.DataFrame({
        "signal_date": ["2026-07-07", "2026-07-07"],
        "strategy": ["bottom_volume", "bottom_volume"],
        "代码": ["600000", "000001"],
        "名称": ["甲", "乙"],
    })
    data = {"600000": _sample_window(), "000001": _win([])}

    def fake_read(code, start, end):
        return data[code]

    perf = track_signals(signals, fake_read, horizons=(1,))
    ok = perf[perf["代码"] == "600000"].iloc[0]
    bad = perf[perf["代码"] == "000001"].iloc[0]
    assert bool(ok["可追踪"]) is True and ok["T+1收益%"] == 10.0
    assert bool(bad["可追踪"]) is False


def test_汇总胜率与盈亏比():
    perf = pd.DataFrame({
        "strategy": ["s", "s", "s", "s"],
        "可追踪": [True, True, True, True],
        "T+1收益%": [10.0, -5.0, 20.0, None],
    })
    got = summarize(perf, horizons=(1,))
    row = got.iloc[0]
    assert row["样本数"] == 3               # None 不计入样本
    assert row["胜率%"] == 66.7
    assert row["平均收益%"] == 8.33
    assert row["盈亏比"] == 3.0             # 平均盈利15 / |平均亏损5|


def test_盈亏比排除平盘():
    perf = pd.DataFrame({
        "strategy": ["s", "s", "s"],
        "可追踪": [True, True, True],
        "T+1收益%": [10.0, 0.0, -5.0],
    })
    got = summarize(perf, horizons=(1,))
    row = got.iloc[0]
    assert row["样本数"] == 3               # 平盘计入样本数
    assert row["胜率%"] == 33.3             # 仅 10.0 > 0
    assert row["盈亏比"] == 2.0             # 平均盈利10 / |平均亏损-5|,平盘不参与


def test_无亏损样本盈亏比为None():
    perf = pd.DataFrame({
        "strategy": ["s", "s"],
        "可追踪": [True, True],
        "T+1收益%": [10.0, 20.0],
    })
    got = summarize(perf, horizons=(1,))
    row = got.iloc[0]
    assert row["盈亏比"] is None


def test_未来行为空最大涨跌为None():
    win = _win([
        ["600000", "2026-07-07", 10.0, 10.0, 10.5, 9.8, 100, 1000, 1.0],
    ])
    out = track_one(win, "2026-07-07", horizons=(1,))
    assert out["区间最大涨幅%"] is None
    assert out["区间最大回撤%"] is None


def test_交易纪律统计分组():
    trades = pd.DataFrame({
        "open_price": [10.0, 10.0, 10.0, 10.0],
        "close_price": [12.0, 9.0, 11.0, None],   # 最后一笔未平仓,不计入
        "shares": [1000, 1000, 1000, 1000],
        "close_date": ["2026-07-10", "2026-07-10", "2026-07-10", None],
        "followed_plan": [True, False, True, None],
    })
    got = trade_stats(trades)
    all_row = got[got["分组"] == "全部"].iloc[0]
    assert all_row["笔数"] == 3
    assert all_row["胜率%"] == 66.7
    assert all_row["总盈亏"] == 2000.0      # +2000 -1000 +1000
    follow = got[got["分组"] == "按计划"].iloc[0]
    broke = got[got["分组"] == "未按计划"].iloc[0]
    assert follow["胜率%"] == 100.0
    assert broke["胜率%"] == 0.0            # 纪律成本一目了然


def test_无已平仓交易返回空表():
    trades = pd.DataFrame({
        "open_price": [10.0], "close_price": [None], "shares": [100],
        "close_date": [None], "followed_plan": [None],
    })
    assert trade_stats(trades).empty
