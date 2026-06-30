import pandas as pd
import pytest
from gxfc.data.fetcher import Fetcher
from gxfc.main import _daily_window, build_board, load_config


class FakeFetcher:
    """返回固定样本的假 Fetcher,模拟各接口。"""

    def market_activity(self):
        return pd.DataFrame(
            {"item": ["上涨", "下跌", "涨停", "跌停", "炸板"],
             "value": [3000, 1800, 40, 5, 10]}
        )

    def zt_pool(self, date):
        return pd.DataFrame({"连板数": [1, 2, 5]})

    def industry_board(self):
        return pd.DataFrame(
            {"板块名称": ["电力", "煤炭"], "涨跌幅": [5.6, 1.2], "领涨股": ["B", "A"]}
        )

    def industry_cons(self, board):
        return pd.DataFrame(
            {"名称": ["甲", "乙"], "涨跌幅": [9.9, 5.5], "成交额": [1e8, 5e7]}
        )

    def yjyg(self, date):
        return pd.DataFrame(
            {"股票代码": ["000001", "000002"], "股票简称": ["甲", "乙"],
             "预测净利润-同比增长": [80.0, 30.0]}
        )

    def stock_daily(self, code, start, end):
        if code == "000001":
            return pd.DataFrame(
                {"日期": ["2026-06-28", "2026-06-29"], "开盘": [10.0, 11.0],
                 "最高": [10.5, 11.8]}
            )
        return pd.DataFrame(
            {"日期": ["2026-06-28", "2026-06-29"], "开盘": [10.0, 10.2],
             "最高": [10.5, 10.6]}
        )


def test_配置加载():
    cfg = load_config()
    assert cfg["profit_fault"]["growth_threshold"] == 50.0


def test_组装面板包含情绪板块与候选():
    cfg = load_config()
    board = build_board(FakeFetcher(), "20260629", "20260331", cfg)
    assert board.date == "20260629"
    assert board.emotion.up_count == 3000
    assert list(board.sectors["板块名称"])[0] == "电力"
    # 仅 000001 增速达标且跳空
    assert list(board.candidates["股票代码"]) == ["000001"]
    # 核心成分股已接入,键为榜单前几名板块
    assert board.sector_cores
    assert set(board.sector_cores).issubset(set(board.sectors["板块名称"]))
    assert "电力" in board.sector_cores


def test_月初窗口跨月回溯():
    start, end = _daily_window("20260601")
    assert start < end
    assert start.startswith("202605")


def test_retries为0抛异常():
    with pytest.raises(ValueError):
        Fetcher(retries=0)
