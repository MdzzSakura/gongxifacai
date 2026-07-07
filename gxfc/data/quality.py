"""入库质量闸门:坏数据不入库。

validate 在写 DuckStore 之前统一过闸,规则按数据集分级:
- 必需列缺失 → 抛 QualityError(该数据集本轮采集失败,由台账记录);
- 全市场日K快照行数较上期骤降超阈值 → 抛 QualityError(半残快照比没有更危险,
  会把大半个市场标成"停牌"并污染涨跌家数);
- 数值粗检(价格≤0、涨跌幅越界)→ 剔除该行并计数告警,不阻塞整体;
- (代码,日期)重复 → 保留最后一条。

快照类空表放行(极端行情下涨停池可以为空),由采集方在台账记 'empty'。
"""
import logging
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# 各数据集必需列(缺失说明上游接口变更或半残返回,必须拦截并指明数据集)
REQUIRED_COLS = {
    "daily": {"代码", "日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额"},
    "daily_snapshot": {"代码", "名称", "今开", "最高", "最低", "收盘", "昨收", "成交量", "成交额"},
    "zt_pool": {"代码", "名称", "涨跌幅"},
    "dt_pool": {"代码", "名称", "涨跌幅"},
    "zb_pool": {"代码", "名称", "涨跌幅"},
    "industry_board": {"板块名称", "涨跌幅"},
    "industry_cons": {"名称", "涨跌幅", "成交额"},
    "yjyg": {"股票代码", "股票简称", "预测指标", "业绩变动幅度"},
}

# 全市场快照行数低于上期的该比例视为半残数据,拒写
_SNAPSHOT_SHRINK_RATIO = 0.8

# A股单日涨跌幅极限约 ±30%(北交所),留余量取 45%;越界视为脏数据行
_PCT_LIMIT = 45.0


class QualityError(Exception):
    """数据未通过质量闸门,该数据集本轮不入库。"""


def validate(dataset: str, df: pd.DataFrame, prev_rows: Optional[int] = None) -> pd.DataFrame:
    """校验并清洗,返回可入库的 DataFrame;不可修复的问题抛 QualityError。

    prev_rows:上期同数据集行数,仅对全市场快照类(daily_snapshot)做骤降检查。
    """
    if df is None:
        raise QualityError(f"{dataset}:返回 None")
    if df.empty:
        return df  # 空表放行,采集方记 'empty'

    required = REQUIRED_COLS.get(dataset)
    if required:
        missing = required - set(df.columns)
        if missing:
            raise QualityError(f"{dataset}:缺必需列 {sorted(missing)}(上游接口可能已变更)")

    out = df

    # 全市场快照:行数骤降拦截
    if dataset == "daily_snapshot" and prev_rows and prev_rows > 0:
        if len(out) < prev_rows * _SNAPSHOT_SHRINK_RATIO:
            raise QualityError(
                f"{dataset}:行数 {len(out)} 较上期 {prev_rows} 骤降超 "
                f"{1 - _SNAPSHOT_SHRINK_RATIO:.0%},疑似半残数据,拒写"
            )

    # 数值粗检:剔除脏行(价格非正、涨跌幅越界)
    if dataset in ("daily", "daily_snapshot"):
        before = len(out)
        close = pd.to_numeric(out["收盘"], errors="coerce")
        keep = close > 0
        if "昨收" in out.columns:
            prev_close = pd.to_numeric(out["昨收"], errors="coerce")
            pct_ok = (prev_close <= 0) | ((close / prev_close - 1).abs() * 100 <= _PCT_LIMIT)
            keep &= pct_ok.fillna(True)
        out = out[keep.fillna(False)]
        dropped = before - len(out)
        if dropped:
            logger.warning("%s:剔除 %d 行脏数据(价格非正/涨跌幅越界)", dataset, dropped)

    # 去重
    if dataset in ("daily", "daily_snapshot"):
        subset = ["代码", "日期"] if "日期" in out.columns else ["代码"]
        before = len(out)
        out = out.drop_duplicates(subset=subset, keep="last")
        if len(out) < before:
            logger.warning("%s:去除 %d 行重复", dataset, before - len(out))

    return out
