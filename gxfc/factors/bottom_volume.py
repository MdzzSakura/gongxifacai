"""底部爆量大涨扫描。

策略:股价在相对低位长期低迷后,某日突放巨量 + 大涨,常为趋势启动信号。
三条件同时满足才入选:
  - 大涨:今日涨跌幅 ≥ rise_threshold(%)
  - 爆量:量比 = 今日成交量 / 前 baseline 日均量 ≥ volume_ratio_threshold
  - 底部:今收 ≤ 之前 window 日最高价 × bottom_ratio(距高点回落足够深视为低位)

数据来源分工(关键):**今日值(涨幅/收盘/成交量)来自实时全市场快照(新浪),
历史值(前N日均量、区间最高)来自日K(Baostock)**。因为日K源当日盘后通常尚未
更新当天 bar,只有实时快照有当日数据;且"底部"本就该用"之前"的高点作参照。
新浪与 Baostock 成交量单位均为"股",量比可直接相除。

业绩高增(净利润预告增速达标)仅作叠加标记,非筛选条件。
"""
import pandas as pd

_HIST_HIGH = "最高"
_HIST_VOLUME = "成交量"

_OUTPUT_COLS = ["代码", "名称", "今日涨跌幅", "量比", "距高点折价", "成交额", "业绩高增"]


def compute_volume_ratio(today_volume: float, hist_volumes: list, baseline: int = 5) -> float:
    """量比 = 今日成交量 / 之前 baseline 日平均成交量。

    hist_volumes 为今日之前的历史成交量序列(升序)。不足 baseline 个或均量
    为 0 时返回 0.0(由调用方按"未达标"处理)。
    """
    if len(hist_volumes) < baseline:
        return 0.0
    prev = [float(v) for v in hist_volumes[-baseline:]]
    average = sum(prev) / baseline
    if average <= 0:
        return 0.0
    return float(today_volume) / average


def is_bottom(today_close: float, hist_highs: list, window: int = 60, ratio: float = 0.6) -> tuple:
    """判断今收是否处于之前 window 日相对低位。

    返回 (是否底部, 距高点折价%)。区间最高取今日之前最近 window 根(不足则取全部);
    区间最高 ≤ 0 视为非底部。距高点折价 = (1 - 今收/区间最高) × 100。
    """
    recent_highs = [float(h) for h in hist_highs[-window:]]
    if not recent_highs:
        return False, 0.0
    period_high = max(recent_highs)
    if period_high <= 0:
        return False, 0.0
    discount = (1.0 - today_close / period_high) * 100.0
    return today_close <= period_high * ratio, discount


def scan_bottom_volume(
    survivors_df: pd.DataFrame,
    daily_map: dict,
    high_growth_codes=None,
    *,
    rise_threshold: float = 7.0,
    volume_ratio_threshold: float = 2.0,
    bottom_ratio: float = 0.6,
    bottom_window: int = 60,
    volume_baseline: int = 5,
) -> pd.DataFrame:
    """扫描满足"底部爆量大涨"的股票。

    Args:
        survivors_df: 粗筛幸存者(当日全市场快照),需含
            '代码','名称','涨跌幅'(今日%),'最新价'(今收),'成交量'(今量),'成交额'。
        daily_map: {代码: 历史日K DataFrame},列含 最高,成交量(均为今日之前的历史)。
        high_growth_codes: 业绩高增股代码集合,用于叠加标记(可空)。
        其余为阈值参数,见模块 docstring。

    Returns:
        DataFrame,列为 _OUTPUT_COLS,按量比降序;无入选返回空表。
    """
    growth_set = {str(c) for c in (high_growth_codes or [])}
    rows = []
    for _, r in survivors_df.iterrows():
        code = str(r.get("代码", "")).strip()
        if not code:
            continue
        daily = daily_map.get(code)
        if daily is None or len(daily) < volume_baseline:
            continue  # 历史不足,无法算量比

        pct_change = float(r.get("涨跌幅", 0.0))
        today_close = float(r.get("最新价", 0.0))
        today_volume = float(r.get("成交量", 0.0))
        if today_close <= 0:
            continue

        volume_ratio = compute_volume_ratio(today_volume, list(daily[_HIST_VOLUME]), volume_baseline)
        bottom_ok, discount = is_bottom(today_close, list(daily[_HIST_HIGH]), bottom_window, bottom_ratio)

        if not (
            pct_change >= rise_threshold
            and volume_ratio >= volume_ratio_threshold
            and bottom_ok
        ):
            continue

        rows.append(
            {
                "代码": code,
                "名称": str(r.get("名称", "")),
                "今日涨跌幅": round(pct_change, 2),
                "量比": round(volume_ratio, 2),
                "距高点折价": round(discount, 2),
                "成交额": float(r.get("成交额", 0.0)),
                "业绩高增": code in growth_set,
            }
        )

    result = pd.DataFrame(rows, columns=_OUTPUT_COLS)
    if not result.empty:
        result = result.sort_values("量比", ascending=False).reset_index(drop=True)
    return result
