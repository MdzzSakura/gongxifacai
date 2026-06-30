"""市场情绪因子:把市场赚钱效应与涨停池算成情绪指标。

设计意图:盘后一眼看清当日情绪冷热——涨跌家数判断广度,炸板率判断
追涨风险,最高板判断市场高度,量能判断持续性。阈值来自用户文档经验值。
"""
import re
from dataclasses import dataclass
from typing import Optional

import pandas as pd


@dataclass
class MarketEmotion:
    up_count: int
    down_count: int
    limit_up: int
    limit_down: int
    broken_board_rate: float
    highest_streak: int
    volume_state: str
    sentiment_hint: str


def _to_int(value) -> int:
    """从 AKShare 的 value(可能是 '1234' 或 1234 或 '12家')里抠出整数。"""
    if value is None:
        return 0
    digits = re.sub(r"[^0-9-]", "", str(value))
    return int(digits) if digits not in ("", "-") else 0


def compute_market_emotion(
    activity_df: pd.DataFrame,
    zt_pool_df: pd.DataFrame,
    today_amount: Optional[float] = None,
    avg5_amount: Optional[float] = None,
    hot_up_count: int = 4500,
    cold_up_count: int = 800,
    volume_up_ratio: float = 1.15,
    volume_down_ratio: float = 0.85,
) -> MarketEmotion:
    table = {str(row["item"]): row["value"] for _, row in activity_df.iterrows()}
    up = _to_int(table.get("上涨"))
    down = _to_int(table.get("下跌"))
    limit_up = _to_int(table.get("涨停"))
    limit_down = _to_int(table.get("跌停"))
    broken = _to_int(table.get("炸板"))

    denom = limit_up + broken
    broken_rate = (broken / denom) if denom > 0 else 0.0

    if zt_pool_df is not None and "连板数" in zt_pool_df.columns and len(zt_pool_df) > 0:
        streaks = pd.to_numeric(zt_pool_df["连板数"], errors="coerce").dropna()
        highest = int(streaks.max()) if len(streaks) > 0 else 0
    else:
        highest = 0

    if today_amount is None or avg5_amount is None or avg5_amount == 0:
        volume_state = "数据不足"
    else:
        ratio = today_amount / avg5_amount
        if ratio >= volume_up_ratio:
            volume_state = "放量"
        elif ratio <= volume_down_ratio:
            volume_state = "缩量"
        else:
            volume_state = "平量"

    if up >= hot_up_count:
        hint = "情绪高点(追涨谨慎)"
    elif up <= cold_up_count:
        hint = "接近冰点(关注机会)"
    else:
        hint = "中性"

    return MarketEmotion(
        up_count=up,
        down_count=down,
        limit_up=limit_up,
        limit_down=limit_down,
        broken_board_rate=broken_rate,
        highest_streak=highest,
        volume_state=volume_state,
        sentiment_hint=hint,
    )
