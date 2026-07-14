"""联网采集编排:项目内唯一触网的入口,把当日全部所需数据增量落进 DuckDB。

主流程(见设计文档 2026-07-07-stable-data-ingest-design.md 第5节):
  日历刷新 → 定位目标交易日 → 快照类数据集(台账续传) → 当日日K快照一次成型
  (含除权检测自愈) → 逐股历史回补(断点续传 + 失败预算) → 打印摘要。

稳定性要点:每日常态请求 ≤20 次(全市场日K靠一次快照成型,不逐股拉);
逐股拉取仅用于历史回补与除权重拉,中断重跑自动接着采(以库内 MAX(日期) 为准)。

CLI:python -m gxfc.ingest [YYYYMMDD] [--limit N] [--db 路径]
     不传日期默认最新已收盘交易日;--limit 限制本轮回补股票数(冒烟用)。
"""
import argparse
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import yaml

from gxfc.data.fetcher import Fetcher
from gxfc.data.quality import validate
from gxfc.dates import dash, derive_quarter_end, ymd
from gxfc.store.duck_store import DuckStore

logger = logging.getLogger(__name__)

# 历史回补默认窗口(自然日):覆盖底部爆量的 60 交易日窗口与断层检测绰绰有余
_BACKFILL_NATURAL_DAYS = 400
# 除权判定容差:快照昨收 与 库内前收 相对偏差超过该值视为发生除权
_EXDIV_TOLERANCE = 0.005
# 失败预算:逐股回补连续失败该数量即判定网络/源整体故障,中止本轮
_FAIL_BUDGET = 20
# A股收盘时间(含收盘集合竞价),此前跑采集取上一交易日
_CLOSE_HOUR, _CLOSE_MINUTE = 15, 5


class IngestAborted(RuntimeError):
    """连续失败超预算,本轮采集中止(已入库数据保留,重跑续传)。"""


def refresh_calendar(fetcher: Fetcher, store: DuckStore, now: datetime) -> None:
    """刷新交易日历:近 400 天到今年年末(1 次请求)。失败时若本地日历已覆盖
    今天则告警继续,否则抛错中止(没有日历就没有增量判断的依据)。"""
    start = (now - timedelta(days=_BACKFILL_NATURAL_DAYS)).strftime("%Y-%m-%d")
    end = f"{now.year}-12-31"
    try:
        store.upsert_calendar(fetcher.trade_dates(start, end))
    except Exception as err:
        cal_max = store.calendar_max()
        if cal_max and cal_max >= now.strftime("%Y-%m-%d"):
            logger.warning("交易日历刷新失败,使用本地既有日历(至 %s):%s", cal_max, err)
        else:
            raise RuntimeError(f"交易日历不可用且本地无覆盖,无法采集:{err}") from err


def _is_closed(now: datetime) -> bool:
    """当前时刻是否已过收盘(不关心今天是否交易日)。"""
    return now.hour > _CLOSE_HOUR or (now.hour == _CLOSE_HOUR and now.minute >= _CLOSE_MINUTE)


def latest_closed_trading_day(store: DuckStore, now: datetime) -> str:
    """最新已收盘交易日('YYYY-MM-DD'):收盘后取今天(若为交易日),否则回溯。"""
    today = now.strftime("%Y-%m-%d")
    closed = _is_closed(now)
    days = store.trading_days((now - timedelta(days=30)).strftime("%Y-%m-%d"), today)
    if not days:
        raise RuntimeError("近30天无交易日历数据,请检查日历采集")
    if days[-1] == today and not closed:
        days = days[:-1]
    if not days:
        raise RuntimeError("盘中运行且无更早交易日,请收盘后再采集")
    return days[-1]


def _load_config(path: str) -> dict:
    try:
        return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        logger.warning("未找到配置 %s,板块下钻用默认参数", path)
        return {}


