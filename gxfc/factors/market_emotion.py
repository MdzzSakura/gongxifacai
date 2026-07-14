"""市场情绪因子:从涨停池/跌停池/炸板池计算情绪指标。

设计意图:盘后一眼看清当日情绪冷热——炸板率判断追涨风险,最高板判断
市场高度。涨跌家数为可选项,传入全市场 spot_df 后精确统计;否则退为
用涨跌停池的粗粒度判断。量能状态由调用方传入两市总成交额与前N日均额后解锁(离线由 daily 表重建)。
阈值来自用户文档经验值。
"""
from dataclasses import dataclass
from typing import Optional

import pandas as pd


@dataclass
class MarketEmotion:
    up_count: Optional[int]
    down_count: Optional[int]
    limit_up: int
    limit_down: int
    broken_board_rate: float
    highest_streak: int
    volume_state: str
    sentiment_hint: str


def compute_market_emotion(
    zt_df: pd.DataFrame,
    dt_df: pd.DataFrame,
    zb_df: pd.DataFrame,
    spot_df: Optional[pd.DataFrame] = None,
    hot_up_count: int = 4500,
    cold_up_count: int = 800,
    turnover: Optional[float] = None,
    turnover_baseline: Optional[float] = None,
    volume_up_ratio: float = 1.15,
    volume_down_ratio: float = 0.85,
) -> MarketEmotion:
    """从三个涨跌停池计算市场情绪指标。

    Args:
        zt_df: 涨停池,列含 代码,名称,涨跌幅,连板数,炸板次数,所属行业 等
        dt_df: 跌停池,列含 代码,名称,涨跌幅,连续跌停 等
        zb_df: 炸板池,列含 代码,名称,涨跌幅,炸板次数 等
        spot_df: 全市场快照(可选),含 '涨跌幅' 列时计算上涨/下跌家数;
                 否则 up_count/down_count 均为 None(调用方可降级)
        hot_up_count: 上涨家数≥此值视为情绪高点(追涨谨慎)
        cold_up_count: 上涨家数≤此值视为接近冰点(关注机会)
        turnover: 今日两市总成交额(可选)
        turnover_baseline: 前N日均成交额(可选)
        volume_up_ratio: 放量阈值倍数(默认1.15,今日≥基准*此值为放量)
        volume_down_ratio: 缩量阈值倍数(默认0.85,今日≤基准*此值为缩量)

    Returns:
        MarketEmotion 情绪指标聚合
    """
    limit_up = len(zt_df) if zt_df is not None else 0
    limit_down = len(dt_df) if dt_df is not None else 0
    broken = len(zb_df) if zb_df is not None else 0

    denom = limit_up + broken
    broken_rate = (broken / denom) if denom > 0 else 0.0

    if zt_df is not None and "连板数" in zt_df.columns and len(zt_df) > 0:
        streaks = pd.to_numeric(zt_df["连板数"], errors="coerce").dropna()
        highest = int(streaks.max()) if len(streaks) > 0 else 0
    else:
        highest = 0

    # 涨跌家数:有 spot 且含 '涨跌幅' 列时精确统计,否则降级为 None
    if spot_df is not None and "涨跌幅" in spot_df.columns:
        up_count: Optional[int] = int((spot_df["涨跌幅"] > 0).sum())
        down_count: Optional[int] = int((spot_df["涨跌幅"] < 0).sum())
    else:
        up_count = None
        down_count = None

    # 量能状态:今日两市总成交额对前N日均额;任一缺失或基准非正则数据不足
    if turnover is not None and turnover_baseline is not None and turnover_baseline > 0:
        ratio = turnover / turnover_baseline
        if ratio >= volume_up_ratio:
            volume_state = f"放量({ratio:.2f})"
        elif ratio <= volume_down_ratio:
            volume_state = f"缩量({ratio:.2f})"
        else:
            volume_state = f"平量({ratio:.2f})"
    else:
        volume_state = "数据不足"

    # 情绪提示
    if up_count is not None:
        # 有全市场涨跌家数时沿用精确阈值
        if up_count >= hot_up_count:
            hint = "情绪高点(追涨谨慎)"
        elif up_count <= cold_up_count:
            hint = "接近冰点(关注机会)"
        else:
            hint = "中性"
    else:
        # 无 spot 数据,用涨跌停池粗粒度判断
        if limit_down > limit_up and highest <= 3:
            hint = "情绪偏冷(关注机会)"
        elif limit_up > limit_down * 2 and highest >= 5:
            hint = "情绪偏热(追涨谨慎)"
        else:
            hint = "中性"

    return MarketEmotion(
        up_count=up_count,
        down_count=down_count,
        limit_up=limit_up,
        limit_down=limit_down,
        broken_board_rate=broken_rate,
        highest_streak=highest,
        volume_state=volume_state,
        sentiment_hint=hint,
    )
