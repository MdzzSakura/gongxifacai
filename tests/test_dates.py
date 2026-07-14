"""日期口径工具测试。"""
from gxfc.dates import dash, derive_quarter_end, ymd


def test_dash归一两种口径():
    assert dash("20260707") == "2026-07-07"
    assert dash("2026-07-07") == "2026-07-07"
    assert dash(" 20260707 ") == "2026-07-07"


def test_ymd归一两种口径():
    assert ymd("2026-07-07") == "20260707"
    assert ymd("20260707") == "20260707"


def test_季度末推导():
    assert derive_quarter_end("2026-07-06") == "20260630"
    assert derive_quarter_end("20260331") == "20260331"
    assert derive_quarter_end("20260215") == "20251231"
    assert derive_quarter_end("20261001") == "20260930"
