import pytest

from gxfc.store.duck_store import DuckStore
from gxfc.web import queries


def test_trading_dates_倒序(seeded_db):
    dates = queries.trading_dates(seeded_db)
    assert dates[0] == "2026-07-08"
    assert dates[-1] == "2026-07-01"


def test_空库各查询返回空态而非抛错(tmp_path):
    db = str(tmp_path / "empty.duckdb")
    DuckStore(db).close()   # 只有系统表，无 signals/trades
    assert queries.trading_dates(db) == []
    assert queries.signal_strategies(db) == []
    perf, summary = queries.tracking_report(db)
    assert perf.empty and summary.empty
    assert queries.list_trades(db).empty
    assert queries.trade_stats_report(db).empty


def test_tracking_report_明细与汇总(seeded_db):
    perf, summary = queries.tracking_report(seeded_db)
    assert len(perf) == 1
    assert bool(perf.iloc[0]["可追踪"])
    # 信号日 07-02 收盘 10.2，T+3 = 07-07 收盘 10.8 → 5.88%
    assert perf.iloc[0]["T+3收益%"] == pytest.approx(5.88, abs=0.01)
    assert not summary.empty
    assert set(summary["策略"]) == {"profit_fault"}


def test_tracking_report_按策略过滤(seeded_db):
    perf, _ = queries.tracking_report(seeded_db, strategy="不存在的策略")
    assert perf.empty


def test_list_trades_与持仓过滤(seeded_db):
    assert len(queries.list_trades(seeded_db)) == 1
    assert queries.list_trades(seeded_db, open_only=True).empty  # 已平仓


def test_trade_stats_report(seeded_db):
    stats = queries.trade_stats_report(seeded_db)
    assert "按计划" in set(stats["分组"])


def test_load_board_情绪段降级(seeded_db):
    board = queries.load_board(seeded_db, "2026-07-08")
    assert board.date == "2026-07-08"
    # zt_pool 未采集 → 情绪段降级并给出引导语
    assert "未采集" in board.emotion.sentiment_hint


def test_db_overview(seeded_db, tmp_path):
    ov = queries.db_overview(seeded_db)
    assert ov["daily_max"] == "2026-07-08"
    assert "daily" in set(ov["tables"]["表"])
    assert queries.db_overview(str(tmp_path / "nope.duckdb")) is None
