"""离线筛选:只读 DuckDB 组装每日复盘面板,全程零网络请求。

离线复盘链路:数据不从 Fetcher 拉,而是读 gxfc.ingest 落好的本地库。
可任意高频重跑、调参数复算,永不触发限流。
某数据集未采集时该段降级并标注,不阻塞整个面板。

本模块**禁止 import gxfc.data.fetcher**——"严格离线"靠依赖关系保证,
而非运行时开关。

CLI:python -m gxfc.screen [YYYYMMDD] [--db 路径] [--out 目录]
     不传日期默认库内最新交易日。
"""
import argparse
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import yaml

from gxfc.factors.bottom_volume import scan_bottom_volume
from gxfc.factors.market_emotion import MarketEmotion, compute_market_emotion
from gxfc.factors.profit_fault import passes_growth, scan_profit_fault
from gxfc.factors.sector import core_stocks, rank_sectors
from gxfc.dates import dash, derive_quarter_end
from gxfc.review.daily_board import DailyBoard, render_console, save_csv
from gxfc.store.duck_store import DuckStore
from gxfc.store.journal_store import JournalStore

logger = logging.getLogger(__name__)

_NET_PROFIT = "归属于上市公司股东的净利润"
# 断层检测窗口(自然日):覆盖最近两个交易日即可,跨长假取 10 天
_FAULT_WINDOW_DAYS = 10
# 底部爆量历史窗口(自然日):覆盖 ≥60 交易日
_SURGE_WINDOW_DAYS = 130


def load_config(path: str = "config/strategy.yaml") -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def _emotion_offline(store: DuckStore, date: str, emo_cfg: dict) -> MarketEmotion:
    """情绪段:三池读快照表,涨跌家数由日K离线重建。未采集则降级标注。"""
    if not store.has_ok("zt_pool", date):
        return MarketEmotion(
            up_count=None, down_count=None, limit_up=0, limit_down=0,
            broken_board_rate=0.0, highest_streak=0,
            volume_state="数据不足", sentiment_hint=f"{date} 情绪数据未采集,请先运行 python -m gxfc.ingest",
        )
    zt = store.read_snapshot("zt_pool", "trade_date", date)
    dt = store.read_snapshot("dt_pool", "trade_date", date)
    zb = store.read_snapshot("zb_pool", "trade_date", date)
    pct = store.read_market_pct(date)
    return compute_market_emotion(
        zt, dt, zb, spot_df=pct if not pct.empty else None,
        hot_up_count=emo_cfg["hot_up_count"],
        cold_up_count=emo_cfg["cold_up_count"],
    )


def _sectors_offline(store: DuckStore, date: str, sec_cfg: dict):
    """板块段:榜单 + 各板块核心成分股。未采集返回空表(面板显示无数据)。"""
    board = store.read_snapshot("industry_board", "trade_date", date)
    if board.empty:
        if not store.has_ok("industry_board", date):
            logger.warning("%s 板块榜未采集,该段降级为空", date)
        return pd.DataFrame(columns=["板块名称", "涨跌幅", "领涨股票"]), {}
    sectors = rank_sectors(board, top_n=sec_cfg["top_n"])
    cores = {}
    cons_all = store.read_snapshot("industry_cons", "trade_date", date)
    if not cons_all.empty:
        for name, group in cons_all.groupby("板块名称", sort=False):
            cores[name] = core_stocks(group, core_top_n=sec_cfg["core_top_n"])
    return sectors, cores


def _profit_fault_offline(store: DuckStore, date: str, quarter_end: str,
                          pf_cfg: dict, top_codes_limit: int):
    """断层段:业绩预告读快照,日K窗口读本地(含目标日当日行,供跳空判定)。"""
    yjyg_full = store.read_snapshot("yjyg", "quarter_end", quarter_end)
    if yjyg_full.empty:
        if not store.has_ok("yjyg", quarter_end):
            logger.warning("业绩预告(%s)未采集,断层候选降级为空", quarter_end)
        empty = pd.DataFrame(columns=["股票代码", "股票简称", "预测指标", "业绩变动幅度"])
        return scan_profit_fault(empty, {}, growth_threshold=pf_cfg["growth_threshold"]), set()

    high_growth_codes: set = set()
    for _, r in yjyg_full.iterrows():
        if r.get("预测指标") != _NET_PROFIT:
            continue
        if passes_growth(r.get("业绩变动幅度"), pf_cfg["growth_threshold"]):
            code = str(r.get("股票代码", "")).strip()
            if code:
                high_growth_codes.add(code)

    yjyg = yjyg_full.head(top_codes_limit)
    end_dt = datetime.strptime(dash(date), "%Y-%m-%d")
    start = (end_dt - timedelta(days=_FAULT_WINDOW_DAYS)).strftime("%Y-%m-%d")
    daily_map = {}
    for code in dict.fromkeys(str(c) for c in yjyg["股票代码"] if c):
        window = store.read_daily(code, start, date)
        if not window.empty:
            daily_map[code] = window
    candidates = scan_profit_fault(yjyg, daily_map, growth_threshold=pf_cfg["growth_threshold"])
    return candidates, high_growth_codes


