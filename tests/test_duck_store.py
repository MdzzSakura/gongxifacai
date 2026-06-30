from gxfc.store.duck_store import DuckStore


def test_新建库无表(tmp_path):
    store = DuckStore(str(tmp_path / "t.duckdb"))
    assert store.table_exists("zt_pool") is False
    store.close()
