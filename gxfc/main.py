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

import pandas as pd
import yaml

from gxfc.data.cache import DataFrameCache
from gxfc.data.fetcher import Fetcher
from gxfc.factors.bottom_volume import scan_bottom_volume
from gxfc.factors.market_emotion import MarketEmotion, compute_market_emotion
from gxfc.factors.profit_fault import scan_profit_fault, passes_growth
from gxfc.factors.sector import rank_sectors, core_stocks

_NET_PROFIT = "归属于上市公司股东的净利润"
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

    # 各段独立降级:单一数据源失败只影响该段,不阻塞整个面板

    # 1) 市场情绪(从三个涨跌停池计算;legu 接口在 AKShare 1.17.83 已坏)
    try:
        zt = fetcher.zt_pool(date)
        dt = fetcher.dt_pool(date)
        zb = fetcher.zb_pool(date)
        # 全market快照用于统计涨跌家数;单独容错,失败只丢家数不影响涨跌停口径
        try:
            spot_df = fetcher.spot()
        except Exception as err:
            logger.warning("获取实时快照失败,涨跌家数降级为空:%s", err)
            spot_df = None
        emotion = compute_market_emotion(
            zt, dt, zb, spot_df=spot_df,
            hot_up_count=emo_cfg["hot_up_count"],
            cold_up_count=emo_cfg["cold_up_count"],
        )
    except Exception as err:
        logger.warning("获取市场情绪失败,该段降级:%s", err)
        emotion = MarketEmotion(
            up_count=None, down_count=None, limit_up=0, limit_down=0,
            broken_board_rate=0.0, highest_streak=0,
            volume_state="数据不足", sentiment_hint="情绪数据获取失败",
        )

    # 2) 板块涨幅榜 + 核心成分股下钻
    sector_cores = {}
    try:
        board_df = fetcher.industry_board()
        sectors = rank_sectors(board_df, top_n=sec_cfg["top_n"])
        drill_names = list(sectors["板块名称"].head(sec_cfg["core_drill_top_n"]))
        for name in drill_names:
            try:
                cons = fetcher.industry_cons(name)
                sector_cores[name] = core_stocks(cons, core_top_n=sec_cfg["core_top_n"])
                time.sleep(0.3)  # 礼貌延迟,避免东财限流
            except Exception as err:
                logger.warning("拉取板块 %s 成分股失败,跳过:%s", name, err)
    except Exception as err:
        logger.warning("获取板块榜失败,该段降级为空:%s", err)
        sectors = pd.DataFrame(columns=["板块名称", "涨跌幅", "领涨股票"])

    # 3) 净利润断层候选
    high_growth_codes: set = set()
    try:
        yjyg_full = fetcher.yjyg(quarter_end)
        # 业绩高增集合(全量,供底部爆量段叠加标记):净利润行且增速达标
        for _, r in yjyg_full.iterrows():
            if r.get("预测指标") != _NET_PROFIT:
                continue
            if passes_growth(r.get("业绩变动幅度"), pf_cfg["growth_threshold"]):
                code = str(r.get("股票代码", "")).strip()
                if code:
                    high_growth_codes.add(code)
        yjyg = yjyg_full.head(top_codes_limit)
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
    except Exception as err:
        logger.warning("获取业绩预告失败,断层候选降级为空:%s", err)
        yjyg = pd.DataFrame(columns=["股票代码", "股票简称", "预测指标", "业绩变动幅度"])
        daily_map = {}
    candidates = scan_profit_fault(yjyg, daily_map, growth_threshold=pf_cfg["growth_threshold"])

    # 4) 全市场底部爆量大涨(混合架构:新浪全市场粗筛 → 幸存者 Baostock 60日K 精算)
    surge = _scan_market_bottom_volume(fetcher, date, config, high_growth_codes)

    return DailyBoard(
        date=date, emotion=emotion, sectors=sectors,
        candidates=candidates, sector_cores=sector_cores,
        surge_candidates=surge,
    )


def _scan_market_bottom_volume(fetcher, date: str, config: dict, high_growth_codes: set) -> pd.DataFrame:
    """全市场底部爆量大涨扫描,整段独立容错,失败返回空表。

    第1步用一次新浪全市场快照按涨幅粗筛,第2步只对幸存者逐只拉 60 日日K
    (走 Baostock 主源,复用缓存),交由 scan_bottom_volume 现算三条件。
    """
    bv_cfg = config["bottom_volume"]
    try:
        spot_all = fetcher.market_spot()
        pct = pd.to_numeric(spot_all["涨跌幅"], errors="coerce")
        survivors = spot_all.assign(_pct=pct)
        survivors = survivors[survivors["_pct"] >= bv_cfg["rise_threshold"]]
        # Baostock 仅覆盖沪深(主板/创业板/科创板);剔除北交所/B股等无免费历史源的代码
        codes = survivors["代码"].astype(str)
        keep = codes.str.startswith(("0", "3", "6"))
        dropped = int((~keep).sum())
        survivors = survivors[keep]
        if dropped:
            logger.info("底部爆量:剔除 %d 只无 Baostock 历史源的代码(北交所/B股)", dropped)
        hit = len(survivors)
        # 按涨幅降序取上限,控制 Baostock 拉取耗时;超出部分明确记日志(非静默截断)
        max_survivors = bv_cfg.get("max_survivors", 60)
        survivors = survivors.sort_values("_pct", ascending=False).head(max_survivors).copy()
        if hit > max_survivors:
            logger.warning("底部爆量:涨幅≥%.1f%% 命中 %d 只,超上限按涨幅取前 %d 只精算",
                           bv_cfg["rise_threshold"], hit, max_survivors)
        else:
            logger.info("底部爆量:粗筛涨幅≥%.1f%% 命中 %d 只,逐只拉日K中",
                        bv_cfg["rise_threshold"], hit)
        # 往前约 130 自然日,确保覆盖 ≥ bottom_window 个交易日
        start = (datetime.strptime(date, "%Y%m%d") - timedelta(days=130)).strftime("%Y%m%d")
        daily_map = {}
        for code in survivors["代码"]:
            code = str(code)
            try:
                daily_map[code] = fetcher.stock_daily(code, start, date)
            except Exception as err:
                logger.warning("拉取 %s 日K失败(底部爆量),跳过:%s", code, err)
        result = scan_bottom_volume(
            survivors, daily_map, high_growth_codes,
            rise_threshold=bv_cfg["rise_threshold"],
            volume_ratio_threshold=bv_cfg["volume_ratio_threshold"],
            bottom_ratio=bv_cfg["bottom_ratio"],
            bottom_window=bv_cfg["bottom_window"],
            volume_baseline=bv_cfg["volume_baseline"],
        )
        return result.head(bv_cfg["top_n"])
    except Exception as err:
        logger.warning("全市场底部爆量扫描失败,该段降级为空:%s", err)
        return pd.DataFrame()


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
