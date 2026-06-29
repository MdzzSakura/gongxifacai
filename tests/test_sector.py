import pandas as pd
from gxfc.factors.sector import rank_sectors, core_stocks


def test_板块按涨幅降序取前N():
    board = pd.DataFrame(
        {
            "板块名称": ["煤炭", "电力", "稀土", "银行"],
            "涨跌幅": [1.2, 5.6, 3.3, -0.4],
            "领涨股": ["A", "B", "C", "D"],
        }
    )
    out = rank_sectors(board, top_n=2)
    assert list(out["板块名称"]) == ["电力", "稀土"]
    assert out.index.tolist() == [0, 1]


def test_板块top_n超过数量时返回全部():
    board = pd.DataFrame({"板块名称": ["X"], "涨跌幅": [1.0], "领涨股": ["a"]})
    out = rank_sectors(board, top_n=10)
    assert len(out) == 1


def test_核心成分股按涨幅降序取前N():
    cons = pd.DataFrame(
        {
            "名称": ["甲", "乙", "丙"],
            "涨跌幅": [2.0, 9.9, 5.0],
            "成交额": [1e8, 5e8, 2e8],
        }
    )
    out = core_stocks(cons, core_top_n=2)
    assert list(out["名称"]) == ["乙", "丙"]
    assert out.index.tolist() == [0, 1]
