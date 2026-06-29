"""净利润断层扫描。

净利润断层 = 业绩超预期 + 次日跳空缺口。受限于 AKShare 无券商一致预期,
"超预期"用"预告净利润同比增速≥阈值"作为 proxy(单季度/预告口径)。
跳空缺口 = 出业绩后下一交易日 今开 > 昨高。
"""
import pandas as pd

_GROWTH_COL = "预测净利润-同比增长"
_CODE_COL = "股票代码"
_NAME_COL = "股票简称"


def detect_gap(daily_df: pd.DataFrame) -> bool:
    """判断是否存在跳空缺口。

    Args:
        daily_df: 按日期升序排列的日K线数据,需包含'日期','开盘','最高'列

    Returns:
        True 如果最后一行开盘 > 前一行最高,否则 False
        不足2行返回 False
    """
    if daily_df is None or len(daily_df) < 2:
        return False
    ordered = daily_df.sort_values("日期").reset_index(drop=True)
    today_open = float(ordered.iloc[-1]["开盘"])
    prev_high = float(ordered.iloc[-2]["最高"])
    return today_open > prev_high


def passes_growth(growth_pct, threshold: float) -> bool:
    """判断同比增速是否达标。

    Args:
        growth_pct: 同比增速值(%)或 None/NaN
        threshold: 阈值

    Returns:
        True 如果增速 >= 阈值,False 如果为 None/NaN 或增速不达标
    """
    if growth_pct is None:
        return False
    try:
        value = float(growth_pct)
    except (TypeError, ValueError):
        return False
    if value != value:  # NaN check
        return False
    return value >= threshold


def scan_profit_fault(
    yjyg_df: pd.DataFrame, daily_map: dict, growth_threshold: float = 50.0
) -> pd.DataFrame:
    """扫描满足净利润断层条件的股票。

    Args:
        yjyg_df: 业绩预告数据,需包含'股票代码','股票简称','预测净利润-同比增长'列
        daily_map: {股票代码: daily_df} 日K线数据字典
        growth_threshold: 增速达标阈值,默认 50.0

    Returns:
        DataFrame,列为'股票代码','股票简称','同比增长','有跳空',已重置索引
        仅包含增速达标 且 存在跳空缺口的候选
    """
    rows = []
    for _, r in yjyg_df.iterrows():
        code = r.get(_CODE_COL)
        if code is None or code == "":
            continue
        code = str(code)
        growth = r.get(_GROWTH_COL)

        # 增速不达标则跳过
        if not passes_growth(growth, growth_threshold):
            continue

        # 缺失日K数据则跳过
        daily = daily_map.get(code)
        if daily is None:
            continue

        # 无跳空缺口则跳过
        if not detect_gap(daily):
            continue

        # 全部通过则纳入结果
        rows.append(
            {
                "股票代码": code,
                "股票简称": r.get(_NAME_COL, ""),
                "同比增长": float(growth),
                "有跳空": True,
            }
        )

    return pd.DataFrame(rows, columns=["股票代码", "股票简称", "同比增长", "有跳空"])
