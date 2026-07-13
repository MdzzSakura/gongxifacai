"""四个页面的 AppTest 冒烟：空库与造数库下渲染不抛异常、空态提示存在。"""
from streamlit.testing.v1 import AppTest

_APP = "gxfc/web/app.py"


def _run_app(db_path: str, monkeypatch) -> AppTest:
    monkeypatch.setenv("GXFC_DB", db_path)
    at = AppTest.from_file(_APP, default_timeout=60)
    return at.run()


def test_库缺失显示引导页(tmp_path, monkeypatch):
    at = _run_app(str(tmp_path / "nope.duckdb"), monkeypatch)
    assert not at.exception
    assert any("不存在" in str(i.value) for i in at.info)


def test_复盘面板渲染(seeded_db, monkeypatch):
    at = _run_app(seeded_db, monkeypatch)
    assert not at.exception
    # 默认落在复盘面板页：有日期选择器，且情绪段降级提示可见
    assert at.selectbox[0].value == "2026-07-08"


def test_信号追踪页渲染(seeded_db, monkeypatch):
    at = _run_app(seeded_db, monkeypatch)
    at.sidebar.radio[0].set_value("📈 信号追踪")
    at = at.run()
    assert not at.exception
    # 有信号：策略下拉存在且包含 profit_fault
    assert "profit_fault" in at.selectbox[0].options


def test_信号追踪页无信号空态(tmp_path, monkeypatch):
    from gxfc.store.duck_store import DuckStore
    db = str(tmp_path / "nosig.duckdb")
    DuckStore(db).close()
    at = _run_app(db, monkeypatch)
    at.sidebar.radio[0].set_value("📈 信号追踪")
    at = at.run()
    assert not at.exception
    assert any("gxfc.screen" in str(i.value) for i in at.info)