def ingest_snapshots(fetcher: Fetcher, store: DuckStore, run_id: str,
                     date: str, is_current: bool, config: dict) -> None:
    """快照类数据集:涨跌停/炸板池、板块榜+成分、业绩预告。逐项独立容错。

    板块榜/成分股是实时接口,无法追溯历史,仅当目标日就是最新交易日时采集。
    """
    date_ymd = ymd(date)
    quarter_end = derive_quarter_end(date)
    jobs = [
        ("zt_pool", date, lambda: fetcher.zt_pool(date_ymd), "东财"),
        ("dt_pool", date, lambda: fetcher.dt_pool(date_ymd), "东财"),
        ("zb_pool", date, lambda: fetcher.zb_pool(date_ymd), "东财"),
        ("yjyg", quarter_end, lambda: fetcher.yjyg(quarter_end), "东财"),
    ]
    for dataset, period, loader, source in jobs:
        _ingest_snapshot_one(fetcher, store, run_id, dataset, period, loader, source)

    if not is_current:
        logger.info("目标日 %s 非最新交易日,板块榜/成分股(实时接口)跳过", date)
        store.log(run_id, "industry_board", date, "skipped", error="实时接口无法追溯历史")
        return
    board = _ingest_snapshot_one(
        fetcher, store, run_id, "industry_board", date,
        fetcher.industry_board, None,
    )
    if board is None or board.empty:
        return
    sec_cfg = config.get("sector", {})
    drill_n = sec_cfg.get("core_drill_top_n", 3)
    if not store.has_ok("industry_cons", date):
        top_boards = list(
            board.sort_values("涨跌幅", ascending=False)["板块名称"].head(drill_n)
        )
        parts = []
        for name in top_boards:
            try:
                cons = validate("industry_cons", fetcher.industry_cons(name))
                cons = cons.copy()
                cons.insert(0, "板块名称", name)
                parts.append(cons)
            except Exception as err:
                logger.warning("拉取板块 %s 成分股失败,跳过:%s", name, err)
        if parts:
            merged = pd.concat(parts, ignore_index=True)
            rows = store.upsert_snapshot("industry_cons", "trade_date", date, merged)
            store.log(run_id, "industry_cons", date, "ok", rows=rows,
                      source=fetcher.last_source)
        else:
            store.log(run_id, "industry_cons", date, "failed", error="全部板块下钻失败")


def _ingest_snapshot_one(fetcher, store, run_id, dataset, period, loader, source):
    """单个快照数据集:续传跳过 → 拉取 → 质量闸门 → upsert → 台账。返回入库数据。"""
    if store.has_ok(dataset, period):
        logger.info("%s(%s) 已采集,跳过", dataset, period)
        return store.read_snapshot(dataset, "trade_date" if dataset != "yjyg" else "quarter_end", period)
    try:
        df = validate(dataset, loader())
        src = source if source is not None else fetcher.last_source
        period_col = "quarter_end" if dataset == "yjyg" else "trade_date"
        if df.empty:
            store.log(run_id, dataset, period, "empty", rows=0, source=src)
            logger.warning("%s(%s) 返回空表(已记台账,不再重采)", dataset, period)
            return df
        rows = store.upsert_snapshot(dataset, period_col, period, df)
        store.log(run_id, dataset, period, "ok", rows=rows, source=src)
        logger.info("%s(%s) 入库 %d 行", dataset, period, rows)
        return df
    except Exception as err:
        store.log(run_id, dataset, period, "failed", error=str(err))
        logger.warning("%s(%s) 采集失败:%s", dataset, period, err)
        return None


