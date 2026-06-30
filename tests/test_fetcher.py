import pandas as pd
import pytest
from gxfc.data.cache import DataFrameCache
from gxfc.data.fetcher import Fetcher


def test_loader成功时返回数据并写入缓存(tmp_path):
    cache = DataFrameCache(str(tmp_path / "t.db"))
    fetcher = Fetcher(cache=cache)
    calls = {"n": 0}

    def loader():
        calls["n"] += 1
        return pd.DataFrame({"a": [1]})

    got = fetcher.fetch("k1", loader)
    assert list(got["a"]) == [1]
    assert calls["n"] == 1
    # 第二次应命中缓存,不再调用 loader
    fetcher.fetch("k1", loader)
    assert calls["n"] == 1


def test_loader失败时按次数重试(tmp_path):
    fetcher = Fetcher(cache=None, retries=3)
    calls = {"n": 0}

    def loader():
        calls["n"] += 1
        raise RuntimeError("模拟网络错误")

    with pytest.raises(RuntimeError):
        fetcher.fetch("k2", loader)
    assert calls["n"] == 3


def test_重试中途成功则返回(tmp_path):
    fetcher = Fetcher(cache=None, retries=3)
    calls = {"n": 0}

    def loader():
        calls["n"] += 1
        if calls["n"] < 2:
            raise RuntimeError("第一次失败")
        return pd.DataFrame({"ok": [1]})

    got = fetcher.fetch("k3", loader)
    assert calls["n"] == 2
    assert list(got["ok"]) == [1]
