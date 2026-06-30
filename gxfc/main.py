"""主流程:盘后一次运行,拉数 → 算因子 → 组装面板 → 打印 + 存 CSV。

build_board 接受注入的 fetcher(便于用假对象测试);run_daily 是真实入口,
内部构建带 SQLite 缓存的 Fetcher。断层扫描仅对业绩预告里的股票拉日K,
并用 top_codes_limit 限制数量以控制网络请求量。
"""
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from gxfc.data.cache import DataFrameCache
from gxfc.data.fetcher import Fetcher
from gxfc.factors.market_emotion import compute_market_emotion
from gxfc.factors.profit_fault import scan_profit_fault
from gxfc.factors.sector import rank_sectors, core_stocks
from gxfc.review.daily_board import DailyBoard, render_console, save_csv

logger = logging.getLogger(__name__)


def load_config(path: str = "config/strategy.yaml") -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def _daily_window(date: str) -> tuple:
    """断层检测需要最近两个交易日,这里取该日往前 10 个自然日,保证跨月也至少覆盖前一交易日。"""
    end_dt = datetime.strptime(date, "%Y%m%d")
    start_dt = end_dt - timedelta(days=10)
    return start_dt.strftime("%Y%m%d"), date


def build_board(fetcher, date: str, quarter_end: str, config: dict,
                top_codes_limit: int = 20) -> DailyBoard:
    emo_cfg = config["emotion"]
    sec_cfg = config["sector"]
    pf_cfg = config["profit_fault"]

    # 从三个涨跌停池计算市场情绪(legu 接口在 AKShare 1.17.83 已坏)
    zt = fetcher.zt_pool(date)
    dt = fetcher.dt_pool(date)
    zb = fetcher.zb_pool(date)
    # TODO: 阶段2可接 spot=fetcher.spot() 以获取全市场涨跌家数,但东财限流敏感
    spot = None
    emotion = compute_market_emotion(
        zt, dt, zb, spot_df=spot,
        hot_up_count=emo_cfg["hot_up_count"],
        cold_up_count=emo_cfg["cold_up_count"],
    )

    board_df = fetcher.industry_board()
    sectors = rank_sectors(board_df, top_n=sec_cfg["top_n"])

    # 对榜单前 core_drill_top_n 个强势板块下钻,取各自核心成分股
    sector_cores = {}
    drill_names = list(sectors["板块名称"].head(sec_cfg["core_drill_top_n"]))
    for name in drill_names:
        try:
            cons = fetcher.industry_cons(name)
            sector_cores[name] = core_stocks(cons, core_top_n=sec_cfg["core_top_n"])
            time.sleep(0.3)  # 礼貌延迟,避免东财限流
        except Exception as err:
            logger.warning("拉取板块 %s 成分股失败,跳过:%s", name, err)

    yjyg = fetcher.yjyg(quarter_end).head(top_codes_limit)
    start, end = _daily_window(date)
    daily_map = {}
    seen_codes: set = set()
    for _, r in yjyg.iterrows():
        code = r.get("股票代码")
        if not code:
            continue
        code = str(code)
        if code in seen_codes:
            continue  # 多行结构中同一股票只拉一次日K
        seen_codes.add(code)
        try:
            daily_map[code] = fetcher.stock_daily(code, start, end)
            time.sleep(0.3)  # 礼貌延迟,避免东财限流
        except Exception as err:
            logger.warning("拉取 %s 日K失败,跳过:%s", code, err)
    candidates = scan_profit_fault(yjyg, daily_map, growth_threshold=pf_cfg["growth_threshold"])

    return DailyBoard(
        date=date, emotion=emotion, sectors=sectors,
        candidates=candidates, sector_cores=sector_cores,
    )


def run_daily(date: str, quarter_end: str, out_dir: str = "out") -> DailyBoard:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
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
