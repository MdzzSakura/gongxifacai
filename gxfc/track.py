"""信号追踪报表:读 signals + daily,输出各策略前向收益明细与汇总。零网络。

回答"我的筛选器筛出来的票,后来到底怎么走"——胜率/平均收益/盈亏比按
策略×持有期呈现,是调参和取舍模式的唯一证据来源。

本模块禁止 import gxfc.data.fetcher(严格离线)。

CLI:python -m gxfc.track [--db 路径] [--strategy 名] [--start YYYYMMDD]
     [--end YYYYMMDD] [--out 目录]
"""
import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml
from tabulate import tabulate

from gxfc.review.tracker import summarize, track_signals
from gxfc.store.duck_store import DuckStore
from gxfc.store.journal_store import JournalStore

logger = logging.getLogger(__name__)


def _load_horizons(path: str = "config/strategy.yaml") -> tuple:
    try:
        config = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        config = {}
    return tuple(config.get("tracking", {}).get("horizons", [1, 3, 5, 10]))


def run_track(db_path: str = "gxfc_data.duckdb", strategy: Optional[str] = None,
              start: Optional[str] = None, end: Optional[str] = None,
              out_dir: str = "out"):
    """追踪库内信号并打印/落盘报表。无信号返回 (None, None)。"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    horizons = _load_horizons()
    store = DuckStore(db_path)
    try:
        journal = JournalStore(store.con)
        signals = journal.read_signals(strategy=strategy, start=start, end=end)
        if signals.empty:
            print("库内无信号:请先运行 python -m gxfc.screen 产出并落库信号")
            return None, None
        perf = track_signals(signals, store.read_daily, horizons)
        summary = summarize(perf, horizons)
        untracked = int((~perf["可追踪"]).sum())
        print(f"===== 信号前向收益明细(共 {len(perf)} 条,其中信号日缺日K不可追踪 {untracked} 条)=====")
        print(tabulate(perf, headers="keys", tablefmt="grid", showindex=False))
        print("\n===== 策略 × 持有期 汇总 =====")
        if summary.empty:
            print("(样本不足:信号日之后尚无足够交易日,过几天再跑)")
        else:
            print(tabulate(summary, headers="keys", tablefmt="grid", showindex=False))
        os.makedirs(out_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d")
        perf_path = os.path.join(out_dir, f"signal_performance_{stamp}.csv")
        summary_path = os.path.join(out_dir, f"signal_summary_{stamp}.csv")
        perf.to_csv(perf_path, index=False, encoding="utf-8-sig")
        summary.to_csv(summary_path, index=False, encoding="utf-8-sig")
        logger.info("已保存:%s", [perf_path, summary_path])
        return perf, summary
    finally:
        store.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="信号前向收益追踪(只读本地 DuckDB,零网络)")
    parser.add_argument("--db", default="gxfc_data.duckdb", help="DuckDB 文件路径")
    parser.add_argument("--strategy", default=None, help="只看某策略(profit_fault/bottom_volume)")
    parser.add_argument("--start", default=None, help="信号起始日 YYYYMMDD")
    parser.add_argument("--end", default=None, help="信号截止日 YYYYMMDD")
    parser.add_argument("--out", default="out", help="CSV 输出目录")
    args = parser.parse_args(argv)
    run_track(db_path=args.db, strategy=args.strategy,
              start=args.start, end=args.end, out_dir=args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
