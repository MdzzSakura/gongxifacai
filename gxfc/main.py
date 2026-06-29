"""主流程:盘后一次运行,拉数 → 算因子 → 组装面板 → 打印 + 存 CSV。

build_board 接受注入的 fetcher(便于用假对象测试);run_daily 是真实入口,
内部构建带 SQLite 缓存的 Fetcher。断层扫描仅对业绩预告里的股票拉日K,
并用 top_codes_limit 限制数量以控制网络请求量。
"""
import logging
import sys
from pathlib import Path

import yaml

from gxfc.data.cache import DataFrameCache
from gxfc.data.fetcher import Fetcher
from gxfc.factors.market_emotion import compute_market_emotion
from gxfc.factors.profit_fault import scan_profit_fault
from gxfc.factors.sector import rank_sectors
from gxfc.review.daily_board import DailyBoard, render_console, save_csv

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def load_config(path: str = "config/strategy.yaml") -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def _daily_window(date: str) -> tuple:
    """断层检测只需最近几天日K,这里取该日往前一个月足够覆盖前一交易日。"""
    year = int(date[:4])
    start = f"{year}{date[4:6]}01"  # 当月1日,足够包含前一交易日
    return start, date


def build_board(fetcher, date: str, quarter_end: str, config: dict,
                top_codes_limit: int = 30) -> DailyBoard:
    emo_cfg = config["emotion"]
    sec_cfg = config["sector"]
    pf_cfg = config["profit_fault"]

    activity = fetcher.market_activity()
    zt = fetcher.zt_pool(date)
    emotion = compute_market_emotion(
        activity, zt,
        hot_up_count=emo_cfg["hot_up_count"],
        cold_up_count=emo_cfg["cold_up_count"],
    )

    board_df = fetcher.industry_board()
    sectors = rank_sectors(board_df, top_n=sec_cfg["top_n"])

    yjyg = fetcher.yjyg(quarter_end).head(top_codes_limit)
    start, end = _daily_window(date)
    daily_map = {}
    for _, r in yjyg.iterrows():
        code = str(r["股票代码"])
        try:
            daily_map[code] = fetcher.stock_daily(code, start, end)
        except Exception as err:
            logger.warning("拉取 %s 日K失败,跳过:%s", code, err)
    candidates = scan_profit_fault(yjyg, daily_map, growth_threshold=pf_cfg["growth_threshold"])

    return DailyBoard(date=date, emotion=emotion, sectors=sectors, candidates=candidates)


def run_daily(date: str, quarter_end: str, out_dir: str = "out") -> DailyBoard:
    config = load_config()
    fetcher = Fetcher(cache=DataFrameCache("gxfc_cache.db"))
    board = build_board(fetcher, date, quarter_end, config)
    print(render_console(board))
    paths = save_csv(board, out_dir)
    logger.info("已保存:%s", paths)
    return board


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("用法: python -m gxfc.main <交易日YYYYMMDD> <季度末YYYYMMDD>")
        sys.exit(1)
    run_daily(sys.argv[1], sys.argv[2])
