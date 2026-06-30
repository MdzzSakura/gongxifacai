import pandas as pd
from gxfc.factors.market_emotion import MarketEmotion
from gxfc.review.daily_board import DailyBoard, render_console, save_csv


def _board():
    emotion = MarketEmotion(
        up_count=3000, down_count=1800, limit_up=40, limit_down=5,
        broken_board_rate=0.2, highest_streak=5, volume_state="放量",
        sentiment_hint="中性",
    )
    sectors = pd.DataFrame({"板块名称": ["电力"], "涨跌幅": [5.6], "领涨股": ["B"]})
    candidates = pd.DataFrame(
        {"股票代码": ["000001"], "股票简称": ["甲"], "同比增长": [80.0], "有跳空": [True]}
    )
    sector_cores = {
        "电力": pd.DataFrame(
            {"名称": ["甲"], "涨跌幅": [9.9], "成交额": [1e8]}
        )
    }
    return DailyBoard(
        date="20260629", emotion=emotion, sectors=sectors,
        candidates=candidates, sector_cores=sector_cores,
    )


def test_渲染包含关键信息():
    text = render_console(_board())
    assert "20260629" in text
    assert "情绪" in text
    assert "电力" in text
    assert "000001" in text
    assert "proxy" in text.lower() or "口径" in text
    assert "主线板块核心股" in text


def test_保存csv文件(tmp_path):
    paths = save_csv(_board(), str(tmp_path))
    assert len(paths) == 4   # sectors / candidates / sector_cores / surge
    for p in paths:
        assert p.endswith(".csv")
        content = open(p, encoding="utf-8-sig").read()
        assert len(content) > 0
    # 候选 CSV 内含股票代码
    joined = "".join(open(p, encoding="utf-8-sig").read() for p in paths)
    assert "000001" in joined
    assert "电力" in joined
    # 列断言:sectors 与 candidates 各自列名
    sectors_cols = list(pd.read_csv(paths[0], encoding="utf-8-sig").columns)
    candidates_cols = list(pd.read_csv(paths[1], encoding="utf-8-sig").columns)
    assert sectors_cols == ["板块名称", "涨跌幅", "领涨股"]
    assert candidates_cols == ["股票代码", "股票简称", "同比增长", "有跳空"]
