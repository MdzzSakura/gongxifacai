"""主流程集成测试。

FakeFetcher 使用三个涨跌停池接口(zt_pool/dt_pool/zb_pool)替代已坏的
market_activity;yjyg 返回真实多行结构(每票含净利润行 + 营收行);
industry_board 使用真实列名 '领涨股票'。

注意:build_board 内部含 time.sleep(0.3) 礼貌延迟;
FakeFetcher 故意将 yjyg 只给 2 只股票,使日K 拉取次数 N=2,
总 sleep ≤ 1.2s 属可接受范围。
"""
import pandas as pd
import pytest
from gxfc.data.fetcher import Fetcher
from gxfc.main import _daily_window, build_board, load_config

_NET = "归属于上市公司股东的净利润"
_REV = "营业收入"


class FakeFetcher:
    """返回固定样本的假 Fetcher,模拟各接口。"""

    def zt_pool(self, date):
        return pd.DataFrame({"连板数": [1, 2, 5]})   # 3 涨停,最高5板

    def dt_pool(self, date):
        return pd.DataFrame({"代码": ["600010"], "名称": ["跌停测试"]})  # 1 跌停

    def zb_pool(self, date):
        return pd.DataFrame({"代码": ["000011"], "名称": ["炸板测试"]})  # 1 炸板

    def spot(self):
        # 全市场快照:2 涨 1 跌 1 平,用于统计涨跌家数
        return pd.DataFrame({"涨跌幅": [3.0, -1.0, 0.0, 2.5]})

    def industry_board(self):
        return pd.DataFrame(
            {
                "板块名称": ["电力", "煤炭"],
                "涨跌幅": [5.6, 1.2],
                "领涨股票": ["B", "A"],  # 真实列名
            }
        )

    def industry_cons(self, board):
        return pd.DataFrame(
            {"名称": ["甲", "乙"], "涨跌幅": [9.9, 5.5], "成交额": [1e8, 5e7]}
        )

    def yjyg(self, date):
        # 多行结构:每只股票含净利润行 + 营收行
        return pd.DataFrame(
            {
                "股票代码": ["000001", "000001", "000002", "000002"],
                "股票简称": ["甲", "甲", "乙", "乙"],
                "预测指标": [_NET, _REV, _NET, _REV],
                "业绩变动幅度": [80.0, 120.0, 30.0, 60.0],
                # 000001 净利润+80% → 达标;000002 净利润+30% → 不达标
            }
        )

    def market_spot(self):
        # 全市场快照(今日值):300888 涨12%、收9、量5000(过粗筛+大涨),其余不过
        return pd.DataFrame({
            "代码": ["300888", "600111", "000002"],
            "名称": ["爆量股", "平淡股", "乙"],
            "涨跌幅": [12.0, 2.0, 1.0],
            "最新价": [9.0, 10.0, 5.0],
            "成交量": [5000, 1000, 800],
            "成交额": [4e8, 1e8, 2e7],
        })

    def stock_daily(self, code, start, end):
        if code == "300888":
            # 历史59根(今日之前):长期低位缩量,前5日均量1000,区间最高20
            return pd.DataFrame({
                "最高": [20.0] + [8.5] * 58,
                "成交量": [1000] * 59,
            })
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
    # limit_up 来自涨停池(3 涨停股);up/down_count 来自 spot 快照(2 涨 1 跌)
    assert board.emotion.limit_up == 3
    assert board.emotion.up_count == 2
    assert board.emotion.down_count == 1
    assert list(board.sectors["板块名称"])[0] == "电力"
    # 仅 000001:净利润增速 80%≥50% 且跳空
    assert list(board.candidates["股票代码"]) == ["000001"]
    # 核心成分股已接入,键为榜单前几名板块
    assert board.sector_cores
    assert set(board.sector_cores).issubset(set(board.sectors["板块名称"]))
    assert "电力" in board.sector_cores
    # 底部爆量大涨:300888 三条件全过应入选
    assert list(board.surge_candidates["代码"]) == ["300888"]
    assert board.surge_candidates.iloc[0]["今日涨跌幅"] == 12.0
    assert board.surge_candidates.iloc[0]["量比"] == 5.0


def test_月初窗口跨月回溯():
    start, end = _daily_window("20260601")
    assert start < end
    assert start.startswith("202605")


def test_retries为0抛异常():
    with pytest.raises(ValueError):
        Fetcher(retries=0)