def ingest_daily_snapshot(fetcher: Fetcher, store: DuckStore, run_id: str, date: str) -> None:
    """当日日K快照一次成型 + 除权检测自愈。

    快照昨收与库内前收比对:偏差超容差判为除权,删该票历史(回补阶段自动重拉
    前复权全窗口);其余票直接把快照转写为当日 daily 行。证券名录顺带 upsert。
    """
    if store.has_ok("daily_snapshot", date):
        logger.info("daily_snapshot(%s) 已采集,跳过", date)
        return
    try:
        snap = validate("daily_snapshot", fetcher.daily_snapshot(),
                        prev_rows=store.last_ok_rows("daily_snapshot"))
    except Exception as err:
        store.log(run_id, "daily_snapshot", date, "failed", error=str(err))
        logger.warning("日K快照采集失败(当日日K交由回补阶段逐股补):%s", err)
        return
    src = fetcher.last_source
    snap = snap.copy()
    snap["代码"] = snap["代码"].astype(str).str.zfill(6)
    store.upsert_securities(snap[["代码", "名称"]])

    # 除权检测:仅对"库内最后一日 == 上一交易日"的票比对(更早落后的票走回补,
    # 回补后整段来自同一前复权源,天然一致,无须比对)
    exdiv_codes: list = []
    prev_td = store.prev_trading_day(date)
    last = store.daily_last_close()
    if prev_td and not last.empty:
        cur = last[last["日期"] == prev_td].rename(columns={"收盘": "库内前收"})
        merged = snap.merge(cur[["代码", "库内前收"]], on="代码", how="inner")
        prev_close = pd.to_numeric(merged["昨收"], errors="coerce")
        diff = (prev_close - merged["库内前收"]).abs() / merged["库内前收"]
        exdiv = merged[diff > _EXDIV_TOLERANCE]
        exdiv_codes = exdiv["代码"].tolist()
        if exdiv_codes:
            store.delete_daily(exdiv_codes)
            for c in exdiv_codes:
                store.clear_ok("daily_hist", c)   # 撤销深度标记,回补阶段整段重拉
            store.log(run_id, "exdiv_repull", date, "ok", rows=len(exdiv_codes),
                      error=",".join(exdiv_codes[:50]))
            logger.info("检测到 %d 只除权,已删历史待回补重拉:%s",
                        len(exdiv_codes), exdiv_codes[:10])

    # 快照 → 当日 daily 行(未成交的停牌票 成交量为0/价格缺失,已被质量闸门或此处过滤)
    traded = snap[pd.to_numeric(snap["成交量"], errors="coerce") > 0]
    rows_df = pd.DataFrame({
        "代码": traded["代码"], "日期": date,
        "开盘": traded["今开"], "收盘": traded["收盘"],
        "最高": traded["最高"], "最低": traded["最低"],
        "成交量": traded["成交量"], "成交额": traded["成交额"],
        "换手率": traded["换手率"],
    })
    # 除权票当日行不从快照写入(不复权口径),交由回补一并重拉,保证整段序列同源
    rows_df = rows_df[~rows_df["代码"].isin(set(exdiv_codes))]
    added = store.append_daily(rows_df)
    store.log(run_id, "daily_snapshot", date, "ok", rows=added, source=src)
    logger.info("日K快照成型:全市场 %d 行入库(源:%s)", added, src)


def ingest_backfill(fetcher: Fetcher, store: DuckStore, run_id: str, date: str,
                    limit: Optional[int] = None) -> dict:
    """逐股历史回补:历史深度首采、除权重拉、往日缺口。断点续传 + 失败预算。

    两类回补对象,统一为 (代码, 窗口起点):
    - 深度回补:台账无 daily_hist 标记的票(首采/除权撤标),整段默认窗口重拉,
      完成后打 daily_hist 标记,此后不再整段拉("快照成型"的当日行不代表有历史);
    - 缺口回补:已有深度标记但 库内最后日期 < 目标日 的票(停牌复牌/往日中断),
      从最后日期+1 拉到目标日。
    每票 1 次请求;连续 _FAIL_BUDGET 只全源失败抛 IngestAborted(快速失败)。
    """
    universe = store.security_codes()
    if not universe:
        logger.warning("证券名录为空(首次运行请在交易日盘后采集),回补跳过")
        return {"total": 0, "ok": 0, "fail": 0, "skipped_bj": 0}
    last = store.daily_last_close().set_index("代码")["日期"] if len(universe) else pd.Series(dtype=str)
    hist_done = store.ok_periods("daily_hist")
    default_start = (datetime.strptime(ymd(date), "%Y%m%d")
                     - timedelta(days=_BACKFILL_NATURAL_DAYS)).strftime("%Y%m%d")
    target_ymd = ymd(date)

    gap_codes = []
    for code in universe:
        last_date = last.get(code)
        if code not in hist_done:
            gap_codes.append((code, None))            # 深度回补:整段默认窗口
        elif last_date is None or last_date < dash(date):
            gap_codes.append((code, last_date))       # 缺口回补:增量窗口
    # 北交所仅东财一源,排到最后:东财熔断时它们必然失败,不应拖垮沪深回补
    gap_codes.sort(key=lambda x: x[0].startswith(("4", "8", "92")))

    total = len(gap_codes)
    if limit is not None and total > limit:
        logger.warning("回补需 %d 只,本轮按 --limit 只处理前 %d 只(其余下次续传)", total, limit)
        gap_codes = gap_codes[:limit]

    stats = {"total": total, "ok": 0, "fail": 0, "skipped_bj": 0}
    consec_fail = 0
    for i, (code, last_date) in enumerate(gap_codes, 1):
        is_bj = code.startswith(("4", "8", "92"))
        if is_bj and fetcher._eastmoney_down:
            stats["skipped_bj"] += 1   # 唯一可用源已熔断,试也白试
            continue
        start = default_start
        if last_date:
            nxt = datetime.strptime(last_date, "%Y-%m-%d") + timedelta(days=1)
            start = nxt.strftime("%Y%m%d")
        try:
            df = validate("daily", _with_code(fetcher.stock_daily(code, start, target_ymd), code))
            store.append_daily(df)
            if last_date is None:   # 深度回补完成,打标记(此后只做增量缺口)
                store.log(run_id, "daily_hist", code, "ok", rows=len(df))
            stats["ok"] += 1
            consec_fail = 0
        except Exception as err:
            stats["fail"] += 1
            consec_fail += 1
            store.log(run_id, f"daily_backfill:{code}", date, "failed", error=str(err))
            logger.warning("回补 %s 失败(%d/%d):%s", code, i, len(gap_codes), err)
            if consec_fail >= _FAIL_BUDGET:
                store.log(run_id, "daily_backfill", date, "aborted",
                          rows=stats["ok"], error=f"连续 {consec_fail} 只全源失败")
                raise IngestAborted(
                    f"连续 {consec_fail} 只全源失败,判定网络/源整体故障,中止本轮 "
                    f"(已入库 {stats['ok']} 只,重跑自动续传)"
                )
        if i % 200 == 0:
            logger.info("回补进度 %d/%d(成功 %d,失败 %d)", i, len(gap_codes),
                        stats["ok"], stats["fail"])
    store.log(run_id, "daily_backfill", date, "ok", rows=stats["ok"],
              error=f"fail={stats['fail']},skipped_bj={stats['skipped_bj']}")
    return stats


