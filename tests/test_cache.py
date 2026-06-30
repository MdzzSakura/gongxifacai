import pandas as pd
from gxfc.data.cache import DataFrameCache


def test_未命中返回None(tmp_path):
    cache = DataFrameCache(str(tmp_path / "t.db"))
    assert cache.get("不存在的键") is None


def test_写入后能读回相同数据(tmp_path):
    cache = DataFrameCache(str(tmp_path / "t.db"))
    df = pd.DataFrame({"代码": ["000001", "000002"], "涨跌幅": [1.5, -2.0]})
    cache.set("sector:20260629", df)
    got = cache.get("sector:20260629")
    pd.testing.assert_frame_equal(got, df)


def test_同键覆盖写入(tmp_path):
    cache = DataFrameCache(str(tmp_path / "t.db"))
    cache.set("k", pd.DataFrame({"a": [1]}))
    cache.set("k", pd.DataFrame({"a": [2, 3]}))
    got = cache.get("k")
    assert list(got["a"]) == [2, 3]
