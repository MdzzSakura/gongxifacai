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
    # 内容级断言：汇总表 + 明细表都已渲染（seeded_db 的信号 T+1/T+3 可评估，summary 非空）
    assert len(at.dataframe) >= 2
    assert any("汇总" in str(s.value) for s in at.subheader)


def test_信号追踪页无信号空态(tmp_path, monkeypatch):
    from gxfc.store.duck_store import DuckStore
    db = str(tmp_path / "nosig.duckdb")
    DuckStore(db).close()
    at = _run_app(db, monkeypatch)
    at.sidebar.radio[0].set_value("📈 信号追踪")
    at = at.run()
    assert not at.exception
    assert any("gxfc.screen" in str(i.value) for i in at.info)


def test_交易日志页渲染(seeded_db, monkeypatch):
    at = _run_app(seeded_db, monkeypatch)
    at.sidebar.radio[0].set_value("📝 交易日志")
    at = at.run()
    assert not at.exception
    # 已有一笔平仓交易:纪律统计表出现"按计划"分组
    assert len(at.dataframe) >= 1


def test_交易日志页空库(tmp_path, monkeypatch):
    from gxfc.store.duck_store import DuckStore
    db = str(tmp_path / "notrade.duckdb")
    DuckStore(db).close()
    at = _run_app(db, monkeypatch)
    at.sidebar.radio[0].set_value("📝 交易日志")
    at = at.run()
    assert not at.exception
    assert any("无已平仓交易" in str(i.value) for i in at.info)


def test_数据采集页渲染(seeded_db, monkeypatch):
    at = _run_app(seeded_db, monkeypatch)
    at.sidebar.radio[0].set_value("⚙️ 数据采集")
    at = at.run()
    assert not at.exception
    labels = [b.label for b in at.button]
    assert any("开始采集" in x for x in labels)
    assert any("重跑筛选" in x for x in labels)


def test_数据采集页库缺失引导(tmp_path, monkeypatch):
    at = _run_app(str(tmp_path / "nope.duckdb"), monkeypatch)
    at.sidebar.radio[0].set_value("⚙️ 数据采集")
    at = at.run()
    assert not at.exception
    assert any("尚不存在" in str(i.value) for i in at.info)
