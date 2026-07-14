"""交易日志 CLI:开仓写计划、平仓记执行、清单与纪律统计。零网络,只写本地库。

设计意图:强制"先写计划再下单"(add 的 --plan 必填),平仓时强制申报是否
按计划执行(--followed / --broke 二选一),stats 用三组对比暴露纪律成本。

用法:
  python -m gxfc.journal add --code 600000 --name 甲 --strategy profit_fault \
      --plan "断层+情绪回暖,破5日线止损" --date 20260707 --price 10.5 --shares 1000
  python -m gxfc.journal close T20260707-001 --date 20260710 --price 11.2 \
      --reason 规则卖点 --followed --note "按计划止盈"
  python -m gxfc.journal list [--open]
  python -m gxfc.journal stats
"""
import argparse
import sys

from tabulate import tabulate

from gxfc.review.tracker import trade_stats
from gxfc.store.duck_store import DuckStore
from gxfc.store.journal_store import JournalStore


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="交易日志(计划-执行-纪律统计)")
    parser.add_argument("--db", default="gxfc_data.duckdb", help="DuckDB 文件路径")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="开仓:先写计划再下单")
    p_add.add_argument("--code", required=True, help="股票代码")
    p_add.add_argument("--name", default="", help="股票名称")
    p_add.add_argument("--strategy", required=True,
                       help="依据的模式,如 profit_fault / bottom_volume / 手动")
    p_add.add_argument("--plan", required=True, help="买入理由 + 卖出规则(必填)")
    p_add.add_argument("--date", required=True, help="开仓日 YYYYMMDD")
    p_add.add_argument("--price", type=float, required=True, help="开仓价")
    p_add.add_argument("--shares", type=int, required=True, help="股数")

    p_close = sub.add_parser("close", help="平仓:记录执行与是否守纪")
    p_close.add_argument("trade_id", help="交易编号,如 T20260707-001")
    p_close.add_argument("--date", required=True, help="平仓日 YYYYMMDD")
    p_close.add_argument("--price", type=float, required=True, help="平仓价")
    p_close.add_argument("--reason", required=True, help="离场原因:规则卖点/止损/情绪/其他")
    grp = p_close.add_mutually_exclusive_group(required=True)
    grp.add_argument("--followed", dest="followed", action="store_true", help="按计划执行")
    grp.add_argument("--broke", dest="followed", action="store_false", help="偏离计划")
    p_close.add_argument("--note", default="", help="备注(卖飞/拿住等复盘线索)")

    p_list = sub.add_parser("list", help="交易清单")
    p_list.add_argument("--open", action="store_true", help="只看持仓中")

    sub.add_parser("stats", help="纪律统计:全部/按计划/未按计划 三组对比")

    args = parser.parse_args(argv)
    store = DuckStore(args.db)
    try:
        journal = JournalStore(store.con)
        try:
            if args.cmd == "add":
                tid = journal.add_trade(args.code, args.name, args.strategy, args.plan,
                                        args.date, args.price, args.shares)
                print(f"已开仓 {tid}:{args.code} {args.shares}股 @ {args.price}")
            elif args.cmd == "close":
                journal.close_trade(args.trade_id, args.date, args.price,
                                    args.reason, args.followed, args.note)
                print(f"已平仓 {args.trade_id}")
            elif args.cmd == "list":
                trades = journal.list_trades(open_only=args.open)
                print(tabulate(trades, headers="keys", tablefmt="grid", showindex=False)
                      if not trades.empty else "(无记录)")
            elif args.cmd == "stats":
                stats = trade_stats(journal.list_trades())
                print(tabulate(stats, headers="keys", tablefmt="grid", showindex=False)
                      if not stats.empty else "(无已平仓交易,统计从首笔平仓后开始)")
        except ValueError as err:
            print(f"错误:{err}")
            return 1
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