def _with_code(df: pd.DataFrame, code: str) -> pd.DataFrame:
    """个股日K结果补上代码列(各源日K接口均不含代码列)。"""
    out = df.copy()
    out["代码"] = str(code).zfill(6)
    return out


def run_ingest(date: Optional[str] = None, db_path: str = "gxfc_data.duckdb",
               config_path: str = "config/strategy.yaml", limit: Optional[int] = None,
               fetcher: Optional[Fetcher] = None, store: Optional[DuckStore] = None,
               now: Optional[datetime] = None) -> pd.DataFrame:
    """采集入口。返回本轮台账摘要 DataFrame。fetcher/store/now 可注入(测试用)。"""
    now = now or datetime.now()
    own_store = store is None
    fetcher = fetcher or Fetcher()
    store = store or DuckStore(db_path)
    run_id = f"run_{now.strftime('%Y%m%d_%H%M%S')}"
    config = _load_config(config_path)
    try:
        refresh_calendar(fetcher, store, now)
        latest = latest_closed_trading_day(store, now)
        target = dash(date) if date else latest
        if target not in store.trading_days(target, target):
            logger.warning("%s 非交易日,退出", target)
            return store.run_summary(run_id)
        if target > latest:
            logger.warning("目标日 %s 尚未收盘(最新已收盘交易日:%s),拒绝采集以免落盘中残缺数据",
                           target, latest)
            return store.run_summary(run_id)
        is_current = target == latest
        logger.info("采集目标日:%s(最新已收盘交易日:%s)run_id=%s", target, latest, run_id)

        ingest_snapshots(fetcher, store, run_id, target, is_current, config)
        # 实时快照反映"当下"行情:今天是交易日且未收盘时,快照是今日盘中价,
        # 不是任何一天的完整日K,写库会污染 OHLC 与成交量,必须跳过走逐股回补
        today = now.strftime("%Y-%m-%d")
        snapshot_live = not _is_closed(now) and bool(store.trading_days(today, today))
        if is_current and not snapshot_live:
            ingest_daily_snapshot(fetcher, store, run_id, target)
        elif is_current:
            logger.warning("盘中运行:实时快照为今日盘中价,跳过快照,%s 日K交由逐股回补", target)
        else:
            logger.info("目标日非最新交易日,当日日K交由逐股回补补齐")
        ingest_backfill(fetcher, store, run_id, target, limit=limit)

        summary = store.run_summary(run_id)
        logger.info("采集完成。台账摘要:\n%s", summary.to_string(index=False))
        logger.info("源健康度:%s", fetcher.health)
        return summary
    finally:
        if own_store:
            store.close()


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="盘后数据采集(增量落 DuckDB)")
    parser.add_argument("date", nargs="?", default=None, help="目标交易日 YYYYMMDD,默认最新已收盘交易日")
    parser.add_argument("--limit", type=int, default=None, help="本轮最多回补的股票数(冒烟用)")
    parser.add_argument("--db", default="gxfc_data.duckdb", help="DuckDB 文件路径")
    args = parser.parse_args(argv)
    try:
        run_ingest(date=args.date, db_path=args.db, limit=args.limit)
        return 0
    except IngestAborted as err:
        logger.error("%s", err)
        return 1


if __name__ == "__main__":
    sys.exit(main())
