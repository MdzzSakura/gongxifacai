"""每日复盘面板:把情绪、板块榜、断层候选组装成可读面板并落地 CSV。

面板回答用户每天盘后最关心的三件事:今天情绪冷热、哪些板块在涨、
哪些票出了净利润断层。候选基于 proxy 口径(同比增速),面板显式声明。
"""
import os
from dataclasses import dataclass

import pandas as pd
from tabulate import tabulate

from gxfc.factors.market_emotion import MarketEmotion


@dataclass
class DailyBoard:
    date: str
    emotion: MarketEmotion
    sectors: pd.DataFrame
    candidates: pd.DataFrame


def render_console(board: DailyBoard) -> str:
    e = board.emotion
    lines = []
    lines.append(f"===== A股每日复盘面板 {board.date} =====")
    lines.append("")
    lines.append("【市场情绪温度计】")
    emotion_rows = [
        ["上涨/下跌家数", f"{e.up_count} / {e.down_count}"],
        ["涨停/跌停家数", f"{e.limit_up} / {e.limit_down}"],
        ["炸板率", f"{e.broken_board_rate:.1%}"],
        ["最高板", f"{e.highest_streak} 板"],
        ["量能状态", e.volume_state],
        ["情绪提示", e.sentiment_hint],
    ]
    lines.append(tabulate(emotion_rows, tablefmt="grid"))
    lines.append("")
    lines.append("【板块涨幅榜】")
    lines.append(tabulate(board.sectors, headers="keys", tablefmt="grid", showindex=False))
    lines.append("")
    lines.append("【净利润断层候选】(proxy口径:预告净利润同比增速,非券商一致预期)")
    if len(board.candidates) > 0:
        lines.append(
            tabulate(board.candidates, headers="keys", tablefmt="grid", showindex=False)
        )
    else:
        lines.append("(当日无达标候选)")
    return "\n".join(lines)


def save_csv(board: DailyBoard, out_dir: str) -> list:
    os.makedirs(out_dir, exist_ok=True)
    sector_path = os.path.join(out_dir, f"sectors_{board.date}.csv")
    cand_path = os.path.join(out_dir, f"candidates_{board.date}.csv")
    board.sectors.to_csv(sector_path, index=False, encoding="utf-8-sig")
    board.candidates.to_csv(cand_path, index=False, encoding="utf-8-sig")
    return [sector_path, cand_path]