def _surge_offline(store: DuckStore, date: str, config: dict, high_growth_codes: set) -> pd.DataFrame:
    """底部爆量段:今日值来自离线重建的市场视图,历史值取目标日**之前**的日K
    (三条件的量比/底部参照的都是"之前",目标日当日行必须排除在历史外)。"""
    bv_cfg = config["bottom_volume"]
    pct = store.read_market_pct(date)
    if pct.empty:
        logger.warning("%s 全市场日K不可用,底部爆量段降级为空", date)
        return pd.DataFrame()
    survivors = pct[pd.to_numeric(pct["涨跌幅"], errors="coerce") >= bv_cfg["rise_threshold"]]
    max_survivors = bv_cfg.get("max_survivors", 60)
    hit = len(survivors)
    survivors = survivors.sort_values("涨跌幅", ascending=False).head(max_survivors)
    if hit > max_survivors:
        logger.warning("底部爆量:命中 %d 只超上限,按涨幅取前 %d 只精算", hit, max_survivors)

    end_dt = datetime.strptime(dash(date), "%Y-%m-%d")
    start = (end_dt - timedelta(days=_SURGE_WINDOW_DAYS)).strftime("%Y-%m-%d")
    prev_day = (end_dt - timedelta(days=1)).strftime("%Y-%m-%d")
    daily_map = {}
    for code in survivors["代码"]:
        hist = store.read_daily(str(code), start, prev_day)   # 严格"之前"的历史
        if not hist.empty:
            daily_map[str(code)] = hist
    result = scan_bottom_volume(
        survivors, daily_map, high_growth_codes,
        rise_threshold=bv_cfg["rise_threshold"],
        volume_ratio_threshold=bv_cfg["volume_ratio_threshold"],
        bottom_ratio=bv_cfg["bottom_ratio"],
        bottom_window=bv_cfg["bottom_window"],
        volume_baseline=bv_cfg["volume_baseline"],
    )
    return result.head(bv_cfg["top_n"])


def build_board_offline(store: DuckStore, date: str, quarter_end: str, config: dict,
                        top_codes_limit: int = 20) -> DailyBoard:
    """从本地库组装每日面板。各段独立降级:某数据集未采集只影响该段。"""
    date = dash(date)
    emotion = _emotion_offline(store, date, config["emotion"])
    sectors, sector_cores = _sectors_offline(store, date, config["sector"])
    candidates, high_growth = _profit_fault_offline(
        store, date, quarter_end, config["profit_fault"], top_codes_limit
    )
    surge = _surge_offline(store, date, config, high_growth)
    return DailyBoard(
        date=date, emotion=emotion, sectors=sectors,
        candidates=candidates, sector_cores=sector_cores,
        surge_candidates=surge,
    )


def run_screen(date: Optional[str] = None, db_path: str = "gxfc_data.duckdb",
               out_dir: str = "out") -> DailyBoard:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = load_config()
    store = DuckStore(db_path)
    try:
        target = dash(date) if date else store.daily_max_date()
        if target is None:
            raise SystemExit("本地库无日K数据,请先运行 python -m gxfc.ingest")
        quarter_end = derive_quarter_end(target)
        board = build_board_offline(store, target, quarter_end, config)
        print(render_console(board))
        paths = save_csv(board, out_dir)
        logger.info("已保存:%s", paths)
        journal = JournalStore(store.con)
        pf = board.candidates.rename(columns={"股票代码": "代码", "股票简称": "名称"})
        n_pf = journal.record_signals(target, "profit_fault", pf)
        n_bv = journal.record_signals(target, "bottom_volume", board.surge_candidates)
        logger.info("信号落库:断层 %d 条,底部爆量 %d 条(重跑同日自动覆盖)", n_pf, n_bv)
        return board
    finally:
        store.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="离线选股筛选(只读本地 DuckDB,零网络)")
    parser.add_argument("date", nargs="?", default=None, help="目标交易日 YYYYMMDD,默认库内最新")
    parser.add_argument("--db", default="gxfc_data.duckdb", help="DuckDB 文件路径")
    parser.add_argument("--out", default="out", help="CSV 输出目录")
    args = parser.parse_args(argv)
    run_screen(date=args.date, db_path=args.db, out_dir=args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
