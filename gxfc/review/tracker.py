"""信号前向收益追踪:量化"筛出来的票后来怎么走",为模式提供胜率/期望值证据。

评估口径(全部基于 daily 前复权序列现算,信号表不存价格):
- 入场基准 = 信号日收盘价;
- T+n 收益 = 信号日之后第 n 个交易行收盘 / 入场基准 - 1(未来行不足记 None);
- 区间最大涨幅/最大回撤 = 之后 max(horizons) 个交易行内 最高/最低 相对入场基准。
信号日在 daily 无行(停牌/未采集)则该信号标记"不可追踪",不掺估算数据。
"""
from datetime import datetime, timedelta
from typing import Callable, Optional, Sequence

import pandas as pd


def track_one(daily_window: pd.DataFrame, signal_date: str,
              horizons: Sequence[int] = (1, 3, 5, 10)) -> Optional[dict]:
    """单信号前向收益。daily_window 为该票自信号日起的日K(可含更晚数据)。

    信号日无行或收盘非正返回 None(调用方计入"不可追踪")。

    注意:"区间最大回撤%" 为区间(未来 max(horizons) 个交易行)最低点相对
    入场基准的收益,不是真正的峰谷回撤口径;单边上涨行情下该值可能为正。
    """
    if daily_window is None or daily_window.empty:
        return None
    win = daily_window.sort_values("日期").reset_index(drop=True)
    hit = win.index[win["日期"] == signal_date]
    if len(hit) == 0:
        return None
    i = int(hit[0])
    entry = float(win.loc[i, "收盘"])
    if entry <= 0:
        return None
    future = win.iloc[i + 1: i + 1 + max(horizons)]
    out: dict = {}
    for n in horizons:
        if len(future) >= n:
            out[f"T+{n}收益%"] = round((float(future.iloc[n - 1]["收盘"]) / entry - 1) * 100, 2)
        else:
            out[f"T+{n}收益%"] = None
    if len(future) > 0:
        out["区间最大涨幅%"] = round((pd.to_numeric(future["最高"]).max() / entry - 1) * 100, 2)
        out["区间最大回撤%"] = round((pd.to_numeric(future["最低"]).min() / entry - 1) * 100, 2)
    else:
        out["区间最大涨幅%"] = None
        out["区间最大回撤%"] = None
    return out


def track_signals(signals: pd.DataFrame,
                  read_daily: Callable[[str, str, str], pd.DataFrame],
                  horizons: Sequence[int] = (1, 3, 5, 10)) -> pd.DataFrame:
    """逐信号追踪。read_daily(code, start, end) 注入 DuckStore.read_daily 即离线复算。

    返回信号明细 + 各期收益;"可追踪" 列区分信号日缺日K的样本。
    """
    span = max(horizons) * 3  # 自然日窗口,覆盖 max(horizons) 个交易日绰绰有余
    rows = []
    for _, s in signals.iterrows():
        end = (datetime.strptime(s["signal_date"], "%Y-%m-%d")
               + timedelta(days=span)).strftime("%Y-%m-%d")
        win = read_daily(s["代码"], s["signal_date"], end)
        perf = track_one(win, s["signal_date"], horizons)
        base = {"signal_date": s["signal_date"], "strategy": s["strategy"],
                "代码": s["代码"], "名称": s["名称"]}
        if perf is None:
            rows.append({**base, "可追踪": False})
        else:
            rows.append({**base, "可追踪": True, **perf})
    return pd.DataFrame(rows)


def summarize(perf: pd.DataFrame, horizons: Sequence[int] = (1, 3, 5, 10)) -> pd.DataFrame:
    """按 策略×持有期 汇总:样本数/胜率/平均收益(即期望值)/中位收益/盈亏比。

    胜率 = 收益>0 占比;盈亏比 = 平均盈利 / |平均亏损|(无亏损样本记 None)。
    盈亏比仅统计严格盈利(>0)与严格亏损(<0)样本,收益恰为 0 的平盘样本
    不计入盈亏比分子分母(不拉低平均亏损、不虚抬盈亏比),但仍计入样本数与胜率分母。
    收益为 None(未来行不足)的信号不计入该持有期样本。
    """
    cols = ["策略", "持有期", "样本数", "胜率%", "平均收益%", "中位收益%", "盈亏比"]
    if perf.empty:
        return pd.DataFrame(columns=cols)
    tracked = perf[perf["可追踪"]] if "可追踪" in perf.columns else perf
    rows = []
    for strategy, g in tracked.groupby("strategy"):
        for n in horizons:
            col = f"T+{n}收益%"
            if col not in g.columns:
                continue
            vals = pd.to_numeric(g[col], errors="coerce").dropna()
            if vals.empty:
                continue
            wins, losses = vals[vals > 0], vals[vals < 0]
            pl = None
            if len(wins) and len(losses) and losses.mean() != 0:
                pl = round(float(wins.mean() / abs(losses.mean())), 2)
            rows.append({
                "策略": strategy, "持有期": f"T+{n}", "样本数": len(vals),
                "胜率%": round(len(wins) / len(vals) * 100, 1),
                "平均收益%": round(float(vals.mean()), 2),
                "中位收益%": round(float(vals.median()), 2),
                "盈亏比": pl,
            })
    return pd.DataFrame(rows, columns=cols)


def trade_stats(trades: pd.DataFrame) -> pd.DataFrame:
    """已平仓交易统计,分 全部/按计划/未按计划 三组对比——两组期望值之差即纪律成本。

    收益% 按价格算(不含费用),总盈亏 = (平仓价-开仓价)×股数 汇总。
    未平仓交易不计入;无已平仓交易返回空表。
    盈亏比只统计严格盈利(>0)/严格亏损(<0),平盘(收益恰为0)不计入分子分母,
    与 summarize 口径一致;但仍计入笔数与胜率分母。
    """
    cols = ["分组", "笔数", "胜率%", "平均收益%", "盈亏比", "总盈亏"]
    if trades.empty:
        return pd.DataFrame(columns=cols)
    closed = trades.dropna(subset=["close_date"]).copy()
    if closed.empty:
        return pd.DataFrame(columns=cols)
    groups = [
        ("全部", closed),
        ("按计划", closed[closed["followed_plan"] == True]),      # noqa: E712  # pandas 布尔过滤
        ("未按计划", closed[closed["followed_plan"] == False]),   # noqa: E712  # pandas 布尔过滤
    ]
    rows = []
    for label, g in groups:
        if g.empty:
            continue
        ret = (pd.to_numeric(g["close_price"]) / pd.to_numeric(g["open_price"]) - 1) * 100
        pnl = (pd.to_numeric(g["close_price"]) - pd.to_numeric(g["open_price"])) \
            * pd.to_numeric(g["shares"])
        wins, losses = ret[ret > 0], ret[ret < 0]
        pl = None
        if len(wins) and len(losses) and losses.mean() != 0:
            pl = round(float(wins.mean() / abs(losses.mean())), 2)
        rows.append({
            "分组": label, "笔数": len(g),
            "胜率%": round(len(wins) / len(g) * 100, 1),
            "平均收益%": round(float(ret.mean()), 2),
            "盈亏比": pl,
            "总盈亏": round(float(pnl.sum()), 2),
        })
    return pd.DataFrame(rows, columns=cols)
