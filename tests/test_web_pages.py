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


def test_数据采集页补读快速结束进程日志(seeded_db, monkeypatch):
    """秒退子进程(从未进过运行中的轮询分支)必须靠后台排空线程写入的日志兜底。

    新结构下渲染线程永不直接读 proc.stdout——排空全由 _drain 后台线程完成,
    已结束分支只管消费 gxfc_proc_log 列表。这里注入的假进程连 stdout 属性都
    不需要,直接模拟"_drain 线程已把管道读完并写入 lines"之后、
    "_launch 发起的 rerun 到达渲染层"之前的时序。语义仍是:快速结束不丢日志。
    """
    class 快速结束进程:
        returncode = 0

        def poll(self):
            return 0

        def wait(self):
            return 0

    at = _run_app(seeded_db, monkeypatch)
    at.sidebar.radio[0].set_value("⚙️ 数据采集")
    at = at.run()
    at.session_state["gxfc_proc"] = 快速结束进程()
    at.session_state["gxfc_proc_name"] = "筛选"
    at.session_state["gxfc_proc_log"] = ["测试日志行"]
    at = at.run()
    assert not at.exception
    assert any("筛选完成" in str(s.value) for s in at.success)
    # _drain 线程预先写入的日志必须渲染出来,而非"(无输出)"
    assert any("测试日志行" in str(c.value) for c in at.code)


def test_C1_采集运行中交互不触发库状态查询导致活锁(seeded_db, monkeypatch):
    """终审 C1 回归。

    根因:子进程持有写锁期间,只读连接一撞锁会**立即**抛 duckdb.IOException
    (而非阻塞等待)。旧实现在 render 开头无条件调 db_overview,一撞锁就被
    app.py 的全局 except duckdb.IOException 捕获、显示"等待其完成"，进度区
    (_render_progress,唯一排空 stdout 管道的地方)永远渲染不到，子进程日志
    写满系统管道缓冲区后阻塞在 write()——采集永久停摆且无恢复路径。

    修复要求 render 在 gxfc_proc 存活时完全跳过 db_overview 调用。用一个
    poll() 首次返回 None(运行中)、此后返回 0(已结束)的假进程，一次 run()
    内覆盖"运行中跳过查询"与"结束后正常收尾"两段路径，不依赖真实 sleep/rerun
    轮询（AppTest 对 rerun 次数有保护，真轮询会导致测试挂起）。
    """
    import duckdb

    calls = []

    def _boom(db_path):
        calls.append(db_path)
        raise duckdb.IOException("Conflicting lock is held")

    monkeypatch.setattr("gxfc.web.queries.db_overview", _boom)

    class 存活后结束进程:
        returncode = 0

        def __init__(self):
            self._calls = 0
            self.stdout = iter(())

        def poll(self):
            self._calls += 1
            return None if self._calls == 1 else 0

        def wait(self):
            return 0

    at = _run_app(seeded_db, monkeypatch)
    at.sidebar.radio[0].set_value("⚙️ 数据采集")
    at.session_state["gxfc_proc"] = 存活后结束进程()
    at.session_state["gxfc_proc_name"] = "采集"
    at.session_state["gxfc_proc_log"] = ["运行中的一行日志"]
    at = at.run()

    assert not at.exception
    # 核心断言:守护生效则 db_overview 压根不会被调用(而不是"调用了但被兜住")
    assert calls == []
    # 运行中提示可见,证明没有卡在异常分支里
    assert any("库状态暂不可查" in str(c.value) for c in at.caption)
    # 第二次 poll() 已结束,收尾分支正常执行、日志未丢
    assert any("采集完成" in str(s.value) for s in at.success)
    assert any("运行中的一行日志" in str(c.value) for c in at.code)


def test__drain排空子进程输出到列表():
    """_drain 单测:逐行消费 stdout 迭代器,去除行尾换行符,保持原始顺序。"""
    from gxfc.web.pages_.ingest import _drain

    class 假进程:
        def __init__(self):
            self.stdout = iter(["第一行\n", "第二行\r\n", "第三行"])

    proc = 假进程()
    lines: list = []
    _drain(proc, lines)
    assert lines == ["第一行", "第二行", "第三行"]


def test__drain超限裁剪日志防止无限增长():
    """长采集可能产生数十万行日志,_LOG_LIMIT 触发后须裁剪,只保留尾部。"""
    from gxfc.web.pages_.ingest import _LOG_LIMIT, _drain

    total = _LOG_LIMIT + 100

    class 假进程:
        def __init__(self):
            self.stdout = iter(f"第{i}行\n" for i in range(total))

    lines: list = []
    _drain(假进程(), lines)
    assert len(lines) <= _LOG_LIMIT
    assert lines[-1] == f"第{total - 1}行"      # 尾部保留,未被误裁
