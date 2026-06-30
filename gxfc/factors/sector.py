"""板块因子:板块涨幅榜与板块内核心成分股。

设计意图:复刻"先看板块涨幅,再在板块里挑核心票"。板块按涨幅排序定主线,
成分股按涨幅(辅以成交额可读性)排序近似"地位/带动性"。
"""
import pandas as pd

_SECTOR_COLS = ["板块名称", "涨跌幅", "领涨股"]
_CORE_COLS = ["名称", "涨跌幅", "成交额"]


def _keep_existing(df: pd.DataFrame, cols: list) -> pd.DataFrame:
    existing = [c for c in cols if c in df.columns]
    return df[existing]


def rank_sectors(board_df: pd.DataFrame, top_n: int = 10) -> pd.DataFrame:
    ranked = board_df.sort_values("涨跌幅", ascending=False).head(top_n)
    return _keep_existing(ranked, _SECTOR_COLS).reset_index(drop=True)


def core_stocks(cons_df: pd.DataFrame, core_top_n: int = 5) -> pd.DataFrame:
    ranked = cons_df.sort_values("涨跌幅", ascending=False).head(core_top_n)
    return _keep_existing(ranked, _CORE_COLS).reset_index(drop=True)
